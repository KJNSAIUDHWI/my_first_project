"""本地识别验收与合一审阅服务（支持整页确认、原地改答案/改框并同步提交 data/m1 训练日志）。"""
import argparse
from datetime import datetime
import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import queue
import secrets
import shutil
import sys
import threading
import time
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import unquote, urlparse

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))
from PIL import Image
from engine.vlm_client import SquarePacker, VlmClient, load_vlm_config, save_json
from engine import api_pricing
from engine.lan_collab import ClaimManager, resolve_host, print_access_urls
from training import box_log as label_events

LABELS = ('已确认', '已订正', '正确', '识别错误', '漏题／漏空', '对应错误', '看不清')
M1_CLS = {0, 1, 4}
M2_CLS = {2, 3}
M3_CLS = {5, 6}
ALL_BOX_CLS = M1_CLS | M2_CLS | M3_CLS
VLM_RECOG_CLS = {0: 'identity', 5: 'fill'}
VALID_ITEM_KINDS = {'choice', 'fill', 'identity', 'unmatched'}


def _read_json_safe(path: Path, retries: int = 8, delay: float = 0.04):
    """带重试读取 JSON，避免后台流水线并发原子替换时偶发读取冲突。"""
    last_exc = None
    for idx in range(max(1, retries)):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            last_exc = exc
            if idx + 1 < retries:
                time.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _find_local_katex_cache(exclude_dir: Path) -> Path | None:
    """优先复用本机已有批次或 tools/vendor/katex 缓存，离线也能瞬间加载公式字体。"""
    candidates = [ROOT_DIR / 'tools' / 'vendor' / 'katex']
    batches_dir = ROOT_DIR / 'batches'
    if batches_dir.is_dir():
        for p in sorted(batches_dir.iterdir(), reverse=True):
            kd = p / 'recognized' / 'vendor' / 'katex'
            if kd.resolve() != exclude_dir.resolve():
                candidates.append(kd)
    for kd in candidates:
        if (kd / 'katex.min.js').is_file() and (kd / 'katex.min.css').is_file():
            return kd
    return None


def prepare_math_assets(folder, async_download: bool = False):
    """固定版本静态公式资源放在本次结果内；优先复用本地缓存，网络下载支持后台异步不阻塞 8904 启动。"""
    target = Path(folder).resolve()/'vendor/katex'
    if (target/'katex.min.js').is_file() and (target/'katex.min.css').is_file():
        return
    cached = _find_local_katex_cache(target)
    if cached is not None:
        try:
            shutil.copytree(cached, target, dirs_exist_ok=True)
            return
        except OSError:
            pass

    def _download():
        try:
            raw = urllib.request.urlopen('https://registry.npmjs.org/katex/-/katex-0.16.22.tgz', timeout=25).read()
            with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
                for member in archive.getmembers():
                    if member.name in ('package/dist/katex.min.js', 'package/dist/katex.min.css') or (member.name.startswith('package/dist/fonts/') and member.name.endswith('.woff2')):
                        dest = (target/member.name.removeprefix('package/dist/')).resolve()
                        if not dest.is_relative_to(target):
                            raise ValueError('公式资源路径异常')
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        fobj = archive.extractfile(member)
                        if fobj is not None:
                            dest.write_bytes(fobj.read())
        except (OSError, ValueError, tarfile.TarError):
            print('公式资源未准备完成，公式暂显示原始转录；联网后刷新即可。', flush=True)

    if async_download:
        threading.Thread(target=_download, daemon=True).start()
    else:
        _download()


class ReviewEventBus:
    """8904 实时事件总线：CLI / AI / 协同修改或焦点投送时即时推送给所有已连浏览器。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._recent: list[dict] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict) -> None:
        ev = {**event, 'ts': datetime.now().strftime('%H:%M:%S')}
        with self._lock:
            self._recent.append(ev)
            if len(self._recent) > 40:
                self._recent = self._recent[-40:]
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(ev)
                except Exception:
                    pass


class ReviewStore:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.path = self.folder/'feedback.json'
        self.lock = threading.RLock()
        self._page_locks_guard = threading.Lock()
        self._page_locks: dict[str, threading.RLock] = {}
        self._page_revs: dict[str, int] = {}
        # Bind feedback to immutable input/model manifest, not changing completion status.
        self.identity = hashlib.sha256((self.folder/'manifest.json').read_bytes()).hexdigest()

    def page_lock(self, page_id: str) -> threading.RLock:
        """单页细粒度可重入锁：不同页并发操作互不阻塞，同一页写操作自动串行化防竞争冒险。"""
        pid = str(page_id or '')
        with self._page_locks_guard:
            lk = self._page_locks.get(pid)
            if lk is None:
                lk = threading.RLock()
                self._page_locks[pid] = lk
            return lk

    def bump_page_rev(self, page_id: str) -> int:
        pid = str(page_id or '')
        with self._page_locks_guard:
            nxt = int(self._page_revs.get(pid) or 0) + 1
            self._page_revs[pid] = nxt
            return nxt

    def page_rev(self, page_id: str) -> int:
        with self._page_locks_guard:
            return int(self._page_revs.get(str(page_id or '')) or 0)

    def page_revs_snapshot(self) -> dict[str, int]:
        with self._page_locks_guard:
            return dict(self._page_revs)

    def get_page_detail(self, page_id: str) -> dict:
        results = _read_json_safe(self.folder / 'results.json')
        pages = results.get('pages') or []
        page = next((p for p in pages if p.get('id') == page_id or p.get('name') == page_id), None)
        if not page:
            raise ValueError(f'目标试卷页不存在：{page_id}')
        fb = self.read()
        fb_items = fb.get('items') or {}
        pid = page['id']
        item_fb = {it['id']: fb_items[it['id']] for it in (page.get('items') or []) if it.get('id') in fb_items}
        return {
            'ok': True,
            'page_id': pid,
            'page_rev': self.page_rev(pid),
            'page': page,
            'page_feedback': fb_items.get(pid),
            'item_feedback': item_fb,
            'feedback_revision': fb.get('revision', 0),
        }

    def set_item_text(
        self,
        *,
        page_id: str,
        item_id: str,
        text: str = '',
        answer: str | None = None,
        label: str = '已订正',
        reviewer: str = 'AI助手',
        expected_page_rev: int | None = None,
    ) -> dict:
        """页级加锁订正单道手写题文字或选择题答案（同时写入 feedback.json，供 CLI/AI 同构调用）。"""
        with self.page_lock(page_id):
            if expected_page_rev is not None and int(expected_page_rev) != self.page_rev(page_id):
                raise ValueError(f'页面 {page_id} 已被其他操作更新（期望 rev={expected_page_rev}，当前 rev={self.page_rev(page_id)}），请先刷新再写')
            with self.lock:
                results = _read_json_safe(self.folder / 'results.json')
                pages = results.get('pages') or []
                page = next((p for p in pages if p.get('id') == page_id), None)
                if not page:
                    raise ValueError(f'目标试卷页不存在：{page_id}')
                item = next((it for it in (page.get('items') or []) if it.get('id') == item_id), None)
                if not item:
                    raise ValueError(f'页面 {page_id} 中不存在题块：{item_id}')
                clean_label = label if label in LABELS + ('',) else '已订正'
                state = self.read()
                now_iso = datetime.now().isoformat(timespec='seconds')
                state['revision'] += 1
                rec = {
                    'label': clean_label,
                    'correction': str(text or '')[:20000],
                    'updated': now_iso,
                    'revision': state['revision'],
                    'reviewer': str(reviewer or 'AI助手')[:32],
                }
                if answer is not None:
                    rec['answer'] = str(answer).strip()[:32]
                state['items'][item_id] = rec
                save_json(self.path, state)
                with (self.folder / 'feedback_history.jsonl').open('a', encoding='utf-8') as fh:
                    fh.write(json.dumps({'id': item_id, **rec}, ensure_ascii=False) + '\n')
                new_rev = self.bump_page_rev(page_id)
                return {
                    'ok': True,
                    'page_id': page_id,
                    'item_id': item_id,
                    'page_rev': new_rev,
                    'record': rec,
                    'feedback': state,
                }

    def delete_item_or_box(
        self,
        *,
        page_id: str,
        item_id: str = '',
        box_index: int | None = None,
        reviewer: str = 'AI助手',
        expected_page_rev: int | None = None,
    ) -> dict:
        """页级加锁删除指定题块/标框并同步持久化到 results.json 与 pages/<pid>.json。"""
        with self.page_lock(page_id):
            if expected_page_rev is not None and int(expected_page_rev) != self.page_rev(page_id):
                raise ValueError(f'页面 {page_id} 已被其他操作更新（期望 rev={expected_page_rev}，当前 rev={self.page_rev(page_id)}）')
            with self.lock:
                results_path = self.folder / 'results.json'
                results = _read_json_safe(results_path)
                pages = results.get('pages') or []
                page = next((p for p in pages if p.get('id') == page_id), None)
                if not page:
                    raise ValueError(f'目标试卷页不存在：{page_id}')
                items = list(page.get('items') or [])
                boxes = list(page.get('boxes') or [])
                deleted_ids = []
                if item_id:
                    target_it = next((it for it in items if it.get('id') == item_id), None)
                    if not target_it:
                        raise ValueError(f'页面 {page_id} 中不存在题块：{item_id}')
                    items = [it for it in items if it.get('id') != item_id]
                    boxes = [b for b in boxes if b.get('item_id') != item_id and self._box_iou(b.get('box') or [0, 0, 0, 0], target_it.get('box') or [0, 0, 0, 0]) < 0.85]
                    deleted_ids.append(item_id)
                elif box_index is not None:
                    bi = int(box_index)
                    if not (0 <= bi < len(boxes)):
                        raise ValueError(f'标框索引越界：{bi}（共 {len(boxes)} 个）')
                    removed = boxes.pop(bi)
                    rid = str(removed.get('item_id') or '')
                    if rid:
                        items = [it for it in items if it.get('id') != rid]
                        deleted_ids.append(rid)
                else:
                    raise ValueError('请提供 item_id 或 box_index')
                page['items'] = items
                page['boxes'] = boxes
                self._recompute_results_stats(results)
                save_json(results_path, results)
                page_file = self.folder / 'pages' / (page_id + '.json')
                if page_file.is_file():
                    try:
                        pf = _read_json_safe(page_file, retries=4, delay=0.03)
                        pf['items'] = items
                        pf['boxes'] = boxes
                        save_json(page_file, pf)
                    except Exception:
                        pass
                new_rev = self.bump_page_rev(page_id)
                mtime = results_path.stat().st_mtime if results_path.is_file() else 0.0
                return {
                    'ok': True,
                    'page_id': page_id,
                    'deleted_item_ids': deleted_ids,
                    'remaining_items': len(items),
                    'remaining_boxes': len(boxes),
                    'page_rev': new_rev,
                    'results_mtime': mtime,
                    'reviewer': reviewer,
                }

    def read(self):
        if not self.path.exists():
            return {'run': self.identity, 'revision': 0, 'items': {}}
        value = _read_json_safe(self.path)
        if value['run'] != self.identity:
            raise ValueError('反馈属于另一批输入')
        return value

    def update(self, request):
        results = _read_json_safe(self.folder/'results.json')
        allowed = {p['id'] for p in results['pages']} | {i['id'] for p in results['pages'] for i in p['items']}
        ident, label = request.get('id'), request.get('label')
        if ident not in allowed or label not in LABELS + ('',):
            raise ValueError('反馈目标或类型不合法')
        correction = request.get('correction', '')
        if not isinstance(correction, str) or len(correction) > 20000:
            raise ValueError('修正文字不合法或过长')
        with self.lock:
            state = self.read()
            req_rev = request.get('revision')
            existing = state['items'].get(ident)
            # 细粒度并发控制：若两人同时改不同题目则自动合并；改同一题且版本过期则拦截。
            if req_rev != state['revision']:
                if not isinstance(req_rev, int) or req_rev < 0 or req_rev > state['revision']:
                    raise ValueError('反馈已被另一页面更新，请刷新后重试；当前草稿保留')
                if existing is not None and int(existing.get('revision', state['revision'])) > req_rev:
                    raise ValueError('反馈已被另一页面更新，请刷新后重试；当前草稿保留')
            state['revision'] += 1
            reviewer = str(request.get('reviewer') or '').strip()[:32]
            item_record = {
                'label': label,
                'correction': correction,
                'updated': datetime.now().isoformat(timespec='seconds'),
                'revision': state['revision'],
            }
            if reviewer:
                item_record['reviewer'] = reviewer
            state['items'][ident] = item_record
            save_json(self.path, state)
            with (self.folder/'feedback_history.jsonl').open('a', encoding='utf-8') as file:
                file.write(json.dumps({'id': ident, **item_record}, ensure_ascii=False) + '\n')
            return state

    def _sanitize_boxes(self, boxes, width, height):
        clean = []
        w = max(1.0, float(width or 1))
        h = max(1.0, float(height or 1))
        for b in boxes or []:
            if not isinstance(b, dict):
                continue
            try:
                cls = int(b.get('cls'))
                coords = b.get('box') or []
                if cls not in ALL_BOX_CLS or len(coords) != 4:
                    continue
                x1, y1, x2, y2 = [float(v) for v in coords]
                x1, x2 = max(0.0, min(w, min(x1, x2))), max(0.0, min(w, max(x1, x2)))
                y1, y2 = max(0.0, min(h, min(y1, y2))), max(0.0, min(h, max(y1, y2)))
                if (x2 - x1) < 4 or (y2 - y1) < 4:
                    continue
                stage = 'm1' if cls in M1_CLS else ('m2' if cls in M2_CLS else 'm3')
                item = {'cls': cls, 'box': [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)], 'stage': stage}
                if 'confidence' in b and b['confidence'] is not None:
                    item['confidence'] = float(b['confidence'])
                if b.get('trusted'):
                    item['trusted'] = True
                if b.get('manual'):
                    item['manual'] = True
                item_id = b.get('item_id')
                if isinstance(item_id, str) and item_id.strip():
                    item['item_id'] = item_id.strip()[:80]
                clean.append(item)
            except (TypeError, ValueError):
                continue
        return clean

    def _sanitize_items(self, items, page_id, width, height, existing_by_id):
        """清洗前端提交的页面答案条目：以磁盘现有数据为安全基线，支持删项、移框及保留新识别项。"""
        clean = []
        seen = set()
        w = max(1.0, float(width or 1))
        h = max(1.0, float(height or 1))
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            ident = str(raw.get('id') or '').strip()[:80]
            if not ident or ident in seen:
                continue
            base = dict(existing_by_id[ident]) if ident in existing_by_id else {}
            kind = str(raw.get('kind') or base.get('kind') or '').strip()
            if kind not in VALID_ITEM_KINDS:
                continue
            coords = raw.get('box') if isinstance(raw.get('box'), list) else base.get('box')
            if not isinstance(coords, list) or len(coords) != 4:
                continue
            try:
                x1, y1, x2, y2 = [float(v) for v in coords]
            except (TypeError, ValueError):
                continue
            x1, x2 = max(0.0, min(w, min(x1, x2))), max(0.0, min(w, max(x1, x2)))
            y1, y2 = max(0.0, min(h, min(y1, y2))), max(0.0, min(h, max(y1, y2)))
            if (x2 - x1) < 4 or (y2 - y1) < 4:
                continue
            box_clean = [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)]

            if base:
                merged = base
                merged['box'] = box_clean
                if 'number' in raw and raw['number'] is not None:
                    merged['number'] = raw['number']
                if isinstance(raw.get('status'), str) and raw['status'].strip():
                    merged['status'] = raw['status'].strip()[:64]
                if isinstance(raw.get('issues'), list):
                    merged['issues'] = [str(x)[:300] for x in raw['issues'][:20]]
                if isinstance(raw.get('transcription'), dict):
                    merged['transcription'] = raw['transcription']
                for k in ('crop', 'grid', 'grid_id', 'grid_box', 'scale', 'manual', 'error'):
                    if k in raw and raw[k] is not None:
                        merged[k] = raw[k]
            else:
                merged = {
                    'id': ident,
                    'page_id': page_id,
                    'kind': kind,
                    'box': box_clean,
                    'number': raw.get('number'),
                    'issues': [str(x)[:300] for x in (raw.get('issues') or [])[:20]] if isinstance(raw.get('issues'), list) else [],
                    'status': str(raw.get('status') or '已识别')[:64],
                }
                for k in ('crop', 'grid', 'grid_id', 'grid_box', 'scale', 'transcription', 'answer', 'recognition', 'manual', 'error'):
                    if k in raw and raw[k] is not None:
                        merged[k] = raw[k]
            seen.add(ident)
            clean.append(merged)
        return clean

    @staticmethod
    def _recompute_results_stats(results):
        stats = results.get('stats')
        if not isinstance(stats, dict):
            stats = {}
            results['stats'] = stats
        pages = results.get('pages') or []
        all_items = [it for p in pages for it in (p.get('items') or [])]
        stats['choice_items'] = sum(1 for it in all_items if it.get('kind') == 'choice')
        stats['fill_items'] = sum(1 for it in all_items if it.get('kind') == 'fill')
        stats['identity_items'] = sum(1 for it in all_items if it.get('kind') == 'identity')
        stats['items_with_transcription'] = sum(1 for it in all_items if 'transcription' in it and it.get('transcription'))

    def _sync_m1_log(self, page, boxes, reviewer):
        """把整页修正后的 M1 框（0/1/4）按中文事件日志格式同步写入 data/m1/log/<stem>.log。"""
        stem = str(page.get('name') or '').strip()
        if not stem:
            return {'synced': False, 'reason': '缺少页名'}
        w = max(1.0, float(page.get('width') or 1))
        h = max(1.0, float(page.get('height') or 1))
        m1_norm = []
        for b in boxes:
            if int(b['cls']) not in M1_CLS:
                continue
            x1, y1, x2, y2 = b['box']
            cx = max(0.0, min(1.0, (x1 + x2) / 2.0 / w))
            cy = max(0.0, min(1.0, (y1 + y2) / 2.0 / h))
            bw = max(0.0, min(1.0, (x2 - x1) / w))
            bh = max(0.0, min(1.0, (y2 - y1) / h))
            if bw <= 0 or bh <= 0:
                continue
            m1_norm.append([int(b['cls']), round(cx, 6), round(cy, 6), round(bw, 6), round(bh, 6)])

        m1_img_dir = ROOT_DIR / 'data' / 'm1' / 'images'
        if not m1_img_dir.is_dir():
            return {'synced': False, 'reason': 'data/m1/images 目录不存在'}
        target_img = m1_img_dir / (stem + '.jpg')
        if not target_img.is_file():
            # 使用当前批次转正后的工作图，确保与画布框像素坐标完全一致
            src_img = self.folder / (page.get('image') or '')
            if src_img.is_file():
                shutil.copy2(src_img, target_img)
            else:
                return {'synced': False, 'reason': '未找到工作图，跳过同步'}
        ev_data = {'e': 'review', 'boxes': m1_norm}
        if reviewer:
            ev_data['reviewer'] = reviewer
        ev = label_events.append_event('m1', stem, ev_data)
        label_events.sync_progress('m1', stem)
        return {'synced': True, 'stem': stem, 'm1_boxes': len(m1_norm), 'unchanged': ev is None}

    def submit_page(self, request, allow_sync_data=True):
        """整页提交：保存整页确认状态、答案/手写文字订正、原地改框与删/改答案同步，并同步提交 data/m1 log。"""
        page_id = str(request.get('page_id') or '').strip()
        reviewer = str(request.get('reviewer') or '').strip()[:32]
        page_status = str(request.get('page_status') or '已确认').strip()
        if page_status not in LABELS:
            page_status = '已确认'
        item_edits = request.get('item_edits') or {}
        if not isinstance(item_edits, dict):
            raise ValueError('item_edits 格式错误')
        boxes_modified = bool(request.get('boxes_modified'))
        items_modified = bool(request.get('items_modified'))
        sync_log = bool(request.get('sync_log', True)) and allow_sync_data

        with self.lock:
            results_path = self.folder / 'results.json'
            results = _read_json_safe(results_path)
            pages = results.get('pages') or []
            page = next((p for p in pages if p.get('id') == page_id), None)
            if not page:
                raise ValueError('目标试卷页不存在：' + page_id)

            prev_items = list(page.get('items') or [])
            prev_item_map = {it['id']: it for it in prev_items if isinstance(it, dict) and it.get('id')}

            # 1. 保存标框与关联答案条目改动到 results.json & pages/<pid>.json
            results_dirty = False
            clean_boxes = page.get('boxes') or []
            if boxes_modified and isinstance(request.get('boxes'), list):
                clean_boxes = self._sanitize_boxes(request.get('boxes'), page.get('width'), page.get('height'))
                page['boxes'] = clean_boxes
                results_dirty = True

            clean_items = prev_items
            deleted_item_ids = []
            if (items_modified or boxes_modified) and isinstance(request.get('items'), list):
                clean_items = self._sanitize_items(
                    request.get('items'),
                    page_id,
                    page.get('width'),
                    page.get('height'),
                    prev_item_map,
                )
                kept_ids = {it['id'] for it in clean_items}
                deleted_item_ids = sorted(set(prev_item_map.keys()) - kept_ids)
                page['items'] = clean_items
                self._recompute_results_stats(results)
                results_dirty = True

            if results_dirty:
                save_json(results_path, results)
                page_file = self.folder / 'pages' / (page_id + '.json')
                if page_file.is_file():
                    try:
                        pf = _read_json_safe(page_file, retries=4, delay=0.03)
                        if boxes_modified and isinstance(request.get('boxes'), list):
                            pf['boxes'] = clean_boxes
                        if (items_modified or boxes_modified) and isinstance(request.get('items'), list):
                            pf['items'] = clean_items
                        save_json(page_file, pf)
                    except Exception:
                        pass

            item_map = {it['id']: it for it in clean_items if isinstance(it, dict) and it.get('id')}
            state = self.read()
            now_iso = datetime.now().isoformat(timespec='seconds')
            history_rows = []

            # 2. 保存每题的文字/选项订正（只存用户改过的，不强迫逐题打卡）
            for item_id, patch in item_edits.items():
                if item_id not in item_map or not isinstance(patch, dict):
                    continue
                correction = str(patch.get('correction', '') or '')[:20000]
                label = str(patch.get('label') or '已订正').strip()
                if label not in LABELS + ('',):
                    label = '已订正'
                state['revision'] += 1
                rec = {
                    'label': label,
                    'correction': correction,
                    'updated': now_iso,
                    'revision': state['revision'],
                }
                if 'answer' in patch and patch['answer'] is not None:
                    rec['answer'] = str(patch['answer']).strip()[:32]
                if patch.get('trusted'):
                    rec['trusted'] = True
                if reviewer:
                    rec['reviewer'] = reviewer
                state['items'][item_id] = rec
                history_rows.append({'id': item_id, **rec})

            # 3. 写入页级已确认记录（背面 0 题同样走这里，按页记账，不再误判）
            prev_page_rec = state['items'].get(page_id) if isinstance(state['items'].get(page_id), dict) else {}
            state['revision'] += 1
            page_rec = {
                'label': page_status,
                'correction': str(request.get('note') or '').strip()[:2000],
                'boxes_modified': boxes_modified or bool(prev_page_rec.get('boxes_modified')),
                'items_modified': items_modified or bool(prev_page_rec.get('items_modified')),
                'updated': now_iso,
                'revision': state['revision'],
            }
            if boxes_modified:
                page_rec['boxes'] = clean_boxes
            elif isinstance(prev_page_rec.get('boxes'), list):
                page_rec['boxes'] = prev_page_rec['boxes']
            if deleted_item_ids:
                prev_del = prev_page_rec.get('deleted_item_ids') if isinstance(prev_page_rec.get('deleted_item_ids'), list) else []
                page_rec['deleted_item_ids'] = sorted(set(prev_del) | set(deleted_item_ids))
            elif isinstance(prev_page_rec.get('deleted_item_ids'), list):
                page_rec['deleted_item_ids'] = prev_page_rec['deleted_item_ids']
            if reviewer:
                page_rec['reviewer'] = reviewer
            state['items'][page_id] = page_rec
            history_rows.append({'id': page_id, **page_rec})
            save_json(self.path, state)

            with (self.folder / 'feedback_history.jsonl').open('a', encoding='utf-8') as fh:
                for row in history_rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + '\n')

            # 4. 同步提交 M1 训练日志（data/m1/log/<stem>.log）
            sync_info = {'synced': False, 'reason': '未开启同步'}
            if sync_log:
                try:
                    sync_info = self._sync_m1_log(page, clean_boxes, reviewer)
                except Exception as exc:
                    sync_info = {'synced': False, 'reason': str(exc)}

            mtime = results_path.stat().st_mtime if results_path.is_file() else 0.0
            return {
                'ok': True,
                'feedback': state,
                'boxes': clean_boxes,
                'items': clean_items,
                'deleted_item_ids': deleted_item_ids,
                'sync': sync_info,
                'results_mtime': mtime,
            }

    @staticmethod
    def _box_iou(a, b):
        ax1, ay1, ax2, ay2 = [float(v) for v in a]
        bx1, by1, bx2, by2 = [float(v) for v in b]
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        if inter <= 0.0:
            return 0.0
        area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
        area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        union = area_a + area_b - inter
        return inter / union if union > 0.0 else 0.0

    def recognize_boxes(self, request, client_factory=None):
        """对调框模式下新增或修改的 1~N 个大题/姓名框直接裁图拼图并调用视觉大模型 API 识别。"""
        t0 = time.monotonic()
        page_id = str(request.get('page_id') or '').strip()
        targets = request.get('targets')
        if not isinstance(targets, list) or not targets:
            raise ValueError('请先选中或绘制至少一个待识别的框')
        if len(targets) > 24:
            raise ValueError('单次最多同时识别 24 个框，请分批点击')

        results_path = self.folder / 'results.json'
        with self.lock:
            results = _read_json_safe(results_path)
            pages = results.get('pages') or []
            page = next((p for p in pages if p.get('id') == page_id), None)
            if not page:
                raise ValueError('目标试卷页不存在：' + page_id)
            img_rel = str(page.get('image') or '')
            img_path = (self.folder / img_rel).resolve()
            if not img_rel or not img_path.is_relative_to(self.folder) or not img_path.is_file():
                raise ValueError('找不到该页工作图，无法裁图识别')
            existing_items = {it['id']: dict(it) for it in (page.get('items') or []) if isinstance(it, dict) and it.get('id')}
            used_ids = set(existing_items.keys())
            crops_dir = self.folder / 'crops'
            if crops_dir.is_dir():
                for cp in crops_dir.glob(f'{page_id}_m*.png'):
                    used_ids.add(cp.stem)

        # 校验视觉大模型配置
        if client_factory is not None:
            client = client_factory(self.folder)
        else:
            cfg = load_vlm_config()
            if not cfg.get('api_key'):
                raise ValueError(
                    '未配置有效的视觉大模型 API Key，请先在工作台右上角「⚙ 大模型 API」填入密钥并测试通过'
                )
            client = VlmClient(self.folder)

        # 读取工作图并裁剪每个目标框
        with Image.open(img_path) as im_raw:
            page_img = im_raw.convert('RGB')
        w, h = page_img.size

        def alloc_item_id():
            seq = 1
            while True:
                cand = f'{page_id}_m{seq:03d}'
                if cand not in used_ids:
                    used_ids.add(cand)
                    return cand
                seq += 1

        crops_dir.mkdir(parents=True, exist_ok=True)
        work_entries = []
        skipped_reasons = []
        for idx, raw in enumerate(targets):
            if not isinstance(raw, dict):
                continue
            try:
                cls = int(raw.get('cls'))
            except (TypeError, ValueError):
                skipped_reasons.append(f'第 {idx + 1} 框类别无效')
                continue
            kind = VLM_RECOG_CLS.get(cls)
            if not kind:
                skipped_reasons.append('仅大题-框 (M3) 与姓名学号 (M1) 支持直接调用大模型 API 识别')
                continue
            coords = raw.get('box')
            if not isinstance(coords, list) or len(coords) != 4:
                skipped_reasons.append(f'第 {idx + 1} 框坐标格式错误')
                continue
            try:
                x1, y1, x2, y2 = [float(v) for v in coords]
            except (TypeError, ValueError):
                skipped_reasons.append(f'第 {idx + 1} 框坐标包含非法数值')
                continue
            ix1 = max(0, min(w - 1, int(min(x1, x2))))
            iy1 = max(0, min(h - 1, int(min(y1, y2))))
            ix2 = max(ix1 + 1, min(w, int(round(max(x1, x2)))))
            iy2 = max(iy1 + 1, min(h, int(round(max(y1, y2)))))
            if (ix2 - ix1) < 8 or (iy2 - iy1) < 8:
                skipped_reasons.append(f'第 {idx + 1} 框过小（需至少 8×8 像素）')
                continue

            req_item_id = str(raw.get('item_id') or '').strip()[:80]
            if req_item_id and req_item_id in existing_items and existing_items[req_item_id].get('kind') == kind:
                ident = req_item_id
                base_item = dict(existing_items[ident])
            else:
                ident = alloc_item_id()
                base_item = {}

            crop_rel = f'crops/{ident}.png'
            crop_im = page_img.crop((ix1, iy1, ix2, iy2))
            crop_im.save(self.folder / crop_rel)

            item = {
                **base_item,
                'id': ident,
                'page_id': page_id,
                'kind': kind,
                'box': [round(float(ix1), 2), round(float(iy1), 2), round(float(ix2), 2), round(float(iy2), 2)],
                'crop': crop_rel,
                'number': base_item.get('number') if base_item else None,
                'issues': [],
                'status': '等待视觉转录',
                'manual': True,
            }
            work_entries.append({
                'cls': cls,
                'kind': kind,
                'client_uid': str(raw.get('client_uid') or ''),
                'item': item,
                'crop_im': crop_im,
                'had_paddle_number': bool(base_item.get('number_detail')),
            })

        if not work_entries:
            raise ValueError(skipped_reasons[0] if skipped_reasons else '没有可调用视觉大模型识别的有效框')

        # 用独立时间戳前缀打包拼图，绝不覆盖主流程的 g000/i000 拼图
        tag = f'u{int(time.time() * 100) % 10000000:07d}_'
        packers = {
            'fill': SquarePacker(self.folder, prefix=tag + 'f'),
            'identity': SquarePacker(self.folder, prefix=tag + 'i'),
        }
        grids = []
        for ent in work_entries:
            it = ent['item']
            tile_meta = {'id': it['id'], 'kind': it['kind'], 'page_id': page_id, 'number': it.get('number'), 'crop': it['crop']}
            done_grid = packers[it['kind']].add(tile_meta, ent['crop_im'])
            if done_grid:
                grids.append(done_grid)
        for pk in packers.values():
            rem = pk.finish()
            if rem:
                grids.append(rem)

        # 调用视觉大模型 API
        responses = []
        prompt_tok = 0
        comp_tok = 0
        cached_tok = 0
        for g in grids:
            rec = client.call(g, refresh=True)
            responses.append(rec)
            usage = rec.get('usage') or {}
            prompt_tok += int(usage.get('prompt_tokens') or 0)
            comp_tok += int(usage.get('completion_tokens') or 0)
            cached_tok += int((usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0)

        # 按与主流程一致的规则合并结果
        prefix = (getattr(client, 'provider_label', None) or '视觉模型').strip() + '：'
        entry_by_id = {e['item']['id']: e for e in work_entries}
        recognized = []
        failed = []
        for grid, response in zip(grids, responses):
            for tile in grid['items']:
                ent = entry_by_id.get(tile['id'])
                if ent is None:
                    continue
                item = ent['item']
                item.update(grid=grid['image'], grid_id=grid['id'], grid_box=tile['grid_box'], scale=tile['scale'])
                item.pop('transcription', None)
                item.pop('error', None)
                parsed = (response.get('items') or {}).get(item['id'])
                if not parsed:
                    item['status'] = '调用失败' if not response.get('ok') else '结构异常'
                    err_txt = (response.get('errors') or {}).get(item['id']) or response.get('error') or '缺图块输出'
                    item['error'] = str(err_txt)
                    item['issues'] = [prefix + str(err_txt)]
                    failed.append({'client_uid': ent['client_uid'], 'item': item, 'error': str(err_txt)})
                    continue
                item['transcription'] = parsed
                item['status'] = '已识别'
                issues = []
                if item['kind'] == 'identity':
                    if any(parsed.get(k) == '看不清' for k in ('name', 'sid')):
                        issues.append(prefix + '身份有看不清字段')
                else:
                    qno = str(parsed.get('question_number') or '').strip()
                    if qno.isdecimal():
                        item['number'] = int(qno)
                    elif qno == '题号看不清':
                        issues.append(prefix + '大题号看不清')
                    answers = parsed.get('answers') or {}
                    if any('.' in key for blanks in answers.values() if isinstance(blanks, dict) for key in blanks):
                        issues.append(prefix + '多级空位按原编号展示，请核对小题对应')
                    if '小题号看不清' in answers:
                        issues.append(prefix + '小题号看不清')
                    if '无小题号' in answers and len(answers) > 1:
                        issues.append(prefix + '同题同时有编号与无编号空位，请核对归属')
                    if any(v == '看不清' for blanks in answers.values() if isinstance(blanks, dict) for v in blanks.values()):
                        issues.append(prefix + '手写有看不清空位')
                if response.get('extra_ids'):
                    issues.append(prefix + '本拼图含额外输出ID，已隔离未绑定')
                item['issues'] = issues
                recognized.append({'client_uid': ent['client_uid'], 'cls': ent['cls'], 'item': item})

        # 将识别成功的条目与统计持久化落盘到 results.json 与 pages/<pid>.json（防止白花 API 费用）
        with self.lock:
            results = _read_json_safe(results_path)
            pages = results.get('pages') or []
            page = next((p for p in pages if p.get('id') == page_id), None)
            if page is not None:
                p_items = list(page.get('items') or [])
                idx_by_id = {it.get('id'): i for i, it in enumerate(p_items) if isinstance(it, dict)}
                p_boxes = list(page.get('boxes') or [])
                for row in recognized:
                    it = row['item']
                    if it['id'] in idx_by_id:
                        p_items[idx_by_id[it['id']]] = it
                    else:
                        idx_by_id[it['id']] = len(p_items)
                        p_items.append(it)
                    # 同步更新或补写对应标框（防止刷新页面时框丢失）
                    matched_bi = -1
                    for bi, b in enumerate(p_boxes):
                        if b.get('item_id') == it['id']:
                            matched_bi = bi
                            break
                    if matched_bi < 0:
                        best_iou = 0.0
                        for bi, b in enumerate(p_boxes):
                            if int(b.get('cls', -1)) != row['cls']:
                                continue
                            ov = self._box_iou(b.get('box') or [0, 0, 0, 0], it['box'])
                            if ov > best_iou:
                                best_iou = ov
                                matched_bi = bi
                        if best_iou < 0.7:
                            matched_bi = -1
                    new_box_obj = {
                        'cls': row['cls'],
                        'box': it['box'],
                        'stage': 'm1' if row['cls'] in M1_CLS else 'm3',
                        'item_id': it['id'],
                        'manual': True,
                    }
                    if matched_bi >= 0:
                        p_boxes[matched_bi] = {**p_boxes[matched_bi], **new_box_obj}
                    else:
                        p_boxes.append(new_box_obj)
                page['items'] = p_items
                page['boxes'] = p_boxes

                # 追加拼图与 token 统计
                if isinstance(results.get('grids'), list):
                    results['grids'].extend(grids)
                stats = results.get('stats')
                if not isinstance(stats, dict):
                    stats = {}
                    results['stats'] = stats
                stats['grids'] = int(stats.get('grids') or 0) + len(grids)
                stats['api_done'] = int(stats.get('api_done') or 0) + len(responses)
                stats['api_success'] = int(stats.get('api_success') or 0) + sum(1 for r in responses if r.get('ok'))
                stats['api_failures'] = int(stats.get('api_failures') or 0) + sum(1 for r in responses if not r.get('ok'))
                stats['prompt_tokens'] = int(stats.get('prompt_tokens') or 0) + prompt_tok
                stats['completion_tokens'] = int(stats.get('completion_tokens') or 0) + comp_tok
                stats['cached_tokens'] = int(stats.get('cached_tokens') or 0) + cached_tok
                self._recompute_results_stats(results)
                save_json(results_path, results)

                page_file = self.folder / 'pages' / (page_id + '.json')
                if page_file.is_file():
                    try:
                        pf = _read_json_safe(page_file, retries=4, delay=0.03)
                        pf['items'] = p_items
                        pf['boxes'] = p_boxes
                        save_json(page_file, pf)
                    except Exception:
                        pass
            mtime = results_path.stat().st_mtime if results_path.is_file() else 0.0

        cost_info = None
        try:
            cost_info = api_pricing.estimate_cost(
                getattr(client, 'model', ''),
                prompt_tokens=prompt_tok,
                completion_tokens=comp_tok,
                cached_tokens=cached_tok,
                pages=1,
            )
        except Exception:
            cost_info = None

        return {
            'ok': len(recognized) > 0,
            'page_id': page_id,
            'recognized': recognized,
            'failed': failed,
            'error': failed[0]['error'] if (not recognized and failed) else '',
            'usage': {
                'prompt_tokens': prompt_tok,
                'completion_tokens': comp_tok,
                'cached_tokens': cached_tok,
            },
            'cost': cost_info,
            'model': getattr(client, 'model', ''),
            'provider_label': getattr(client, 'provider_label', ''),
            'elapsed_seconds': round(time.monotonic() - t0, 2),
            'results_mtime': mtime,
        }


def make_server(folder, port=8903, host='127.0.0.1', lock_ttl=300, allow_sync_data=True, client_factory=None, hub_port=8888):
    store = ReviewStore(folder)
    claims = ClaimManager(ttl_seconds=lock_ttl)
    bus = ReviewEventBus()
    ui_states: dict[str, dict] = {}
    ui_lock = threading.Lock()
    stop_event = threading.Event()
    token = secrets.token_urlsafe(24)
    template_path = Path(__file__).resolve().parent / 'index.html'

    def _is_loopback_cli(handler: BaseHTTPRequestHandler) -> bool:
        ip = str((handler.client_address or ('', 0))[0])
        if ip not in ('127.0.0.1', '::1', 'localhost'):
            return False
        if handler.headers.get('Origin'):
            return False
        return handler.headers.get('X-AutoGrade-Internal') == 'cli'

    def _proxy_hub(handler: BaseHTTPRequestHandler, method: str, target_path: str, raw_body: bytes | None = None):
        """将 8904 页内 AI 帮手 / Agent 探测 / 离屏截图请求同源代理到 8888 工作台，零 CORS 冲突且共享多会话管理器。"""
        url = f'http://127.0.0.1:{int(hub_port)}{target_path}'
        headers = {}
        if raw_body is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(url, data=raw_body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = resp.read()
                ctype = resp.headers.get('Content-Type') or 'application/json; charset=utf-8'
                return handler.reply(data, code=resp.status, content_type=ctype)
        except urllib.error.HTTPError as exc:
            try:
                data = exc.read()
            except Exception:
                data = json.dumps({'ok': False, 'error': f'工作台返回 HTTP {exc.code}'}, ensure_ascii=False).encode()
            return handler.reply(data, code=exc.code)
        except Exception as exc:
            return handler.reply({
                'ok': False,
                'error': f'无法连接工作台控制中心 (127.0.0.1:{hub_port})，请先启动工作台后再使用 AI 帮手：{exc}',
            }, 503)

    def _latest_ui_state(client_id: str = '') -> dict:
        with ui_lock:
            now = time.time()
            # 清理 10 分钟未活跃的客户端 UI 快照
            stale = [k for k, v in ui_states.items() if now - float(v.get('_ts') or 0) > 600]
            for k in stale:
                ui_states.pop(k, None)
            if client_id and client_id in ui_states:
                return dict(ui_states[client_id])
            if not ui_states:
                return {}
            latest = max(ui_states.values(), key=lambda x: float(x.get('_ts') or 0))
            return dict(latest)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, value, code=200, content_type='application/json; charset=utf-8'):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            name = unquote(parsed.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if name == '/':
                template = template_path.read_text(encoding='utf-8')
                return self.reply(template.replace('__TOKEN__', token).encode(), content_type='text/html; charset=utf-8')
            if name == '/api/events':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'keep-alive')
                self.end_headers()
                q = bus.subscribe()
                try:
                    self.wfile.write(b': connected\n\n')
                    self.wfile.flush()
                    while not stop_event.is_set():
                        try:
                            ev = q.get(timeout=5)
                            chunk = ('data: ' + json.dumps(ev, ensure_ascii=False) + '\n\n').encode('utf-8')
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except queue.Empty:
                            self.wfile.write(b': keepalive\n\n')
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    bus.unsubscribe(q)
                return
            if name == '/api/ui-state':
                cid = str((qs.get('client_id') or [''])[0] or '').strip()
                with ui_lock:
                    all_clients = {k: {kk: vv for kk, vv in v.items() if kk != '_ts'} for k, v in ui_states.items()}
                cur = _latest_ui_state(cid)
                cur.pop('_ts', None)
                return self.reply({'ok': True, 'active': cur, 'clients': all_clients, 'page_revs': store.page_revs_snapshot()})
            if name == '/api/review/status':
                try:
                    results = _read_json_safe(store.folder / 'results.json')
                    fb = store.read()
                    fb_items = fb.get('items') or {}
                    pages = results.get('pages') or []
                    reviewed = sum(1 for p in pages if fb_items.get(p.get('id'), {}).get('label'))
                    issue_items = []
                    for p in pages:
                        for it in (p.get('items') or []):
                            if (it.get('issues') or []) or it.get('status') in ('调用失败', '结构异常', '待复核'):
                                issue_items.append({
                                    'page_id': p.get('id'),
                                    'item_id': it.get('id'),
                                    'kind': it.get('kind'),
                                    'number': it.get('number'),
                                    'status': it.get('status'),
                                    'issues': it.get('issues') or [],
                                })
                    cur_ui = _latest_ui_state()
                    cur_ui.pop('_ts', None)
                    return self.reply({
                        'ok': True,
                        'folder': str(store.folder),
                        'pages_total': len(pages),
                        'pages_reviewed': reviewed,
                        'issue_count': len(issue_items),
                        'issue_items': issue_items[:60],
                        'stats': results.get('stats') or {},
                        'locks': claims.snapshot(),
                        'ui_state': cur_ui,
                        'page_revs': store.page_revs_snapshot(),
                    })
                except (OSError, ValueError) as exc:
                    return self.reply({'ok': False, 'error': str(exc)}, 409)
            if name.startswith('/api/review/page/'):
                pid = name[len('/api/review/page/'):].strip('/')
                try:
                    return self.reply(store.get_page_detail(pid))
                except ValueError as exc:
                    return self.reply({'ok': False, 'error': str(exc)}, 404)
            if name == '/api/hub/events':
                # SSE 代理：把 8888 的 AI 会话实时事件转发给 8904 抽屉
                url = f'http://127.0.0.1:{int(hub_port)}/api/events'
                try:
                    req = urllib.request.Request(url, method='GET')
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                        self.send_header('Cache-Control', 'no-cache')
                        self.send_header('Connection', 'keep-alive')
                        self.end_headers()
                        while not stop_event.is_set():
                            line = resp.readline()
                            if not line:
                                break
                            self.wfile.write(line)
                            self.wfile.flush()
                except Exception:
                    pass
                return
            if name.startswith('/api/hub/'):
                target_path = '/api/' + name[len('/api/hub/'):]
                if parsed.query:
                    target_path += '?' + parsed.query
                return _proxy_hub(self, 'GET', target_path)
            if name == '/api/state':
                try:
                    results_path = store.folder/'results.json'
                    result = _read_json_safe(results_path)
                    live = store.folder/'benchmark_live.json'
                    if live.is_file() and not result.get('benchmark'):
                        result['benchmark'] = _read_json_safe(live)
                    mtime = results_path.stat().st_mtime if results_path.is_file() else 0.0
                    return self.reply({
                        'result': result,
                        'feedback': store.read(),
                        'locks': claims.snapshot(),
                        'results_mtime': mtime,
                        'page_revs': store.page_revs_snapshot(),
                    })
                except (OSError, ValueError) as exc:
                    return self.reply({'error': str(exc)}, 409)
            if name == '/api/locks':
                return self.reply({'ok': True, 'locks': claims.snapshot()})
            if name == '/api/live':
                try:
                    results_path = store.folder/'results.json'
                    mtime = results_path.stat().st_mtime if results_path.is_file() else 0.0
                    res_meta = {}
                    try:
                        res = _read_json_safe(results_path, retries=3, delay=0.02)
                        st = res.get('stats') or {}
                        pages_list = res.get('pages') or []
                        res_meta = {
                            'result_status': str(res.get('status') or ''),
                            'pages_count': len(pages_list),
                            'items_with_transcription': int(st.get('items_with_transcription') if 'items_with_transcription' in st else sum('transcription' in it for p in pages_list for it in (p.get('items') or []))),
                            'stats': st,
                        }
                    except Exception:
                        pass
                    return self.reply({
                        'ok': True,
                        'feedback': store.read(),
                        'locks': claims.snapshot(),
                        'results_mtime': mtime,
                        'page_revs': store.page_revs_snapshot(),
                        **res_meta,
                    })
                except (OSError, ValueError) as exc:
                    return self.reply({'ok': False, 'error': str(exc)}, 409)
            if name.startswith('/asset/'):
                path = (store.folder/name.removeprefix('/asset/')).resolve()
                if not path.is_relative_to(store.folder) or path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.svg', '.js', '.css', '.woff', '.woff2', '.ttf') or not path.is_file():
                    return self.reply({'error': '文件不存在'}, 404)
                return self.reply(path.read_bytes(), content_type=mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
            self.reply({'error': '接口不存在'}, 404)

        def do_POST(self):
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self.reply({'error': '禁止跨站修改'}, 403)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2000000:
                    raise ValueError('请求过长或为空')
                raw_bytes = self.rfile.read(size)
                value = json.loads(raw_bytes)
            except (ValueError, KeyError, TypeError) as exc:
                return self.reply({'error': str(exc)}, 400)

            req_token = self.headers.get('X-Review-Token') or (value.get('token') if isinstance(value, dict) else None)
            is_cli = _is_loopback_cli(self)
            if req_token != token and not is_cli:
                return self.reply({'error': '请求无效'}, 403)

            if path.startswith('/api/hub/'):
                target_path = '/api/' + path[len('/api/hub/'):]
                if parsed.query:
                    target_path += '?' + parsed.query
                return _proxy_hub(self, 'POST', target_path, raw_bytes)

            if path == '/api/ui-state':
                cid = str(value.get('client_id') or 'default').strip()[:64] or 'default'
                snap = {
                    'client_id': cid,
                    'page_id': str(value.get('page_id') or '')[:64],
                    'page_name': str(value.get('page_name') or '')[:120],
                    'mode': str(value.get('mode') or 'view')[:24],
                    'selected_item_id': str(value.get('selected_item_id') or '')[:80],
                    'selected_box_idx': int(value.get('selected_box_idx') if value.get('selected_box_idx') is not None else -1),
                    'zoom': float(value.get('zoom') or 1.0),
                    'fill_display_mode': str(value.get('fill_display_mode') or 'hover')[:24],
                    'show_overlay': bool(value.get('show_overlay', True)),
                    'show_conf': bool(value.get('show_conf', False)),
                    'reviewer': str(value.get('reviewer') or '')[:32],
                    'recent_errors': [str(x)[:200] for x in (value.get('recent_errors') or [])[:8]] if isinstance(value.get('recent_errors'), list) else [],
                    '_ts': time.time(),
                }
                with ui_lock:
                    ui_states[cid] = snap
                return self.reply({'ok': True})

            if path == '/api/ui/command':
                cmd_ev = {
                    'type': 'ui_command',
                    'source': str(value.get('source') or ('cli' if is_cli else 'gui')),
                    'action': str(value.get('action') or 'focus').strip()[:32],
                    'target': str(value.get('target') or '').strip()[:160],
                    'page_id': str(value.get('page_id') or '').strip()[:64],
                    'item_id': str(value.get('item_id') or '').strip()[:80],
                    'client_id': str(value.get('client_id') or '').strip()[:64],
                    'label': str(value.get('label') or '').strip()[:120],
                    'force_nav': bool(value.get('force_nav', False)),
                }
                bus.publish(cmd_ev)
                return self.reply({'ok': True, 'dispatched': cmd_ev})

            if path == '/api/review/set-text':
                try:
                    out = store.set_item_text(
                        page_id=str(value.get('page_id') or '').strip(),
                        item_id=str(value.get('item_id') or '').strip(),
                        text=str(value.get('text') if 'text' in value else value.get('correction', '')),
                        answer=value.get('answer'),
                        label=str(value.get('label') or '已订正'),
                        reviewer=str(value.get('reviewer') or 'AI助手'),
                        expected_page_rev=value.get('expected_page_rev'),
                    )
                    bus.publish({
                        'type': 'data_updated',
                        'source': 'ai' if is_cli else 'gui',
                        'action': 'set_text',
                        'page_id': out['page_id'],
                        'item_id': out['item_id'],
                        'page_rev': out['page_rev'],
                        'message': f"AI 已更新 {out['page_id']} / {out['item_id']} 订正内容",
                    })
                    return self.reply(out)
                except ValueError as exc:
                    return self.reply({'ok': False, 'error': str(exc)}, 409)

            if path == '/api/review/set-choice':
                try:
                    ans = str(value.get('answer') or '').strip().upper()[:16]
                    out = store.set_item_text(
                        page_id=str(value.get('page_id') or '').strip(),
                        item_id=str(value.get('item_id') or '').strip(),
                        text=ans,
                        answer=ans,
                        label=str(value.get('label') or '已订正'),
                        reviewer=str(value.get('reviewer') or 'AI助手'),
                        expected_page_rev=value.get('expected_page_rev'),
                    )
                    bus.publish({
                        'type': 'data_updated',
                        'source': 'ai' if is_cli else 'gui',
                        'action': 'set_choice',
                        'page_id': out['page_id'],
                        'item_id': out['item_id'],
                        'page_rev': out['page_rev'],
                        'message': f"AI 已修改选择题 {out['item_id']} 为 {ans}",
                    })
                    return self.reply(out)
                except ValueError as exc:
                    return self.reply({'ok': False, 'error': str(exc)}, 409)

            if path == '/api/review/delete-box':
                try:
                    out = store.delete_item_or_box(
                        page_id=str(value.get('page_id') or '').strip(),
                        item_id=str(value.get('item_id') or '').strip(),
                        box_index=value.get('box_index'),
                        reviewer=str(value.get('reviewer') or 'AI助手'),
                        expected_page_rev=value.get('expected_page_rev'),
                    )
                    bus.publish({
                        'type': 'data_updated',
                        'source': 'ai' if is_cli else 'gui',
                        'action': 'delete_box',
                        'page_id': out['page_id'],
                        'deleted_item_ids': out['deleted_item_ids'],
                        'page_rev': out['page_rev'],
                        'results_mtime': out['results_mtime'],
                        'message': f"AI 已删除 {out['page_id']} 中的标框",
                    })
                    return self.reply(out)
                except ValueError as exc:
                    return self.reply({'ok': False, 'error': str(exc)}, 409)

            if path == '/api/claim':
                client_id = str(value.get('client_id') or '').strip()
                user = str(value.get('user') or '成员').strip() or '成员'
                exclude = value.get('exclude') or []
                result = _read_json_safe(store.folder/'results.json')
                fb = store.read().get('items', {})
                pages = result.get('pages', [])
                def page_done(pid):
                    return bool(fb.get(pid, {}).get('label'))
                res = claims.claim_next(client_id, user, [p['id'] for p in pages], page_done, exclude=exclude)
                res['page_id'] = res.get('key')
                res['locks'] = claims.snapshot()
                return self.reply(res)
            if path == '/api/submit-page':
                try:
                    pid = str(value.get('page_id') or '').strip()
                    with store.page_lock(pid):
                        out = store.submit_page(value, allow_sync_data=allow_sync_data)
                        new_rev = store.bump_page_rev(pid)
                    client_id = str(value.get('client_id') or '').strip()
                    if client_id and pid:
                        claims.release(client_id, pid)
                    out['locks'] = claims.snapshot()
                    out['page_rev'] = new_rev
                    bus.publish({
                        'type': 'data_updated',
                        'source': 'ai' if is_cli else 'gui',
                        'action': 'submit_page',
                        'page_id': pid,
                        'page_rev': new_rev,
                        'results_mtime': out.get('results_mtime'),
                        'message': f"已保存并确认 {pid}",
                    })
                    return self.reply(out)
                except (ValueError, KeyError, TypeError) as exc:
                    return self.reply({'ok': False, 'error': str(exc)}, 409)
                except OSError:
                    return self.reply({'ok': False, 'error': '保存失败，请重试'}, 500)
            if path == '/api/recognize-box':
                try:
                    pid = str(value.get('page_id') or '').strip()
                    with store.page_lock(pid):
                        out = store.recognize_boxes(value, client_factory=client_factory)
                        new_rev = store.bump_page_rev(pid)
                    out['page_rev'] = new_rev
                    if out.get('ok'):
                        bus.publish({
                            'type': 'data_updated',
                            'source': 'ai' if is_cli else 'gui',
                            'action': 'recognize_box',
                            'page_id': pid,
                            'page_rev': new_rev,
                            'results_mtime': out.get('results_mtime'),
                            'message': f"已重新识别 {pid} 中的 {len(out.get('recognized') or [])} 个框",
                        })
                    return self.reply(out, 200 if out.get('ok') else 502)
                except (ValueError, KeyError, TypeError) as exc:
                    return self.reply({'ok': False, 'error': str(exc)}, 400)
                except OSError as exc:
                    return self.reply({'ok': False, 'error': '裁图或保存失败：' + str(exc)}, 500)
                except Exception as exc:
                    return self.reply({'ok': False, 'error': '识别调用异常：' + str(exc)}, 500)
            if path == '/api/acquire':
                pid = str(value.get('page_id') or '').strip()
                client_id = str(value.get('client_id') or '').strip()
                user = str(value.get('user') or '成员').strip() or '成员'
                res = claims.acquire(pid, client_id, user, force=bool(value.get('force', False)))
                res['locks'] = claims.snapshot()
                return self.reply(res, 200 if res.get('ok') else 409)
            if path == '/api/heartbeat':
                pid = str(value.get('page_id') or '').strip()
                client_id = str(value.get('client_id') or '').strip()
                user = str(value.get('user') or '成员').strip() or '成员'
                res = claims.heartbeat(pid, client_id, user)
                res['locks'] = claims.snapshot()
                return self.reply(res)
            if path == '/api/release':
                client_id = str(value.get('client_id') or '').strip()
                pid = value.get('page_id')
                return self.reply(claims.release(client_id, pid if pid else None))
            if path != '/api/feedback':
                return self.reply({'error': '请求无效'}, 403)
            try:
                out = store.update(value)
                bus.publish({'type': 'data_updated', 'source': 'ai' if is_cli else 'gui', 'action': 'feedback', 'id': value.get('id')})
                self.reply(out)
            except (ValueError, KeyError, TypeError) as exc:
                self.reply({'error': str(exc)}, 409)
            except OSError:
                self.reply({'error': '保存失败，草稿保留，请重试'}, 500)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv._stop_event = stop_event
    return srv


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8903)
    parser.add_argument('--hub-port', type=int, default=8888, help='主控工作台端口（用于 8904 内 AI 帮手同源代理）')
    parser.add_argument('--host', default=None, help='监听地址，默认 127.0.0.1；多人局域网协作传 --lan 或 --host 0.0.0.0')
    parser.add_argument('--lan', action='store_true', help='开启局域网监听（等价于 --host 0.0.0.0）')
    parser.add_argument('--lock-ttl', type=int, default=300, help='多人协作单页锁定超时秒数，默认 300 秒')
    args = parser.parse_args()
    prepare_math_assets(args.dir, async_download=True)
    host = resolve_host(args.host, args.lan)
    server = make_server(args.dir, args.port, host=host, lock_ttl=args.lock_ttl, hub_port=args.hub_port)
    print('识别验收服务已启动', flush=True)
    print_access_urls(host, server.server_port, label='识别验收')
    server.serve_forever()

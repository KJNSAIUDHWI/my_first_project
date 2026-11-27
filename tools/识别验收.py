"""本地识别验收与合一审阅服务（支持整页确认、原地改答案/改框并同步提交 data/m1 训练日志）。"""
import argparse
from datetime import datetime
import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import shutil
import sys
import threading
import time
import tarfile
import urllib.request
from urllib.parse import unquote, urlparse

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / 'pipeline'))
from common.recognition_api import save_json
from common.lan_collab import ClaimManager, resolve_host, print_access_urls
from common import events as label_events

LABELS = ('已确认', '已订正', '正确', '识别错误', '漏题／漏空', '对应错误', '看不清')
M1_CLS = {0, 1, 4}
M2_CLS = {2, 3}
M3_CLS = {5, 6}
ALL_BOX_CLS = M1_CLS | M2_CLS | M3_CLS


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


def prepare_math_assets(folder):
    """固定版本静态公式资源放在本次结果内；以后看报告不依赖外部CDN。"""
    target = Path(folder).resolve()/'vendor/katex'
    if (target/'katex.min.js').is_file() and (target/'katex.min.css').is_file():
        return
    try:
        raw = urllib.request.urlopen('https://registry.npmjs.org/katex/-/katex-0.16.22.tgz', timeout=30).read()
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            for member in archive.getmembers():
                if member.name in ('package/dist/katex.min.js', 'package/dist/katex.min.css') or (member.name.startswith('package/dist/fonts/') and member.name.endswith('.woff2')):
                    dest = (target/member.name.removeprefix('package/dist/')).resolve()
                    if not dest.is_relative_to(target):
                        raise ValueError('公式资源路径异常')
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(archive.extractfile(member).read())
    except (OSError, ValueError, tarfile.TarError):
        print('公式资源未准备完成，公式暂显示原始转录；重启服务可重试。', flush=True)


class ReviewStore:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.path = self.folder/'feedback.json'
        self.lock = threading.Lock()
        # Bind feedback to immutable input/model manifest, not changing completion status.
        self.identity = hashlib.sha256((self.folder/'manifest.json').read_bytes()).hexdigest()

    def read(self):
        if not self.path.exists():
            return {'run': self.identity, 'revision': 0, 'items': {}}
        value = json.loads(self.path.read_text(encoding='utf-8'))
        if value['run'] != self.identity:
            raise ValueError('反馈属于另一批输入')
        return value

    def update(self, request):
        results = json.loads((self.folder/'results.json').read_text(encoding='utf-8'))
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
                clean.append(item)
            except (TypeError, ValueError):
                continue
        return clean

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
        """整页提交：保存整页确认状态、答案/手写文字订正、原地改框结果，并同步提交 data/m1 log。"""
        page_id = str(request.get('page_id') or '').strip()
        reviewer = str(request.get('reviewer') or '').strip()[:32]
        page_status = str(request.get('page_status') or '已确认').strip()
        if page_status not in LABELS:
            page_status = '已确认'
        item_edits = request.get('item_edits') or {}
        if not isinstance(item_edits, dict):
            raise ValueError('item_edits 格式错误')
        boxes_modified = bool(request.get('boxes_modified'))
        sync_log = bool(request.get('sync_log', True)) and allow_sync_data

        with self.lock:
            results_path = self.folder / 'results.json'
            results = json.loads(results_path.read_text(encoding='utf-8'))
            pages = results.get('pages') or []
            page = next((p for p in pages if p.get('id') == page_id), None)
            if not page:
                raise ValueError('目标试卷页不存在：' + page_id)
            item_map = {it['id']: it for it in (page.get('items') or [])}

            state = self.read()
            now_iso = datetime.now().isoformat(timespec='seconds')
            history_rows = []

            # 1. 保存每题的文字/选项订正（只存用户改过的，不强迫逐题打卡）
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

            # 2. 保存标框改动到 results.json & pages/<pid>.json
            clean_boxes = page.get('boxes') or []
            if boxes_modified and isinstance(request.get('boxes'), list):
                clean_boxes = self._sanitize_boxes(request.get('boxes'), page.get('width'), page.get('height'))
                page['boxes'] = clean_boxes
                save_json(results_path, results)
                page_file = self.folder / 'pages' / (page_id + '.json')
                if page_file.is_file():
                    try:
                        pf = json.loads(page_file.read_text(encoding='utf-8'))
                        pf['boxes'] = clean_boxes
                        save_json(page_file, pf)
                    except Exception:
                        pass

            # 3. 写入页级已确认记录（背面 0 题同样走这里，按页记账，不再误判）
            state['revision'] += 1
            page_rec = {
                'label': page_status,
                'correction': str(request.get('note') or '').strip()[:2000],
                'boxes_modified': boxes_modified,
                'updated': now_iso,
                'revision': state['revision'],
            }
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

            return {'ok': True, 'feedback': state, 'boxes': clean_boxes, 'sync': sync_info}


def make_server(folder, port=8903, host='127.0.0.1', lock_ttl=300, allow_sync_data=True):
    store = ReviewStore(folder)
    claims = ClaimManager(ttl_seconds=lock_ttl)
    token = secrets.token_urlsafe(24)
    template_path = Path(__file__).with_suffix('.html')
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
            name = unquote(urlparse(self.path).path)
            if name == '/':
                template = template_path.read_text(encoding='utf-8')
                return self.reply(template.replace('__TOKEN__', token).encode(), content_type='text/html; charset=utf-8')
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
            path = unquote(urlparse(self.path).path)
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self.reply({'error': '禁止跨站修改'}, 403)
            # release 允许 sendBeacon 携带 body 内 token
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2000000:
                    raise ValueError('请求过长或为空')
                value = json.loads(self.rfile.read(size))
            except (ValueError, KeyError, TypeError) as exc:
                return self.reply({'error': str(exc)}, 400)
            req_token = self.headers.get('X-Review-Token') or (value.get('token') if isinstance(value, dict) else None)
            if req_token != token:
                return self.reply({'error': '请求无效'}, 403)
            if path == '/api/claim':
                client_id = str(value.get('client_id') or '').strip()
                user = str(value.get('user') or '成员').strip() or '成员'
                exclude = value.get('exclude') or []
                result = json.loads((store.folder/'results.json').read_text(encoding='utf-8'))
                fb = store.read().get('items', {})
                pages = result.get('pages', [])
                def page_done(pid):
                    return bool(fb.get(pid, {}).get('label'))
                res = claims.claim_next(client_id, user, [p['id'] for p in pages], page_done, exclude=exclude)
                res['locks'] = claims.snapshot()
                return self.reply(res)
            if path == '/api/submit-page':
                try:
                    out = store.submit_page(value, allow_sync_data=allow_sync_data)
                    client_id = str(value.get('client_id') or '').strip()
                    pid = str(value.get('page_id') or '').strip()
                    if client_id and pid:
                        claims.release(client_id, pid)
                    out['locks'] = claims.snapshot()
                    return self.reply(out)
                except (ValueError, KeyError, TypeError) as exc:
                    return self.reply({'ok': False, 'error': str(exc)}, 409)
                except OSError:
                    return self.reply({'ok': False, 'error': '保存失败，请重试'}, 500)
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
                self.reply(store.update(value))
            except (ValueError, KeyError, TypeError) as exc:
                self.reply({'error': str(exc)}, 409)
            except OSError:
                self.reply({'error': '保存失败，草稿保留，请重试'}, 500)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8903)
    parser.add_argument('--host', default=None, help='监听地址，默认 127.0.0.1；多人局域网协作传 --lan 或 --host 0.0.0.0')
    parser.add_argument('--lan', action='store_true', help='开启局域网监听（等价于 --host 0.0.0.0）')
    parser.add_argument('--lock-ttl', type=int, default=300, help='多人协作单页锁定超时秒数，默认 300 秒')
    args = parser.parse_args()
    prepare_math_assets(args.dir)
    host = resolve_host(args.host, args.lan)
    server = make_server(args.dir, args.port, host=host, lock_ttl=args.lock_ttl)
    print('识别验收服务已启动', flush=True)
    print_access_urls(host, server.server_port, label='识别验收')
    server.serve_forever()

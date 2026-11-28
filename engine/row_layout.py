"""题目印刷行排版解析（优先聚合 VLM 输出的 rows，老批次自动用本地印刷横线检测兜底）。"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - 可选本地 CV 依赖
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]

CACHE_VERSION = 1


def normalize_row_layout(raw: Any) -> list[int] | None:
    """将模型或检测返回的每行空数规范为正整数列表，非法时返回 None（不阻断有效转录）。"""
    if not isinstance(raw, (list, tuple)) or not raw or len(raw) > 20:
        return None
    out: list[int] = []
    for v in raw:
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            n = v
        elif isinstance(v, str) and v.strip().isdigit():
            n = int(v.strip())
        else:
            return None
        if n < 1 or n > 30:
            return None
        out.append(n)
    return out


def normalize_question_key(item: dict) -> str:
    """提取大题号标准键（如 '20.' / 20 -> '20'），用于同批次跨卷版面模板聚合。"""
    tr = item.get('transcription') if isinstance(item.get('transcription'), dict) else {}
    raw = str(tr.get('question_number') or item.get('number') or '').strip()
    if not raw or '看不清' in raw:
        return ''
    m = re.search(r'\d+', raw)
    return m.group(0) if m else raw


def compute_item_expected_blanks(item: dict) -> int:
    """统计一道填空/实验题当前可渲染的总空位数（含图舍弃小题计 1 项）。"""
    tr = item.get('transcription') if isinstance(item.get('transcription'), dict) else {}
    ans = tr.get('answers')
    if not isinstance(ans, dict) or not ans:
        return 0
    total = 0
    for blanks in ans.values():
        if isinstance(blanks, dict):
            total += 1 if blanks.get('舍弃') == '舍弃' else len(blanks)
        else:
            total += 1
    return total


def detect_underline_rows_from_gray(gray) -> list[int]:
    """从灰度图块中提取印刷填空横线并按行聚类，返回每行横线数量列表。"""
    if cv2 is None or np is None or gray is None:
        return []
    h, w = gray.shape[:2]
    if h < 20 or w < 40:
        return []
    bw = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 12
    )
    my = max(2, int(h * 0.04))
    mx = max(2, int(w * 0.01))
    bw[:my, :] = 0
    bw[h - my:, :] = 0
    bw[:, :mx] = 0
    bw[:, w - mx:] = 0

    kw = max(48, min(110, int(w * 0.065)))
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kw, 1))
    lines = cv2.morphologyEx(bw, cv2.MORPH_OPEN, horiz_kernel)
    lines = cv2.dilate(lines, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))

    num_labels, _, stats, centroids = cv2.connectedComponentsWithStats(lines)
    segs: list[tuple[float, int, int, int]] = []
    max_sh = max(16, int(h * 0.20))
    for i in range(1, num_labels):
        x, _, sw, sh, _ = stats[i]
        if sw < kw or sw > w * 0.88 or sh > max_sh:
            continue
        segs.append((float(centroids[i][1]), int(x), int(x + sw), int(sw)))
    if not segs:
        return []

    med_w = float(np.median([s[3] for s in segs]))
    min_w = max(float(kw), med_w * 0.35)
    segs = [s for s in segs if s[3] >= min_w]
    if not segs:
        return []

    segs.sort(key=lambda s: s[0])
    y_tol = max(16.0, min(34.0, h * 0.14))
    rows: list[dict[str, Any]] = []
    for seg in segs:
        if not rows or abs(seg[0] - rows[-1]['cy']) > y_tol:
            rows.append({'cy': seg[0], 'segs': [seg]})
        else:
            r = rows[-1]
            r['segs'].append(seg)
            r['cy'] = sum(x[0] for x in r['segs']) / len(r['segs'])

    out: list[int] = []
    for r in rows:
        rs = sorted(r['segs'], key=lambda s: s[1])
        merged: list[tuple[int, int]] = []
        for _, x1, x2, _ in rs:
            if merged and x1 <= merged[-1][1] + 2:
                merged[-1] = (merged[-1][0], max(merged[-1][1], x2))
            else:
                merged.append((x1, x2))
        out.append(len(merged))
    return out


def _read_gray_unicode(path: Path):
    if cv2 is None or np is None or not path.is_file():
        return None
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    except OSError:
        return None


def detect_crop_underline_rows(
    crop_path: Path | None,
    page_img_path: Path | None = None,
    box: list | None = None,
) -> list[int]:
    """读取裁切图或从原卷按 box 现裁，检测该题每行印刷横线数。"""
    if crop_path is not None and crop_path.is_file():
        return detect_underline_rows_from_gray(_read_gray_unicode(crop_path))
    if page_img_path is not None and page_img_path.is_file() and box and len(box) == 4:
        gray = _read_gray_unicode(page_img_path)
        if gray is None:
            return []
        h, w = gray.shape[:2]
        x1 = max(0, min(w, int(round(box[0]))))
        y1 = max(0, min(h, int(round(box[1]))))
        x2 = max(x1, min(w, int(round(box[2]))))
        y2 = max(y1, min(h, int(round(box[3]))))
        if x2 - x1 < 40 or y2 - y1 < 20:
            return []
        return detect_underline_rows_from_gray(gray[y1:y2, x1:x2])
    return []


def _pick_weighted_layout(votes: Counter[tuple[int, ...]], expected_mode: int) -> list[int] | None:
    if not votes:
        return None
    # 优先选总和与该题典型空位数一致的方案
    if expected_mode > 0:
        exact = [(score, layout) for layout, score in votes.items() if sum(layout) == expected_mode]
        if exact:
            exact.sort(key=lambda x: (-x[0], -len(x[1]), x[1]))
            return list(exact[0][1])
    best = sorted(votes.items(), key=lambda x: (-x[1], -len(x[0]), x[0]))[0][0]
    return list(best)


def build_batch_row_layouts(
    result: dict,
    recognized_dir: Path | None = None,
    *,
    allow_cv_fallback: bool = True,
    use_cache: bool = True,
) -> dict[str, list[int]]:
    """
    汇总整批试卷各题号的行排版模板 {question_number: [row1_blanks, row2_blanks, ...]}：
    1) 优先聚合所有页面 VLM 返回的 transcription.rows（按题号加权众数）；
    2) 对尚无 VLM rows 的老批次题号，自动抽样至多 5 页做本地印刷横线检测兜底；
    3) 结果按 results.json mtime/size 缓存在 row_layouts_cache.json，后续读取 <1ms。
    """
    cache_path = (recognized_dir / 'row_layouts_cache.json') if recognized_dir else None
    sig = None
    if use_cache and recognized_dir is not None:
        res_path = recognized_dir / 'results.json'
        if res_path.is_file():
            try:
                st = res_path.stat()
                sig = f'{CACHE_VERSION}:{st.st_mtime_ns}:{st.st_size}:{int(bool(allow_cv_fallback))}'
                if cache_path and cache_path.is_file():
                    cached = json.loads(cache_path.read_text(encoding='utf-8'))
                    if isinstance(cached, dict) and cached.get('sig') == sig and isinstance(cached.get('layouts'), dict):
                        return cached['layouts']
            except (OSError, ValueError):
                pass

    vlm_votes: dict[str, Counter[tuple[int, ...]]] = defaultdict(Counter)
    exp_counts: dict[str, Counter[int]] = defaultdict(Counter)
    cv_samples: dict[str, list[tuple[Path | None, Path | None, list | None, int]]] = defaultdict(list)

    for page in result.get('pages') or []:
        if not isinstance(page, dict):
            continue
        page_img = (recognized_dir / page['image']) if (recognized_dir and page.get('image')) else None
        for item in page.get('items') or []:
            if not isinstance(item, dict) or item.get('kind') != 'fill':
                continue
            qkey = normalize_question_key(item)
            if not qkey:
                continue
            exp_n = compute_item_expected_blanks(item)
            if exp_n > 0:
                exp_counts[qkey][exp_n] += 1
            tr = item.get('transcription') if isinstance(item.get('transcription'), dict) else {}
            vlm_rows = normalize_row_layout(tr.get('rows'))
            if vlm_rows:
                weight = 3 if (exp_n > 0 and sum(vlm_rows) == exp_n) else 1
                vlm_votes[qkey][tuple(vlm_rows)] += weight
            elif allow_cv_fallback and recognized_dir is not None and len(cv_samples[qkey]) < 5:
                crop_p = (recognized_dir / item['crop']) if item.get('crop') else None
                cv_samples[qkey].append((crop_p, page_img, item.get('box'), exp_n))

    layouts: dict[str, list[int]] = {}
    all_qkeys = set(vlm_votes.keys()) | set(cv_samples.keys())
    for qkey in sorted(all_qkeys, key=lambda x: (int(x) if x.isdigit() else 9999, x)):
        exp_mode = exp_counts[qkey].most_common(1)[0][0] if exp_counts[qkey] else 0
        if vlm_votes.get(qkey):
            picked = _pick_weighted_layout(vlm_votes[qkey], exp_mode)
            if picked:
                layouts[qkey] = picked
            continue
        if not allow_cv_fallback or cv2 is None:
            continue
        cv_votes: Counter[tuple[int, ...]] = Counter()
        for crop_p, page_img, box, exp_n in cv_samples.get(qkey) or []:
            det = normalize_row_layout(detect_crop_underline_rows(crop_p, page_img, box))
            if not det:
                continue
            w = 3 if (exp_n > 0 and sum(det) == exp_n) else 1
            cv_votes[tuple(det)] += w
        if cv_votes:
            picked = _pick_weighted_layout(cv_votes, exp_mode)
            # 本地 CV 兜底仅在总空数匹配或明确有效多行时采用，避免误拆
            if picked and (exp_mode <= 0 or sum(picked) == exp_mode):
                layouts[qkey] = picked

    if use_cache and cache_path is not None and sig is not None:
        try:
            cache_path.write_text(
                json.dumps({'sig': sig, 'layouts': layouts}, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
        except OSError:
            pass

    return layouts

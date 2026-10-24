# -*- coding: utf-8 -*-
"""选择题区 -> 答案。

输入：选择题区 crop 图 + 该图上的框（工具类别 2=题号，3=选项，归一化 cx/cy/w/h）
输出：每题 {no, answer, digit_conf, choice_conf}
      no=0 表示题号读成非数字（大概率误检），answer='' 表示选项读不出。
"""
import os
import sys

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cnn.choice import ChoiceReader  # noqa: E402
from cnn.digit import DigitReader  # noqa: E402
from stages.match import match_nums_to_options  # noqa: E402


def _crop(img, cx, cy, bw, bh, pad=2):
    h, w = img.shape[:2]
    x1 = max(0, int((cx - bw / 2) * w) - pad)
    x2 = min(w, int((cx + bw / 2) * w) + pad)
    y1 = max(0, int((cy - bh / 2) * h) - pad)
    y2 = min(h, int((cy + bh / 2) * h) + pad)
    return img[y1:y2, x1:x2]


def _box_px(cx, cy, bw, bh, w, h, pad=2):
    return (max(0, int((cx - bw / 2) * w) - pad),
            max(0, int((cy - bh / 2) * h) - pad),
            min(w, int((cx + bw / 2) * w) + pad),
            min(h, int((cy + bh / 2) * h) + pad))


def crop_option_row(img, nums_px, ox1, oy1, ox2, oy2, paired=None, w=None):
    """从选项大框裁出纯选项部分（左上/右下像素坐标）。

    框内有题号：起点 = 框内最右侧题号的右边缘；
    框内没有题号（2026.10 多选 Q11/Q12，题号印在框外）：起点 = 大框左边缘；
    特例：框内没有题号、且大框左缘离题号太近（框把「（多选）」也框进去了，
    如 2026.10.0311），起点右移 8% 图宽，跳过文字。
    """
    inside = [n for n in nums_px if ox1 <= n[0] <= ox2 and oy1 <= n[1] <= oy2]
    if inside:
        cut = int(max(n[0] + n[2] / 2 for n in inside)) + 2
    else:
        cut = ox1
        if paired is not None and w:
            px2 = paired[0] + paired[2] / 2
            if ox1 - px2 < 0.05 * w:
                cut = int(px2 + 0.08 * w)
    if cut >= ox2 - 20 or cut < ox1:
        cut = ox1
    return img[oy1:oy2, cut:ox2]


def read_choice_zone(img, boxes, digit_reader=None, choice_reader=None,
                     min_digit_conf=0.3, min_choice_conf=0.3, fix_no=True, is_en=False):
    dr = digit_reader or DigitReader()
    cr = choice_reader or ChoiceReader()
    h, w = img.shape[:2]
    nums, opts = [], []
    for b in boxes:
        cls, cx, cy, bw, bh = b[:5]
        if cls == 2:
            nums.append((cx, cy, bw, bh))
        elif cls == 3:
            opts.append((cx, cy, bw, bh))
    # 像素坐标做配对，y_weight=10 才有意义
    nums_px = [(x * w, y * h, bw * w, bh * h) for x, y, bw, bh in nums]
    opts_px = [(x * w, y * h, bw * w, bh * h) for x, y, bw, bh in opts]
    pairs = match_nums_to_options(nums_px, opts_px)

    rows = []
    for i, j, dist in pairs:
        ncx, ncy, nbw, nbh = nums[i]
        ocx, ocy, obw, obh = opts[j]
        no, nconf = dr.read(_crop(img, ncx, ncy, nbw, nbh))
        ox1, oy1, ox2, oy2 = _box_px(ocx, ocy, obw, obh, w, h)
        cc = crop_option_row(img, nums_px, ox1, oy1, ox2, oy2, paired=nums_px[i], w=w)
        ans, aconf = cr.read(cc)
        rows.append({
            "no": no,
            "answer": ans,
            "digit_conf": round(nconf, 3),
            "choice_conf": round(aconf, 3),
            "valid": bool(no > 0 and nconf >= min_digit_conf and aconf >= min_choice_conf),
            "pair_cost": round(dist, 1),
            "num_box": [ncx, ncy, nbw, nbh],
            "opt_box": [ocx, ocy, obw, obh],
        })
    rows.sort(key=lambda r: (r["no"] if r["no"] > 0 else 999, -r["choice_conf"]))
    if fix_no:
        fix_no_sequence(rows, is_en=is_en)
        for r in rows:
            r["valid"] = bool(r["no"] > 0 and r["digit_conf"] >= min_digit_conf
                              and r["choice_conf"] >= min_choice_conf)
    return rows


def sequence_score(rows):
    """整区题号是否 1..N 连续；返回 (是否连续, 题号列表)。"""
    nos = [r["no"] for r in rows if r["no"] > 0]
    if not nos:
        return False, []
    return nos == list(range(nos[0], nos[0] + len(nos))), nos


# ---------- 题号连续性纠正 ----------
# 卷面题号必然连续，单个读错可以用位置纠正：按行/列排好序，用读数投票定起点，
# 再按位置重新赋题号。三种候选排序（列优先 / 行优先 / 按 y 分块后列优先），
# 取投票一致性最高且起点 >= 1 的那个。

def _order_col(items):
    s = sorted(items, key=lambda r: r["opt_box"][0])
    cols, cur = [], []
    for r in s:
        if cur and r["opt_box"][0] - cur[-1]["opt_box"][0] > 0.08:
            cols.append(cur)
            cur = []
        cur.append(r)
    if cur:
        cols.append(cur)
    return [r for c in cols for r in sorted(c, key=lambda r: r["opt_box"][1])]


def _order_row(items):
    s = sorted(items, key=lambda r: r["opt_box"][1])
    groups, cur = [], []
    for r in s:
        if cur and r["opt_box"][1] - cur[-1]["opt_box"][1] > 0.06:
            groups.append(cur)
            cur = []
        cur.append(r)
    if cur:
        groups.append(cur)
    return [r for g in groups for r in sorted(g, key=lambda r: r["opt_box"][0])]


def _order_block_col(items, gap=0.25):
    """先按 y 把上下分开的大块切开（如底部多选题），块内再列优先。"""
    s = sorted(items, key=lambda r: r["opt_box"][1])
    blocks, cur = [], []
    for r in s:
        if cur and r["opt_box"][1] - cur[-1]["opt_box"][1] > gap:
            blocks.append(cur)
            cur = []
        cur.append(r)
    if cur:
        blocks.append(cur)
    return [r for b in blocks for r in _order_col(b)]


def _vote_seq(seq):
    from collections import Counter
    v = [r["no"] for r in seq]
    cand = Counter(x - i for i, x in enumerate(v) if x > 0)
    if not cand:
        return 0.0, 0
    m, hits = cand.most_common(1)[0]
    return hits / len(v), m


def fix_no_sequence(rows, is_en=False, min_conf=0.6):
    """按位置 + 读数投票重赋题号。返回 (方法, 一致性, 起点)。

    行数 < 3 的区不做纠正（2 个读数投票没有冗余）。
    一致性低于 min_conf 或算不出起点 >= 1 时保持原值。
    """
    if len(rows) < 3:
        return "keep", 0.0, 0
    cands = [("col", _order_col(rows)), ("row", _order_row(rows)),
             ("block_col", _order_block_col(rows))]
    best = None
    pref = "row" if is_en else "col"
    for name, seq in cands:
        conf, m = _vote_seq(seq)
        if m < 1:
            continue
        score = conf + (0.001 if name == pref else 0)
        if best is None or score > best[0]:
            best = (score, name, seq, conf, m)
    if best is None or best[3] < min_conf:
        return "keep", 0.0, 0
    _, name, seq, conf, m = best
    for i, r in enumerate(seq):
        r["no"] = m + i
    rows.sort(key=lambda r: (r["no"], -r["choice_conf"]))
    return name, conf, m

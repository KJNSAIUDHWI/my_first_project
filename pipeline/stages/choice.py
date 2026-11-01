# -*- coding: utf-8 -*-
"""选择题区 -> 答案。

输入：选择题区 crop 图 + 该图上的框（工具类别 2=题号，3=选项，归一化 cx/cy/w/h）
输出：每题 {no, answer, digit_conf, choice_conf}
      no=0 表示题号未读出，answer='' 表示未涂或选项读不出。
"""
import os
import sys
from collections import Counter
from statistics import median

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

    只去掉已配对且确实位于框内的题号。框外题号不改变选项左边缘；
    不以整图宽度猜测“多选”等文字的长度，避免截掉A选项。
    """
    candidates = [paired] if paired is not None else nums_px
    inside = [n for n in candidates if ox1 <= n[0] <= ox2 and oy1 <= n[1] <= oy2]
    if inside:
        cut = int(max(n[0] + n[2] / 2 for n in inside)) + 2
    else:
        cut = ox1
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
            "no_raw": no,
            "no_source": "model",
            "number_conf": round(nconf, 3),
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
    counts = Counter(r["no"] for r in rows)
    for r in rows:
        r["valid"] = bool(r["no"] > 0 and counts[r["no"]] == 1
                          and r["number_conf"] >= min_digit_conf
                          and r["choice_conf"] >= min_choice_conf)
    return rows


def sequence_score(rows):
    """整区题号是否 1..N 连续；返回 (是否连续, 题号列表)。"""
    nos = [r["no"] for r in rows if r["no"] > 0]
    if not nos:
        return False, []
    return nos == list(range(nos[0], nos[0] + len(nos))), nos


# ---------- 题号连续性纠正 ----------
# 连续编号只是一种假设；跳号、缺框和稀疏语文题区均不能强排为1..N。


def _anchor(row):
    return row.get("num_box", row["opt_box"])


def _groups(items, axis):
    """以题号框尺寸和组中心分组，避免相邻项逐步串联成同一行/列。"""
    groups = []
    for row in sorted(items, key=lambda r: _anchor(r)[axis]):
        box = _anchor(row)
        if groups:
            group = groups[-1]
            center = median(_anchor(r)[axis] for r in group)
            tolerance = 0.6 * min(box[axis + 2], median(_anchor(r)[axis + 2] for r in group))
        if not groups or abs(box[axis] - center) > tolerance:
            groups.append([row])
        else:
            groups[-1].append(row)
    return groups

def _order_col(items):
    return [r for c in _groups(items, 0) for r in sorted(c, key=lambda r: _anchor(r)[1])]


def _order_row(items):
    return [r for g in _groups(items, 1) for r in sorted(g, key=lambda r: _anchor(r)[0])]


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
    v = [r["no"] for r in seq]
    cand = Counter(x - i for i, x in enumerate(v) if x > 0)
    if not cand:
        return 0.0, 0
    m, hits = cand.most_common(1)[0]
    return hits / len(v), m


def _supported_sequence(seq, min_conf):
    conf, start = _vote_seq(seq)
    if len(seq) < 3 or start < 1 or conf < min_conf:
        return None
    # 两个误读也可能碰巧连续，至少需要三个独立位置支持才能改写题号。
    if sum(r["no"] == start + i for i, r in enumerate(seq)) < 3:
        return None
    # 两个相邻读数若支持另一个连续起点，可能是真实跳号/漏框，不能覆盖。
    for i in range(len(seq) - 1):
        a, b = seq[i]["no"], seq[i + 1]["no"]
        if a > 0 and b == a + 1 and a - i != start:
            return None
    return conf, start


def _column_blocks(rows):
    """同列内选项宽度突变或有大空隙时拆块，不跨不同版式投票。"""
    blocks = []
    for column in _groups(rows, 0):
        column = sorted(column, key=lambda r: _anchor(r)[1])
        gaps = [_anchor(b)[1] - _anchor(a)[1] for a, b in zip(column, column[1:])]
        pitch = median(gaps) if gaps else 1.0
        cur = []
        for row in column:
            if cur:
                previous = cur[-1]
                widths = [row["opt_box"][2], previous["opt_box"][2]]
                changed = max(widths) > 1.5 * min(widths)
                gap = _anchor(row)[1] - _anchor(previous)[1]
                if changed or gap > 1.6 * pitch:
                    blocks.append(cur)
                    cur = []
            cur.append(row)
        if cur:
            blocks.append(cur)
    return blocks


def fix_no_sequence(rows, is_en=False, min_conf=0.6):
    """有足够连续读数支持才纠正，冲突时保留原读数。返回 (方法, 一致性, 起点)。

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
        supported = _supported_sequence(seq, min_conf)
        if supported is None:
            continue
        conf, m = supported
        score = conf + (0.001 if name == pref else 0)
        if best is None or score > best[0]:
            best = (score, name, seq, conf, m)
    if best is None:
        # 全区不连续时，仅纠正各自有证据的版式块；不猜其他块的起始题号。
        plans = []
        for seq in _column_blocks(rows):
            supported = _supported_sequence(seq, min_conf)
            if supported is not None:
                conf, start = supported
                plans.append((seq, conf, start))
        if not plans:
            return "keep", 0.0, 0
        name, conf, m = "blocks", min(p[1] for p in plans), 0
    else:
        _, name, seq, conf, m = best
        plans = [(seq, conf, m)]
    for seq, confidence, start in plans:
        for i, r in enumerate(seq):
            r.setdefault("no_raw", r["no"])
            if r["no"] != start + i:
                r["no"] = start + i
                r["no_source"] = "sequence"
                r["number_conf"] = round(confidence, 3)
    rows.sort(key=lambda r: (r["no"], -r["choice_conf"]))
    return name, conf, m

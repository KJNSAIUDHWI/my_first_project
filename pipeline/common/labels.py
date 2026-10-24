# -*- coding: utf-8 -*-
"""标签配对与读写。

标签命名：
    未审核 / 预标注  2026.7.001.物理.txt
    审核后          2026.7.001.物理.2026.10.21.09.35审核过（第1次）.txt

配对规则：同一张图的所有标签按前缀 <stem>. 开头，取“审核过（第N次）”里 N 最大的；
没有审核版就用 <stem>.txt。
"""
import os
import re
import glob

REVIEW_RE = re.compile(
    r"^(?P<base>.+)\.(?P<date>\d{4}\.\d{1,2}\.\d{1,2})\.(?P<time>\d{1,2}\.\d{2})审核过（第(?P<n>\d+)次）$"
)

TO_TOOL = {0: 0, 1: 1, 2: 4}      # 训练 id -> 工具 id
TO_TRAIN = {0: 0, 1: 1, 4: 2}     # 工具 id -> 训练 id


def parse_review(stem):
    """返回 (base, 次数) 或 None"""
    m = REVIEW_RE.match(stem)
    if not m:
        return None
    return m.group("base"), int(m.group("n"))


def list_labels(labels_dir, stem):
    """返回该图所有标签文件（含未审核版），按路径排序"""
    return sorted(glob.glob(os.path.join(labels_dir, stem + ".txt")) +
                  glob.glob(os.path.join(labels_dir, stem + ".*审核过（第*次）.txt")))


def latest_label(labels_dir, stem):
    """返回最新标签路径；没有返回 None"""
    plain = os.path.join(labels_dir, stem + ".txt")
    best = None
    best_n = -1
    for p in glob.glob(os.path.join(labels_dir, stem + ".*审核过（第*次）.txt")):
        r = parse_review(os.path.splitext(os.path.basename(p))[0])
        if r and r[1] > best_n:
            best, best_n = p, r[1]
    if best:
        return best
    if os.path.exists(plain):
        return plain
    return None


def read_boxes(path):
    """读 YOLO txt，返回 [(cls, cx, cy, w, h)]"""
    rows = []
    if not path or not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8", errors="ignore") as f:
        for ln in f:
            q = ln.split()
            if len(q) >= 5:
                try:
                    rows.append((int(q[0]), float(q[1]), float(q[2]), float(q[3]), float(q[4])))
                except ValueError:
                    pass
    return rows


def write_boxes(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for c, cx, cy, w, h in rows:
            f.write("%d %.6f %.6f %.6f %.6f\n" % (c, cx, cy, w, h))


def to_train(rows):
    """工具 id -> 训练 id；不在映射里的类别直接丢弃（m1 只认 0/1/4）。"""
    return [(TO_TRAIN[c], cx, cy, w, h) for c, cx, cy, w, h in rows if c in TO_TRAIN]


def to_tool(rows):
    return [(TO_TOOL.get(c, c), cx, cy, w, h) for c, cx, cy, w, h in rows]

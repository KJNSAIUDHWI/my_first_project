# -*- coding: utf-8 -*-
"""【历史入口，默认停止】从 777 旧 m2 训练集生成 digit 训练集（2026-10-22 用过一次）。

旧 crop 里有很多是倒的/横的，做法是对每张图试 0/90/180/270 四个方向，
各自重排框、读数字、算投票一致性，选一致性最高的方向；仍低于 0.7 的图丢弃。

历史产物在 data/digit_legacy。本脚本不再执行：输入在桌面 777（项目边界外，
不读不改），输出会覆盖既有 labels.csv 与图片。现行训练快照入口：
tools/迭代.py snapshot --task digit。本脚本不提供任何可读 777 的 force。
"""
import csv
import os
import sys
from collections import Counter

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from build_digit_dataset import clip_box, load_model, order_boxes, read_digit  # noqa: E402

LEGACY = "C:\\Users\\25495\\Desktop\\777\\train_data\\model2\\r8_final"
MIN_RATIO = 0.7
VAL_EVERY = 7
NEG_PER_IMG = 2
PAD = 2

ROT = [None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE]


def read_yolo(p):
    boxes = []
    for ln in open(p, encoding="utf-8", errors="replace"):
        s = ln.split()
        if len(s) >= 5:
            boxes.append((int(s[0]), float(s[1]), float(s[2]), float(s[3]), float(s[4])))
    return boxes


def rot_box(b, k):
    cls, cx, cy, bw, bh = b
    if k == 0:
        return (cls, cx, cy, bw, bh)
    if k == 1:      # 顺时针 90
        return (cls, 1 - cy, cx, bh, bw)
    if k == 2:      # 180
        return (cls, 1 - cx, 1 - cy, bw, bh)
    return (cls, cy, 1 - cx, bh, bw)   # 逆时针 90


def vote(model, gray, boxes, k, W, H):
    """返回 (ratio, m, ordered, rotated_gray, 宽, 高)"""
    g = gray if k == 0 else cv2.rotate(gray, ROT[k])
    nw, nh = (W, H) if k in (0, 2) else (H, W)
    rb = [rot_box(b, k) for b in boxes]
    ordered = order_boxes(rb)
    vals = []
    for cls, cx, cy, bw, bh in ordered:
        x1, y1, x2, y2 = clip_box(cx, cy, bw, bh, nw, nh, 0)
        vals.append(read_digit(model, g[y1:y2, x1:x2]))
    cand = Counter(v - i for i, v in enumerate(vals) if v > 0)
    if not cand:
        return 0.0, 0, ordered, g, nw, nh
    m, hits = cand.most_common(1)[0]
    return hits / len(vals), m, ordered, g, nw, nh


def main():
    raise SystemExit(
        "历史生成入口，默认停止（不读 777、不加载模型、不写任何产物）。\n"
        "输入在桌面 777（项目边界外，本任务及后续迭代均不读不改）；重跑还会\n"
        "覆盖 data/digit_legacy 的 labels.csv 与图片。\n"
        "现行训练快照入口：tools/迭代.py snapshot --task digit。\n"
        "本脚本不提供任何可读 777 的 force。")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""【历史入口，默认停止】从 m2 的已审题号框生成 digit 训练集（2026-10-22 用过一次）。

真值不靠模型读数，靠卷面规律推断：
  同一张选择题区里，题号按列从左到右、列内从上到下连续。
  框排好序后第 i 个框的真值 = m + i，m 由多数投票（读数 - 位置）得出。
  只有投票一致性 >= 0.7 且 m >= 1 的图才收；其余（框不全、题号不连续）跳过。

历史产物在 data/digit。本脚本不再执行：重跑会覆盖既有 labels.csv 与图片，
且读数用的是已判定污染的旧 digit_cnn_v2 权重；无明确范围、新输出与覆盖预检。
现行训练快照入口：tools/迭代.py snapshot --task digit。需要新的增量生成
入口时由用户明确决定后另行实现；本脚本不提供 force。

下方保留纯函数供 build_digit_legacy 等历史脚本引用，不包含任何写盘逻辑。
"""
import io
import os
import sys
from collections import Counter

import cv2
import numpy as np
import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.paths import MODELS  # noqa: E402

MIN_RATIO = 0.7
VAL_EVERY = 7          # 每 7 张图抽 1 张进 val
NEG_PER_IMG = 2        # 每张图最多抽几个选项框当负样本
PAD = 2                # 裁框外扩像素


def order_boxes(boxes):
    """列聚类：cx 间距 > 8% 图宽算新列；列内按 cy 从上到下。"""
    items = sorted(boxes, key=lambda b: b[1])
    cols, cur = [], []
    for b in items:
        if cur and b[1] - cur[-1][1] > 0.08:
            cols.append(cur)
            cur = []
        cur.append(b)
    if cur:
        cols.append(cur)
    out = []
    for col in cols:
        out.extend(sorted(col, key=lambda b: b[2]))
    return out


def load_model():
    """旧 v2 读数模型（已判定 8/9 类污染，仅历史脚本引用，不要用于新数据）。"""
    p = MODELS / "cnn" / "digit_cnn_20261007_01_v2题号8-9污染.pt"
    m = torch.jit.load(io.BytesIO(open(p, "rb").read()), map_location="cpu")
    m.eval()
    return m


def read_digit(model, gray):
    if gray.size == 0:
        return 0
    g = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_LANCZOS4)
    g = g.astype(np.float32) / 255.0
    if g.mean() > 0.5:
        g = 1.0 - g
    x = torch.from_numpy(g).unsqueeze(0).unsqueeze(0)
    with torch.no_grad():
        idx = int(torch.argmax(model(x), 1))
    return 0 if idx == 60 else idx + 1


def clip_box(cx, cy, bw, bh, W, H, pad=PAD):
    x1 = int(max(0, (cx - bw / 2) * W)) - pad
    x2 = int(min(W, (cx + bw / 2) * W)) + pad
    y1 = int(max(0, (cy - bh / 2) * H)) - pad
    y2 = int(min(H, (cy + bh / 2) * H)) + pad
    return max(0, x1), max(0, y1), min(W, x2), min(H, y2)


def main():
    raise SystemExit(
        "历史生成入口，默认停止（不读数据、不加载模型、不写任何产物）。\n"
        "重跑会覆盖 data/digit 的 labels.csv 与图片，且读数模型是已判定污染的\n"
        "digit_cnn_v2；没有明确范围、新输出目录和不覆盖预检。\n"
        "现行训练快照入口：tools/迭代.py snapshot --task digit。\n"
        "新的增量生成入口属新功能，由用户明确决定后另行实现；本脚本不提供 force。")


if __name__ == "__main__":
    main()

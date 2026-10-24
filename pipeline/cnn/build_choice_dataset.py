# -*- coding: utf-8 -*-
"""【历史入口，默认停止】从 m2 裁出选择题答题行，生成 data/choice（2026-10-22 用过一次）。

裁切规则（现行推理仍在 stages/choice.py 使用，纯函数保留在下方）：
  选项大框内有题号 -> 起点取框内最右侧题号的右边缘；
  框内没有题号（2026.10 多选 Q11/Q12）-> 起点取大框左边缘。
  行序：列优先/行优先两种排序都算，用题号读数连续性投票选。

本脚本不再执行：默认全库写入，会覆盖 data/choice 的 manifest.csv、图片和
pred 日志；没有明确范围、新输出目录、不覆盖预检和父身份继承检查。
现行训练快照入口：tools/迭代.py snapshot --task choice（读取已审答题行）。
补答题行素材属数据准备新功能，由用户明确决定后另行实现。
"""
import os
import sys
from collections import Counter

PIPE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PIPE)

THR = 0.06


def cluster(items, key, thr=THR):
    items = sorted(items, key=key)
    groups, cur = [], [items[0]]
    for it in items[1:]:
        if key(it) - key(cur[-1]) > thr:
            groups.append(cur)
            cur = [it]
        else:
            cur.append(it)
    groups.append(cur)
    return groups


def order_row(items):
    out = []
    for g in cluster(items, lambda it: it["cy"]):
        out.extend(sorted(g, key=lambda it: it["cx"]))
    return out


def order_col(items):
    out = []
    for g in cluster(items, lambda it: it["cx"]):
        out.extend(sorted(g, key=lambda it: it["cy"]))
    return out


def consistency(seq):
    vals = [it["no"] for it in seq]
    cand = Counter(v - i for i, v in enumerate(vals) if v > 0)
    if not cand:
        return 0.0
    return cand.most_common(1)[0][1] / len(vals)


def n_options(stem, row_w):
    if "\u82f1\u8bed" in stem:
        return 6 if row_w > 220 else 3
    return 4


def main():
    raise SystemExit(
        "历史生成入口，默认停止（不读数据、不加载模型、不写任何产物）。\n"
        "重跑会全量覆盖 data/choice 的 manifest.csv、答题行图片和 pred 日志；\n"
        "没有明确范围、新输出目录、不覆盖预检与父身份继承检查。\n"
        "现行训练快照入口：tools/迭代.py snapshot --task choice。\n"
        "补答题行素材属数据准备新功能，由用户明确决定后另行实现。")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""题号框 ↔ 选项框配对（匈牙利算法）。

输入用中心点 + 宽高（像素），来自 m2 的检测框。
代价 = 横向距离 + 纵向距离 × y_weight（同一行里题号和选项横向拉开，纵向几乎对齐）。
"""
import numpy as np
from scipy.optimize import linear_sum_assignment


def match_nums_to_options(nums, opts, y_weight=10.0):
    n, m = len(nums), len(opts)
    if not n or not m:
        return []
    size = max(n, m)
    cost = np.full((size, size), 1e9, dtype=np.float64)
    for i, (nx, ny, _nw, _nh) in enumerate(nums):
        for j, (ox, oy, _ow, _oh) in enumerate(opts):
            cost[i, j] = abs(nx - ox) + abs(ny - oy) * y_weight
    rows, cols = linear_sum_assignment(cost)
    pairs = []
    for i, j in zip(rows, cols):
        if i < n and j < m and cost[i, j] < 1e8:
            pairs.append((int(i), int(j), float(cost[i, j])))
    return pairs

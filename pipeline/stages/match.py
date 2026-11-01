# -*- coding: utf-8 -*-
"""题号框 ↔ 选项框配对（匈牙利算法）。

输入用中心点 + 宽高（像素），来自 m2 的检测框。
先筛选同排、题号位于选项左侧的候选，再做一对一匹配；允许缺框不配对。
"""
import numpy as np
from scipy.optimize import linear_sum_assignment


def match_nums_to_options(nums, opts, y_weight=10.0):
    n, m = len(nums), len(opts)
    if not n or not m:
        return []
    # 每个题号均有自己的“不匹配”位置，不用无关框补齐数量。
    cost = np.full((n, m + n), 1e6, dtype=np.float64)
    cost[:, m:] = 2.0
    for i, (nx, ny, nw, nh) in enumerate(nums):
        for j, (ox, oy, ow, oh) in enumerate(opts):
            if not np.isfinite((nx, ny, nw, nh, ox, oy, ow, oh)).all():
                continue
            if min(nw, nh, ow, oh) <= 0:
                continue
            overlap = min(ny + nh / 2, oy + oh / 2) - max(ny - nh / 2, oy - oh / 2)
            if overlap < 0.35 * min(nh, oh) or abs(ny - oy) > 0.6 * max(nh, oh):
                continue
            left = ox - ow / 2
            gap = left - (nx + nw / 2)
            # 为题号后的“（多选）”留出间距；仍限制同排及近邻，禁止跨列远配。
            if nx > left + 0.35 * ow or gap > max(4 * nw, 0.25 * ow):
                continue
            inside = left <= nx <= ox + ow / 2
            cost[i, j] = (abs(ny - oy) / max(nh, oh)
                          + 0.1 * abs(nx - left) / max(nw, ow)
                          + (0.0 if inside else 0.5))
    rows, cols = linear_sum_assignment(cost)
    pairs = []
    for i, j in zip(rows, cols):
        if j < m and cost[i, j] < 2.0:
            nx, ny, *_ = nums[i]
            ox, oy, *_ = opts[j]
            # 保持调用方 pair_cost 的像素单位。
            pairs.append((int(i), int(j), float(abs(nx - ox) + abs(ny - oy) * y_weight)))
    return pairs

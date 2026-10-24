# -*- coding: utf-8 -*-
"""题号读取（digit CNN，当前部署 v4）。

权重：models/cnn/digit_cnn_v4_20261022_补多选区.pt（35 类：0=非数字，1~34=题号）
v3 保留在 models/cnn/digit_cnn_v3.pt；v4 补了多选区 11/12 的样本。
预处理：等比缩放到长边 56 + 居中补白到 64×64 + /255 + 反色
"""
import io
import os
import sys

import cv2
import numpy as np
import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.paths import model_weight  # noqa: E402

SIZE = 64
INNER = 56


def _to_gray(img):
    if img is None or img.size == 0:
        return None
    if len(img.shape) == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def prep(img):
    g = _to_gray(img)
    if g is None:
        return None
    h, w = g.shape
    s = float(INNER) / max(w, h)
    nw = max(1, int(round(w * s)))
    nh = max(1, int(round(h * s)))
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LANCZOS4
    r = cv2.resize(g, (nw, nh), interpolation=interp)
    canvas = np.full((SIZE, SIZE), 255, np.uint8)
    x = (SIZE - nw) // 2
    y = (SIZE - nh) // 2
    canvas[y:y + nh, x:x + nw] = r
    a = canvas.astype(np.float32) / 255.0
    if a.mean() > 0.5:
        a = 1.0 - a
    return torch.from_numpy(a).unsqueeze(0).unsqueeze(0)


class DigitReader:
    def __init__(self, weight=None, device=None):
        self.device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
        p = str(weight or model_weight("cnn"))
        with open(p, "rb") as f:
            self.model = torch.jit.load(io.BytesIO(f.read()), map_location=self.device)
        self.model.eval()

    def read(self, img):
        """返回 (digit, conf)：digit 0=非数字，1~34=题号。"""
        x = prep(img)
        if x is None:
            return 0, 0.0
        x = x.to(self.device)
        with torch.no_grad():
            prob = torch.softmax(self.model(x), dim=1)
            conf, idx = torch.max(prob, 1)
        return int(idx), float(conf)

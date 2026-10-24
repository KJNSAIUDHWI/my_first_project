# -*- coding: utf-8 -*-
"""选项涂黑读取。

v3（默认）：models/cnn/choice_cnn_v3.pt，多标签 6 输出（A~F），任意组合。
输入：整行选项图，等比缩放到 188×44 + 居中补白到 192×48，灰度 + 反色。
输出：组合字符串 + 置信度（所有选中字母概率的最小值）。

legacy=True 时走 7 月的 v1 旧 11 类模型（models/cnn/choice_cnn_v1_20260718_旧11类.pt，64×64 拉伸）。
"""
import io
import os
import sys

import cv2
import numpy as np
import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.model_registry import current
from common.paths import MODELS  # noqa: E402

LETTERS = "ABCDEF"
SIZE_W, SIZE_H = 192, 48
INNER_W, INNER_H = 188, 44
LEGACY_CLASSES = ["A", "AB", "AC", "ACD", "AD", "B", "BC", "BD", "C", "CD", "D"]


def prep_row(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    h, w = g.shape
    s = min(float(INNER_W) / w, float(INNER_H) / h)
    nw = max(1, int(round(w * s)))
    nh = max(1, int(round(h * s)))
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LANCZOS4
    r = cv2.resize(g, (nw, nh), interpolation=interp)
    canvas = np.full((SIZE_H, SIZE_W), 255, np.uint8)
    x = (SIZE_W - nw) // 2
    y = (SIZE_H - nh) // 2
    canvas[y:y + nh, x:x + nw] = r
    a = canvas.astype(np.float32) / 255.0
    if a.mean() > 0.5:
        a = 1.0 - a
    return torch.from_numpy(a).unsqueeze(0).unsqueeze(0)


class ChoiceReader:
    def __init__(self, weight=None, device=None, min_conf=0.5, legacy=False):
        self.device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
        self.min_conf = min_conf
        self.legacy = legacy
        if legacy:
            p = str(weight or (MODELS / "cnn" / "choice_cnn_v1_20260718_旧11类.pt"))
        else:
            p = str(weight or current("choice"))
        with open(p, "rb") as f:
            self.model = torch.jit.load(io.BytesIO(f.read()), map_location=self.device)
        self.model.eval()

    def read(self, img):
        if img is None or img.size == 0:
            return "", 0.0
        if self.legacy:
            g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
            g = cv2.resize(g, (64, 64), interpolation=cv2.INTER_AREA)
            a = g.astype(np.float32) / 255.0
            if a.mean() > 0.5:
                a = 1.0 - a
            x = torch.from_numpy(a).unsqueeze(0).unsqueeze(0).to(self.device)
            with torch.no_grad():
                prob = torch.softmax(self.model(x), dim=1)
                conf, idx = torch.max(prob, 1)
            if float(conf) < self.min_conf:
                return "", float(conf)
            return LEGACY_CLASSES[int(idx)], float(conf)

        x = prep_row(img).to(self.device)
        with torch.no_grad():
            p = torch.sigmoid(self.model(x))[0].cpu().numpy()
        picked = [(LETTERS[i], float(p[i])) for i in range(6) if p[i] >= self.min_conf]
        if not picked:
            return "", float(p.max())
        ans = "".join(ch for ch, _ in picked)
        return ans, min(c for _, c in picked)

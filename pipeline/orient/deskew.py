# -*- coding: utf-8 -*-
"""倾斜校正（deskew）——数据准备流水线的固定一步。

放在“方向转正”之后、“预标注”之前，对 data/<m>/images 里的整页图做：
  投影法（文字行投影方差）为主，卡边界时用 Hough 兜底；
  转到正、白色填充，日志里的框同步变换，并记一条 deskew 事件。

幂等：日志里已有 deskew 事件的图默认跳过；--force 才重新检测。

用法：
    python pipeline/orient/deskew.py              # m1
    python pipeline/orient/deskew.py --m m2
    python pipeline/orient/deskew.py --force
    python pipeline/orient/deskew.py --dry-run
"""
import os
import sys
import math
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import cv2
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.paths import m_images
from common import events

THRESHOLD = 0.1
PROJ_LIMIT = 1.5


def _gray(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)


def proj_angle(gray):
    H, W = gray.shape
    s = 800.0 / W
    small = cv2.resize(gray, (800, max(1, int(H * s))))
    binary = (small < 180).astype(np.uint8)
    h, w = binary.shape
    best_a, best = 0.0, -1.0
    for a100 in range(-150, 151, 5):
        a = a100 / 100.0
        Mx = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1)
        rot = cv2.warpAffine(binary, Mx, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        score = float(np.var(rot.sum(axis=1).astype(np.float32)))
        if score > best:
            best, best_a = score, a
    return round(best_a, 2)


def hough_angle(gray):
    H, W = gray.shape
    s = 1000.0 / max(W, H)
    small = cv2.resize(gray, (int(W * s), int(H * s)))
    edges = cv2.Canny(small, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, 80,
                            minLineLength=small.shape[1] * 0.25, maxLineGap=10)
    angs = []
    if lines is not None:
        for l in lines[:, 0]:
            x1, y1, x2, y2 = l
            dx, dy = x2 - x1, y2 - y1
            if dx == 0:
                continue
            a = math.degrees(math.atan2(dy, dx))
            if abs(a) < 10:
                angs.append((a, math.hypot(dx, dy)))
    if not angs:
        return 0.0
    angs.sort()
    total = sum(w for _, w in angs)
    acc = 0
    med = angs[-1][0]
    for a, w in angs:
        acc += w
        if acc >= total / 2:
            med = a
            break
    return round(med, 2)


def detect(gray):
    """返回 (角度, 方法)。角度为正表示需要逆时针转。"""
    pa = proj_angle(gray)
    if abs(pa) >= PROJ_LIMIT - 0.05:
        ha = hough_angle(gray)
        if abs(ha) > 0.3:
            return ha, "hough"
        return 0.0, "none"
    return pa, "proj"


def detect_one(path):
    gray = _gray(path)
    if gray is None:
        return None
    a, method = detect(gray)
    return round(a, 2), method


def transform(boxes, a, W, H, W2, H2):
    th = math.radians(a)
    cos, sin = math.cos(th), math.sin(th)
    out = []
    for c, cx, cy, w, h in boxes:
        x1, y1 = (cx - w / 2) * W, (cy - h / 2) * H
        x2, y2 = (cx + w / 2) * W, (cy + h / 2) * H
        pts = []
        for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
            dx, dy = x - W / 2, y - H / 2
            pts.append((dx * cos + dy * sin + W2 / 2, -dx * sin + dy * cos + H2 / 2))
        nx1 = min(p[0] for p in pts)
        nx2 = max(p[0] for p in pts)
        ny1 = min(p[1] for p in pts)
        ny2 = max(p[1] for p in pts)
        out.append([int(c), (nx1 + nx2) / 2 / W2, (ny1 + ny2) / 2 / H2,
                    (nx2 - nx1) / W2, (ny2 - ny1) / H2])
    return out


def deskew_image(m, stem, path, a, method, dry=False):
    """旋转一张图、变换日志框、补 deskew 事件。返回是否真的转了。"""
    if dry:
        return abs(a) >= THRESHOLD
    rotated = abs(a) >= THRESHOLD
    if rotated:
        im = Image.open(path).convert("RGB")
        W, H = im.size
        out = im.rotate(a, expand=True, resample=Image.BICUBIC, fillcolor=(255, 255, 255))
        W2, H2 = out.size
        out.save(path, quality=95, subsampling=0)
        st = events.load_state(m, stem)
        if st["boxes"]:
            events.append_event(m, stem, {"e": "save", "boxes": transform(st["boxes"], a, W, H, W2, H2),
                                          "n": len(st["boxes"])})
    events.append_event(m, stem, {"e": "deskew", "deg": a if rotated else 0,
                                  "detected": a, "method": method})
    return rotated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--m", default="m1")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    m = args.m
    img_dir = str(m_images(m))
    if not os.path.isdir(img_dir):
        print("找不到图片目录:", img_dir)
        return 1
    files = sorted(f for f in os.listdir(img_dir) if f.lower().endswith(".jpg"))
    todo = []
    for f in files:
        stem = os.path.splitext(f)[0]
        if not args.force:
            evs = events.read_events(m, stem)
            if any(e.get("e") == "deskew" or e.get("事件") == "倾斜校正" for e in evs):
                continue
        todo.append(f)
    print("图片:", len(files), " 待检测:", len(todo))

    results = {}
    with ProcessPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(detect_one, os.path.join(img_dir, f)): f for f in todo}
        for i, fu in enumerate(as_completed(futs)):
            r = fu.result()
            if r is not None:
                results[futs[fu]] = r
            if (i + 1) % 200 == 0:
                print("  检测", i + 1, "/", len(todo), flush=True)

    rotated = 0
    for f in todo:
        if f not in results:
            continue
        a, method = results[f]
        stem = os.path.splitext(f)[0]
        if deskew_image(m, stem, os.path.join(img_dir, f), a, method, args.dry_run):
            rotated += 1
    print("完成：检测", len(results), " 实际旋转", rotated, ("（dry-run，未写盘）" if args.dry_run else ""))
    if not args.dry_run:
        events.rebuild_progress(m)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""流水线入口（框架）：整页图 -> 方向转正 -> m1 三大区域。

当前实现方向 + m1，输出每张图的框 JSON 和转正图。
m2 / m3 / m4 / digit / 判分 后续接进 stages/。

用法：
    python pipeline/run.py --input <图片目录> --out <输出目录>
"""
import os
import sys
import json
import argparse
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common.paths import model_weight

import cv2
cv2.setNumThreads(0)
from ultralytics import YOLO

TO_TOOL = {0: 0, 1: 1, 2: 4}


def rotate_by_angle(im, deg):
    if deg == 0:
        return im
    return im.rotate(-deg, expand=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--conf", type=float, default=0.2)
    args = ap.parse_args()

    out_img = os.path.join(args.out, "images")
    os.makedirs(out_img, exist_ok=True)

    orient = YOLO(str(model_weight("orient")))
    m1 = YOLO(str(model_weight("m1")))

    files = sorted(f for f in os.listdir(args.input) if f.lower().endswith(".jpg"))
    print("图片:", len(files))
    result = {}
    for i, f in enumerate(files):
        p = os.path.join(args.input, f)
        r = orient(p, imgsz=1024, verbose=False)[0]
        deg = int(r.names[int(r.probs.top1)])
        from PIL import Image
        from orient.deskew import detect
        import numpy as np
        im = rotate_by_angle(Image.open(p).convert("RGB"), deg)
        # 大角度转正之后做小角度微调
        a, method = detect(np.array(im.convert("L")))
        if abs(a) >= 0.1:
            im = im.rotate(a, expand=True, resample=Image.BICUBIC, fillcolor=(255, 255, 255))
        fixed = os.path.join(out_img, f)
        im.save(fixed, quality=95, subsampling=0)

        rr = m1(fixed, imgsz=1024, conf=args.conf, iou=0.5, verbose=False)[0]
        H, W = rr.orig_shape
        boxes = []
        for b in rr.boxes:
            c = int(b.cls[0])
            if c not in TO_TOOL:
                continue
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            boxes.append({"cls": TO_TOOL[c],
                          "cx": round((x1 + x2) / 2 / W, 6),
                          "cy": round((y1 + y2) / 2 / H, 6),
                          "w": round((x2 - x1) / W, 6),
                          "h": round((y2 - y1) / H, 6)})
        result[f] = {"rotate": deg, "boxes": boxes}
        if (i + 1) % 50 == 0:
            print(i + 1, "/", len(files), flush=True)

    with open(os.path.join(args.out, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    cnt = Counter()
    for v in result.values():
        for b in v["boxes"]:
            cnt[b["cls"]] += 1
    print("完成。框数:", dict(cnt), " 输出:", args.out)


if __name__ == "__main__":
    main()

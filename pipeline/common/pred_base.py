# -*- coding: utf-8 -*-
"""通用预标注：对某一级 images 图打框，写 labels/<stem>.txt。"""
import os
import csv
import hashlib
from pathlib import Path

from .paths import m_images, model_weight
from . import events

import cv2
cv2.setNumThreads(0)
from ultralytics import YOLO


def pred(m, cls_map, only_missing=True, conf=0.25, iou=0.5, imgsz=1024, subset=None, weights=None):
    """cls_map: 模型类别 -> 工具类别"""
    imgs_dir = m_images(m)
    weight = Path(weights) if weights else model_weight(m)
    weight_hash = hashlib.sha256(weight.read_bytes()).hexdigest()
    model = YOLO(str(weight))
    files = sorted(f for f in os.listdir(imgs_dir) if f.lower().endswith(".jpg"))
    selected = None
    if subset:
        with open(subset, encoding="utf-8-sig", newline="") as f:
            selected = {row["new_name"] for row in csv.DictReader(f)}
        if not selected:
            raise ValueError("预标注清单为空: " + str(subset))
        available = {os.path.splitext(f)[0] for f in files}
        missing = selected - available
        if missing:
            raise ValueError("预标注清单图片缺失: " + ", ".join(sorted(missing)[:10]))
        already_labeled = [s for s in selected if events.has_events(m, s)]
        if already_labeled:
            raise ValueError("限定批次中存在已有事件，拒绝重打: " + ", ".join(sorted(already_labeled)[:10]))
    todo = []
    for f in files:
        stem = os.path.splitext(f)[0]
        if selected is not None and stem not in selected:
            continue
        if only_missing and events.has_events(m, stem):
            continue
        todo.append(f)
    print("模型:", weight, " 待预标注:", len(todo), "/", len(files))
    for i, f in enumerate(todo):
        stem = os.path.splitext(f)[0]
        # M3 was trained with square padding; minimal rectangular padding caused block misses.
        padding = {"rect": False} if m == "m3" else {}
        r = model(os.path.join(imgs_dir, f), imgsz=imgsz, conf=conf, iou=iou, verbose=False, **padding)[0]
        H, W = r.orig_shape
        rows = []
        for b in r.boxes:
            c = int(b.cls[0])
            if c not in cls_map:
                continue
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            if (x2 - x1) < 6 or (y2 - y1) < 6:
                continue
            rows.append((cls_map[c], (x1 + x2) / 2 / W, (y1 + y2) / 2 / H,
                         (x2 - x1) / W, (y2 - y1) / H))
        events.append_event(m, stem, {"e": "pred", "model": weight.name,
                                      "model_sha256": weight_hash, "model_path": str(weight),
                                      "conf": conf, "iou": iou, "imgsz": imgsz,
                                      **padding, "boxes": rows, "n": len(rows)})
        if (i + 1) % 100 == 0:
            print(i + 1, "/", len(todo), flush=True)
    if todo:
        events.rebuild_progress(m)
    print("完成:", len(todo))

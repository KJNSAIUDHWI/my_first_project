# -*- coding: utf-8 -*-
"""用当前 m1 权重给整页图预标注。

输出 data/m1/labels/<图名>.txt，类别 0/1/4（工具口径）。
默认只给“还没有任何标签”的图打；--all 则全部重打（不会覆盖审核版）。

用法：
    python pipeline/m1/pred.py                # 只补没标签的
    python pipeline/m1/pred.py --all
    python pipeline/m1/pred.py --conf 0.3
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.paths import m_images, model_weight
from common import events

import cv2
cv2.setNumThreads(0)
from ultralytics import YOLO

CLASS_MAP = {0: 0, 1: 1, 2: 4}   # 模型类别 -> 工具类别


def main():
    imgs_dir = m_images("m1")

    weight = str(model_weight("m1"))
    conf = 0.2
    iou = 0.5
    do_all = "--all" in sys.argv
    if "--conf" in sys.argv:
        conf = float(sys.argv[sys.argv.index("--conf") + 1])

    model = YOLO(weight)
    imgs = sorted(f for f in os.listdir(imgs_dir) if f.lower().endswith(".jpg"))
    todo = []
    skipped_reviewed = 0
    for f in imgs:
        stem = os.path.splitext(f)[0]
        if events.load_state("m1", stem)["reviewed"]:
            skipped_reviewed += 1
            continue
        if do_all or not events.has_events("m1", stem):
            todo.append(f)
    print("权重:", weight)
    print("待预标注:", len(todo), "/", len(imgs))
    if skipped_reviewed:
        print("跳过已审图:", skipped_reviewed)

    for i, f in enumerate(todo):
        stem = os.path.splitext(f)[0]
        r = model(os.path.join(imgs_dir, f), imgsz=1024, conf=conf, iou=iou, verbose=False)[0]
        H, W = r.orig_shape
        rows = []
        for b in r.boxes:
            c = int(b.cls[0])
            if c not in CLASS_MAP:
                continue
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            if (x2 - x1) < 16 or (y2 - y1) < 16:
                continue
            rows.append((CLASS_MAP[c], (x1 + x2) / 2 / W, (y1 + y2) / 2 / H,
                         (x2 - x1) / W, (y2 - y1) / H))
        events.append_event("m1", stem, {"e": "pred", "model": os.path.basename(weight),
                                         "model_path": weight,
                                         "boxes": rows, "n": len(rows)})
        if (i + 1) % 100 == 0:
            print(i + 1, "/", len(todo), flush=True)
    if todo:
        events.rebuild_progress("m1")
    print("完成")


if __name__ == "__main__":
    main()

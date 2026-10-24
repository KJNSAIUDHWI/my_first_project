# -*- coding: utf-8 -*-
"""空白页误检评估：只跑预测，不看 mAP。

统计目标：
- 有误检的图占比（false detection rate）
- 平均每张误检框数
- 按类别的误检框数
- 置信度分布

用法：
    python pipeline/m1/eval_empty.py --weights <best.pt>
    python pipeline/m1/eval_empty.py --weights <best.pt> --conf 0.25 --out train/xxx/empty_metrics.json
"""
import os
import sys
import json
import glob
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.paths import m_dataset

import cv2
cv2.setNumThreads(0)
from ultralytics import YOLO


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", required=True)
    ap.add_argument("--images", default=str(m_dataset("m1") / "empty_test" / "images"))
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--imgsz", type=int, default=1024)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.images, "*.jpg")))
    if not files:
        print("没有图片:", args.images)
        return 1
    model = YOLO(args.weights)
    per_class = {}
    confs = []
    with_det = 0
    total_boxes = 0
    worst = []
    for r in model.predict(source=files, imgsz=args.imgsz, conf=args.conf, batch=args.batch,
                           stream=True, verbose=False):
        n = len(r.boxes)
        total_boxes += n
        if n:
            with_det += 1
        name = os.path.basename(r.path)
        if n:
            worst.append((n, name))
        for b in r.boxes:
            c = int(b.cls[0])
            per_class[c] = per_class.get(c, 0) + 1
            confs.append(float(b.conf[0]))

    res = {
        "weights": args.weights,
        "images": len(files),
        "conf_threshold": args.conf,
        "images_with_false_det": with_det,
        "false_det_rate": round(with_det / len(files), 4),
        "false_boxes": total_boxes,
        "boxes_per_image": round(total_boxes / len(files), 4),
        "per_class": {str(k): v for k, v in sorted(per_class.items())},
        "conf_avg": round(sum(confs) / len(confs), 4) if confs else None,
        "conf_max": round(max(confs), 4) if confs else None,
        "worst_images": sorted(worst, reverse=True)[:10],
    }
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if args.out:
        json.dump(res, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

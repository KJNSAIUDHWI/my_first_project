# -*- coding: utf-8 -*-
import os
import sys
import argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.pred_base import pred

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--subset", required=True, help="本批CSV的new_name列；已有事件时拒绝重打")
    ap.add_argument("--weights", required=True, help="已核对的M3权重路径，不修改部署")
    ap.add_argument("--imgsz", type=int, default=1024, help="推理输入；历史n候选对照显式用640")
    args = ap.parse_args()
    pred("m3", {0: 5, 1: 6}, conf=0.25, iou=0.5, imgsz=args.imgsz,
         subset=args.subset, weights=args.weights)

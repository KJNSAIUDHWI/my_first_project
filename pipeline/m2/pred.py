# -*- coding: utf-8 -*-
import os
import sys
import argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.pred_base import pred

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--subset", help="仅预标注指定CSV的new_name列；拒绝重打已有事件")
    ap.add_argument("--weights", help="显式指定本批预标注权重，不替换部署别名")
    args = ap.parse_args()
    # conf 0.5：2026-10-22 全量扫描定档，val 上 0 漏检 0 假框（0.45~0.55 都是满分区间）
    pred("m2", {0: 2, 1: 3}, conf=0.5, iou=0.5, imgsz=1024, subset=args.subset, weights=args.weights)

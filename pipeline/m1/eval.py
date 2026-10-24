# -*- coding: utf-8 -*-
"""评估检测权重在指定数据集上的指标（独立入口，不训练）。

  python pipeline/m1/eval.py --weights A.pt --data <快照>/data.yaml \
      --results <独立结果目录> [--json 汇总JSON路径]

--results 是本次评估的产物目录（ultralytics val 的中间产物），必须是新目录：
已存在直接拒绝，不与其它评估混用（不使用 exist_ok=True）。
--json 是汇总指标 JSON；不传时写到 <results>/metrics.json。
两份权重的对比评估：同一固定快照、各自独立 --results 目录，分别运行两次。

示例（新旧对比，各用独立目录）：
  python pipeline/m1/eval.py --weights models/m1/m1.pt --data train/<run>/dataset_snapshot/data.yaml --results train/<run>/eval_old
  python pipeline/m1/eval.py --weights models/m1/<候选>.pt --data train/<run>/dataset_snapshot/data.yaml --results train/<run>/eval_new
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.paths import TRAIN  # noqa: E402
from common.snapshot_dataset import SnapshotValidator


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weights", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--results", required=True,
                    help="本次评估产物目录；必须是尚不存在的新目录")
    ap.add_argument("--json", default="",
                    help="汇总指标 JSON 输出路径；默认 <results>/metrics.json")
    ap.add_argument("--imgsz", type=int, default=1024)
    args = ap.parse_args()

    results_dir = Path(args.results)
    if results_dir.exists():
        raise SystemExit("评估产物目录已存在，拒绝覆盖（换一个新目录）：{}".format(results_dir))
    metrics_json = Path(args.json) if args.json else results_dir / "metrics.json"
    if metrics_json.exists():
        raise SystemExit('汇总JSON已存在，拒绝覆盖：' + str(metrics_json))

    from ultralytics import YOLO
    model = YOLO(args.weights)
    r = model.val(validator=SnapshotValidator, data=args.data, imgsz=args.imgsz, batch=8, device=0, workers=0,
                  verbose=False, project=str(results_dir.parent),
                  name=results_dir.name, exist_ok=False)
    res = {
        "weights": args.weights,
        "data": args.data,
        "results_dir": str(results_dir),
        "precision": float(r.box.mp),
        "recall": float(r.box.mr),
        "mAP50": float(r.box.map50),
        "mAP50_95": float(r.box.map),
        "per_class": {r.names[i]: {"P": float(r.box.p[i]), "R": float(r.box.r[i]),
                                   "mAP50": float(r.box.ap50[i]), "mAP50_95": float(r.box.ap[i])}
                      for i in range(len(r.names))},
    }
    print(json.dumps(res, ensure_ascii=False, indent=1))
    metrics_json.parent.mkdir(parents=True, exist_ok=True)
    with metrics_json.open('x', encoding='utf-8') as f:
        f.write(json.dumps(res, ensure_ascii=False, indent=1) + '\n')
    print("产物目录: %s" % results_dir)
    print("汇总JSON: %s" % metrics_json)


if __name__ == "__main__":
    main()

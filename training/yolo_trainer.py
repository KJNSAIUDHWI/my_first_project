# -*- coding: utf-8 -*-
"""YOLO 三级检测统一训练入口（m1 版面分区 / m2 选择题框 / m3 填空实验大题框）。

用法：
  python training/yolo_trainer.py --task m1 --data <snapshot/data.yaml> --name <run_name>
  python training/yolo_trainer.py --task m2 --data <snapshot/data.yaml> --name <run_name> --fliplr 0
  python training/yolo_trainer.py --task m3 --data <snapshot/data.yaml> --name <run_name> --fliplr 0
"""
import argparse
import sys
from pathlib import Path

ROOT_DIR = str(Path(__file__).resolve().parents[1])
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import cv2  # noqa: E402
cv2.setNumThreads(0)
import torch  # noqa: E402
from ultralytics import YOLO  # noqa: E402

from engine.paths import TRAIN, pretrained  # noqa: E402
from training.grad_guard import GradGuardTrainer  # noqa: E402
from training.records import write_json  # noqa: E402
from training.yolo_snapshot import validate_training_input  # noqa: E402

TASK_DEFAULTS = {
    "m1": {"model": "yolo11n.pt", "batch": 8, "fliplr": 0.5, "epochs": 50},
    "m2": {"model": "yolo11s.pt", "batch": 4, "fliplr": 0.0, "epochs": 50},
    "m3": {"model": "yolo11s.pt", "batch": 4, "fliplr": 0.0, "epochs": 50},
}


def main():
    ap = argparse.ArgumentParser(description="YOLO 三级检测模型防 NaN 微调训练入口")
    ap.add_argument("--task", choices=("m1", "m2", "m3"), required=True, help="检测阶段：m1/m2/m3")
    ap.add_argument("--data", required=True, help="快照 data.yaml 路径")
    ap.add_argument("--name", required=True, help="train/ 下的本次训练目录名")
    ap.add_argument("--model", default=None, help="微调起点权重路径")
    ap.add_argument("--epochs", type=int, default=None)
    ap.add_argument("--imgsz", type=int, default=1024)
    ap.add_argument("--batch", type=int, default=None)
    ap.add_argument("--device", default="0")
    ap.add_argument("--workers", type=int, default=4, help="数据加载线程数；CPU 可设为 0")
    ap.add_argument("--patience", type=int, default=15)
    ap.add_argument("--optimizer", default="AdamW")
    ap.add_argument("--lr0", type=float, default=0.0001)
    ap.add_argument("--lrf", type=float, default=0.01)
    ap.add_argument("--momentum", type=float, default=0.937)
    ap.add_argument("--weight-decay", type=float, default=0.0005)
    ap.add_argument("--warmup", type=float, default=3.0)
    ap.add_argument("--warmup-bias-lr", type=float, default=0.0001)
    ap.add_argument("--erasing", type=float, default=0.0)
    ap.add_argument("--deterministic", action="store_true")
    ap.add_argument("--scale", type=float, default=0.0)
    ap.add_argument("--translate", type=float, default=0.0)
    ap.add_argument("--fliplr", type=float, default=None, help="水平翻转概率；m2/m3 默认 0")
    ap.add_argument("--amp", action="store_true", help="启用混合精度（数值保护始终开启）")
    ap.add_argument("--no-cudnn", action="store_true")
    ap.add_argument("--no-grad-guard", action="store_true", help="已停用：数值保护不可关闭")
    args = ap.parse_args()

    if args.no_grad_guard:
        ap.error("数值保护不可关闭；请移除 --no-grad-guard")
    if args.no_cudnn:
        torch.backends.cudnn.enabled = False
    if args.deterministic:
        torch.backends.cudnn.benchmark = False
        if not args.amp:
            torch.backends.cudnn.allow_tf32 = False
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.set_float32_matmul_precision("highest")

    defaults = TASK_DEFAULTS[args.task]
    model_path = args.model or str(pretrained(defaults["model"]))
    epochs = args.epochs or defaults["epochs"]
    batch = args.batch or defaults["batch"]
    fliplr = defaults["fliplr"] if args.fliplr is None else args.fliplr

    out = (TRAIN / args.name).resolve()
    validate_training_input(args.task, out, args.data)
    if (out / "results.csv").exists() or (out / "weights").exists():
        raise FileExistsError("运行已有训练产物，拒绝覆盖：" + str(out))
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "training_args.json", {**vars(args), "model": model_path, "epochs": epochs, "batch": batch, "fliplr": fliplr})

    model = YOLO(model_path)
    model.train(
        trainer=GradGuardTrainer,
        data=args.data,
        epochs=epochs,
        imgsz=args.imgsz,
        batch=batch,
        device=args.device,
        workers=args.workers,
        amp=args.amp,
        mosaic=0.0,
        seed=42,
        patience=args.patience,
        optimizer=args.optimizer,
        lr0=args.lr0,
        lrf=args.lrf,
        momentum=args.momentum,
        weight_decay=args.weight_decay,
        warmup_epochs=args.warmup,
        warmup_bias_lr=args.warmup_bias_lr,
        erasing=args.erasing,
        deterministic=args.deterministic,
        scale=args.scale,
        translate=args.translate,
        fliplr=fliplr,
        project=str(TRAIN),
        name=args.name,
        exist_ok=True,
    )
    print("训练产物:", out)
    print("best:", out / "weights" / "best.pt")


if __name__ == "__main__":
    main()

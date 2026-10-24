# -*- coding: utf-8 -*-
"""m1 训练入口。直接运行本脚本，训练产物写入 train/<名字>/。

  python pipeline/m1/train_base.py --data <yaml> --name m1_2026-10-21-10-30_1497张 \
      --epochs 150 --imgsz 1024 --batch 8
"""
import os
import sys
import shutil
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.paths import TRAIN, MODELS, pretrained
from common.yolo_snapshot import validate_training_input
from common.records import write_json

import cv2
cv2.setNumThreads(0)
import torch
from ultralytics import YOLO
from ultralytics.models.yolo.detect import DetectionTrainer
from ultralytics.utils import LOGGER


from common.numerical_trainer import GradGuardTrainer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--model", default=str(pretrained("yolo11n.pt")))
    ap.add_argument("--epochs", type=int, default=150)
    ap.add_argument("--imgsz", type=int, default=1024)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--device", default="0")
    ap.add_argument("--patience", type=int, default=15)
    ap.add_argument("--optimizer", default="SGD",
                    help="SGD/AdamW/auto。别用 auto：yolo11n 会被换成 AdamW lr≈0.0014，在 1024/空图多时第 9 轮就 NaN")
    ap.add_argument("--lr0", type=float, default=0.01)
    ap.add_argument("--lrf", type=float, default=0.01)
    ap.add_argument("--momentum", type=float, default=0.937)
    ap.add_argument("--weight-decay", type=float, default=0.0005)
    ap.add_argument("--warmup", type=float, default=3.0)
    ap.add_argument("--warmup-bias-lr", type=float, default=0.02,
                    help="warmup 期间 bias 组学习率；AdamW 下 0.1 太大，会把分类头推坏")
    ap.add_argument("--erasing", type=float, default=0.0, help="随机擦除增强，默认关；8.4 新增，NaN 排查时先关")
    ap.add_argument("--deterministic", action="store_true", help="确定性训练，默认关")
    ap.add_argument("--scale", type=float, default=0.5, help="随机缩放幅度")
    ap.add_argument("--translate", type=float, default=0.1, help="随机平移幅度；小框多时可设 0 排查 NaN")
    ap.add_argument("--amp", action="store_true", help="启用混合精度；数值保护仍会对异常立即报错停止")
    ap.add_argument("--no-grad-guard", action="store_true",
                    help="已停用：数值保护不可关闭")
    ap.add_argument("--promote", default="", help="训练完把 best.pt 复制成 models/<m>/<名字>.pt")
    args = ap.parse_args()
    if args.no_grad_guard:
        ap.error("数值保护不可关闭；移除 --no-grad-guard")

    out = (TRAIN / args.name).resolve()
    validate_training_input('m1', out, args.data)
    if (out / 'results.csv').exists() or (out / 'weights').exists():
        raise FileExistsError('运行已有训练产物，拒绝覆盖：' + str(out))
    if args.promote:
        ap.error('请通过tools/迭代.py启动，完整性检查后统一留档和部署')
    out.mkdir(parents=True, exist_ok=True)
    write_json(out/'training_args.json', vars(args))
    model = YOLO(args.model)
    model.train(
        trainer=GradGuardTrainer,
        data=args.data,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        workers=4,
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
        project=str(TRAIN),
        name=args.name,
        exist_ok=True,
    )
    best = out / "weights" / "best.pt"
    print("训练产物:", out)
    print("best:", best)



if __name__ == "__main__":
    main()

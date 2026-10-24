# -*- coding: utf-8 -*-
"""Digit CNN 训练。

数据：data/digit + data/digit_legacy + data/digit_extra（审核/推断后的 labels.csv）
类别：0=非数字，1~34=题号（35 类；19 类当前无样本，留空位）
预处理：等比缩放到长边 56 + 居中补白到 64×64 + /255 + 反色（白底黑字 -> 黑底白字）
负样本：宽条（选项框）训练时随机裁成方形，避免模型学「宽=非数字」
增强：±5° 旋转、0.9~1.12 缩放、±6% 平移、亮度对比度、模糊、闭运算、Otsu、灰底/噪点
输出：train/digit_<日期>_<序号>/ + models/cnn/<--save-name>（默认 digit_cnn_next.pt，不覆盖部署版）

用法：py3136 pipeline\\cnn\\train_digit.py [--epochs 40] [--batch 256] [--save-name digit_cnn_v5.pt]
"""
import argparse
import csv
import json
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.paths import DATA, MODELS, TRAIN  # noqa: E402

from common.cnn_data import load_frozen
from common.records import write_json
from common.cnn_training import initialize, guard, metrics
cv2.setNumThreads(0)
NC = 35
SIZE = 64
INNER = 56


class Net(nn.Module):
    def __init__(self, nc=NC):
        super().__init__()
        self.c1 = nn.Conv2d(1, 24, 3, padding=1)
        self.c2 = nn.Conv2d(24, 48, 3, padding=1)
        self.c3 = nn.Conv2d(48, 96, 3, padding=1)
        self.p = nn.MaxPool2d(2, 2)
        self.fc1 = nn.Linear(96 * 8 * 8, 128)
        self.drop = nn.Dropout(0.3)
        self.fc2 = nn.Linear(128, nc)

    def forward(self, x):
        x = F.relu(self.c1(x))
        x = F.relu(self.c2(self.p(x)))
        x = F.relu(self.c3(self.p(x)))
        x = self.p(x).flatten(1)
        x = F.relu(self.fc1(x))
        return self.fc2(self.drop(x))




def crop_neg(g):
    """宽条负样本随机裁成接近方形的小块。"""
    h, w = g.shape
    if w > h * 1.6 and h >= 12:
        side = h
        x0 = random.randint(0, w - side)
        g = g[:, x0:x0 + side]
    return g


def make_input(g, inner=INNER, size=SIZE):
    h, w = g.shape
    s = float(inner) / max(w, h)
    nw = max(1, int(round(w * s)))
    nh = max(1, int(round(h * s)))
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LANCZOS4
    r = cv2.resize(g, (nw, nh), interpolation=interp)
    canvas = np.full((size, size), 255, np.uint8)
    x = (size - nw) // 2
    y = (size - nh) // 2
    canvas[y:y + nh, x:x + nw] = r
    return canvas


def augment(g):
    if random.random() < 0.8:
        ang = random.uniform(-5, 5)
        sc = random.uniform(0.9, 1.12)
        tx = random.uniform(-0.06, 0.06) * SIZE
        ty = random.uniform(-0.06, 0.06) * SIZE
        m = cv2.getRotationMatrix2D((SIZE / 2, SIZE / 2), ang, sc)
        m[0, 2] += tx
        m[1, 2] += ty
        g = cv2.warpAffine(g, m, (SIZE, SIZE), flags=cv2.INTER_LINEAR, borderValue=255)
    if random.random() < 0.5:
        a = random.uniform(0.8, 1.2)
        b = random.uniform(-25, 25)
        g = np.clip(g.astype(np.float32) * a + b, 0, 255).astype(np.uint8)
    if random.random() < 0.25:
        g = cv2.GaussianBlur(g, (3, 3), 0)
    if random.random() < 0.2:
        g = cv2.morphologyEx(g, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    if random.random() < 0.15:
        _, g = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return g


def to_tensor(g):
    x = g.astype(np.float32) / 255.0
    if x.mean() > 0.5:
        x = 1.0 - x
    return torch.from_numpy(x).unsqueeze(0)


def seq_accuracy(items, preds):
    groups = {}
    for it, p in zip(items, preds):
        if it["y"] <= 0:
            continue
        m = re.search(r"_(\d+)\.png$", it["fname"])
        if not m:
            continue
        groups.setdefault(it["stem"], []).append((int(m.group(1)), it["y"], int(p)))
    ok = 0
    for rows in groups.values():
        rows.sort()
        if all(y == p for _, y, p in rows):
            ok += 1
    return ok, len(groups)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--patience", type=int, default=8)
    ap.add_argument('--name', required=True)
    ap.add_argument('--data', required=True, help='独立CNN dataset_snapshot目录')
    ap.add_argument('--initial', help='微调起点，支持本架构TorchScript或state_dict')
    ap.add_argument('--scratch', action='store_true')
    args = ap.parse_args()

    random.seed(42)
    np.random.seed(42)
    torch.manual_seed(42)

    items = load_frozen(args.data, "digit")
    tr = [it for it in items if it["split"] == "train"]
    va = [it for it in items if it["split"] == "val"]
    print("train %d / val %d" % (len(tr), len(va)), flush=True)

    run_name = args.name
    out_dir = os.path.join(TRAIN, run_name)
    os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(os.path.join(out_dir, 'results.csv')):
        raise FileExistsError('运行已有指标，禁止覆盖')
    write_json(Path(out_dir)/'training_args.json', vars(args))
    logf = open(os.path.join(out_dir, "trainer.log"), "w", encoding="utf-8", buffering=1)

    def log(s):
        print(s, flush=True)
        logf.write(s + "\n")

    log("run: %s  train %d / val %d  NC=%d" % (run_name, len(tr), len(va), NC))
    log("args: %s" % json.dumps(vars(args)))

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    log("device: %s" % device)

    y_tr = np.array([it["y"] for it in tr])
    cnt = np.bincount(y_tr, minlength=NC).astype(np.float64)
    w = np.sqrt(len(y_tr) / (NC * np.maximum(cnt, 1)))
    w = np.clip(w, 0.5, 3.0)
    log("class weights: " + " ".join("%d:%.2f" % (i, w[i]) for i in range(NC) if cnt[i] or i == 0))

    net = Net(NC).to(device)
    initialize(net, args.initial, args.scratch)
    guard(out_dir, net)
    opt = torch.optim.AdamW(net.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    crit = nn.CrossEntropyLoss(weight=torch.tensor(w, dtype=torch.float32, device=device))

    def val_pass():
        net.eval()
        preds, ys = [], []
        with torch.no_grad():
            for i in range(0, len(va), 512):
                chunk = va[i:i + 512]
                x = torch.stack([to_tensor(make_input(it["g"])) for it in chunk]).to(device)
                out = net(x)
                guard(out_dir, net, out, [it['file'] for it in chunk], x=x)
                preds.extend(out.argmax(1).cpu().tolist())
                ys.extend(it["y"] for it in chunk)
        acc = float(np.mean(np.array(preds) == np.array(ys)))
        seq_ok, seq_n = seq_accuracy(va, preds)
        rec = {}
        ys_a = np.array(ys)
        pr_a = np.array(preds)
        for d in range(NC):
            mask = ys_a == d
            if mask.sum() >= 5:
                rec[d] = float((pr_a[mask] == d).mean())
        return acc, seq_ok, seq_n, rec

    best_acc = -1.0
    best_epoch = -1
    bad = 0
    for ep in range(1, args.epochs + 1):
        net.train()
        idx = list(range(len(tr)))
        random.shuffle(idx)
        t0 = time.time()
        total_loss = 0.0
        total_hit = 0
        total_n = 0
        for i in range(0, len(idx), args.batch):
            batch = [tr[j] for j in idx[i:i + args.batch]]
            xs = []
            ys = []
            for it in batch:
                g = it["g"]
                if it["y"] == 0:
                    g = crop_neg(g)
                g = make_input(g)
                g = augment(g)
                xs.append(to_tensor(g))
                ys.append(it["y"])
            x = torch.stack(xs).to(device)
            y = torch.tensor(ys, dtype=torch.long, device=device)
            opt.zero_grad()
            out = net(x)
            loss = crit(out, y)
            guard(out_dir, net, loss, [it['file'] for it in batch], ep, i//args.batch, x, y)
            loss.backward()
            guard(out_dir, net, loss, [it['file'] for it in batch], ep, i//args.batch, x, y, gradients=True)
            opt.step()
            guard(out_dir, net, files=[it['file'] for it in batch], epoch=ep, step=i//args.batch, x=x, y=y)
            total_loss += float(loss) * len(ys)
            total_hit += int((out.argmax(1) == y).sum())
            total_n += len(ys)
        sched.step()
        acc, seq_ok, seq_n, rec = val_pass()
        dt = time.time() - t0
        log("ep %2d  loss %.4f  train %.2f%%  val %.2f%%  seq %d/%d  %.0fs"
            % (ep, total_loss / total_n, 100.0 * total_hit / total_n, 100.0 * acc,
               seq_ok, seq_n, dt))
        if ep % 5 == 0 or ep == args.epochs:
            bad_rec = ["%d:%.0f%%" % (d, 100 * r) for d, r in sorted(rec.items()) if r < 0.95]
            log("  recall<95%%: %s" % (bad_rec or "none"))
        metrics(out_dir, {'epoch':ep,'train/loss':total_loss/total_n,'metrics/accuracy':acc,'metrics/sequence':seq_ok/max(seq_n,1)})
        torch.save({'model_state':net.state_dict(),'optimizer':opt.state_dict(),'scheduler':sched.state_dict(),'epoch':ep,'args':vars(args)}, os.path.join(out_dir,'last.pt'))
        if acc > best_acc + 1e-6:
            best_acc = acc
            best_epoch = ep
            bad = 0
            torch.save(net.state_dict(), os.path.join(out_dir, "best.pt"))
        else:
            bad += 1
            if bad >= args.patience:
                log("early stop at ep %d (best ep %d acc %.2f%%)" % (ep, best_epoch, 100 * best_acc))
                break

    log("best: ep %d val %.2f%%" % (best_epoch, 100 * best_acc))
    net.load_state_dict(torch.load(os.path.join(out_dir, "best.pt"), map_location=device))
    net.eval().cpu()
    traced = torch.jit.trace(net, torch.randn(1, 1, SIZE, SIZE))
    # 在内存导出，再用Python写中文路径，避免C++路径限制和磁盘临时文件。
    import io
    buffer = io.BytesIO()
    torch.jit.save(traced, buffer)
    with open(os.path.join(out_dir, 'inference.pt'), 'wb') as f:
        f.write(buffer.getvalue())
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"nc": NC, "size": SIZE, "inner": INNER, "classes": "0=non-digit, 1..34=number",
                   "best_epoch": best_epoch, "best_val_acc": best_acc,
                   "train": len(tr), "val": len(va), "args": vars(args)},
                  f, ensure_ascii=False, indent=1)
    logf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""选择题涂黑 CNN v2：多标签训练。

数据：data/choice 里已审核的行（reviewer 通过，special=illegible 排除）
输入：整行选项图，等比缩放到 188×44 内 + 居中补白到 192×48，灰度 + 反色
输出：6 个 sigmoid（A~F），任意组合
样本权重：special=changed（学生涂改过）→ 2.0

用法：py3136 pipeline\\cnn\\train_choice.py [--epochs 60] [--batch 32]
"""
import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

PIPE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PIPE)
from common.paths import DATA, MODELS, TRAIN  # noqa: E402

from common.cnn_data import load_frozen
from common.records import write_json
from common.cnn_training import initialize, guard, metrics
cv2.setNumThreads(0)
W, H = 192, 48
INNER_W, INNER_H = 188, 44
LETTERS = "ABCDEF"


class Net(nn.Module):
    def __init__(self, nc=6):
        super().__init__()
        self.c1 = nn.Conv2d(1, 16, 3, padding=1)
        self.b1 = nn.BatchNorm2d(16)
        self.c2 = nn.Conv2d(16, 32, 3, padding=1)
        self.b2 = nn.BatchNorm2d(32)
        self.c3 = nn.Conv2d(32, 64, 3, padding=1)
        self.b3 = nn.BatchNorm2d(64)
        self.c4 = nn.Conv2d(64, 64, 3, padding=1)
        self.b4 = nn.BatchNorm2d(64)
        self.p = nn.MaxPool2d(2, 2)
        self.fc1 = nn.Linear(64 * 12 * 3, 128)
        self.drop = nn.Dropout(0.3)
        self.fc2 = nn.Linear(128, nc)

    def forward(self, x):
        x = self.p(F.relu(self.b1(self.c1(x))))
        x = self.p(F.relu(self.b2(self.c2(x))))
        x = self.p(F.relu(self.b3(self.c3(x))))
        x = self.p(F.relu(self.b4(self.c4(x))))
        x = x.flatten(1)
        x = F.relu(self.fc1(x))
        return self.fc2(self.drop(x))


def prep(gray):
    """等比放进 188×44，居中补白到 192×48，灰度 + 反色。"""
    h, w = gray.shape
    s = min(float(INNER_W) / w, float(INNER_H) / h)
    nw = max(1, int(round(w * s)))
    nh = max(1, int(round(h * s)))
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LANCZOS4
    r = cv2.resize(gray, (nw, nh), interpolation=interp)
    canvas = np.full((H, W), 255, np.uint8)
    x = (W - nw) // 2
    y = (H - nh) // 2
    canvas[y:y + nh, x:x + nw] = r
    a = canvas.astype(np.float32) / 255.0
    if a.mean() > 0.5:
        a = 1.0 - a
    return a


def augment(g):
    if random.random() < 0.7:
        ang = random.uniform(-2, 2)
        sc = random.uniform(0.92, 1.08)
        tx = random.uniform(-0.03, 0.03) * W
        ty = random.uniform(-0.05, 0.05) * H
        m = cv2.getRotationMatrix2D((W / 2, H / 2), ang, sc)
        m[0, 2] += tx
        m[1, 2] += ty
        g = cv2.warpAffine(g, m, (W, H), flags=cv2.INTER_LINEAR, borderValue=0)
    if random.random() < 0.5:
        a = random.uniform(0.75, 1.25)
        b = random.uniform(-30, 30)
        g = np.clip(g.astype(np.float32) * a + b, 0, 255).astype(np.uint8)
    if random.random() < 0.25:
        g = cv2.GaussianBlur(g, (3, 3), 0)
    return g


def to_label(answer):
    y = np.zeros(6, np.float32)
    for ch in answer:
        if ch in LETTERS:
            y[LETTERS.index(ch)] = 1.0
    return y




def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--patience", type=int, default=15)
    ap.add_argument('--name', required=True)
    ap.add_argument('--data', required=True, help='独立CNN dataset_snapshot目录')
    ap.add_argument('--initial', help='微调起点，支持本架构TorchScript或state_dict')
    ap.add_argument('--scratch', action='store_true')
    args = ap.parse_args()

    random.seed(42)
    np.random.seed(42)
    torch.manual_seed(42)

    items = load_frozen(args.data, "choice")
    print("reviewed rows:", len(items))
    if len(items) < 50:
        print("审核行太少，先审子集再训")
        return 1
    tr = [it for it in items if it['split'] == 'train']
    va = [it for it in items if it['split'] == 'val']
    run = args.name
    out_dir = os.path.join(TRAIN, run)
    os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(os.path.join(out_dir, 'results.csv')):
        raise FileExistsError('运行已有指标，禁止覆盖')
    write_json(Path(out_dir)/'training_args.json', vars(args))
    logf = open(os.path.join(out_dir, "trainer.log"), "w", encoding="utf-8", buffering=1)

    def log(s):
        print(s, flush=True)
        logf.write(s + "\n")

    log("run %s  train %d / val %d" % (run, len(tr), len(va)))

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    net = Net(6).to(device)
    initialize(net, args.initial, args.scratch)
    guard(out_dir, net)
    opt = torch.optim.AdamW(net.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    crit = nn.BCEWithLogitsLoss(reduction="none")

    def run_batch(batch, train_mode):
        xs, ys, ws = [], [], []
        for it in batch:
            g = prep(it["img"])
            if train_mode:
                g8 = np.clip(g * 255, 0, 255).astype(np.uint8)
                g = augment(g8).astype(np.float32) / 255.0
                if g.mean() > 0.5:
                    g = 1.0 - g
            xs.append(torch.from_numpy(g).unsqueeze(0))
            ys.append(torch.from_numpy(to_label(it["answer"])))
            ws.append(float(it.get("weight", 1.0)))
        x = torch.stack(xs).to(device)
        y = torch.stack(ys).to(device)
        w = torch.tensor(ws, dtype=torch.float32, device=device).unsqueeze(1)
        out = net(x)
        loss = (crit(out, y) * w).mean()
        guard(out_dir, net, loss, [it['file'] for it in batch], x=x, y=y)
        return out, y, loss, x

    def evaluate():
        net.eval()
        probs, ys = [], []
        with torch.no_grad():
            for i in range(0, len(va), 128):
                batch = va[i:i + 128]
                out, y, _, _ = run_batch(batch, False)
                probs.append(torch.sigmoid(out).cpu().numpy())
                ys.append(y.cpu().numpy())
        p = np.concatenate(probs)
        y = np.concatenate(ys)
        pred = (p >= 0.5).astype(np.float32)
        exact = float(np.mean(np.all(pred == y, axis=1)))
        per = []
        for k in range(6):
            tp = float(np.sum((pred[:, k] == 1) & (y[:, k] == 1)))
            fp = float(np.sum((pred[:, k] == 1) & (y[:, k] == 0)))
            fn = float(np.sum((pred[:, k] == 0) & (y[:, k] == 1)))
            pr = tp / (tp + fp) if tp + fp else 1.0
            rc = tp / (tp + fn) if tp + fn else 1.0
            per.append((pr, rc))
        char_acc = float(np.mean((pred == y).astype(np.float32)))
        return exact, char_acc, per

    best = -1.0
    bad = 0
    for ep in range(1, args.epochs + 1):
        net.train()
        idx = list(range(len(tr)))
        random.shuffle(idx)
        t0 = time.time()
        total = 0.0
        n = 0
        for i in range(0, len(idx), args.batch):
            batch = [tr[j] for j in idx[i:i + args.batch]]
            out, y, loss, x = run_batch(batch, True)
            opt.zero_grad()
            loss.backward()
            guard(out_dir, net, loss, [it['file'] for it in batch], ep, i//args.batch, x, y, gradients=True)
            opt.step()
            guard(out_dir, net, files=[it['file'] for it in batch], epoch=ep, step=i//args.batch, x=x, y=y)
            total += float(loss) * len(batch)
            n += len(batch)
        sched.step()
        exact, char_acc, per = evaluate()
        log("ep %2d  loss %.4f  val exact %.2f%%  char %.2f%%  %.0fs"
            % (ep, total / n, 100 * exact, 100 * char_acc, time.time() - t0))
        metrics(out_dir, {'epoch':ep,'train/loss':total/n,'metrics/exact':exact,'metrics/char_accuracy':char_acc})
        torch.save({'model_state':net.state_dict(),'optimizer':opt.state_dict(),'scheduler':sched.state_dict(),'epoch':ep,'args':vars(args)}, os.path.join(out_dir,'last.pt'))
        if exact > best + 1e-6:
            best = exact
            bad = 0
            torch.save(net.state_dict(), os.path.join(out_dir, "best.pt"))
            log("   per-letter P/R: " + " ".join(
                "%s %.2f/%.2f" % (LETTERS[k], per[k][0], per[k][1]) for k in range(6)))
        else:
            bad += 1
            if bad >= args.patience:
                log("early stop ep %d, best %.2f%%" % (ep, 100 * best))
                break

    net.load_state_dict(torch.load(os.path.join(out_dir, "best.pt"), map_location=device))
    net.eval().cpu()
    traced = torch.jit.trace(net, torch.randn(1, 1, H, W))
    # 在内存导出，再用Python写中文路径，避免C++路径限制和磁盘临时文件。
    import io
    buffer = io.BytesIO()
    torch.jit.save(traced, buffer)
    with open(os.path.join(out_dir, 'inference.pt'), 'wb') as f:
        f.write(buffer.getvalue())
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"letters": LETTERS, "size": [H, W], "best_exact": best,
                   "train": len(tr), "val": len(va), "args": vars(args)}, f,
                  ensure_ascii=False, indent=1)
    logf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

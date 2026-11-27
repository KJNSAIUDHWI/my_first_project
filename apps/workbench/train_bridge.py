# -*- coding: utf-8 -*-
"""工作台模型训练桥接模块（标框同步 → 独立快照 → 数值保护微调训练 → 安全部署与回退）。

既支持 CLI 直接调用（供工作台后台子进程运行），也供 apps/workbench/server.py 导入查询状态与执行同步/部署。
特点：
1. 兼容作者已有 `data/<task>/splits.csv` 固定分区，也兼容新克隆仓库从零建库（按父页确定性划分 train/val，不跨页泄露）；
2. 生成的快照严格满足 `training/yolo_snapshot.validate_training_input()` 与 `training/cnn_dataset.load_frozen()` 校验；
3. 训练全部经由 `training/supervisor.supervise()` + `GradGuardTrainer` 数值保护执行，绝不自动覆盖线上权重；
4. 部署前自动保存 `models/current.prev.json` 备份，支持一键回退上一版权重。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from engine import model_registry  # noqa: E402
from engine.paths import DATA, MODELS, TRAIN  # noqa: E402
from training import box_log as events, cnn_dataset as cnn_data, row_log, split_plan as fixed_dataset  # noqa: E402
from training.records import now as now_iso, read_csv, read_json, sha, write_csv, write_json  # noqa: E402
from training.supervisor import supervise  # noqa: E402
from training.yolo_snapshot import MAPS, NAMES, validate_training_input  # noqa: E402

YOLO_TASKS = ("m1", "m2", "m3")
CNN_TASKS = ("choice",)
ALL_TASKS = ("m1", "m2", "m3", "choice")

TASK_META = {
    "m1": {
        "name": "M1 版面分区",
        "desc": "识别整页中的姓名学号区、选择题区、填空/实验大题区",
        "kind": "yolo",
        "default_epochs": 50,
        "default_imgsz": 1024,
        "default_batch_gpu": 8,
        "default_batch_cpu": 2,
        "default_lr": 0.0001,
    },
    "m2": {
        "name": "M2 选择题框",
        "desc": "在选择题区裁图中检测题号框与选项行框（禁用水平翻转）",
        "kind": "yolo",
        "default_epochs": 50,
        "default_imgsz": 1024,
        "default_batch_gpu": 4,
        "default_batch_cpu": 2,
        "default_lr": 0.0001,
    },
    "m3": {
        "name": "M3 填空/实验大题框",
        "desc": "在填空/实验区裁图中检测完整大题框与大题号框（禁用水平翻转）",
        "kind": "yolo",
        "default_epochs": 50,
        "default_imgsz": 1024,
        "default_batch_gpu": 4,
        "default_batch_cpu": 2,
        "default_lr": 0.0001,
    },
    "choice": {
        "name": "选择题涂卡 CNN",
        "desc": "识别整行选项涂黑组合（A~F 多选），需至少 50 条已审样本",
        "kind": "cnn",
        "default_epochs": 40,
        "default_imgsz": 0,
        "default_batch_gpu": 32,
        "default_batch_cpu": 32,
        "default_lr": 0.0005,
    },
}

# 定档防 NaN 训练配方
YOLO_RECIPES = {
    "m1": ["--optimizer", "AdamW", "--lrf", "0.01", "--warmup", "3",
           "--warmup-bias-lr", "0.0001", "--scale", "0", "--translate", "0"],
    "m2": ["--optimizer", "AdamW", "--lrf", "0.01", "--warmup", "3",
           "--warmup-bias-lr", "0.0001", "--scale", "0", "--translate", "0", "--fliplr", "0"],
    "m3": ["--optimizer", "AdamW", "--lrf", "0.01", "--warmup", "3",
           "--warmup-bias-lr", "0.0001", "--scale", "0", "--translate", "0", "--fliplr", "0"],
}

_HW_CACHE: dict | None = None


def detect_hardware() -> dict:
    """探测本机 CUDA GPU 信息（进程内缓存，避免反复 import torch 拖慢接口）。"""
    global _HW_CACHE
    if _HW_CACHE is not None:
        return dict(_HW_CACHE)
    info = {"cuda": False, "gpu_name": "", "default_device": "cpu"}
    try:
        import torch
        if torch.cuda.is_available():
            info["cuda"] = True
            info["gpu_name"] = torch.cuda.get_device_name(0)
            info["default_device"] = "0"
    except Exception:
        pass
    _HW_CACHE = info
    return dict(info)


def _parent_group(stem: str) -> str:
    return stem.split(".选择题区")[0].split(".填空题区")[0]


def _deterministic_split(group: str, val_ratio: float = 0.15) -> str:
    h = int(hashlib.sha256(group.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "val" if h < val_ratio else "train"


def _sanitize_purpose(purpose: str) -> str:
    import re
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", (purpose or "").strip()).strip("_")
    return cleaned[:24] or "工作台微调"


def _normalize_box(box: list[float], w: float, h: float) -> tuple[float, float, float, float] | None:
    x1, y1, x2, y2 = [float(v) for v in box]
    x1, x2 = max(0.0, min(w, min(x1, x2))), max(0.0, min(w, max(x1, x2)))
    y1, y2 = max(0.0, min(h, min(y1, y2))), max(0.0, min(h, max(y1, y2)))
    bw = (x2 - x1) / w
    bh = (y2 - y1) / h
    if bw <= 1e-4 or bh <= 1e-4:
        return None
    cx = (x1 + x2) / 2.0 / w
    cy = (y1 + y2) / 2.0 / h
    return round(cx, 6), round(cy, 6), round(bw, 6), round(bh, 6)


def sync_batch_to_training_data(
    root: Path = ROOT,
    batches_root: Path | None = None,
    batch_id: str | None = None,
) -> dict:
    """将当前批次中已确认审阅的页面框（M1/M2/M3）无损同步到 data/<task>/ 供微调训练。"""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import core as wb_core

    b_root = batches_root or (root / "batches")
    active = wb_core.set_active_batch(batch_id, b_root) if batch_id else wb_core.get_active_batch(b_root)
    if not active:
        raise ValueError("当前没有已导入的批次")
    rec_dir = Path(active["recognize_dir"])
    results_path = rec_dir / "results.json"
    if not results_path.is_file():
        raise ValueError("当前批次尚未完成自动识别（缺少 results.json）")

    results = read_json(results_path, {}) or {}
    fb = read_json(rec_dir / "feedback.json", {}) or {}
    fb_items = fb.get("items") or {}
    pages = results.get("pages") or []

    for task in YOLO_TASKS:
        (root / "data" / task / "images").mkdir(parents=True, exist_ok=True)
        (root / "data" / task / "log").mkdir(parents=True, exist_ok=True)

    counts = {"pages_confirmed": 0, "m1_synced": 0, "m2_synced": 0, "m3_synced": 0, "skipped_unconfirmed": 0}
    for page in pages:
        pid = page.get("id")
        page_fb = fb_items.get(pid) or {}
        if not page_fb.get("label"):
            counts["skipped_unconfirmed"] += 1
            continue
        stem = str(page.get("name") or "").strip()
        img_rel = page.get("image") or ""
        src_img = rec_dir / img_rel
        if not stem or not src_img.is_file():
            continue

        counts["pages_confirmed"] += 1
        reviewer = str(page_fb.get("reviewer") or "工作台同步").strip()[:32]
        boxes = page_fb.get("boxes") if isinstance(page_fb.get("boxes"), list) else (page.get("boxes") or [])

        with Image.open(src_img) as im:
            pw, ph = im.size
            # 1. M1 整页同步
            m1_boxes = []
            m1_choice_regions = []
            m1_fill_regions = []
            for b in boxes:
                cls = int(b.get("cls", -1))
                coords = b.get("box") or []
                if len(coords) != 4:
                    continue
                if cls in (0, 1, 4):
                    nb = _normalize_box(coords, pw, ph)
                    if nb:
                        m1_boxes.append([cls, *nb])
                    if cls == 1:
                        m1_choice_regions.append([float(v) for v in coords])
                    elif cls == 4:
                        m1_fill_regions.append([float(v) for v in coords])

            m1_dst = root / "data" / "m1" / "images" / f"{stem}.jpg"
            if not m1_dst.is_file():
                shutil.copy2(src_img, m1_dst)
            ev1 = events.append_event("m1", stem, {"e": "review", "boxes": m1_boxes, "reviewer": reviewer})
            events.sync_progress("m1", stem)
            if ev1 is not None or m1_dst.is_file():
                counts["m1_synced"] += 1

            # 2. M2 选择题区裁图 + 局部框同步
            m2_all = [b for b in boxes if int(b.get("cls", -1)) in (2, 3) and len(b.get("box") or []) == 4]
            m1_choice_regions.sort(key=lambda r: (r[1], r[0]))
            for idx, reg in enumerate(m1_choice_regions, 1):
                rx1, ry1, rx2, ry2 = [int(round(v)) for v in reg]
                rx1, ry1 = max(0, rx1), max(0, ry1)
                rx2, ry2 = min(pw, rx2), min(ph, ry2)
                cw, ch = rx2 - rx1, ry2 - ry1
                if cw < 24 or ch < 24:
                    continue
                crop_stem = f"{stem}.选择题区{idx}"
                crop_dst = root / "data" / "m2" / "images" / f"{crop_stem}.jpg"
                if not crop_dst.is_file():
                    im.crop((rx1, ry1, rx2, ry2)).convert("RGB").save(crop_dst, format="JPEG", quality=95)
                local_boxes = []
                for b in m2_all:
                    bx1, by1, bx2, by2 = [float(v) for v in b["box"]]
                    cx, cy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0
                    if rx1 <= cx <= rx2 and ry1 <= cy <= ry2:
                        nb = _normalize_box([bx1 - rx1, by1 - ry1, bx2 - rx1, by2 - ry1], cw, ch)
                        if nb:
                            local_boxes.append([int(b["cls"]), *nb])
                events.append_event("m2", crop_stem, {"e": "review", "boxes": local_boxes, "reviewer": reviewer})
                events.sync_progress("m2", crop_stem)
                counts["m2_synced"] += 1

            # 3. M3 填空/实验大题区裁图 + 局部框同步
            m3_all = [b for b in boxes if int(b.get("cls", -1)) in (5, 6) and len(b.get("box") or []) == 4]
            m1_fill_regions.sort(key=lambda r: (r[1], r[0]))
            for idx, reg in enumerate(m1_fill_regions, 1):
                rx1, ry1, rx2, ry2 = [int(round(v)) for v in reg]
                rx1, ry1 = max(0, rx1), max(0, ry1)
                rx2, ry2 = min(pw, rx2), min(ph, ry2)
                cw, ch = rx2 - rx1, ry2 - ry1
                if cw < 24 or ch < 24:
                    continue
                crop_stem = f"{stem}.填空题区{idx}"
                crop_dst = root / "data" / "m3" / "images" / f"{crop_stem}.jpg"
                if not crop_dst.is_file():
                    im.crop((rx1, ry1, rx2, ry2)).convert("RGB").save(crop_dst, format="JPEG", quality=95)
                local_boxes = []
                for b in m3_all:
                    bx1, by1, bx2, by2 = [float(v) for v in b["box"]]
                    cx, cy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0
                    if rx1 <= cx <= rx2 and ry1 <= cy <= ry2:
                        nb = _normalize_box([bx1 - rx1, by1 - ry1, bx2 - rx1, by2 - ry1], cw, ch)
                        if nb:
                            local_boxes.append([int(b["cls"]), *nb])
                events.append_event("m3", crop_stem, {"e": "review", "boxes": local_boxes, "reviewer": reviewer})
                events.sync_progress("m3", crop_stem)
                counts["m3_synced"] += 1

    return {
        "ok": True,
        "batch_id": active["batch_id"],
        **counts,
        "message": (
            f"已从批次 {active['batch_id']} 同步 {counts['pages_confirmed']} 页已确认审阅结果"
            f"（M1: {counts['m1_synced']} 张，M2: {counts['m2_synced']} 张，M3: {counts['m3_synced']} 张）"
        ),
    }


def _count_reviewed_yolo(root: Path, task: str) -> dict:
    base = root / "data" / task
    img_dir = base / "images"
    if not img_dir.is_dir():
        return {"total_images": 0, "reviewed": 0, "ready": False}
    imgs = {p.stem for p in img_dir.glob("*.jpg")}
    if not imgs:
        return {"total_images": 0, "reviewed": 0, "ready": False}
    reviewed = 0
    prog = base / "progress.csv"
    if prog.is_file():
        with prog.open("r", encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                nm = (r.get("new_name") or r.get("stem") or "").strip()
                st_str = (r.get("status") or "").strip()
                if nm in imgs and st_str.startswith("已审"):
                    reviewed += 1
    else:
        log_dir = base / "log"
        if log_dir.is_dir():
            for stem in imgs:
                if (log_dir / f"{stem}.log").is_file():
                    st = events.load_state(task, stem)
                    if st.get("reviewed") and not st.get("special"):
                        reviewed += 1
    return {"total_images": len(imgs), "reviewed": reviewed, "ready": reviewed >= 2}


def create_yolo_workbench_snapshot(
    task: str,
    run_dir: Path,
    val_ratio: float = 0.15,
) -> dict:
    """为 YOLO (m1/m2/m3) 生成满足 validate_training_input() 的独立快照（兼容无 splits.csv 新库）。"""
    if task not in YOLO_TASKS:
        raise ValueError(f"不支持的 YOLO 任务：{task}")
    img_dir = DATA / task / "images"
    if not img_dir.is_dir():
        raise ValueError(f"未找到训练图片目录：{img_dir}，请先完成标注或同步已审页面")

    fixed_dataset._PLAN_CACHE.pop(task, None)
    plan = fixed_dataset.read_plan(task) or {}

    candidates = []
    skipped_invalid = []
    for img_path in sorted(img_dir.glob("*.jpg")):
        stem = img_path.stem
        st = events.load_state(task, stem)
        if not st.get("reviewed") or st.get("special"):
            continue
        try:
            with Image.open(img_path) as im:
                size = im.size
            labels = []
            for idx, box in enumerate(st.get("boxes") or []):
                if len(box) != 5 or not all(math.isfinite(float(v)) for v in box):
                    raise ValueError(f"非有限框 #{idx}")
                c, x, y, w, h = map(float, box)
                if int(c) not in MAPS[task] or w <= 0 or h <= 0:
                    continue
                x1, y1, x2, y2 = x - w / 2, y - h / 2, x + w / 2, y + h / 2
                bounds = [max(0.0, x1), max(0.0, y1), min(1.0, x2), min(1.0, y2)]
                if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                    continue
                a, b2, c2, d2 = bounds
                labels.append([MAPS[task][int(c)], (a + c2) / 2, (b2 + d2) / 2, c2 - a, d2 - b2])
        except Exception as exc:
            skipped_invalid.append({"name": stem, "error": str(exc)})
            continue

        row_plan = plan.get(stem)
        if row_plan and row_plan.get("split") in ("train", "val"):
            split = row_plan["split"]
            group = row_plan.get("group") or _parent_group(stem)
            stage = row_plan.get("stage", 0)
            subj = row_plan.get("subject_original") or ""
        else:
            group = _parent_group(stem)
            split = ""
            stage = 0
            subj = ""
        candidates.append({
            "new_name": stem,
            "image": img_path.resolve(),
            "split": split,
            "stage": stage,
            "group": group,
            "subject_original": subj,
            "labels": labels,
        })

    if len(candidates) < 2:
        raise ValueError(f"{task} 已审样本不足（当前仅 {len(candidates)} 张，至少需要 2 张才能划分训练/验证集）")

    # 统一按 group 分配未划分样本的 split，严格避免同一父页跨 train/val
    group_split: dict[str, str] = {}
    for c in candidates:
        if c["split"] in ("train", "val"):
            group_split.setdefault(c["group"], c["split"])
    for c in candidates:
        g = c["group"]
        if g not in group_split:
            group_split[g] = _deterministic_split(g, val_ratio=val_ratio)
        c["split"] = group_split[g]

    counts = Counter(c["split"] for c in candidates)
    # 兜底：若全部落入同一侧，把最后一个独立 group 划给空的一侧
    if counts["val"] == 0 or counts["train"] == 0:
        unique_groups = sorted(group_split.keys())
        if len(unique_groups) < 2:
            # 同一父页的多张裁图（如只有 1 页试卷时），按样本本身保底分出 1 张 val
            candidates[-1]["split"] = "val" if counts["val"] == 0 else "train"
            candidates[-1]["group"] = candidates[-1]["new_name"]
        else:
            flip_group = unique_groups[-1]
            target_split = "val" if counts["val"] == 0 else "train"
            for c in candidates:
                if c["group"] == flip_group:
                    c["split"] = target_split
        counts = Counter(c["split"] for c in candidates)

    if counts["train"] == 0 or counts["val"] == 0:
        raise ValueError(f"{task} 无法构成非空的 train/val 划分（当前 {dict(counts)}）")

    output = run_dir / "dataset_snapshot"
    output.mkdir(parents=True, exist_ok=False)
    try:
        for sp in ("train", "val"):
            (output / "labels" / sp).mkdir(parents=True)
        for c in candidates:
            sp = c["split"]
            text = "".join("%d %.8f %.8f %.8f %.8f\n" % tuple(b) for b in c["labels"])
            (output / "labels" / sp / f"{c['new_name']}.txt").write_text(text, encoding="utf-8")
        for sp in ("train", "val"):
            lines = [str(c["image"]) for c in candidates if c["split"] == sp]
            (output / f"{sp}_list.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        fields = ["new_name", "split", "stage", "group", "subject_original"]
        write_csv(output / "selection.csv", [{k: c[k] for k in fields} for c in candidates], fields)
        yaml_text = "path: %s\ntrain: %s\nval: %s\nnames: %s\n" % (
            ROOT.resolve().as_posix(),
            (output / "train_list.txt").resolve().as_posix(),
            (output / "val_list.txt").resolve().as_posix(),
            str(NAMES[task]),
        )
        (output / "data.yaml").write_text(yaml_text, encoding="utf-8")
        audit = {
            "task": task,
            "source": "workbench",
            "counts": dict(counts),
            "skipped_invalid": skipped_invalid,
            "created_at": now_iso(),
        }
        write_json(run_dir / "snapshot_audit.json", audit)
        validate_training_input(task, run_dir, output / "data.yaml")
        return audit
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise


def create_choice_workbench_snapshot(run_dir: Path) -> dict:
    """为选择题涂卡 CNN 创建独立快照。"""
    output = run_dir / "dataset_snapshot"
    rows = cnn_data.freeze("choice", output, allow_inferred=False)
    counts = Counter(r["split"] for r in rows)
    if len(rows) < 50:
        raise ValueError(f"涂卡已审样本不足（当前 {len(rows)} 条，至少需要 50 条）")
    audit = {
        "task": "choice",
        "source": "workbench",
        "counts": dict(counts),
        "created_at": now_iso(),
    }
    write_json(run_dir / "snapshot_audit.json", audit)
    return audit


def run_supervised_training(
    task: str,
    purpose: str = "工作台微调",
    epochs: int | None = None,
    imgsz: int | None = None,
    batch: int | None = None,
    device: str | None = None,
    lr: float | None = None,
    run_name: str | None = None,
) -> dict:
    """一键执行：创建独立快照 → 启动受监督防 NaN 训练 → 权重完整性检查与候补留档。"""
    if task not in ALL_TASKS:
        raise ValueError(f"不支持的训练模块：{task}")
    meta = TASK_META[task]
    hw = detect_hardware()
    clean_purpose = _sanitize_purpose(purpose)
    name = run_name or f"{task}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{clean_purpose}"
    run_dir = (TRAIN / name).resolve()
    if run_dir.parent != TRAIN.resolve():
        raise ValueError("run 必须位于 train/ 下一级目录")
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"[工作台训练] 开始准备 {meta['name']} 独立快照：{name}", flush=True)
    if task in YOLO_TASKS:
        audit = create_yolo_workbench_snapshot(task, run_dir)
    else:
        audit = create_choice_workbench_snapshot(task, run_dir)
    print(f"[工作台训练] 快照就绪：train={audit['counts'].get('train', 0)}, val={audit['counts'].get('val', 0)}", flush=True)

    try:
        initial = model_registry.current(task)
    except FileNotFoundError:
        raise FileNotFoundError(f"未找到 {task} 的当前推理权重，请先在「运行环境」解压模型权重后再微调")

    eff_device = (device or hw["default_device"]).strip()
    is_cuda = eff_device.lower() != "cpu" and hw["cuda"]
    if not is_cuda:
        eff_device = "cpu"
    eff_epochs = int(epochs or meta["default_epochs"])
    eff_batch = int(batch or (meta["default_batch_gpu"] if is_cuda else meta["default_batch_cpu"]))
    eff_lr = float(lr or meta["default_lr"])

    snap = run_dir / "dataset_snapshot"
    if task in YOLO_TASKS:
        eff_imgsz = int(imgsz or meta["default_imgsz"])
        if eff_imgsz <= 0 or eff_imgsz % 32 != 0:
            eff_imgsz = 1024
        cmd = [
            sys.executable, "-B", "training/yolo_trainer.py",
            "--task", task,
            "--name", run_dir.name,
            "--data", str(snap / "data.yaml"),
            "--model", str(initial),
            *YOLO_RECIPES[task],
            "--epochs", str(eff_epochs),
            "--patience", str(min(15, max(5, eff_epochs // 2))),
            "--lr0", str(eff_lr),
            "--batch", str(eff_batch),
            "--imgsz", str(eff_imgsz),
            "--device", eff_device,
        ]
        if is_cuda:
            cmd.append("--amp")
        if task in ("m2", "m3") and not is_cuda:
            cmd += ["--workers", "0"]
    else:
        cmd = [
            sys.executable, "-B", "training/choice_cnn_trainer.py",
            "--name", run_dir.name,
            "--data", str(snap),
            "--initial", str(initial),
            "--epochs", str(eff_epochs),
            "--batch", str(eff_batch),
            "--lr", str(eff_lr),
        ]

    print(f"[工作台训练] 微调起点：{initial.name} | 设备：{eff_device} | 轮数：{eff_epochs} | Batch：{eff_batch}", flush=True)
    print(f"[工作台训练] 启动监督训练（含 NaN/梯度保护与权重自动留档）...", flush=True)
    result = supervise(task, run_dir, cmd, initial, clean_purpose, deploy=False)
    print(f"[工作台训练] 结束：success={result.get('success')}, error={result.get('error', '')}", flush=True)
    return {"run": name, "task": task, "audit": audit, "result": result}


def _read_latest_metrics(run_dir: Path) -> dict:
    csv_path = run_dir / "results.csv"
    if not csv_path.is_file():
        return {}
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            return {}
        last = {k.strip(): v.strip() for k, v in rows[-1].items() if k}
        out = {"completed_epochs": len(rows)}
        for key, alias in (
            ("metrics/mAP50(B)", "mAP50"),
            ("metrics/mAP50-95(B)", "mAP50_95"),
            ("metrics/exact", "exact"),
            ("metrics/char_accuracy", "char_acc"),
            ("train/box_loss", "box_loss"),
            ("train/loss", "loss"),
        ):
            if key in last:
                try:
                    val = float(last[key])
                    if math.isfinite(val):
                        out[alias] = round(val, 4)
                except ValueError:
                    pass
        return out
    except Exception:
        return {}


def _read_dict(path: Path) -> dict:
    try:
        val = read_json(path, {})
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def list_training_runs(root: Path = ROOT, limit: int = 25) -> list[dict]:
    """列出 train/ 下最近的训练记录，附带指标、部署状态和可否一键启用。"""
    train_root = root / "train"
    if not train_root.is_dir():
        return []
    cur_map = _read_dict(root / "models" / "current.json")
    runs = []
    for p in sorted(train_root.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_dir() or p.name.startswith("."):
            continue
        launch = _read_dict(p / "launch.json")
        audit = _read_dict(p / "snapshot_audit.json")
        comp = _read_dict(p / "completion_check.json")
        exit_st = _read_dict(p / "exit_status.json")
        hb = _read_dict(p / "heartbeat.json")
        task = launch.get("task") or audit.get("task") or (p.name.split("_")[0] if "_" in p.name else "")
        if task not in ALL_TASKS:
            continue
        raw_cand = comp.get("candidate")
        candidate = raw_cand if isinstance(raw_cand, dict) else _read_dict(p / "candidate.json")
        cand_path = candidate.get("path") or ""
        cand_sha = candidate.get("sha256") or ""
        cur_row = cur_map.get(task) or {}
        is_deployed = bool(cand_sha and cur_row.get("sha256") == cand_sha)
        can_deploy = bool(
            comp.get("success")
            and cand_path
            and (root / cand_path).is_file()
        )
        metrics = _read_latest_metrics(p)
        if hb.get("status") == "running" and not exit_st:
            status = "running"
        elif comp.get("success"):
            status = "success"
        elif comp or exit_st:
            status = "failed"
        else:
            status = "incomplete"

        runs.append({
            "run": p.name,
            "task": task,
            "task_name": TASK_META[task]["name"],
            "purpose": launch.get("purpose") or "",
            "started": launch.get("started") or audit.get("created_at") or "",
            "seconds": exit_st.get("seconds"),
            "status": status,
            "error": comp.get("error") or "",
            "counts": audit.get("counts") or {},
            "metrics": metrics,
            "candidate_path": cand_path,
            "can_deploy": can_deploy,
            "is_deployed": is_deployed,
        })
        if len(runs) >= limit:
            break
    return runs


def deploy_run(task: str, run_name: str, root: Path = ROOT) -> dict:
    """安全部署指定训练 run 的候补权重，并在 models/current.prev.json 自动保留回退点。"""
    if task not in ALL_TASKS:
        raise ValueError(f"不支持的模块：{task}")
    run_dir = (root / "train" / run_name).resolve()
    if run_dir.parent != (root / "train").resolve() or not run_dir.is_dir():
        raise ValueError(f"找不到训练记录：{run_name}")
    comp = read_json(run_dir / "completion_check.json", {}) or {}
    if not comp.get("success"):
        raise ValueError("该次训练未通过完整性检查，禁止部署")
    cand = comp.get("candidate") or {}
    if cand.get("task") != task or not cand.get("path"):
        raise ValueError("候补权重记录与目标模块不匹配")
    source = model_registry.model_record_path(cand["path"])
    if not source.is_file():
        raise FileNotFoundError(f"候补权重文件不存在：{source}")

    # 部署前保存上一版回退快照
    prev_path = root / "models" / "current.prev.json"
    prev_map = read_json(prev_path, {}) or {}
    cur_map = read_json(root / "models" / "current.json", {}) or {}
    old_row = cur_map.get(task)
    if old_row and old_row.get("path") and (root / old_row["path"]).is_file():
        prev_map[task] = old_row
    else:
        default_rel = "models/" + model_registry.DEFAULTS[task]
        default_file = root / default_rel
        if default_file.is_file():
            backup_file = default_file.with_name(f"{default_file.stem}_prev_backup.pt")
            if not backup_file.is_file():
                shutil.copy2(default_file, backup_file)
            prev_map[task] = {
                "task": task,
                "path": str(backup_file.relative_to(root)),
                "sha256": sha(backup_file),
                "finite": True,
                "run": "初始部署备份",
                "updated": now_iso(),
            }
    write_json(prev_path, prev_map)

    launch = read_json(run_dir / "launch.json", {}) or {}
    purpose = _sanitize_purpose(launch.get("purpose") or "工作台部署")
    published = model_registry.publish(task, source, run_dir, purpose=purpose)
    return {"ok": True, "task": task, "run": run_name, "deployed": published, "message": f"已将 {TASK_META[task]['name']} 切换为 {run_name}"}


def rollback_model(task: str, root: Path = ROOT) -> dict:
    """将指定模块回退到上一次部署前备份的权重。"""
    if task not in ALL_TASKS:
        raise ValueError(f"不支持的模块：{task}")
    prev_path = root / "models" / "current.prev.json"
    prev_map = read_json(prev_path, {}) or {}
    prev_row = prev_map.get(task)
    if not prev_row or not prev_row.get("path"):
        raise ValueError(f"{TASK_META[task]['name']} 没有可回退的历史权重记录")
    src = model_registry.model_record_path(prev_row["path"])
    if not src.is_file():
        raise FileNotFoundError(f"回退权重文件不存在：{src}")
    evidence = model_registry.inspect_weight(src, task)

    alias = (root / "models" / ("cnn/%s.pt" % task)) if task in CNN_TASKS else (root / "models" / model_registry.DEFAULTS[task])
    pending = alias.with_suffix(".pt.pending")
    shutil.copy2(src, pending)
    pending.replace(alias)

    cur_map = read_json(root / "models" / "current.json", {}) or {}
    restored = {
        **prev_row,
        **evidence,
        "alias": str(alias.relative_to(root)),
        "restored_at": now_iso(),
    }
    cur_map[task] = restored
    write_json(root / "models" / "current.json", cur_map)
    return {"ok": True, "task": task, "restored": restored, "message": f"已将 {TASK_META[task]['name']} 回退到上一版权重（{src.name}）"}


def get_training_overview(root: Path = ROOT, batches_root: Path | None = None) -> dict:
    """返回工作台「模型训练」面板所需的全部状态：硬件、各模块样本数、当前权重、可回退状态、历史训练列表。"""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import core as wb_core

    hw = detect_hardware()
    cur_map = read_json(root / "models" / "current.json", {}) or {}
    prev_map = read_json(root / "models" / "current.prev.json", {}) or {}
    choice_stat = wb_core._count_choice_or_digit(root, "choice", "manifest.csv")

    modules = []
    for task in ALL_TASKS:
        meta = TASK_META[task]
        if task in YOLO_TASKS:
            ds = _count_reviewed_yolo(root, task)
        else:
            ds = {
                "total_images": choice_stat["total"],
                "reviewed": choice_stat["done"],
                "ready": choice_stat["done"] >= 50,
            }
        cur_row = cur_map.get(task) or {}
        cur_path = cur_row.get("path") or ("models/" + model_registry.DEFAULTS[task])
        cur_exists = (root / cur_path).is_file()
        has_rollback = bool((prev_map.get(task) or {}).get("path") and (root / prev_map[task]["path"]).is_file())
        modules.append({
            "task": task,
            **meta,
            "dataset": ds,
            "current_weight": Path(cur_path).name if cur_exists else "未就绪",
            "current_exists": cur_exists,
            "has_rollback": has_rollback,
        })

    b_root = batches_root or (root / "batches")
    active_batch = wb_core.get_active_batch(b_root)
    return {
        "ok": True,
        "hardware": hw,
        "modules": modules,
        "runs": list_training_runs(root),
        "active_batch": active_batch,
    }


def main():
    ap = argparse.ArgumentParser(description="工作台模型微调训练桥接 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run-train", help="生成独立快照并执行受监督防 NaN 微调训练")
    p_run.add_argument("--task", required=True, choices=ALL_TASKS)
    p_run.add_argument("--purpose", default="工作台微调")
    p_run.add_argument("--run", default=None)
    p_run.add_argument("--epochs", type=int, default=None)
    p_run.add_argument("--imgsz", type=int, default=None)
    p_run.add_argument("--batch", type=int, default=None)
    p_run.add_argument("--device", default=None)
    p_run.add_argument("--lr", type=float, default=None)

    args = ap.parse_args()
    if args.cmd == "run-train":
        res = run_supervised_training(
            task=args.task,
            purpose=args.purpose,
            epochs=args.epochs,
            imgsz=args.imgsz,
            batch=args.batch,
            device=args.device,
            lr=args.lr,
            run_name=args.run,
        )
        ok = bool(res.get("result", {}).get("success"))
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

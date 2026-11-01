# -*- coding: utf-8 -*-
"""Use live m1 inference and reviewed box counts to create stage-scoped crops.

Example: python pipeline/cascade/crop.py --stage 1

Only m2 choice crops are created. Crops inherit the parent's fixed split identity.
m1 权重在执行时从 model_registry.current('m1') 解析（current.json 登记，
无登记时用 DEFAULTS），实际路径和哈希写进裁图清单与报告；不在导入时解析。
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pipeline"))
from common import events
from common.model_registry import current as current_weight
from common.records import sha as sha256

M1 = ROOT / "data" / "m1"
M2 = ROOT / "data" / "m2"
RAW = ROOT / "data" / "raw"
CLASS_IDS = {"info": 0, "choice": 1, "fill": 4}
M2_SPLIT_FIELDS = ["new_name", "split", "stage", "group", "subject_original"]
CROP_FIELDS = ["源图", "区域", "序号", "conf", "宽", "高", "文件名",
               "subject_original", "split", "stage", "group", "raw_source",
               "m1_source", "x1", "y1", "x2", "y2", "m1_weight", "m1_sha256"]
REJECT_FIELDS = ["源图", "subject_original", "split", "stage", "原因",
                 "人工info框数", "预测info框数", "人工choice框数", "预测choice框数",
                 "人工fill框数", "预测fill框数"]


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def validate_parent_sources(raw, image, row, name):
    if not image.is_file():
        raise FileNotFoundError('m1工作父页缺失: ' + name)
    expected = row.get('working_sha256') or row.get('oriented_sha256')
    if raw.is_file():
        if sha256(raw) != row['raw_sha256']:
            raise ValueError('raw哈希不匹配: ' + name)
    elif not expected:
        raise ValueError('原始扫描已清理且缺少工作图指纹: ' + name)
    if expected and sha256(image) != expected:
        raise ValueError('m1图哈希不匹配: ' + name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, type=int, choices=(1, 2, 3, 4),
                    help="只处理固定分区中该stage的训练父页")
    ap.add_argument("--report-dir", help="诊断报告目录；默认放入D:/temp本次codex目录")
    ap.add_argument("--parent-list", type=Path, help="只处理CSV中new_name列指定的本stage父页")
    ap.add_argument('--review-list', type=Path, help='本批审核清单另存为data/m2内的新CSV，同stage可分批裁图')
    args = ap.parse_args()
    subset_path = (args.review_list or M2 / ('review_stage%d.csv' % args.stage)).resolve()
    if subset_path.parent != M2.resolve() or subset_path.suffix.lower() != '.csv':
        raise ValueError('审核清单必须是data/m2内的CSV')

    sampling = {r["new_name"]: r for r in read_csv(M1 / "sampling.csv")}
    splits = {r["new_name"]: r for r in read_csv(M1 / "splits.csv")}
    parents = []
    for name, row in sampling.items():
        split = splits.get(name)
        if (row.get("split") == "train" and split and split.get("split") == "train"
                and int(row.get("stage", -1)) == args.stage
                and int(split.get("stage", -1)) == args.stage):
            if row.get("subject_original") == "物理":
                raise ValueError("跨学科stage出现物理父页，拒绝处理: " + name)
            if any(row.get(k) != split.get(k) for k in ("split", "stage", "group", "subject_original")):
                raise ValueError("sampling与固定分区不一致: " + name)
            parents.append((name, row, split))
    if not parents:
        raise ValueError("该stage没有固定训练父页")
    if args.parent_list:
        requested = [r['new_name'] for r in read_csv(args.parent_list)]
        known = {name for name, _, _ in parents}
        if not requested or len(set(requested)) != len(requested) or set(requested) - known:
            raise ValueError('父页清单为空、重复或包含不属于本stage/train的页面')
        parents = [item for item in parents if item[0] in set(requested)]
    if subset_path.exists():
        raise FileExistsError('本stage审核清单已存在，拒绝覆盖')

    # Validate the full selected batch before running inference or writing data.
    for name, row, _ in parents:
        raw = RAW / (name + ".jpg")
        image = M1 / "images" / (name + ".jpg")
        validate_parent_sources(raw, image, row, name)
        st = events.load_state("m1", name)
        if not st["reviewed"] and not st["special"]:
            raise ValueError("父页尚未审核: " + name)

    # 执行时解析当前 m1 权重（登记优先，无登记回退 DEFAULTS）；导入时不加载模型
    from PIL import Image
    from ultralytics import YOLO

    weight = current_weight("m1")
    weight_hash = sha256(weight)
    model = YOLO(str(weight))
    pending_crops, rejected, subject_stats = [], [], Counter()
    for i, (name, row, split) in enumerate(parents, 1):
        subject = row["subject_original"]
        subject_stats[(subject, "parents")] += 1
        state = events.load_state("m1", name)
        if state["special"]:
            rejected.append({"源图": name, "subject_original": subject, "split": split["split"],
                             "stage": args.stage, "原因": "人工特殊页"})
            subject_stats[(subject, "rejected_special")] += 1
            continue

        with Image.open(M1 / "images" / (name + ".jpg")) as source:
            page_w, page_h = source.size
        manual = Counter()
        for box in state["boxes"]:
            if len(box) == 5 and int(box[0]) in (0, 1, 4):
                if float(box[3]) * page_w >= 16 and float(box[4]) * page_h >= 16:
                    manual[int(box[0])] += 1
        result = model(str(M1 / "images" / (name + ".jpg")), imgsz=1024,
                       conf=0.2, iou=0.5, verbose=False)[0]
        predicted = Counter()
        boxes = []
        for b in result.boxes:
            model_cls = int(b.cls[0])
            if model_cls not in (0, 1, 2):
                continue
            x1, y1, x2, y2 = map(float, b.xyxy[0].tolist())
            if x2 - x1 < 16 or y2 - y1 < 16:
                continue
            tool_cls = {0: 0, 1: 1, 2: 4}[model_cls]
            predicted[tool_cls] += 1
            boxes.append({"cls": tool_cls, "xyxy": (x1, y1, x2, y2),
                          "conf": float(b.conf[0])})
        counts_match = all(manual[c] == predicted[c] for c in (0, 1, 4))
        details = {"人工info框数": manual[0], "预测info框数": predicted[0],
                   "人工choice框数": manual[1], "预测choice框数": predicted[1],
                   "人工fill框数": manual[4], "预测fill框数": predicted[4]}
        if not counts_match:
            rejected.append({"源图": name, "subject_original": subject, "split": split["split"],
                             "stage": args.stage, "原因": "三类框数不一致", **details})
            subject_stats[(subject, "rejected_count_mismatch")] += 1
            continue
        if sum(manual[c] for c in (0, 1, 4)) == 0:
            rejected.append({"源图": name, "subject_original": subject, "split": split["split"],
                             "stage": args.stage, "原因": "无目标框", **details})
            subject_stats[(subject, "rejected_no_target")] += 1
            continue
        selected = [b for b in boxes if b["cls"] == 1]
        if not selected:
            subject_stats[(subject, "matched_without_choice")] += 1
            rejected.append({"源图": name, "subject_original": subject, "split": split["split"],
                             "stage": args.stage, "原因": "无选择题区", **details})
            continue
        img_path = M1 / "images" / (name + ".jpg")
        with Image.open(img_path) as source:
            im = source.convert("RGB")
            W, H = im.size
            for n, b in enumerate(selected, 1):
                x1f, y1f, x2f, y2f = b["xyxy"]
                x1, y1 = max(0, int(x1f)), max(0, int(y1f))
                x2, y2 = min(W, int(x2f + 0.999999)), min(H, int(y2f + 0.999999))
                if x2 <= x1 or y2 <= y1:
                    continue
                suffix = "" if n == 1 else ".%d" % n
                crop_stem = name + ".选择题区" + suffix
                pending_crops.append({
                    "stem": crop_stem, "parent": name, "subject_original": subject,
                    "split": split["split"], "stage": args.stage, "group": split["group"],
                    "raw_source": str(RAW / (name + ".jpg")),
                    "m1_source": str(img_path), "box": (x1, y1, x2, y2),
                    "conf": b["conf"], "image": im.crop((x1, y1, x2, y2)),
                    "weight_hash": weight_hash, "ordinal": n})
                subject_stats[(subject, "crops")] += 1
        if i % 20 == 0:
            print("现场推理", i, "/", len(parents), flush=True)

    # Fail closed if any target is already present. Existing crops/logs are never replaced.
    old_split_rows = read_csv(M2 / "splits.csv")
    old_split_names = {r["new_name"] for r in old_split_rows}
    for item in pending_crops:
        stem = item["stem"]
        if stem in old_split_names or (M2 / "images" / (stem + ".jpg")).exists() or events.has_events("m2", stem):
            raise FileExistsError("拒绝覆盖既有crop、分区或日志: " + stem)

    crop_path = M2 / "crop清单.csv"
    previous_crops = read_csv(crop_path) if crop_path.exists() else []
    crop_rows = [{f: r.get(f, "") for f in CROP_FIELDS} for r in previous_crops]
    reject_path = M2 / "舍弃清单.csv"
    previous_rejects = read_csv(reject_path) if reject_path.exists() else []
    reject_rows = [{f: r.get(f, "") for f in REJECT_FIELDS} for r in previous_rejects]
    split_rows = list(old_split_rows)
    created = []
    try:
        for item in pending_crops:
            name = item["stem"]
            img_path = M2 / "images" / (name + ".jpg")
            img_path.parent.mkdir(parents=True, exist_ok=True)
            item["image"].save(img_path, format="JPEG", quality=95)
            created.append(img_path)
            x1, y1, x2, y2 = item["box"]
            crop_rows.append({"源图": item["parent"], "区域": "选择题区", "序号": item["ordinal"],
                              "conf": "%.4f" % item["conf"], "宽": x2-x1, "高": y2-y1,
                              "文件名": name + ".jpg", "subject_original": item["subject_original"],
                              "split": item["split"], "stage": item["stage"], "group": item["group"],
                              "raw_source": item["raw_source"], "m1_source": item["m1_source"],
                              "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                              "m1_weight": str(weight.relative_to(ROOT)), "m1_sha256": weight_hash})
            split_rows.append({"new_name": name, "split": item["split"], "stage": item["stage"],
                               "group": item["group"], "subject_original": item["subject_original"]})
        for r in rejected:
            reject_rows.append({f: r.get(f, "") for f in REJECT_FIELDS})
        write_csv(M2 / "splits.csv", M2_SPLIT_FIELDS, split_rows)
        write_csv(crop_path, CROP_FIELDS, crop_rows)
        write_csv(reject_path, REJECT_FIELDS, reject_rows)

        write_csv(subset_path, ["new_name"], [{"new_name": x["stem"]} for x in pending_crops])
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise

    report_dir = Path(args.report_dir) if args.report_dir else Path("D:/temp") / (
        datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d-%H-%M") +
        "_codex_m2_stage%d裁图" % args.stage)
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "stage": args.stage, "parent_count": len(parents), "crop_count": len(pending_crops),
        "parent_list": str(args.parent_list) if args.parent_list else None,
        "parents": [name for name, _, _ in parents],
        "m1_weight": str(weight.relative_to(ROOT)), "m1_sha256": weight_hash,
        "subject_stats": {s: {k: v for (sub, k), v in subject_stats.items() if sub == s}
                          for s in sorted({r[1]["subject_original"] for r in parents})},
        "rejected_this_stage": rejected, "review_csv": str(subset_path),
    }
    (report_dir / ("crop_stage%d_report.json" % args.stage)).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["subject_stats"], ensure_ascii=False, indent=2))
    print("裁图:", len(pending_crops), "舍弃父页:", len(rejected), "审核清单:", report["review_csv"])
    print("报告:", report_dir / ("crop_stage%d_report.json" % args.stage))


if __name__ == "__main__":
    main()

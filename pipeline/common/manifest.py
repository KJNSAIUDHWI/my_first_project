# -*- coding: utf-8 -*-
"""manifest.csv / progress.csv 读写。"""
import os
import csv

MANIFEST_FIELDS = ["new_name", "batch", "subject", "source", "orig_name",
                   "session", "page", "dup_of", "has_label"]
PROGRESS_FIELDS = ["new_name", "status", "review_count", "last_review",
                   "reviewer", "boxes", "note"]


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def load_progress(path):
    rows = read_csv(path)
    return {r["new_name"]: r for r in rows}


def save_progress(path, by_name):
    write_csv(path, list(by_name.values()), PROGRESS_FIELDS)


def update_progress(path, name, status=None, review_count=None,
                    last_review=None, reviewer=None, boxes=None, note=None):
    by = load_progress(path)
    r = by.get(name) or {"new_name": name, "status": "未标", "review_count": "0",
                         "last_review": "", "reviewer": "", "boxes": "0", "note": ""}
    if status is not None:
        r["status"] = status
    if review_count is not None:
        r["review_count"] = str(review_count)
    if last_review is not None:
        r["last_review"] = last_review
    if reviewer is not None:
        r["reviewer"] = reviewer
    if boxes is not None:
        r["boxes"] = str(boxes)
    if note is not None:
        r["note"] = note
    by[name] = r
    save_progress(path, by)
    return r

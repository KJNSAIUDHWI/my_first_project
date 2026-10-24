# -*- coding: utf-8 -*-
"""从 data/choice 抽审核子集：A/B/C/D 各 100 + 物理多选 150。"""
import csv
import json
import os
import random
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'pipeline'))
from common import row_log  # noqa: E402
DATA = os.path.join(ROOT, "data", "choice")
MANIFEST = os.path.join(DATA, "manifest.csv")
OUT = os.path.join(DATA, "subset500.csv")
random.seed(42)


def pred_of(stem, manifest_rows):
    out = {}
    for r in manifest_rows:
        row = int(r["row"])
        name = "%s_%03d" % (stem, row)
        st = row_log.choice_state(row_log.read(DATA, name))
        out[row] = st["pred_answer"]
    return out


rows = []
by_stem = {}
with open(MANIFEST, encoding="utf-8", newline="") as f:
    for r in csv.DictReader(f):
        by_stem.setdefault(r["stem"], []).append(r)

for stem, rs in by_stem.items():
    ans = pred_of(stem, rs)
    for r in rs:
        rows.append({"stem": stem, "row": int(r["row"]), "no": int(r["no"]),
                     "n_opts": int(r["n_opts"]), "file": "%s_%03d.png" % (stem, int(r["row"])),
                     "answer": ans.get(int(r["row"]), "")})

pick = []
for letter in "ABCD":
    cand = [r for r in rows if r["answer"] == letter]
    pick += random.sample(cand, min(100, len(cand)))
    print(letter, "candidates", len(cand), "picked", min(100, len(cand)))

multi = [r for r in rows if len(r["answer"]) >= 2 and "\u7269\u7406" in r["stem"]]
pick += random.sample(multi, min(150, len(multi)))
print("physics multi candidates", len(multi), "picked", min(150, len(multi)))

seen = set()
final = []
for r in pick:
    k = (r["stem"], r["row"])
    if k in seen:
        continue
    seen.add(k)
    final.append(r)

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["stem", "row", "no", "n_opts", "file", "answer"])
    w.writeheader()
    w.writerows(final)

print("total", len(final), "->", OUT)
print("answer dist", Counter(r["answer"] for r in final).most_common())

# -*- coding: utf-8 -*-
"""从 data/choice 抽下一轮审核子集（排除已审行）。

默认：A/B/C/D 各 100 + 物理多选 200。
用法：py3136 tools\\涂黑审核\\make_round.py --out data\\choice\\subset_round2.csv
"""
import argparse
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


def state_of(stem, manifest_rows):
    out = {}
    for r in manifest_rows:
        row = int(r["row"])
        name = "%s_%03d" % (stem, row)
        st = row_log.choice_state(row_log.read(DATA, name))
        out[row] = {"answer": st["pred_answer"], "touched": st["reviewed"] or st["special"]}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA, "subset_round2.csv"))
    ap.add_argument("--per", type=int, default=100)
    ap.add_argument("--multi", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    random.seed(args.seed)

    by_stem = {}
    with open(MANIFEST, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            by_stem.setdefault(r["stem"], []).append(r)

    rows = []
    for stem, rs in by_stem.items():
        st = state_of(stem, rs)
        for r in rs:
            row = int(r["row"])
            s = st.get(row)
            if not s or s["touched"]:
                continue
            rows.append({"stem": stem, "row": row, "no": int(r["no"]),
                         "n_opts": int(r["n_opts"]),
                         "file": "%s_%03d.png" % (stem, row),
                         "answer": s["answer"]})
    print("unreviewed rows:", len(rows))

    pick = []
    for letter in "ABCD":
        cand = [r for r in rows if r["answer"] == letter]
        pick += random.sample(cand, min(args.per, len(cand)))
        print(letter, "cand", len(cand), "picked", min(args.per, len(cand)))

    multi = [r for r in rows if len(r["answer"]) >= 2 and "\u7269\u7406" in r["stem"]]
    pick += random.sample(multi, min(args.multi, len(multi)))
    print("multi cand", len(multi), "picked", min(args.multi, len(multi)))

    seen = set()
    final = []
    for r in pick:
        k = (r["stem"], r["row"])
        if k in seen:
            continue
        seen.add(k)
        final.append(r)

    with open(args.out, "x", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["stem", "row", "no", "n_opts", "file", "answer"])
        w.writeheader()
        w.writerows(final)
    print("total", len(final), "->", args.out)
    print("dist", Counter(r["answer"] for r in final).most_common())


if __name__ == "__main__":
    main()

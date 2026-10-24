# -*- coding: utf-8 -*-
"""用 choice 模型重新预标注还没审核的答题行。

每行一个日志：data/choice/log/<图名>.log。
已有审核 / 特殊 / 撤销记录的行不动；只给没有审核事件的行追加一条预标注。

用法：py3136 pipeline\\cnn\\predict_choice.py
"""
import csv
import os
import sys
from collections import Counter

import cv2
import numpy as np

PIPE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PIPE)
from common.paths import DATA  # noqa: E402
from common import row_log  # noqa: E402
from cnn.choice import ChoiceReader  # noqa: E402

DATA_DIR = DATA / "choice"
IMG_DIR = DATA_DIR / "images"
TOUCHED = {"审核", "review", "特殊", "special", "撤销审核", "unreview"}


def main():
    reader = ChoiceReader()
    manifest = {}
    with open(DATA_DIR / "manifest.csv", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            manifest.setdefault(r["stem"], []).append(r)

    updated_rows = 0
    updated_imgs = 0
    ans_dist = Counter()
    for stem, rows in manifest.items():
        new_rows = []
        for r in rows:
            row = int(r["row"])
            name = "%s_%03d" % (stem, row)
            events = row_log.read(DATA_DIR, name)
            if any(row_log.event_name(e) in TOUCHED for e in events):
                continue
            p = IMG_DIR / (name + ".png")
            g = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
            if g is None:
                continue
            ans, conf = reader.read(g)
            row_log.append(DATA_DIR, name, {
                "事件": "预标注", "题号": str(r["no"]), "预测答案": ans,
                "置信度": round(conf, 3),
                "说明": "预标注：第%s题预测%s" % (r["no"], ans or "空")})
            ans_dist[ans] += 1
            new_rows.append(row)
        if new_rows:
            updated_rows += len(new_rows)
            updated_imgs += 1
            if updated_imgs % 200 == 0:
                print("  %d imgs / %d rows" % (updated_imgs, updated_rows), flush=True)

    print("done: %d imgs, %d rows" % (updated_imgs, updated_rows))
    print("answer dist:", ans_dist.most_common(20))


if __name__ == "__main__":
    main()

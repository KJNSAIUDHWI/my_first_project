# -*- coding: utf-8 -*-
"""事件日志：每张图一个 data/<m>/log/<图名>.log，一行一个事件。

日志是唯一事实源；progress.csv 只是投影缓存，可随时用 rebuild_progress 重建。
日志字段全中文，程序内部仍用 boxes/reviewed 这些英文名，互不干扰。

事件：
  预标注     模型先猜一遍（模型、框、说明）
  审核       人工结论（第几轮、结果、变化、框）
  特殊       正常标注表达不了的情况（类型、备注）
  撤销审核   退回未审
  旋转       看图旋转，图片本身没转
  调整框     图片转正等操作后框跟着变，不算审核
  倾斜校正   校正完成记录，不改变框
  迁移       旧日志转成新格式，原值未改

旧日志里的 pred/save/review/special/unreview/rotate 仍能读，迁移后就不需要了。
"""
import os
import json
import glob
import csv
import threading
from datetime import datetime
from pathlib import Path

from .paths import m_dir, m_progress, ROOT

VERSION = 2
LOCK = threading.RLock()

CLASS_NAMES = {
    'm1': {0: '姓名学号', 1: '选择题区', 4: '填空题区'},
    'm2': {2: '题号', 3: '选项'},
    'm3': {5: '大题框', 6: '大题号'},
    'm4': {7: '小题号', 9: '填空行'},
}
CLASS_IDS = {name: cid for table in CLASS_NAMES.values() for cid, name in table.items()}

OLD_EVENTS = {'pred': '预标注', 'review': '审核', 'special': '特殊',
              'unreview': '撤销审核', 'rotate': '旋转', 'save': '调整框',
              'deskew': '倾斜校正', 'migrate': '迁移'}
NEW_EVENTS = {v: k for k, v in OLD_EVENTS.items() if k != 'migrate'}


def log_dir(m):
    return m_dir(m) / "log"


def log_path(m, stem):
    return log_dir(m) / (stem + ".log")


def old_log_path(m, stem):
    return log_dir(m) / (stem + ".jsonl")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_lines(path):
    from .records import read_events as read_strict
    return read_strict(path)


def read_events(m, stem):
    """读当前日志；还没升级的旧 .jsonl 原样返回，升级由首次写入完成。"""
    p = log_path(m, stem)
    if p.exists():
        return _read_lines(p)
    return _read_lines(old_log_path(m, stem))


def _class_name(m, cid):
    table = CLASS_NAMES.get(m, {})
    return table.get(int(cid), "编号%d" % int(cid))


def boxes_to_cn(m, boxes):
    """内部 [类别,中心X,中心Y,宽,高] -> 日志里的中文框。"""
    out = []
    for b in boxes:
        out.append({"类别": _class_name(m, b[0]), "类别编号": int(b[0]),
                    "中心X": round(float(b[1]), 6), "中心Y": round(float(b[2]), 6),
                    "宽": round(float(b[3]), 6), "高": round(float(b[4]), 6)})
    return out


def _cid_of(b):
    cid = b.get("类别编号")
    if cid is not None:
        return int(cid)
    name = b.get("类别")
    if name in CLASS_IDS:
        return CLASS_IDS[name]
    if isinstance(name, str) and name.startswith("编号") and name[2:].isdigit():
        return int(name[2:])
    if isinstance(name, str) and name.isdigit():
        return int(name)
    return None


def boxes_from_cn(m, rows):
    out = []
    for b in rows or []:
        cid = _cid_of(b)
        if cid is None:
            continue
        try:
            out.append([cid, float(b["中心X"]), float(b["中心Y"]),
                        float(b["宽"]), float(b["高"])])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _boxes_of(m, ev):
    if "框" in ev:
        return boxes_from_cn(m, ev["框"])
    return ev.get("boxes", [])


def _event_of(ev):
    name = ev.get("事件")
    if name in NEW_EVENTS:
        return NEW_EVENTS[name]
    old = ev.get("e", "")
    return old


def _time_of(ev):
    return ev.get("时间") or ev.get("t", "")


def _counts_text(m, boxes):
    order = []
    counts = {}
    for b in boxes:
        name = _class_name(m, b[0])
        if name not in counts:
            order.append(name)
            counts[name] = 0
        counts[name] += 1
    return "、".join("%s %d 个" % (name, counts[name]) for name in order)


def _position_text(m, boxes, index):
    cid = int(boxes[index][0])
    name = _class_name_guess(cid) if m is None else _class_name(m, cid)
    n = sum(1 for b in boxes[:index + 1] if int(b[0]) == cid)
    return "%s第%d个" % (name, n)


def _cn_box(m, b):
    cid = int(b[0])
    return {"类别": _class_name_guess(cid) if m is None else _class_name(m, cid),
            "类别编号": cid,
            "中心X": round(float(b[1]), 6), "中心Y": round(float(b[2]), 6),
            "宽": round(float(b[3]), 6), "高": round(float(b[4]), 6)}


def _iou(a, b):
    ax1, ay1 = a[1] - a[3] / 2, a[2] - a[4] / 2
    ax2, ay2 = a[1] + a[3] / 2, a[2] + a[4] / 2
    bx1, by1 = b[1] - b[3] / 2, b[2] - b[4] / 2
    bx2, by2 = b[1] + b[3] / 2, b[2] + b[4] / 2
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    union = a[3] * a[4] + b[3] * b[4] - inter
    return inter / union if union > 0 else 0.0


def _same(a, b, eps=0.001):
    return int(a[0]) == int(b[0]) and all(abs(float(a[k]) - float(b[k])) <= eps for k in (1, 2, 3, 4))


def _change_detail(m, old, new, iou_thr=0.3, eps=0.001):
    """按最终结果和基线比，列出改了什么。"""
    used = [False] * len(new)
    out = []
    for i, o in enumerate(old):
        best, bi = 0.0, -1
        for j, n in enumerate(new):
            if used[j]:
                continue
            score = _iou(o, n)
            if score > best:
                best, bi = score, j
        if bi < 0 or best < iou_thr:
            out.append({"动作": "删除", "哪个框": _position_text(m, old, i),
                        "改前": _cn_box(m, o)})
            continue
        used[bi] = True
        n = new[bi]
        if _same(o, n, eps):
            continue
        actions = []
        if int(o[0]) != int(n[0]):
            actions.append("改类别")
        if abs(float(o[3]) - float(n[3])) > eps or abs(float(o[4]) - float(n[4])) > eps:
            actions.append("缩放")
        if abs(float(o[1]) - float(n[1])) > eps or abs(float(o[2]) - float(n[2])) > eps:
            actions.append("移动")
        out.append({"动作": "并".join(actions),
                    "哪个框": _position_text(m, old, i),
                    "改前": _cn_box(m, o), "改后": _cn_box(m, n)})
    for j, n in enumerate(new):
        if not used[j]:
            out.append({"动作": "新增", "哪个框": _position_text(m, new, j),
                        "改后": _cn_box(m, n)})
    return out


def _class_name_guess(cid):
    for table in CLASS_NAMES.values():
        if int(cid) in table:
            return table[int(cid)]
    return "编号%d" % int(cid)


def _last_pred_boxes(events):
    base = []
    for e in events:
        if _event_of(e) == 'pred':
            base = _boxes_of(None, e)
    return base


def _describe(m, ev):
    kind = _event_of(ev)
    if kind == 'pred':
        return "预标注：" + (_counts_text(m, _boxes_of(m, ev)) or "没有框")
    if kind == 'review':
        n = ev.get("第几轮") or ev.get("n") or 1
        result = ev.get("结果") or ("人工修正" if ev.get("kind") == "fixed" else "预标注正确")
        return "第%d次审核：%s" % (int(n), result)
    if kind == 'special':
        return "特殊：" + (ev.get("类型") or ev.get("note") or ev.get("备注") or "特殊情况")
    if kind == 'unreview':
        n = ev.get("撤销第几轮") or ev.get("n") or 0
        return "撤销第%d次审核" % int(n) if n else "撤销审核"
    if kind == 'rotate':
        return "看图旋转 %d 度，图片本身没转" % int(ev.get("角度", ev.get("deg", 0)))
    if kind == 'save':
        return "调整框：" + (_counts_text(m, _boxes_of(m, ev)) or "无框")
    return ev.get("说明", "")


def _review_summary(changes, final_ok=False):
    if not changes:
        return "预标注正确" if final_ok else "确认当前结果（与预标注不同）"
    text = "；".join(c.get("动作", "") + c.get("哪个框", "") for c in changes)
    if final_ok:
        return "改回预标注，结果预标注正确（本次变化：%s）" % text
    return text


def _upgrade_events(m, events):
    """把旧英文日志转成中文格式。只翻译字段，不补造当时没记录的变化。"""
    out = []
    count = 0
    for e in events:
        kind = _event_of(e)
        if kind not in OLD_EVENTS:
            out.append(e)
            continue
        ev = {"时间": _time_of(e).replace("T", " "), "事件": OLD_EVENTS[kind]}
        boxes = _boxes_of(m, e)
        if kind == 'pred':
            ev["模型"] = e.get("model", "")
            ev["框"] = boxes_to_cn(m, boxes)
        elif kind == 'deskew':
            ev.update({'角度': e.get('deg', 0), '检测角度': e.get('detected', 0),
                       '方法': e.get('method', '')})
        elif kind == 'save':
            ev["框"] = boxes_to_cn(m, boxes)
        elif kind == 'review':
            count += 1
            ev["第几轮"] = int(e.get("n", count))
            ev["结果"] = "人工修正" if e.get("kind") == "fixed" else "预标注正确"
            ev["框"] = boxes_to_cn(m, boxes)
        elif kind == 'special':
            ev["类型"] = e.get("note") or "其他"
            ev["备注"] = e.get("note") or ""
            if "boxes" in e:
                ev["框"] = boxes_to_cn(m, boxes)
        elif kind == 'unreview':
            ev["撤销第几轮"] = int(e.get("n", count))
            count = max(0, count - 1)
        elif kind == 'rotate':
            ev["角度"] = int(e.get("deg", 0)) % 360
        ev["说明"] = _describe(m, ev)
        out.append(ev)
    return out


def append_event(m, stem, event):
    """追加一条事件，返回实际写入的中文事件；无变化时不重复记账，返回 None。"""
    kind = event.get("e") or event.get("事件")
    kind = NEW_EVENTS.get(kind, kind)
    with LOCK:
        p = log_path(m, stem)
        old = old_log_path(m, stem)
        os.makedirs(log_dir(m), exist_ok=True)
        if not p.exists() and old.exists():
            events = _upgrade_events(m, _read_lines(old))
            with open(p, "w", encoding="utf-8-sig", newline="\n") as f:
                for e in events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        else:
            events = read_events(m, stem)
        st = project(events, m)
        ev = {"时间": _now(), "事件": OLD_EVENTS.get(kind, kind)}
        if kind == 'pred':
            boxes = event.get("boxes", [])
            ev["模型"] = event.get("model", "")
            if event.get("model_path"):
                try:
                    ev["模型路径"] = str(Path(event["model_path"]).resolve().relative_to(ROOT))
                except (ValueError, OSError):
                    ev["模型路径"] = str(event["model_path"])
            ev["框"] = boxes_to_cn(m, boxes)
            ev["说明"] = _describe(m, ev)
        elif kind == 'review':
            boxes = event.get("boxes", st["boxes"])
            changes = _change_detail(m, st["boxes"], boxes)
            if st["reviewed"] and not st["special"] and not changes:
                return None
            n = sum(1 for e in events if _event_of(e) == 'review') + 1
            final = _change_detail(m, _last_pred_boxes(events), boxes)
            ev["第几轮"] = n
            ev["结果"] = "人工修正" if final else "预标注正确"
            ev["变化"] = changes
            ev["框"] = boxes_to_cn(m, boxes)
            ev["说明"] = "第%d次审核：%s" % (n, _review_summary(changes, not final))
        elif kind == 'special':
            ev["类型"] = event.get("type") or event.get("note") or event.get("备注") or "其他"
            ev["备注"] = event.get("note") or event.get("备注") or ""
            if "boxes" in event:
                ev["框"] = boxes_to_cn(m, event["boxes"])
            ev["说明"] = _describe(m, ev)
        elif kind == 'unreview':
            ev["撤销第几轮"] = int(event.get("n", st["review_count"]))
            ev["说明"] = _describe(m, ev)
        elif kind == 'rotate':
            ev["角度"] = int(event.get("deg", event.get("rotation", 0))) % 360
            ev["说明"] = _describe(m, ev)
        elif kind == 'deskew':
            ev.update({'角度': event.get('deg', 0), '检测角度': event.get('detected', 0),
                       '方法': event.get('method', ''), '说明': '倾斜校正已完成'})
        elif kind == 'save':
            boxes = event.get("boxes", [])
            ev["框"] = boxes_to_cn(m, boxes)
            ev["说明"] = _describe(m, ev)
        elif kind in ('migrate', '迁移'):
            ev.update({k: v for k, v in event.items() if k not in ('e', 't', 'v')})
            ev["说明"] = event.get("说明", "从旧格式转换，原值未改")
        else:
            ev.update({k: v for k, v in event.items() if k not in ('e', 't', 'v')})
        os.makedirs(log_dir(m), exist_ok=True)
        new = not p.exists() or p.stat().st_size == 0
        with open(p, "a", encoding="utf-8-sig" if new else "utf-8", newline="\n") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def _same_boxes(a, b, eps=0.001):
    if len(a) != len(b):
        return False
    return all(_same(x, y, eps) for x, y in zip(a, b))


def project(events, m=None):
    st = {"boxes": [], "reviewed": False, "review_count": 0, "kind": "",
          "special": False, "note": "", "rotation": 0, "last": "", "reviewed_at": ""}
    for e in events:
        t = _event_of(e)
        st["last"] = _time_of(e) or st["last"]
        if t in ("pred", "save"):
            boxes = _boxes_of(m, e)
            if t == "pred" or boxes != st["boxes"]:
                st["reviewed"] = False
                st["reviewed_at"] = ""
            st["boxes"] = boxes
        elif t == "review":
            if "框" in e or "boxes" in e:
                st["boxes"] = _boxes_of(m, e)
            st["reviewed"] = True
            st["review_count"] = int(e.get("第几轮", e.get("n", st["review_count"] + 1)))
            st["kind"] = "fixed" if e.get("结果") == "人工修正" else e.get("kind", "correct")
            st["special"] = False
            st["note"] = ""
            st["reviewed_at"] = _time_of(e)
        elif t == "special":
            st["special"] = True
            st["note"] = e.get("note") or e.get("备注") or e.get("类型") or ""
        elif t == "unreview":
            st["reviewed"] = False
            st["special"] = False
            st["note"] = ""
            st["kind"] = ""
            st["reviewed_at"] = ""
        elif t == "rotate":
            st["rotation"] = int(e.get("角度", e.get("deg", 0))) % 360
    return st


def load_state(m, stem):
    return project(read_events(m, stem), m)


def diff_boxes(old, new, iou_thr=0.3, eps=0.001):
    """按最终结果统计 (调整, 新增, 删除)，与日志里的变化口径一致。"""
    detail = _change_detail(None, old, new, iou_thr, eps)
    moved = sum(1 for c in detail if c["动作"] not in ("新增", "删除"))
    added = sum(1 for c in detail if c["动作"] == "新增")
    deleted = sum(1 for c in detail if c["动作"] == "删除")
    return moved, added, deleted


def edit_stats(m, stem, evs=None):
    """相对最后一次预标注，人工改了多少：调整 / 新增 / 删除。"""
    evs = read_events(m, stem) if evs is None else evs
    base = _last_pred_boxes(evs)
    cur = project(evs, m)["boxes"]
    moved, added, deleted = diff_boxes(base, cur)
    return {"moved": moved, "added": added, "deleted": deleted,
            "total": moved + added + deleted}


def has_events(m, stem):
    return log_path(m, stem).exists() or old_log_path(m, stem).exists()


def migrate_log(m, stem):
    """把旧 .jsonl 转成中文 .log；已有 .log 或旧文件不存在时返回 None。"""
    p, old = log_path(m, stem), old_log_path(m, stem)
    if p.exists() or not old.exists():
        return None
    with LOCK:
        evs = _upgrade_events(m, _read_lines(old))
        os.makedirs(log_dir(m), exist_ok=True)
        with open(p, "w", encoding="utf-8-sig", newline="\n") as f:
            for e in evs:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return len(evs)


def status_of(st, nboxes):
    if st["special"]:
        return "特殊"
    if st["reviewed"]:
        return "已审-修正" if st["kind"] == "fixed" else "已审-正确"
    return "已标" if nboxes else "未标"


PROGRESS_FIELDS = ["new_name", "status", "review_count", "last_review",
                   "reviewer", "boxes", "note"]


def _progress_row(stem, st):
    return {"new_name": stem, "status": status_of(st, len(st["boxes"])),
            "review_count": str(st["review_count"]),
            "last_review": st["reviewed_at"] if st["reviewed"] else "",
            "reviewer": "", "boxes": str(len(st["boxes"])), "note": st["note"]}


def rebuild_progress(m):
    by = {}
    paths = glob.glob(str(log_dir(m) / "*.log")) + glob.glob(str(log_dir(m) / "*.jsonl"))
    for p in paths:
        stem = os.path.splitext(os.path.basename(p))[0]
        if stem in by and p.lower().endswith(".jsonl"):
            continue
        by[stem] = _progress_row(stem, project(read_events(m, stem), m))
    p = m_progress(m)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PROGRESS_FIELDS)
        w.writeheader()
        for r in by.values():
            w.writerow({k: r.get(k, "") for k in PROGRESS_FIELDS})
    return by


def sync_progress(m, stem):
    st = load_state(m, stem)
    by = read_progress(m)
    by[stem] = _progress_row(stem, st)
    write_progress(m, by)
    return by[stem]


def read_progress(m):
    by = {}
    p = m_progress(m)
    if os.path.exists(p):
        with open(p, encoding="utf-8-sig", errors="replace", newline="") as f:
            for r in csv.DictReader(f):
                name = (r.get("new_name") or "").strip()
                if name:
                    by[name] = r
    return by


def write_progress(m, by):
    p = str(m_progress(m))
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PROGRESS_FIELDS)
        w.writeheader()
        for r in by.values():
            w.writerow({k: r.get(k, "") for k in PROGRESS_FIELDS})
    os.replace(tmp, p)


def to_text(boxes):
    return "".join("%d %.6f %.6f %.6f %.6f\n" % tuple(b) for b in boxes)


def from_text(text):
    out = []
    for ln in text.splitlines():
        q = ln.split()
        if len(q) >= 5:
            try:
                out.append([int(q[0]), float(q[1]), float(q[2]), float(q[3]), float(q[4])])
            except ValueError:
                pass
    return out

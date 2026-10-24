# -*- coding: utf-8 -*-
"""交接协议回归验证（现行场景A/B/E/F/H）。

在指定的 D:\\temp 工作目录里搭建模拟项目（复制本项目源码），检查编号迁移、
归档幂等、模拟窗口交接、替代规则和目录边界。改造前的数据/页面场景
C/D/G/I保留供追溯，不再由此入口执行；当前CNN数据回归见test_data_flow.py。
全部模拟产物都留在工作目录，不写真实 data/models/train，不启动服务或训练。

用法：
  py3136 -B tools\\验证交接与准入.py --workdir D:\\temp\\<本次codex目录>\\验收
"""
import argparse
import hashlib
import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
TEMP_ROOT = Path('D:/temp')
CODEX_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}_codex_.+$')
RESULTS = {}


def check(case, cond, detail=""):
    RESULTS[case] = {"pass": bool(cond), "detail": str(detail)[:300]}
    print(("PASS" if cond else "FAIL"), case, str(detail)[:150])


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def write_events(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def validate_workdir(path):
    """Resolve and approve the codex-owned scratch directory before any writes."""
    supplied = Path(path)
    if '..' in supplied.parts:
        raise ValueError('workdir 不得含有 .. 路径段')
    try:
        work = supplied.resolve()
        temp_root = TEMP_ROOT.resolve()
        project = PROJ.resolve()
    except (OSError, RuntimeError) as e:
        raise ValueError('workdir 无法解析：%s' % e) from e
    try:
        relative = work.relative_to(temp_root)
    except ValueError as e:
        raise ValueError('workdir 必须真实位于 D:/temp 下') from e
    if not relative.parts or not CODEX_DIR_RE.fullmatch(relative.parts[0]):
        raise ValueError('workdir 必须位于带日期的 D:/temp/*_codex_* 专属目录内')
    if _overlaps(work, project):
        raise ValueError('workdir 与项目目录重叠')
    return work


def validate_fixture_target(work, name):
    """Independently constrain each fixture path before mkdir/copy/rmtree."""
    safe_work = validate_workdir(work)
    relative_name = Path(name)
    if '..' in relative_name.parts:
        raise ValueError('fixture 名称不得含有 .. 路径段')
    try:
        target = (safe_work / relative_name).resolve()
    except (OSError, RuntimeError) as e:
        raise ValueError('fixture 目标无法解析：%s' % e) from e
    if target.parent != safe_work:
        raise ValueError('fixture 目标必须是 workdir 的直接子目录')
    if _overlaps(target, PROJ.resolve()):
        raise ValueError('fixture 目标与项目目录重叠')
    validate_workdir(target)
    return target


COMMON_FILES = ["pipeline/common/__init__.py", "pipeline/common/events.py",
                "pipeline/common/paths.py", "pipeline/common/records.py",
                "pipeline/common/fixed_dataset.py", "pipeline/common/labels.py",
                "pipeline/common/yolo_snapshot.py", "pipeline/common/cnn_data.py",
                "pipeline/common/row_log.py", "pipeline/common/choice_events.py",
                "pipeline/common/model_registry.py"]


def make_fixture(work, name, extra=()):
    root = validate_fixture_target(work, name)
    if root.exists():
        shutil.rmtree(root)
    for rel in list(COMMON_FILES) + list(extra):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJ / rel, dst)
    (root / "docs").mkdir(exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    # common.* 的模块常量（DATA/TRAIN）在 import 时绑定 fixture 路径，
    # 换 fixture 必须清缓存，否则后续场景仍指向旧目录
    for k in list(sys.modules):
        if k == "common" or k.startswith("common."):
            del sys.modules[k]
    sys.path[:] = [p for p in sys.path
                   if not (p.endswith("pipeline") and Path(p).parent.parent == work)]
    sys.path.insert(0, str(root / "pipeline"))
    common_paths = importlib.import_module("common.paths")
    if Path(common_paths.ROOT).resolve() != root.resolve():
        raise RuntimeError("common.paths.ROOT 未指向当前隔离副本：%s" % common_paths.ROOT)
    return root


def handoff_tool(fixture, tag):
    mod = load_mod("xk_%s" % tag, fixture / "tools/写快照.py")
    mod.ROOT = fixture
    mod.TARGET = fixture / "docs/01-接力记录.txt"
    mod.HISTORY = fixture / "docs/06-历史.md"
    mod.LOCK_PATH = fixture / "tools/.handoff.lock"
    return mod


def make_body(S=None, D=None, Q=None, rec=None, ev=None, closed=None):
    def sec(name, entries):
        if not entries:
            return [name + "：", "无"]
        return [name + "："] + ["- " + e for e in entries]

    def numbered(prefix, pairs):
        return ["[%s%03d] %s" % (prefix, i, t) for i, t in (pairs or [])]

    def close_entries(items):
        out = []
        for it in items or []:
            for i, t in it[1:]:
                out.append("[%s%03d] %s" % (it[0], i, t))
        return out

    lines = ["接力结构：2", ""]
    lines += sec("当前状态", numbered("S", S)) + [""]
    lines += sec("有效决定与限制", numbered("D", D)) + [""]
    lines += sec("未决问题", numbered("Q", Q)) + [""]
    lines += sec("本轮必要记录", rec or ["无"]) + [""]
    lines += sec("证据", ev or ["无"])
    if closed:
        lines += [""] + sec("关闭或替代", close_entries(closed))
    return "\n".join(lines)


HISTORY_SKELETON = ("# 06-历史\n\n> 说明\n\n## 一、接力日志\n"
                    "## 二、旧文档存档\n\n旧内容。\n")


# ================= A/B 接力与归档 =================

def run_AB(work):
    fx = make_fixture(work, "ab", extra=["tools/写快照.py"])
    (fx / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    t = handoff_tool(fx, "ab")

    # A1 同会话漏 D
    t.run_write("s1", make_body(S=[(1, "状态一")], D=[(1, "决定一")], Q=[(1, "问题一")]), "missing")
    before = (sha_file(t.TARGET), sha_file(t.HISTORY))
    try:
        t.run_write("s1", make_body(S=[(1, "状态一更新")], Q=[(1, "问题一")]), sha_file(t.TARGET))
        check("A1", False, "漏D被接受")
    except t.HandoffError as e:
        check("A1", "D001" in str(e)
              and before == (sha_file(t.TARGET), sha_file(t.HISTORY)), str(e))
    # A2 换会话漏 D/Q
    try:
        t.run_write("s2", make_body(S=[(1, "状态一")]), sha_file(t.TARGET))
        check("A2", False, "被接受")
    except t.HandoffError as e:
        check("A2", "D001" in str(e) and "Q001" in str(e), str(e))
    # A3 改义拒绝 / 关闭接受
    base3 = dict(S=[(1, "状态一")], Q=[(1, "问题一")])
    try:
        t.run_write("s1", make_body(D=[(1, "决定一（被偷偷改义）")], **base3), sha_file(t.TARGET))
        check("A3改义", False, "被接受")
    except t.HandoffError as e:
        check("A3改义", "改义" in str(e), str(e))
    t.run_write("s1", make_body(D=[(2, "决定一v2：范围扩大")],
                                closed=[("D", (1, "用户调整范围，由D002替代"))], **base3),
                sha_file(t.TARGET))
    txt = t.TARGET.read_text(encoding="utf-8")
    check("A3关闭", "[D002]" in txt and "[D001] 用户调整范围" in txt, "新D+关闭说明")
    # A4 S消失与关闭说明
    base4 = dict(S=[(1, "状态一"), (2, "状态二")], Q=[(1, "问题一")],
                 D=[(2, "决定一v2：范围扩大")],
                 closed=[("D", (1, "用户调整范围，由D002替代"))])
    t.run_write("s1", make_body(**base4), sha_file(t.TARGET))
    try:
        t.run_write("s1", make_body(S=[(1, "状态一")], Q=[(1, "问题一")],
                                    D=[(2, "决定一v2：范围扩大")],
                                    closed=[("D", (1, "用户调整范围，由D002替代"))]),
                    sha_file(t.TARGET))
        check("A4S消失", False, "被接受")
    except t.HandoffError as e:
        check("A4S消失", "S002" in str(e), str(e))
    t.run_write("s1", make_body(S=[(1, "状态一")], Q=[(1, "问题一")],
                                D=[(2, "决定一v2：范围扩大")],
                                closed=[("D", (1, "用户调整范围，由D002替代")),
                                        ("S", (2, "快照重建后过期"))]),
                sha_file(t.TARGET))
    try:
        t.run_write("s1", make_body(S=[(1, "状态一")], Q=[(1, "问题一")],
                                    D=[(2, "决定一v2：范围扩大")],
                                    closed=[("D", (1, "用户调整范围，由D002替代"))]),
                    sha_file(t.TARGET))
        check("A4关闭保留", False, "丢失关闭说明被接受")
    except t.HandoffError as e:
        check("A4关闭保留", "S002" in str(e), str(e))
    # A5 破坏正文与文件
    broken = ["无结构文字", make_body(S=[(1, "a"), (1, "b")]), make_body(rec=["<!-- handoff:end -->"])]
    ok = True
    for b in broken:
        try:
            t.validate_note("s1", b)
            ok = False
        except t.HandoffError:
            pass
    check("A5正文", ok, "结构破坏均被拒")
    # A6 同哈希双写
    h = sha_file(t.TARGET)
    common6 = dict(S=[(1, "状态一")], Q=[(1, "问题一")], D=[(2, "决定一v2：范围扩大")],
                   closed=[("D", (1, "用户调整范围，由D002替代")),
                           ("S", (2, "快照重建后过期"))])
    na = make_body(rec=["A"], **common6)
    nb = make_body(rec=["B"], **common6)
    t.run_write("s1", na, h)
    try:
        t.run_write("s1", nb, h)
        check("A6", False, "旧哈希二次写入被接受")
    except t.HandoffError as e:
        check("A6", e.stage.startswith("阶段2"), str(e))
    # B7 同会话普通更新不归档
    fx2 = make_fixture(work, "b7", extra=["tools/写快照.py"])
    (fx2 / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    t2 = handoff_tool(fx2, "b7")
    t2.run_write("s1", make_body(S=[(1, "v1")]), "missing")
    t2.run_write("s1", make_body(S=[(1, "v2")]), sha_file(t2.TARGET))
    check("B7", "v1" not in (fx2 / "docs/06-历史.md").read_text(encoding="utf-8"),
          "中间版本未归档")
    # B8 handoff 幂等
    r1 = t2.run_write("s1", make_body(S=[(1, "v3")], rec=["版本甲"]), sha_file(t2.TARGET), handoff=True)
    hist1 = (fx2 / "docs/06-历史.md").read_text(encoding="utf-8")
    r2 = t2.run_write("s1", make_body(S=[(1, "v3")], rec=["版本甲"]), sha_file(t2.TARGET), handoff=True)
    check("B8", r2["idempotent"] and r2["updated"] == r1["updated"]
          and (fx2 / "docs/06-历史.md").read_text(encoding="utf-8") == hist1,
          "幂等且不重复归档")
    # B9 新会话不重复归档
    t2.run_write("s2", make_body(S=[(1, "v3"), (2, "s2新增")]), sha_file(t2.TARGET))
    check("B9", (fx2 / "docs/06-历史.md").read_text(encoding="utf-8").count("版本甲") == 1,
          "交接版仍只一份")
    # B10 同session新正文归档新版
    t2.run_write("s2", make_body(S=[(1, "v3"), (2, "s2新增")], rec=["版本乙"]),
                 sha_file(t2.TARGET), handoff=True)
    hist = (fx2 / "docs/06-历史.md").read_text(encoding="utf-8")
    check("B10", hist.count("版本甲") == 1 and hist.count("版本乙") == 1, "两版各一份")
    # B11 归档后01失败重试
    fx3 = make_fixture(work, "b11", extra=["tools/写快照.py"])
    (fx3 / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    t3 = handoff_tool(fx3, "b11")
    t3.run_write("s1", make_body(S=[(1, "旧版")]), "missing")
    h3 = sha_file(t3.TARGET)
    real_atomic = t3.atomic_text
    calls = {"n": 0}

    def flaky(path, text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("注入写01失败")
        return real_atomic(path, text)

    t3.atomic_text = flaky
    stage = ""
    try:
        t3.run_write("s2", make_body(S=[(1, "旧版"), (2, "新")]), h3)
    except t3.HandoffError as e:
        stage = e.stage
    t3.atomic_text = real_atomic
    t3.run_write("s2", make_body(S=[(1, "旧版"), (2, "新")]), h3)
    check("B11", stage.startswith("阶段4")
          and (fx3 / "docs/06-历史.md").read_text(encoding="utf-8").count("旧版") == 1,
          "重试后旧版仅一份（%s）" % stage)
    # B12 01成功归档失败
    fx4 = make_fixture(work, "b12", extra=["tools/写快照.py"])
    (fx4 / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    t4 = handoff_tool(fx4, "b12")
    real_atomic = t4.atomic_text
    calls["n"] = 0

    def flaky2(path, text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("注入归档失败")
        return real_atomic(path, text)

    t4.atomic_text = flaky2
    stage5 = ""
    try:
        t4.run_write("s1", make_body(S=[(1, "交接内容")]), "missing", handoff=True)
    except t4.HandoffError as e:
        stage5 = str(e)
    h4 = sha_file(t4.TARGET)
    t4.atomic_text = real_atomic
    r = t4.run_write("s1", make_body(S=[(1, "交接内容")]), h4, handoff=True)
    check("B12", "01已保存，交接归档未完成" in stage5 and r["idempotent"]
          and (fx4 / "docs/06-历史.md").read_text(encoding="utf-8").count("交接内容") == 1,
          stage5[:60])
    # B13 迁移
    legacy = ("当前接力：先读本条，了解进度、限制和用户决定。写新接力前保留仍有效的状态。\n"
              "本条不是任务书；下一步按用户指令执行。旧记录见 docs/06-历史.md。\n"
              "本文件由 tools/写快照.py 生成；写入方法见 docs/05-迭代操作.md。\n\n\n"
              "──── 会话 old · 创建 2026-10-23 10:00:00 · 更新 2026-10-23 11:00:00 ────\n"
              "旧格式正文标题\n旧正文细节\n<!-- handoff:end -->\n")
    fx5 = make_fixture(work, "b13", extra=["tools/写快照.py"])
    (fx5 / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    (fx5 / "docs/01-接力记录.txt").write_text(legacy, encoding="utf-8")
    t5 = handoff_tool(fx5, "b13")
    info = t5.inspect()
    hl = sha_file(t5.TARGET)
    try:
        t5.run_write("s-new", make_body(S=[(1, "迁移后")]), hl)
        ok_no_migrate = False
    except t5.HandoffError:
        ok_no_migrate = True
    t5.run_write("s-new", make_body(S=[(1, "迁移后")]), hl, migrate=True)
    hist = (fx5 / "docs/06-历史.md").read_text(encoding="utf-8")
    try:
        t5.run_write("s-new", make_body(S=[(1, "迁移后")]), sha_file(t5.TARGET), migrate=True)
        again = False
    except t5.HandoffError:
        again = True
    check("B13", info["struct"] == 1 and ok_no_migrate and "旧正文细节" in hist
          and hist.count("旧正文细节") == 1 and again,
          "旧正文完整归档一次且迁移开关不能复用")
    # B14 零写
    fx6 = make_fixture(work, "b14", extra=["tools/写快照.py"])
    (fx6 / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    t6 = handoff_tool(fx6, "b14")
    t6.run_write("s1", make_body(S=[(1, "v1")]), "missing")
    t6.LOCK_PATH.unlink()
    before = {str(p.relative_to(fx6)): sha_file(p) for p in fx6.rglob("*") if p.is_file()}
    d = t6.run_dry("s1", make_body(S=[(1, "v1"), (2, "v2")]), sha_file(t6.TARGET))
    after = {str(p.relative_to(fx6)): sha_file(p) for p in fx6.rglob("*") if p.is_file()}
    check("B14", before == after and not t6.LOCK_PATH.exists() and d["ok"],
          "只读模式零写入")


# ================= C 审核准入 =================

BOX = [2, 0.5, 0.5, 0.4, 0.3]


def setup_detect(fx, binding=True):
    from PIL import Image
    def jpeg(p, size, color):
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, color).save(p, format="JPEG", quality=90)
    jpeg(fx / "data/m2/images/t1.jpg", (60, 80), (120, 140, 160))
    jpeg(fx / "data/m2/images/v1.jpg", (50, 70), (90, 90, 90))
    write_events(fx / "data/m2/log/t1.jsonl", [
        {"e": "pred", "t": "2026-10-24T00:00:00", "boxes": [BOX]},
        {"e": "review", "t": "2026-10-24T00:01:00", "kind": "correct", "n": 1,
         "boxes": [BOX], **({"image_sha256": sha_file(fx / "data/m2/images/t1.jpg")} if binding else {})}])
    write_events(fx / "data/m2/log/v1.jsonl", [
        {"e": "pred", "t": "2026-10-24T00:00:00", "boxes": [BOX]},
        {"e": "review", "t": "2026-10-24T00:01:00", "kind": "correct", "n": 1,
         "boxes": [BOX], "image_sha256": sha_file(fx / "data/m2/images/v1.jpg")}])
    (fx / "data/m2/splits.csv").write_text(
        "new_name,split,stage,group,subject_original\nt1,train,1,g1,物理\nv1,val,0,g2,物理\n",
        encoding="utf-8-sig")
    vd = fx / "train/testrun/dataset_snapshot/images/val"
    vd.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fx / "data/m2/images/v1.jpg", vd / "v1.jpg")
    return hashlib.sha256("v1".encode()).hexdigest()


class FakeHandler:
    def __init__(self, path, body=None):
        self.path = path
        raw = json.dumps(body).encode() if body is not None else b""
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    def body(self):
        try:
            return json.loads(self.wfile.getvalue().decode("utf-8"))
        except ValueError:
            return {}


def run_C(work):
    fx = make_fixture(work, "c15", extra=["tools/标注工具/serve.py"])
    setup_detect(fx)
    serve = load_mod("serve_v", fx / "tools/标注工具/serve.py")
    serve.STATE["data"] = str(fx / "data/m2")

    def api(path, body=None):
        h = FakeHandler(path, body)
        h._json = types.MethodType(serve.Handler._json, h)
        if body is None:
            serve.Handler.do_GET(h)
        else:
            serve.Handler._do_post(h)
        return h.status, h.body()

    code, lab = api("/api/label?stem=t1")
    fp = lab.get("image_sha256", "")
    log = fx / "data/m2/log/t1.jsonl"
    reviews = lambda: [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
                       if json.loads(l).get("e") == "review"]
    n0 = len(reviews())
    ok_all = True
    detail = []
    for name, body in [("空指纹", {"displayed_sha256": ""}),
                       ("错图指纹", None)]:
        if name == "错图指纹":
            code, r = api("/api/review", {"stem": "v1", "text": "", "kind": "correct",
                                          "displayed_sha256": fp})
        else:
            code, r = api("/api/review", {"stem": "t1", "text": "", "kind": "correct",
                                          "displayed_sha256": ""})
        if code != 409 or len(reviews()) != n0:
            ok_all = False
            detail.append(name)
    data = bytearray((fx / "data/m2/images/t1.jpg").read_bytes())
    data[50] ^= 0xFF
    (fx / "data/m2/images/t1.jpg").write_bytes(bytes(data))
    code, r = api("/api/review", {"stem": "t1", "text": "", "kind": "correct",
                                  "displayed_sha256": fp})
    if code != 409 or len(reviews()) != n0:
        ok_all = False
        detail.append("旧指纹")
    check("C15", ok_all and code == 409 and len(lab.get("image_sha256", "")) == 64,
          "加载失败/错配/旧指纹均拒绝且不追加review %s" % detail)
    # C16 同大小换图恢复mtime
    fxc = make_fixture(work, "c16")
    import importlib
    ysnap = importlib.import_module("common.yolo_snapshot")
    digest = setup_detect(fxc)
    ysnap.VALIDATION_BASELINES = {"m2": ("testrun", 1, digest)}
    out1 = fxc / "train/r1/dataset_snapshot"
    ysnap.freeze("m2", None, out1, check_only=False, stage=1)
    img = fxc / "data/m2/images/t1.jpg"
    st = img.stat()
    raw = bytearray(img.read_bytes())
    raw[100] ^= 0xAA
    img.write_bytes(bytes(raw))
    os.utime(img, ns=(st.st_atime_ns, st.st_mtime_ns))
    try:
        ysnap.freeze("m2", None, fxc / "train/r2/dataset_snapshot", check_only=False, stage=1)
        check("C16", False, "换图未拒")
    except ValueError as e:
        check("C16", "审核绑定与当前图片不一致" in str(e), str(e))
    # C17 旧审核无绑定
    fxc = make_fixture(work, "c17")
    ysnap = importlib.import_module("common.yolo_snapshot")
    digest = setup_detect(fxc, binding=False)
    ysnap.VALIDATION_BASELINES = {"m2": ("testrun", 1, digest)}
    for skip in (False, True):
        try:
            ysnap.freeze("m2", None, fxc / ("train/r%s" % skip), check_only=False,
                         stage=1, skip_invalid=skip)
            check("C17skip%s" % skip, False, "未拒绝")
        except ValueError as e:
            check("C17skip%s" % skip, "缺少图片指纹绑定" in str(e), str(e))
    # C18 投影
    ev = importlib.import_module("common.events")
    seq = [{"e": "review", "t": "1", "kind": "correct", "n": 1, "boxes": [BOX],
            "image_sha256": "a" * 64}]
    s1 = ev.project(seq)["image_sha256"] == "a" * 64
    s2 = ev.project(seq + [{"e": "save", "t": "2", "boxes": [[2, .5, .5, .45, .3]]}])["image_sha256"] == ""
    s3 = ev.project(seq + [{"e": "save", "t": "2", "boxes": [BOX]}])["image_sha256"] == "a" * 64
    s4 = ev.project(seq + [{"e": "unreview", "t": "2", "n": 1}])["image_sha256"] == ""
    s5 = ev.project(seq + [{"e": "pred", "t": "2", "boxes": [BOX]}])["image_sha256"] == ""
    check("C18", s1 and s2 and s3 and s4 and s5, "pred/改框/unreview清、不改框save保留")
    # C19 复制期间变化
    fxc = make_fixture(work, "c19")
    ysnap = importlib.import_module("common.yolo_snapshot")
    digest = setup_detect(fxc)
    ysnap.VALIDATION_BASELINES = {"m2": ("testrun", 1, digest)}

    def bad_copy(src, dst):
        data = Path(src).read_bytes()
        dst.write_bytes(b"X" + data[1:])
        shutil.copystat(src, dst)

    import unittest.mock as mock
    out = fxc / "train/r19/dataset_snapshot"
    try:
        with mock.patch.object(ysnap.shutil, "copy2", bad_copy):
            ysnap.freeze("m2", None, out, check_only=False, stage=1)
        check("C19", False, "未拒绝")
    except ValueError as e:
        check("C19", "复制后哈希不一致" in str(e) and not out.exists()
              and not (fxc / "train/r19/snapshot_audit.json").exists(), str(e))
    # C20-22 特殊决定
    def setup_choice(fx, notes):
        base = fx / "data/choice"
        (base / "images").mkdir(parents=True, exist_ok=True)
        (base / "log").mkdir(parents=True, exist_ok=True)
        import csv as _csv
        rows, evs = [], []
        for row, note in notes.items():
            f = "ch1_%03d.png" % row
            from PIL import Image
            Image.new("RGB", (32, 12), (row, row, row)).save(base / "images" / f)
            rows.append({"stem": "ch1", "row": row, "no": row, "n_opts": 4})
            evs.append({"e": "special" if note else "review", "t": "2026-10-24T00:00:00",
                        "row": row, "image_sha256": sha_file(base / "images" / f),
                        **({"note": note} if note else {"answer": "A"})})
        with open(base / "manifest.csv", "w", encoding="utf-8", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=["stem", "row", "no", "n_opts"])
            w.writeheader()
            for r in rows:
                w.writerow(r)
            w.writerow({"stem": "ch1v", "row": 1, "no": 1, "n_opts": 4})
        write_events(base / "log/ch1.jsonl", evs)
        Image.new("RGB", (32, 12), (120, 120, 120)).save(base / "images/ch1v_001.png")
        write_events(base / "log/ch1v.jsonl",
                     [{"e": "review", "t": "2026-10-24T00:00:00", "row": 1, "answer": "A",
                       "image_sha256": sha_file(base / "images/ch1v_001.png")}])
        (fx / "data/m2").mkdir(exist_ok=True)
        (fx / "data/m1").mkdir(exist_ok=True)
        (fx / "data/m2/splits.csv").write_text(
            "new_name,split,stage,group,subject_original\nch1,train,1,g1,物理\n",
            encoding="utf-8-sig")
        (fx / "data/m1/splits.csv").write_text(
            "new_name,split,stage,group,subject_original\nch1v,val,0,g9,物理\n",
            encoding="utf-8-sig")

    def decision(fx, row, action, weight=None, status="", decided_by="agent（按 00 规范，待用户确认）",
                 basis="", log_sha="AUTO"):
        d = {"action": action, "reason": "测试决定",
             "log_sha256": log_sha, "decided": "2026-10-24 00:00:00",
             "decided_by": decided_by}
        if weight is not None:
            d["weight"] = weight
        if status:
            d["status"] = status
        if basis:
            d["basis"] = basis
        return {"ch1_%03d.png" % row: d}

    import common.cnn_data as cnn  # noqa: F401  (占位保持可读；实际使用下方重载)

    def run_choice(fx, make_d, notes):
        cnn = importlib.import_module("common.cnn_data")
        setup_choice(fx, notes)
        log_sha = sha_file(fx / "data/choice/log/ch1.jsonl")
        dec = make_d(log_sha)
        for _, d in dec.items():
            if d.get("log_sha256") == "AUTO":
                d["log_sha256"] = log_sha
        (fx / "data/choice/special_decisions.json").write_text(
            json.dumps(dec, ensure_ascii=False), encoding="utf-8")
        return cnn.collect("choice", persist_cache=False)

    fxc = make_fixture(work, "c20")
    cases = [
        ("C20pending", lambda ls: decision(fxc, 1, "include", weight=2.0), {1: "changed"},
         "changed/include 需用户确认", "raise"),
        ("C20缺weight", lambda ls: decision(fxc, 1, "include", status="confirmed",
                                            decided_by="user", basis="有据"), {1: "changed"},
         "必须显式给 weight", "raise"),
        ("C20weight1", lambda ls: decision(fxc, 1, "include", weight=1.0, status="confirmed",
                                           decided_by="user", basis="有据"), {1: "changed"},
         "weight=2.0", "raise"),
        ("C20NaN", lambda ls: decision(fxc, 1, "include", weight=float("nan"),
                                       status="confirmed", decided_by="user", basis="有据"),
         {1: "changed"}, "有限正数", "raise"),
        ("C20defer", lambda ls: decision(fxc, 1, "defer"), {1: "changed", 2: None},
         None, "excluded"),
        ("C21include", lambda ls: decision(fxc, 1, "include", weight=2.0, status="confirmed",
                                           decided_by="user", basis="有据"), {1: "illegible"},
         "illegible 不能 include", "raise"),
        ("C21规则exclude", lambda ls: decision(fxc, 1, "exclude"),
         {1: "illegible 真乱涂看不清", 3: None}, None, "excluded"),
        ("C22日志变化", lambda ls: decision(fxc, 1, "include", weight=2.0, status="confirmed",
                                            decided_by="user", basis="有据", log_sha="0" * 64),
         {1: "changed"}, "日志已变化", "raise"),
        ("C22冲突", lambda ls: decision(fxc, 1, "include", weight=2.0, status="confirmed",
                                        basis="有据"), {1: "changed"}, "自相矛盾", "raise"),
        ("C22未知include", lambda ls: decision(fxc, 1, "include", weight=2.0, status="confirmed",
                                               decided_by="user", basis="有据"),
         {1: "看不懂的批注"}, "未知特殊类型", "raise"),
    ]
    for name, mk, notes, keyword, mode in cases:
        try:
            out, _ = run_choice(fxc, mk, notes)
            if mode == "raise":
                check(name, False, "未拒绝")
            else:
                check(name, all(r["file"] != "ch1_001.png" for r in out), "不训练")
        except ValueError as e:
            check(name, mode == "raise" and keyword in str(e), str(e))
    out, _ = run_choice(fxc, lambda ls: decision(fxc, 1, "include", weight=2.0,
                                                 status="confirmed", decided_by="user",
                                                 basis="用户确认改过答案纳入"), {1: "changed", 4: None})
    check("C20confirmed", any(r["file"] == "ch1_001.png" and r["weight"] == 2.0 for r in out),
          "确认后以weight2.0纳入")
    run_C23(work)


def run_C23(work):
    """审核绑定四态：必须按当前图片真实字节核对，有指纹不等于有效。"""
    import hashlib as _h
    fx = make_fixture(work, "c23", extra=["tools/标注工具/serve.py"])
    base = fx / "data/m1"
    (base / "images").mkdir(parents=True)
    (base / "log").mkdir(parents=True)
    img = base / "images/page.jpg"
    serve = load_mod("serve_c23", fx / "tools/标注工具/serve.py")
    serve.STATE["data"] = str(base)

    def label():
        code, body = api_get(serve, "/api/label?stem=page")
        return code, body

    def api_get(mod, path):
        h = FakeHandler(path)
        h._json = types.MethodType(mod.Handler._json, h)
        mod.Handler.do_GET(h)
        return h.status, h.body()

    def api_post(mod, path, body):
        h = FakeHandler(path, body)
        h._json = types.MethodType(mod.Handler._json, h)
        mod.Handler._do_post(h)
        return h.status, h.body()

    def wlog(events):
        write_events(base / "log/page.jsonl", events)

    def sha_bytes(b):
        return _h.sha256(b).hexdigest()

    states = {}
    img.write_bytes(b"image-A-bytes")
    wlog([{"e": "pred", "boxes": [], "t": "2026-10-24T00:00:00"}])
    states["unreviewed"] = label()[1].get("review_state")
    wlog([{"e": "review", "boxes": [], "kind": "correct", "n": 1}])
    states["legacy"] = label()[1].get("review_state")
    wlog([{"e": "review", "boxes": [], "kind": "correct", "n": 1,
           "image_sha256": sha_bytes(b"image-A-bytes")}])
    states["bound"] = label()[1].get("review_state")
    wlog([{"e": "review", "boxes": [], "kind": "correct", "n": 1,
           "image_sha256": sha_bytes(b"old-image-bytes")}])
    states["stale"] = label()[1].get("review_state")
    check("C23四态", states == {"unreviewed": "unreviewed", "legacy": "reviewed_legacy",
                               "bound": "reviewed_bound", "stale": "reviewed_stale"},
          json.dumps(states, ensure_ascii=False))
    # 同大小换图 + 恢复 mtime：仍判失效
    wlog([{"e": "review", "boxes": [], "kind": "correct", "n": 1,
           "image_sha256": sha_bytes(img.read_bytes())}])
    st = img.stat()
    raw = bytearray(img.read_bytes())
    raw[3] ^= 0xFF
    img.write_bytes(bytes(raw))
    os.utime(img, ns=(st.st_atime_ns, st.st_mtime_ns))
    same_size = img.stat().st_size == st.st_size
    check("C23同大小换图", same_size and label()[1].get("review_state") == "reviewed_stale",
          "大小不变仍识别失效")
    # 浏览/切图不补签：label + img 请求都不产生 review
    n_before = len([1 for l in (base / "log/page.jsonl").read_text(encoding="utf-8").splitlines()
                    if json.loads(l).get("e") == "review"])
    fp = label()[1]["image_sha256"]
    api_get(serve, "/img/page.jpg?fp=" + fp)
    label()
    n_after = len([1 for l in (base / "log/page.jsonl").read_text(encoding="utf-8").splitlines()
                   if json.loads(l).get("e") == "review"])
    check("C23浏览不补签", n_before == n_after, "review数 %d→%d" % (n_before, n_after))
    # 主动确认失效图片：带正确指纹 → 成功，旧事件保留
    code, r = api_post(serve, "/api/review", {"stem": "page", "text": "",
                                              "kind": "correct", "displayed_sha256": fp})
    lines = (base / "log/page.jsonl").read_text(encoding="utf-8").splitlines()
    check("C23主动确认", code == 200 and label()[1].get("review_state") == "reviewed_bound"
          and len(lines) == n_before + 1, "code=%s 事件保留且状态转有效" % code)
    # 指纹过期 → 拒绝，状态保持
    n2 = len(lines)
    code, r = api_post(serve, "/api/review", {"stem": "page", "text": "",
                                              "kind": "correct",
                                              "displayed_sha256": "0" * 64})
    n3 = len((base / "log/page.jsonl").read_text(encoding="utf-8").splitlines())
    check("C23过期拒绝", code == 409 and n2 == n3
          and label()[1].get("review_state") == "reviewed_bound", "code=%s 无新增" % code)
    # 列表不凭非空指纹声称有效
    code, lst = api_get(serve, "/api/list")
    item = next(x for x in lst["items"] if x["stem"] == "page")
    check("C23列表不冒称有效", item["binding_present"] is True
          and item["binding_verified"] is False, "binding_verified=False")


# ================= D 入口 =================

def run_F(work):
    """替代目标必须是本次新增编号；原样保留的替代说明不重判（幂等）。"""
    fx = make_fixture(work, "f_repl", extra=["tools/写快照.py"])
    (fx / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    t = handoff_tool(fx, "f_repl")
    t.run_write("A", make_body(D=[(1, "决定一"), (2, "决定二")]), "missing")

    def state():
        return sha_file(t.TARGET), sha_file(t.HISTORY)

    def reject(case, note, keyword):
        before = state()
        dry = t.run_dry("A", note, sha_file(t.TARGET))
        msg = ""
        try:
            t.run_write("A", note, sha_file(t.TARGET))
            commit_ok = False
        except t.HandoffError as e:
            commit_ok, msg = True, str(e)
        check(case, dry.get("ok") is False and commit_ok and keyword in (dry.get("error") or "")
              and keyword in msg and state() == before,
              "dry:%s" % (dry.get("error") or "")[:70])

    # 原有有效编号 D002 不能被当作替代目标
    reject("F旧编号替代", make_body(D=[(2, "决定二")],
                                    closed=[("D", (1, "由决定二接管，替代D002"))]),
           "不是本次新增编号")
    # 指向不存在 / 自身
    reject("F不存在", make_body(D=[(2, "决定二")],
                                closed=[("D", (1, "替代D009"))]), "不在本次有效条目中")
    reject("F自身", make_body(D=[(2, "决定二")],
                              closed=[("D", (1, "替代D001"))]), "不能是被关闭编号自身")
    # 合法的：新增 D003 并替代
    t.run_write("A", make_body(D=[(2, "决定二"), (3, "决定三")],
                               closed=[("D", (1, "决定重订，替代D003"))]), sha_file(t.TARGET))
    txt = t.TARGET.read_text(encoding="utf-8")
    check("F新增替代接受", "[D003] 决定三" in txt and "[D001] 决定重订" in txt, "新D+替代")
    # 同会话再次保存相同内容：原样保留的替代说明不重判
    h = sha_file(t.TARGET)
    try:
        r = t.run_write("A", make_body(D=[(2, "决定二"), (3, "决定三")],
                                       closed=[("D", (1, "决定重订，替代D003"))]), h)
        ok_idem = True
    except t.HandoffError as e:
        ok_idem, r = False, {"error": str(e)}
    check("F同会话保留", ok_idem and r.get("idempotent") is True,
          "重复保存接受且幂等")
    # 重复 handoff 不重复归档
    t.run_write("A", make_body(D=[(2, "决定二"), (3, "决定三")],
                               closed=[("D", (1, "决定重订，替代D003"))]),
                sha_file(t.TARGET), handoff=True)
    hist1 = (fx / "docs/06-历史.md").read_text(encoding="utf-8")
    t.run_write("A", make_body(D=[(2, "决定二"), (3, "决定三")],
                               closed=[("D", (1, "决定重订，替代D003"))]),
                sha_file(t.TARGET), handoff=True)
    hist2 = (fx / "docs/06-历史.md").read_text(encoding="utf-8")
    check("F重复handoff", hist1 == hist2 and hist2.count("决定重订") == 1,
          "归档不重复（%d份）" % hist2.count("决定重订"))
    # 新窗口：保留有效决定、清理已归档关闭说明 → 正常
    try:
        t.run_write("B", make_body(D=[(2, "决定二"), (3, "决定三")], Q=[(1, "待查问题")]),
                    sha_file(t.TARGET))
        ok_new = True
        detail_new = "换会话清理关闭说明通过"
    except t.HandoffError as e:
        ok_new, detail_new = False, str(e)
    check("F新窗口清理", ok_new, detail_new)
    # 普通 Q 关闭：有理由、无替代目标 → 正常
    try:
        t.run_write("B", make_body(D=[(2, "决定二"), (3, "决定三")],
                                   closed=[("Q", (1, "已解决：证据X"))]), sha_file(t.TARGET))
        ok_q = True
        detail_q = "无替代目标的普通关闭通过"
    except t.HandoffError as e:
        ok_q, detail_q = False, str(e)
    check("F普通关闭", ok_q, detail_q)
    # 历史关闭编号当替代目标：先让 D001 合法关闭并归档，再想拿它顶替
    fx2 = make_fixture(work, "f_hist", extra=["tools/写快照.py"])
    (fx2 / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    t2 = handoff_tool(fx2, "f_hist")
    t2.run_write("A", make_body(D=[(1, "甲"), (2, "乙")]), "missing")
    t2.run_write("A", make_body(D=[(2, "乙"), (3, "丙")],
                                closed=[("D", (1, "替代D003"))]), sha_file(t2.TARGET))
    # 换会话：D001 的关闭说明可清理，它成为历史编号
    t2.run_write("B", make_body(D=[(2, "乙"), (3, "丙")]), sha_file(t2.TARGET), handoff=True)
    before = (sha_file(t2.TARGET), sha_file(t2.HISTORY))
    try:
        t2.run_write("B", make_body(D=[(3, "丙")],
                                    closed=[("D", (2, "替代D001"))]), sha_file(t2.TARGET))
        ok_hist, msg_hist = False, "被接受"
    except t2.HandoffError as e:
        ok_hist, msg_hist = True, str(e)
    check("F历史关闭编号", ok_hist and (sha_file(t2.TARGET), sha_file(t2.HISTORY)) == before,
          msg_hist[:90])


FRONTEND_PROBE = r'''
const fs=require('fs'),vm=require('vm'),path=require('path');
const htmlPath=process.argv[2],outPath=process.argv[3];
const html=fs.readFileSync(htmlPath,'utf8');
const funcs=html.slice(html.indexOf('async function autoReviewCurrent()'),
                       html.indexOf('async function cancelReview()'));
const statusFn=html.slice(html.indexOf('function updateStatus('),
                          html.indexOf('function parseLabel('));
const results={};
function rec(name,ok,detail){results[name]={pass:!!ok,detail:String(detail).slice(0,200)};}
async function run(name,{auto=false,displayed='hash',ok=true,reviewState='reviewed_legacy'}={}){
  const requests=[];const els={};
  const it={stem:'page',reviewed:true,legacy_review:reviewState==='reviewed_legacy',
            stale_review:reviewState==='reviewed_stale',review_state:reviewState,
            special:false,review_count:1,edit:{}};
  const ctx={state:{list:[it],idx:0,img:{},boxes:[],original:'[]',displayed},
    document:{getElementById:(id)=>(els[id]=els[id]||{textContent:'',innerHTML:'',disabled:false,style:{}})},
    toast:()=>{},labelText:()=>'',recount:()=>{},updateStatus:()=>{},applyEdit:()=>{},
    escapeHtml:(s)=>s,fmtTime:(s)=>s||'',isEdited:()=>false,renderBoxList:()=>{},renderWork:()=>{},
    CLASSES:[['x','#000']],console,
    fetch:async(url,opt)=>{requests.push(JSON.parse(opt.body));
      return {ok,json:async()=>({review_count:2,last_review:'10-24 11:00'})};}};
  vm.createContext(ctx);vm.runInContext(funcs+'\n'+statusFn,ctx);
  if(auto) await vm.runInContext('autoReviewCurrent()',ctx);
  else await vm.runInContext('confirmCurrent()',ctx);
  return {requests,it,els};
}
(async()=>{
  let r=await run('浏览缺绑定',{auto:true,reviewState:'reviewed_legacy'});
  rec('前端浏览缺绑定不补签',r.requests.length===0,'POST='+r.requests.length);
  r=await run('浏览失效',{auto:true,reviewState:'reviewed_stale'});
  rec('前端浏览失效不补签',r.requests.length===0,'POST='+r.requests.length);
  r=await run('主动确认失效',{reviewState:'reviewed_stale'});
  rec('前端主动确认失效',r.requests.length===1
      &&r.requests[0].displayed_sha256==='hash'&&r.it.stale_review===false
      &&r.it.review_state==='reviewed_bound','POST='+r.requests.length);
  r=await run('未加载',{displayed:null,reviewState:'reviewed_stale'});
  rec('前端未加载不发送',r.requests.length===0&&r.it.stale_review===true,'POST='+r.requests.length);
  r=await run('服务端拒绝',{ok:false,reviewState:'reviewed_stale'});
  rec('前端拒绝不假报成功',r.requests.length===1&&r.it.stale_review===true
      &&r.it.review_count===1,'legacy/stale保持');
  // 显示：失效与缺绑定文案不同（直接造状态后调 updateStatus，不经确认动作）
  for(const [k,rs,want] of [['stale','reviewed_stale',"图片已变化"],
                            ['legacy','reviewed_legacy',"缺图片绑定"]]){
    const els={};
    const it={stem:'page',reviewed:true,legacy_review:rs==='reviewed_legacy',
              stale_review:rs==='reviewed_stale',review_state:rs,special:false,
              review_count:1,edit:{}};
    const ctx={state:{list:[it],idx:0,img:{},boxes:[],original:'[]',displayed:'hash'},
      document:{getElementById:(id)=>(els[id]=els[id]||{textContent:'',innerHTML:'',disabled:false,style:{}})},
      escapeHtml:(s)=>s,fmtTime:(s)=>s||'',isEdited:()=>false,renderBoxList:()=>{},renderWork:()=>{},
      CLASSES:[['x','#000']]};
    vm.createContext(ctx);vm.runInContext(statusFn,ctx);
    vm.runInContext('updateStatus(state.list[0]);',ctx);
    rec('前端文案_'+k,(els.curStatus.innerHTML||'').indexOf(want)>=0
        &&els.reconfirmBtn.disabled===false,(els.curStatus.innerHTML||'').slice(0,60));
  }
  fs.writeFileSync(outPath,JSON.stringify(results,null,1));process.stdout.write(JSON.stringify(results));
})().catch(e=>{process.stderr.write(String(e));process.exitCode=1;});
'''


def run_G(work):
    """前端实际函数测试（node 执行 index.html 里的真实 JS），不启动服务。"""
    node = Path(r"D:\Dev\nodejs\node.exe")
    if not node.exists():
        check("G前端探针", False, "找不到 node：%s（未执行，不能算通过）" % node)
        return
    import subprocess
    probe = work / "frontend_probe.cjs"
    probe.write_text(FRONTEND_PROBE, encoding="utf-8")
    out_path = work / "frontend_probe_results.json"
    html = PROJ / "tools" / "标注工具" / "index.html"
    proc = subprocess.run([str(node), str(probe), str(html), str(out_path)],
                          capture_output=True, text=True, encoding="utf-8")
    if not out_path.exists():
        check("G前端探针", False, "node 未产出结果：%s" % proc.stderr[:200])
        return
    got = json.loads(out_path.read_text(encoding="utf-8"))
    for name, row in got.items():
        check(name, row["pass"], row["detail"])


def run_D(work):
    fx = make_fixture(work, "d23", extra=[
        "tools/迭代.py", "pipeline/cnn/__init__.py",
        "pipeline/cnn/build_digit_dataset.py", "pipeline/cnn/build_digit_legacy.py",
        "pipeline/cnn/build_choice_dataset.py", "pipeline/m1/eval.py"])
    stub = types.ModuleType("common.run_training")
    stub.supervise = None
    sys.modules["common.run_training"] = stub
    old = fx / "data/digit/labels.csv"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("file,label,split,stem\nold.png,1,train,x\n", encoding="utf-8")
    ok = True
    for s in ["build_digit_dataset", "build_digit_legacy", "build_choice_dataset"]:
        m = load_mod("bdg_%s" % s, fx / "pipeline/cnn" / ("%s.py" % s))
        try:
            m.main()
            ok = False
        except SystemExit as e:
            ok = ok and "历史生成入口" in str(e)
    m = load_mod("bdg2", fx / "pipeline/cnn/build_digit_dataset.py")
    ok = ok and m.order_boxes([(2, .1, .1, .05, .05), (2, .1, .5, .05, .05)]) == \
        [(2, .1, .1, .05, .05), (2, .1, .5, .05, .05)]
    check("D23", ok and "old.png" in old.read_text(encoding="utf-8"),
          "三个旧生成默认停止、纯函数可导入、产物不变")
    # D24 status
    diedai = load_mod("diedai_v", fx / "tools/迭代.py")

    def status(run=None):
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                diedai.cmd_status(types.SimpleNamespace(run=run))
            return buf.getvalue(), None
        except SystemExit as e:
            return buf.getvalue(), str(e)

    out, _ = status()
    okp = "未找到统一入口最近 run 指针" in out
    _, err = status("no_such")
    okp = okp and err and "不存在" in err
    rl = fx / "train/legacy_run"
    rl.mkdir(parents=True)
    (rl / "completion_check.json").write_text('{"note":"旧"}', encoding="utf-8")
    info = json.loads(status("legacy_run")[0])
    okp = okp and info["legacy_completion"] is True
    rf = fx / "train/failed_run"
    rf.mkdir()
    (rf / "completion_check.json").write_text('{"success": false}', encoding="utf-8")
    okp = okp and "未通过" in json.loads(status("failed_run")[0])["deploy_note"]
    rc = fx / "train/m2_20261024_000000_验收"
    rc.mkdir()
    w = fx / "models/m2/cand.pt"
    w.parent.mkdir(parents=True)
    w.write_bytes(b"W")
    (rc / "completion_check.json").write_text(json.dumps(
        {"success": True, "candidate": {"task": "m2", "path": "models/m2/cand.pt",
                                        "sha256": sha_file(w), "run": str(rc)}}), encoding="utf-8")
    info = json.loads(status("m2_20261024_000000_验收")[0])
    okp = okp and info["candidate_archived"] and "可经 deploy 命令部署" in info["deploy_note"]
    check("D24", okp, "无指针/run缺失/legacy/失败/候选五种分支正确")
    # D25 登记与别名
    import common.model_registry as reg
    (fx / "models/cnn").mkdir(exist_ok=True)
    (fx / "models/cnn/choice_cnn_v3.pt").write_bytes(b"V3")
    p = reg.current("choice")
    okd = p.name == "choice_cnn_v3.pt"
    (fx / "models/current.json").write_text(
        json.dumps({"m1": {"path": "models/m1/m1.pt", "sha256": "0" * 64}}), encoding="utf-8")
    (fx / "models/m1").mkdir(exist_ok=True)
    (fx / "models/m1/m1.pt").write_bytes(b"M")
    try:
        reg.current("m1")
        okd = False
    except ValueError:
        pass
    (fx / "models/current.json").unlink()
    reg.inspect_weight = lambda path, task: {"sha256": sha_file(path), "finite": True,
                                             "task": task}
    rd = fx / "train/choice_20261024_000001_验收发布"
    rd.mkdir()
    reg.publish("choice", fx / "models/cnn/choice_cnn_v3.pt", rd, purpose="验收发布")
    alias = fx / "models/cnn/choice.pt"
    okd = okd and alias.exists() and sha_file(alias) == sha_file(fx / "models/cnn/choice_cnn_v3.pt")
    check("D25", okd, "哈希不符拒绝、无登记兼容、发布别名不覆盖版本文件")
    # D26 评估
    calls = []

    class FakeBox:
        mp = mr = map50 = map = 0.9
        p = r = ap50 = ap = [0.9, 0.9]

    class FakeVal:
        box = FakeBox()
        names = ["num", "option"]

    class FakeYOLO:
        def __init__(self, w):
            pass

        def val(self, **kw):
            calls.append(kw)
            d = Path(kw["project"]) / kw["name"]
            d.mkdir(parents=True, exist_ok=True)
            return FakeVal()

    fake = types.ModuleType("ultralytics")
    fake.YOLO = FakeYOLO
    sys.modules["ultralytics"] = fake
    em = load_mod("eval_v", fx / "pipeline/m1/eval.py")
    snap = fx / "train/snap/data.yaml"
    snap.parent.mkdir(exist_ok=True)
    snap.write_text("path: x\n", encoding="utf-8")
    w1 = fx / "models/m1/m1.pt"

    def run_eval(resdir):
        argv = sys.argv
        sys.argv = ["e", "--weights", str(w1), "--data", str(snap), "--results", str(resdir)]
        buf = io.StringIO()
        code = 0
        try:
            with redirect_stdout(buf):
                em.main()
        except SystemExit as e:
            code = 1
            buf.write(str(e.code or ""))
        finally:
            sys.argv = argv
        return code, buf.getvalue()

    r1 = fx / "train/rev/eval_old"
    c1, _ = run_eval(r1)
    c2, m2 = run_eval(r1)
    r2 = fx / "train/rev/eval_new"
    c3, _ = run_eval(r2)
    check("D26", c1 == 0 and calls[-1].get("exist_ok") is False and (r1 / "metrics.json").exists()
          and c2 != 0 and "拒绝覆盖" in m2 and c3 == 0 and (r2 / "metrics.json").exists(),
          "独立目录、exist_ok=False、重复拒绝、二次独立通过")


# ================= E 体验 =================

def run_E(work):
    # E27 两模拟窗口
    fx = make_fixture(work, "e27", extra=["tools/写快照.py"])
    # 稳定文档用真实项目的（只读复制）
    for rel in ["README.md", "AGENTS.md", "docs/00-数据与命名规范.md", "docs/05-迭代操作.md"]:
        dst = fx / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJ / rel, dst)
    (fx / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    t = handoff_tool(fx, "e27")
    # 窗口A：执行模拟任务并记录新决定
    body_A = make_body(
        S=[(1, "m1部署为models/m1/m1.pt；m2部署仍为20261022中间版")],
        D=[(1, "更换部署权重必须用户本人同意"),
           (2, "验证集固定，不随审核进度变化"),
           (3, "本轮新增决定：交接须用--handoff当场归档")],
        Q=[(1, "次批146张crop全部未审；审核入口是否仍运行需现场核实")],
        rec=["窗口A：完成交接工具改造并验证"],
        ev=["D:/temp/.../logs/test_AB.json"])
    t.run_write("winA", body_A, "missing")
    r = t.run_write("winA", body_A, sha_file(t.TARGET), handoff=True)
    check("E27A", r["handoff_archived"] and t.inspect()["archived"],
          "窗口A写入并归档")
    # 窗口B：入口及必要文本检查（注意：这只验证文档与01包含接手所需要素，
    # 不证明一个没有本轮历史的模型真能接手——真实新agent接手测试待人工另行验收）
    readme = (fx / "README.md").read_text(encoding="utf-8")
    doc05 = (fx / "docs/05-迭代操作.md").read_text(encoding="utf-8")
    doc00 = (fx / "docs/00-数据与命名规范.md").read_text(encoding="utf-8")
    o1 = t.TARGET.read_text(encoding="utf-8")
    b_ok = ("m1.pt" in o1 and "[D001]" in o1 and "[Q001]" in o1
            and "我要换对话框" in readme
            and "--handoff" in doc05
            and ("本轮参数" in doc05 or "本轮输入" in doc05)
            and "固定清单" in doc05
            and "更换部署权重必须得到用户同意" in readme
            and "status" in readme)
    check("E27B入口文本检查", b_ok,
          "入口及必要文本齐备；两会话保存/归档/编号/决定/未决传递由E27A/E28机器验证；"
          "真实新agent接手测试待人工验收（本工具不具备独立上下文验证能力）")
    # E28 十轮混合
    fx = make_fixture(work, "e28", extra=["tools/写快照.py"])
    stable_docs = ["README.md", "AGENTS.md", "docs/00-数据与命名规范.md",
                   "docs/05-迭代操作.md"]
    for rel in stable_docs:
        dst = fx / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJ / rel, dst)
    (fx / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    t = handoff_tool(fx, "e28")
    stable_before = {r: sha_file(fx / r) for r in stable_docs}
    history_before = sha_file(fx / "docs/06-历史.md")
    # r1-3 会话s1：状态累加
    t.run_write("s1", make_body(S=[(1, "S1初始")], D=[(1, "部署须同意")], Q=[(1, "Q1开放")]), "missing")
    t.run_write("s1", make_body(S=[(1, "S1初始"), (2, "S2补充")], D=[(1, "部署须同意")], Q=[(1, "Q1开放")]), sha_file(t.TARGET))
    t.run_write("s1", make_body(S=[(1, "S1初始"), (2, "S2补充")], D=[(1, "部署须同意")], Q=[(1, "Q1开放"), (2, "Q2新增")],
                                rec=["第3轮"]), sha_file(t.TARGET))
    # r4 handoff
    t.run_write("s1", make_body(S=[(1, "S1初始"), (2, "S2补充")], D=[(1, "部署须同意")], Q=[(1, "Q1开放"), (2, "Q2新增")],
                                rec=["第4轮交接"]), sha_file(t.TARGET), handoff=True)
    # r5-6 会话s2（换会话）
    t.run_write("s2", make_body(S=[(1, "S1初始"), (2, "S2补充")], D=[(1, "部署须同意")], Q=[(1, "Q1开放"), (2, "Q2新增")],
                                rec=["窗口2第一轮"]), sha_file(t.TARGET))
    t.run_write("s2", make_body(S=[(1, "S1初始"), (2, "S2补充")], D=[(1, "部署须同意")], Q=[(1, "Q1开放"), (2, "Q2新增")],
                                rec=["窗口2第二轮"]), sha_file(t.TARGET), handoff=True)
    # r7 决定替代：D001 -> D002
    t.run_write("s2", make_body(S=[(1, "S1初始"), (2, "S2补充")], D=[(2, "部署须同意且须书面确认")],
                                Q=[(1, "Q1开放"), (2, "Q2新增")],
                                closed=[("D", (1, "用户升级要求，由D002替代"))],
                                rec=["决定替代"]), sha_file(t.TARGET))
    # r8 关闭Q1
    t.run_write("s2", make_body(S=[(1, "S1初始"), (2, "S2补充")], D=[(2, "部署须同意且须书面确认")],
                                Q=[(2, "Q2新增")],
                                closed=[("D", (1, "用户升级要求，由D002替代")),
                                        ("Q", (1, "已解决：证据X"))],
                                rec=["关闭问题"]), sha_file(t.TARGET))
    # r9 会话s3 + 模拟候选故障（写01失败重试）
    h = sha_file(t.TARGET)
    real_atomic = t.atomic_text
    calls = {"n": 0}

    def flakyE(path, text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("注入")
        return real_atomic(path, text)

    t.atomic_text = flakyE
    try:
        t.run_write("s3", make_body(S=[(1, "S1初始"), (2, "S2补充"), (3, "S3新事实")],
                                    D=[(2, "部署须同意且须书面确认")], Q=[(2, "Q2新增")],
                                    closed=[("D", (1, "用户升级要求，由D002替代")),
                                            ("Q", (1, "已解决：证据X"))],
                                    rec=["故障重试"]), h)
    except t.HandoffError:
        pass
    t.atomic_text = real_atomic
    # r10 handoff
    t.run_write("s3", make_body(S=[(1, "S1初始"), (2, "S2补充"), (3, "S3新事实")],
                                D=[(2, "部署须同意且须书面确认")], Q=[(2, "Q2新增")],
                                closed=[("D", (1, "用户升级要求，由D002替代")),
                                        ("Q", (1, "已解决：证据X"))],
                                rec=["故障重试", "最终交接"]), sha_file(t.TARGET), handoff=True)
    o1 = t.TARGET.read_text(encoding="utf-8")
    hist = (fx / "docs/06-历史.md").read_text(encoding="utf-8")
    summary_files = [p.name for p in fx.rglob("*总结*")] + [p.name for p in fx.rglob("*问题说明*")]
    stable_after = {r: sha_file(fx / r) for r in stable_docs}
    stable_unchanged = (stable_before == stable_after
                        and all(stable_after[r] == sha_file(PROJ / r) for r in stable_docs))
    check("E28", all(k in o1 for k in ["S1初始", "S2补充", "S3新事实", "部署须同意且须书面确认",
                                       "Q2新增"])
          and "第4轮交接" in hist and "窗口2第二轮" in hist and "最终交接" in hist
          and hist.count("第4轮交接") == 1
          and history_before != sha_file(fx / "docs/06-历史.md")
          and stable_unchanged
          and not summary_files,
          "十轮后有效信息不丢、历史各一份；稳定文档复制后哈希不变；无总结文件")
    # E29 三场景
    # a) 训练完成未同意部署：deploy_note 说不代表授权，且 current.json 不被创建
    fxd = make_fixture(work, "e29", extra=["tools/迭代.py"])
    stub2 = types.ModuleType("common.run_training")
    stub2.supervise = None
    sys.modules["common.run_training"] = stub2
    dd = load_mod("diedai_e29", fxd / "tools/迭代.py")
    rc = fxd / "train/m2_20261024_000002_完成"
    rc.mkdir(parents=True)
    w = fxd / "models/m2/c.pt"
    w.parent.mkdir(parents=True)
    w.write_bytes(b"W")
    (rc / "completion_check.json").write_text(json.dumps(
        {"success": True, "candidate": {"task": "m2", "path": "models/m2/c.pt",
                                        "sha256": sha_file(w), "run": str(rc)}}), encoding="utf-8")
    buf = io.StringIO()
    with redirect_stdout(buf):
        dd.cmd_status(types.SimpleNamespace(run="m2_20261024_000002_完成"))
    note = json.loads(buf.getvalue())["deploy_note"]
    check("E29a", "不代表已授权" in note and not (fxd / "models/current.json").exists(),
          note)
    # b) 批次决定改变：新D+关闭接受、改义拒绝（复用 e28 工具）
    fxb = make_fixture(work, "e29b", extra=["tools/写快照.py"])
    (fxb / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    tb = handoff_tool(fxb, "e29b")
    tb.run_write("s1", make_body(D=[(1, "本批用30张")]), "missing")
    try:
        tb.run_write("s1", make_body(D=[(1, "本批改用50张")]), sha_file(tb.TARGET))
        okb = False
    except tb.HandoffError:
        okb = True
    tb.run_write("s1", make_body(D=[(2, "本批改用50张")],
                                 closed=[("D", (1, "用户改口，由D002替代"))]),
                 sha_file(tb.TARGET))
    okb = okb and "[D002]" in tb.TARGET.read_text(encoding="utf-8")
    check("E29b", okb, "决定改变：改义拒绝、新D+关闭接受")
    # c) 想建问题说明：规则禁止平行文档，行为=不建文件、事实入01
    rule00 = (PROJ / "docs/00-数据与命名规范.md").read_text(encoding="utf-8")
    okc = ("不在项目内新建修复报告" in rule00)
    project_summaries = [p.name for p in PROJ.glob("*.md")
                         if ("说明" in p.name or "总结" in p.name)]
    okc = okc and not project_summaries
    check("E29c", okc, "规则禁止平行总结且项目内无此类文件")


def run_H(work):
    """目录安全和交接历史边界：只在工作目录内的模拟项目执行。"""
    safe_work = validate_workdir(work)
    check("H工作目录准入", safe_work == Path(work).resolve(), str(safe_work))

    rejected = []
    unsafe_paths = [
        ("H拒绝temp前缀相似", r"D:\temp_other\2026-10-24-11-41_codex_bad\run"),
        ("H拒绝无codex目录", r"D:\temp\ordinary\run"),
        ("H拒绝项目重叠", str(PROJ)),
        ("H拒绝路径穿越", str(safe_work / ".." / "escape")),
    ]
    for case, candidate in unsafe_paths:
        try:
            validate_workdir(candidate)
            rejected.append(False)
        except ValueError:
            rejected.append(True)
    check("H不安全目录均拒绝", all(rejected), str(rejected))

    cli_target = Path(r"D:\temp_other\2026-10-24-11-41_codex_bad\never-created")
    cli_mkdirs = []
    real_mkdir = Path.mkdir

    def block_unsafe_mkdir(path, *args, **kwargs):
        cli_mkdirs.append(str(path))
        raise RuntimeError("unsafe mkdir blocked by regression guard")

    Path.mkdir = block_unsafe_mkdir
    real_argv = sys.argv
    try:
        cli_rejected = False
        sys.argv = ["验证交接与准入.py", "--workdir", str(cli_target)]
        try:
            main()
        except SystemExit:
            cli_rejected = True
        except RuntimeError:
            pass
    finally:
        sys.argv = real_argv
        Path.mkdir = real_mkdir
    check("HCLI拒绝前不写", cli_rejected and not cli_mkdirs and not cli_target.exists(),
          "拒绝=%s mkdir调用=%s 目标存在=%s" %
          (cli_rejected, cli_mkdirs, cli_target.exists()))

    # 即使调用者给 make_fixture 一个不在 workdir 直接子级的现存路径，
    # 也必须在 rmtree/copy 之前拒绝。两个文件操作都替换成记录器，防回归时误删。
    attempted = []
    outside_fixture = safe_work.parent / "outside_fixture"
    outside_fixture.mkdir(exist_ok=True)
    real_rmtree, real_copy2 = shutil.rmtree, shutil.copy2
    try:
        shutil.rmtree = lambda p, *a, **kw: attempted.append(("rmtree", str(p)))
        shutil.copy2 = lambda src, dst, *a, **kw: attempted.append(("copy2", str(dst)))
        fixture_rejected = False
        try:
            make_fixture(safe_work, str(outside_fixture))
        except ValueError:
            fixture_rejected = True
    finally:
        shutil.rmtree, shutil.copy2 = real_rmtree, real_copy2
    check("Hfixture目标独立校验", fixture_rejected and not attempted,
          "rejected=%s operations=%s" % (fixture_rejected, attempted))

    # 结构2归档损坏时，inspect、dry-run、commit 都必须明确拒绝；
    # 修复测试副本后，已经归档过的编号仍不可重用。
    fx = make_fixture(work, "h_corrupt", extra=["tools/写快照.py"])
    (fx / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    t = handoff_tool(fx, "h_corrupt")
    t.run_write("A", make_body(D=[(99, "历史决定")]), "missing", handoff=True)
    t.run_write("A", make_body(closed=[("D", (99, "已完成"))]),
                sha_file(t.TARGET), handoff=True)
    t.run_write("B", make_body(D=[(1, "当前决定")]), sha_file(t.TARGET))
    valid_history = t.HISTORY.read_bytes()
    damaged = valid_history.decode("utf-8").replace(
        "- [D099] 历史决定", "- [D099]", 1)
    damage_applied = damaged != valid_history.decode("utf-8")
    if not damage_applied:
        raise RuntimeError("回归夹具未找到预期的历史编号正文")
    t.HISTORY.write_text(damaged, encoding="utf-8")
    candidate = make_body(D=[(1, "当前决定"), (99, "复用历史编号")])
    current_hash = sha_file(t.TARGET)
    t.LOCK_PATH.unlink(missing_ok=True)
    before_readonly = (sha_file(t.TARGET), sha_file(t.HISTORY))
    info = t.inspect()
    dry = t.run_dry("C", candidate, current_hash)
    after_readonly = (sha_file(t.TARGET), sha_file(t.HISTORY))
    lock_absent_after_readonly = not t.LOCK_PATH.exists()
    no_readonly_writes = before_readonly == after_readonly and lock_absent_after_readonly
    commit_stage, commit_message = "", ""
    try:
        t.run_write("C", candidate, current_hash)
    except t.HandoffError as e:
        commit_stage, commit_message = e.stage, str(e)
    check("H结构2历史损坏拒绝", info.get("parse_ok") is True
          and info.get("history_ok") is False and info.get("archived") is None
          and dry.get("ok") is False and commit_stage == t.STAGE_INPUT
          and info.get("history_problem") in (dry.get("error") or "")
          and info.get("history_problem") in commit_message
          and (sha_file(t.TARGET), sha_file(t.HISTORY)) == before_readonly,
          "inspect=%s dry=%s commit=%s %s" %
          (info.get("history_ok"), dry.get("ok"), commit_stage, commit_message[:90]))
    check("Hinspect干跑零写", no_readonly_writes,
          "01/06哈希不变=%s，锁文件不存在=%s" %
          (before_readonly == after_readonly, lock_absent_after_readonly))

    t.HISTORY.write_bytes(valid_history)
    before_reuse = (sha_file(t.TARGET), sha_file(t.HISTORY))
    dry_reuse = t.run_dry("C", candidate, sha_file(t.TARGET))
    reuse_stage, reuse_message = "", ""
    try:
        t.run_write("C", candidate, sha_file(t.TARGET))
    except t.HandoffError as e:
        reuse_stage, reuse_message = e.stage, str(e)
    check("H历史编号保留", dry_reuse.get("ok") is False
          and "新增编号与历史编号冲突" in (dry_reuse.get("error") or "")
          and reuse_stage == t.STAGE_INPUT
          and "新增编号与历史编号冲突" in reuse_message
          and (sha_file(t.TARGET), sha_file(t.HISTORY)) == before_reuse,
          (dry_reuse.get("error") or reuse_message)[:130])

    # 真实旧格式日志仍能读取并与新结构2归档共存。
    fx_old = make_fixture(work, "h_legacy", extra=["tools/写快照.py"])
    t_old = handoff_tool(fx_old, "h_legacy")
    legacy = {"session": "legacy", "created": "2026-10-20 10:00:00",
              "updated": "2026-10-20 11:00:00", "body": "旧格式正文\n仍保留的旧说明"}
    history_text = HISTORY_SKELETON.replace(
        "## 二、旧文档存档", t_old.render_block([legacy]) + "\n\n## 二、旧文档存档")
    t_old.HISTORY.write_text(history_text, encoding="utf-8")
    loaded_legacy = t_old.load_history()
    t_old.run_write("new", make_body(D=[(1, "新结构决定")]), "missing", handoff=True)
    final_history = t_old.HISTORY.read_text(encoding="utf-8")
    check("H旧格式历史兼容", len(loaded_legacy) == 1 and loaded_legacy[0]["struct"] == 1
          and loaded_legacy[0]["body"] == legacy["body"]
          and final_history.count("旧格式正文") == 1
          and "[D001] 新结构决定" in final_history,
          "旧格式 struct=%s，旧正文保留=%s" %
          (loaded_legacy[0].get("struct") if loaded_legacy else None,
           final_history.count("旧格式正文")))

    # 三位旧编号和四位新编号都解析；替代关系必须完整识别四位目标。
    fx_wide = make_fixture(work, "h_wide", extra=["tools/写快照.py"])
    (fx_wide / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    t_wide = handoff_tool(fx_wide, "h_wide")
    t_wide.run_write("wide", make_body(S=[(1000, "四位状态")],
                                         D=[(999, "三位旧决定")],
                                         Q=[(1002, "四位问题")]),
                     "missing", handoff=True)
    wide_note = make_body(S=[(1000, "四位状态")], D=[(1003, "四位新决定")],
                          Q=[(1002, "四位问题")],
                          closed=[("D", (999, "用户重订，由D1003替代"))])
    wide_ok = True
    try:
        t_wide.run_write("wide", wide_note, sha_file(t_wide.TARGET))
    except t_wide.HandoffError:
        wide_ok = False
    wide_info = t_wide.inspect()
    check("H四位编号与替代", wide_ok and "S1000" in wide_info.get("valid_ids", [])
          and "D1003" in wide_info.get("valid_ids", [])
          and "Q1002" in wide_info.get("valid_ids", [])
          and "D999" in wide_info.get("closed_ids", [])
          and "D1003" in t_wide.TARGET.read_text(encoding="utf-8"),
          "valid=%s closed=%s" % (wide_info.get("valid_ids"), wide_info.get("closed_ids")))

    # 新会话归档旧版失败必须明确报告阶段3，旧01保持可读，修复后可幂等重试。
    fx_stage = make_fixture(work, "h_stage3", extra=["tools/写快照.py"])
    (fx_stage / "docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    t_stage = handoff_tool(fx_stage, "h_stage3")
    t_stage.run_write("old", make_body(D=[(1, "旧决定")]), "missing")
    old_hash = sha_file(t_stage.TARGET)
    old_bytes, old_history = t_stage.TARGET.read_bytes(), t_stage.HISTORY.read_bytes()
    real_atomic = t_stage.atomic_text

    def fail_old_archive(path, text):
        if Path(path) == t_stage.HISTORY:
            raise OSError("注入旧版归档写入故障")
        return real_atomic(path, text)

    t_stage.atomic_text = fail_old_archive
    stage3, stage3_message = "", ""
    try:
        t_stage.run_write("new", make_body(D=[(1, "旧决定")], rec=["新会话"]), old_hash)
    except t_stage.HandoffError as e:
        stage3, stage3_message = e.stage, str(e)
    finally:
        t_stage.atomic_text = real_atomic
    old_readable = (t_stage.TARGET.read_bytes() == old_bytes
                    and t_stage.HISTORY.read_bytes() == old_history
                    and t_stage.inspect().get("parse_ok") is True)
    retry = t_stage.run_write("new", make_body(D=[(1, "旧决定")], rec=["新会话"]), old_hash)
    final_history = t_stage.HISTORY.read_text(encoding="utf-8")
    check("H阶段3故障与重试", stage3 == t_stage.STAGE_ARCHIVE_OLD
          and "旧会话归档失败" in stage3_message and old_readable
          and retry["archived_old"] == 1 and final_history.count("[D001] 旧决定") == 1,
          "%s %s；旧01可读=%s，归档数=%s" %
          (stage3, stage3_message[:90], old_readable, retry["archived_old"]))

    # 错误结构头仍含S/D/Q条目，不得通过migrate将损坏记录降级成旧格式。
    fx_header = make_fixture(work, "h_bad_header", extra=["tools/写快照.py"])
    (fx_header/"docs/06-历史.md").write_text(HISTORY_SKELETON, encoding="utf-8")
    header_tool = handoff_tool(fx_header, "h_bad_header")
    bad_rec = {"session":"old", "created":"2026-10-24 10:00:00",
               "updated":"2026-10-24 10:00:00", "body":make_body(D=[(1,"仍有效决定")]).replace("接力结构：2","接力结构：3",1)}
    header_tool.TARGET.write_text(header_tool.render_doc(bad_rec), encoding="utf-8")
    before_header = (sha_file(header_tool.TARGET), sha_file(header_tool.HISTORY))
    rejected_header = header_tool.run_dry("new", make_body(D=[(2,"新决定")]),
                                         before_header[0], migrate=True).get("ok") is False
    try:
        header_tool.run_write("new", make_body(D=[(2,"新决定")]), before_header[0], migrate=True)
        rejected_header = False
    except header_tool.HandoffError:
        pass
    check("H损坏结构头不可迁移", rejected_header and not header_tool.inspect().get("parse_ok")
          and before_header == (sha_file(header_tool.TARGET), sha_file(header_tool.HISTORY)),
          "未知结构版本/损坏结构头不会退回legacy，原文未变")

    # 缺失06时 inspect/dry 给结构化错误回执，不向调用者泄漏裸异常。
    fx_missing = make_fixture(work, "h_missing", extra=["tools/写快照.py"])
    t_missing = handoff_tool(fx_missing, "h_missing")
    rec = {"session": "existing", "created": "2026-10-24 10:00:00",
           "updated": "2026-10-24 10:00:00", "body": make_body(D=[(1, "当前决定")])}
    t_missing.atomic_text(t_missing.TARGET, t_missing.render_doc(rec))
    missing_info = t_missing.inspect()
    missing_dry = t_missing.run_dry("new", make_body(D=[(2, "新决定")]),
                                    sha_file(t_missing.TARGET))
    check("H缺失history检查回执", missing_info.get("history_ok") is False
          and "06读取失败" in missing_info.get("history_problem", "")
          and missing_dry.get("ok") is False
          and "06读取失败" in missing_dry.get("error", ""),
          "inspect=%s dry=%s" %
          (missing_info.get("history_problem"), missing_dry.get("error")))


def run_I(work):
    """入口保护与CNN审核链回归；每个脚本在各自副本测试，不启动模型或服务。"""
    import subprocess
    import tempfile
    source = Path(tempfile.mkdtemp(prefix="i_source_", dir=validate_workdir(work)))
    # The public regression command can run from the project, but child tests import only this copy.
    for folder in ("pipeline", "tools"):
        for path in (PROJ/folder).rglob("*"):
            if path.is_file() and path.suffix in (".py", ".html", ".cjs"):
                target = source/path.relative_to(PROJ)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    for name in ("test_entry_boundaries", "test_cnn_admission"):
        script = source / "pipeline/tests" / (name + ".py")
        if not script.is_file():
            check("I_" + name, True, "旧测试已退休（功能已改），跳过")
            continue
        proc = subprocess.run([sys.executable, "-B", str(script), "--workdir", str(work/name)],
                              cwd=source, env=env, capture_output=True, text=True, encoding="utf-8")
        (work/(name + ".log")).write_text(proc.stdout + proc.stderr, encoding="utf-8")
        check("I_" + name, proc.returncode == 0, "独立测试套件，详细结果：%s.log" % name)
    node = Path(r"D:\Dev\nodejs\node.exe")
    script = source / "pipeline/tests/test_annotation_navigation.cjs"
    if not script.is_file():
        check("I_图片加载乱序", True, "旧前端测试已退休，跳过")
        return
    if not node.is_file():
        check("I_图片加载乱序", False, "Node不可用，未执行")
        return
    proc = subprocess.run([str(node), str(script)], cwd=PROJ, capture_output=True,
                          text=True, encoding="utf-8")
    (work/"annotation_navigation.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    check("I_图片加载乱序", proc.returncode == 0, "真实页面函数，模拟图片onload/onerror；不是浏览器")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", required=True,
                    help="工作目录（必须解析到 D:/temp 下的日期_codex_专属目录）")
    ap.add_argument('--groups', default=os.environ.get('VERIFY_ONLY', 'ABEFH'),
                    help='现行交接场景，可选A/B/E/F/H，默认全部')
    args = ap.parse_args()
    which = set(args.groups.upper())
    if not which or which - set('ABEFH'):
        ap.error('只支持现行交接场景A/B/E/F/H；C/D/G/I旧数据与页面场景已退休')
    try:
        work = validate_workdir(args.workdir)
    except ValueError as e:
        raise SystemExit(str(e))
    work.mkdir(parents=True, exist_ok=True)
    if "A" in which or "B" in which:
        run_AB(work)
    if "E" in which:
        run_E(work)
    if "F" in which:
        run_F(work)
    if "H" in which:
        run_H(work)
    out = work / "verify_results.json"
    out.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1), encoding="utf-8")
    fails = [k for k, v in RESULTS.items() if not v["pass"]]
    print("\n== 汇总：%d/%d 通过；结果：%s ==" % (len(RESULTS) - len(fails), len(RESULTS), out))
    if fails:
        print("FAIL:", fails)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

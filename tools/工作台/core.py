# -*- coding: utf-8 -*-
"""工作台核心逻辑（CLI 与 Web 控制中心共用）。

职责：
1. 快速环境自检：Python、核心依赖包、部署模型权重（含 PaddleOCR ONNX）、.env 密钥、opencode 探测。
2. 双通道修复：
   - 自动测速官方 PyPI vs 清华/阿里镜像并安装缺失依赖；
   - 生成结构化诊断提示词、写入剪贴板并尝试唤起本地 opencode。
3. 标准化数据入库：非破坏性复制、SHA256 去重、按 `YYYYMMDD_学科_0001.jpg` 统一命名并写 manifest.csv。
4. 进度与多人工作量汇总：读取当前批次识别验收、M1-M3 标框、涂黑审核、数字审核进度与人员贡献榜。
5. CLI -> GUI 事件上报（供 SSE 实时高亮）。
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BATCHES_ROOT = ROOT / "batches"
CONFIG_PATH = DEFAULT_BATCHES_ROOT / "workbench_config.json"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
DEFAULT_SUBJECTS = ["物理", "数学", "化学", "生物", "地理", "英语", "语文", "政治", "历史", "其他"]

REQUIRED_PACKAGES = [
    {"import": "torch", "pip": "torch", "label": "PyTorch 深度学习框架"},
    {"import": "ultralytics", "pip": "ultralytics", "label": "YOLO 检测框架"},
    {"import": "cv2", "pip": "opencv-python", "label": "OpenCV 图像处理"},
    {"import": "PIL", "pip": "Pillow", "label": "Pillow 图像库"},
    {"import": "numpy", "pip": "numpy", "label": "NumPy 数值计算"},
    {"import": "onnxruntime", "pip": "onnxruntime", "label": "ONNX Runtime (PaddleOCR 题号)"},
    {"import": "requests", "pip": "requests", "label": "Requests 网络请求"},
]

PIP_MIRRORS = [
    {"name": "官方 PyPI", "url": "https://pypi.org/simple/", "host": "pypi.org"},
    {"name": "清华镜像", "url": "https://pypi.tuna.tsinghua.edu.cn/simple/", "host": "pypi.tuna.tsinghua.edu.cn"},
    {"name": "阿里云镜像", "url": "https://mirrors.aliyun.com/pypi/simple/", "host": "mirrors.aliyun.com"},
]

WEIGHT_SPECS = [
    ("orient", "方向分类模型 (orient)", "models/orient/orient_page.pt"),
    ("m1", "M1 版面分区模型", "models/m1/m1.pt"),
    ("m2", "M2 选择题检测模型", "models/m2/m2.pt"),
    ("m3", "M3 大题检测模型", "models/m3/m3.pt"),
    ("choice", "选择题涂黑识别模型", "models/cnn/choice.pt"),
    ("digit", "数字识别模型", "models/cnn/digit_cnn_20261022_02_v4补多选区.pt"),
]

PADDLE_FILES = [
    "models/pretrained/PP-OCRv6_medium_rec_onnx/inference.onnx",
    "models/pretrained/PP-OCRv6_medium_rec_onnx/inference.yml",
]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path: Path, default=None):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_config(batches_root: Path = DEFAULT_BATCHES_ROOT) -> dict:
    cfg_path = batches_root / "workbench_config.json"
    cfg = read_json(cfg_path, {}) or {}
    return cfg


def save_config(patch: dict, batches_root: Path = DEFAULT_BATCHES_ROOT) -> dict:
    cfg_path = batches_root / "workbench_config.json"
    cfg = load_config(batches_root)
    cfg.update(patch)
    write_json(cfg_path, cfg)
    return cfg


def find_opencode(cfg: dict | None = None) -> str | None:
    cfg = cfg or {}
    custom = (cfg.get("opencode_path") or "").strip()
    if custom and Path(custom).is_file():
        return custom
    for name in ("opencode", "opencode.exe", "opencode.cmd"):
        found = shutil.which(name)
        if found:
            return found
    home = Path.home()
    candidates = [
        home / ".opencode" / "bin" / "opencode.exe",
        home / "AppData" / "Local" / "Programs" / "opencode" / "opencode.exe",
        home / "AppData" / "Roaming" / "npm" / "opencode.cmd",
        home / ".local" / "bin" / "opencode.exe",
        Path("C:/Program Files/opencode/opencode.exe"),
    ]
    for p in candidates:
        if p.is_file():
            return str(p)
    return None


def find_7z() -> str | None:
    found = shutil.which("7z") or shutil.which("7z.exe")
    if found:
        return found
    for p in (
        Path("C:/Program Files/7-Zip/7z.exe"),
        Path("C:/Program Files (x86)/7-Zip/7z.exe"),
    ):
        if p.is_file():
            return str(p)
    return None


def _resolve_weight_path(root: Path, task: str, fallback_rel: str) -> Path:
    cur = read_json(root / "models" / "current.json", {}) or {}
    row = cur.get(task)
    if isinstance(row, dict) and row.get("path"):
        p = (root / row["path"]).resolve()
        if p.is_file():
            return p
    return (root / fallback_rel).resolve()


def _read_env_key(root: Path, key: str) -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    env_file = root / ".env"
    if not env_file.is_file():
        return ""
    for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return ""


def check_environment(root: Path = ROOT, batches_root: Path = DEFAULT_BATCHES_ROOT) -> dict:
    """快速环境自检（毫秒~百毫秒级，不做重型 import）。"""
    t0 = time.perf_counter()
    cfg = load_config(batches_root)
    items = []

    # 1. Python 解释器
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 10)
    items.append({
        "id": "python",
        "category": "runtime",
        "name": "Python 解释器",
        "ok": py_ok,
        "severity": "error" if not py_ok else "ok",
        "detail": f"Python {py_ver} ({sys.executable})",
        "fix": None if py_ok else "需要 Python 3.10 及以上版本",
    })

    # 2. 核心依赖库（使用 find_spec，避免加载 torch 等重包拖慢启动）
    missing_pkgs = []
    for pkg in REQUIRED_PACKAGES:
        spec = importlib.util.find_spec(pkg["import"])
        ok = spec is not None
        if not ok:
            missing_pkgs.append(pkg["pip"])
        items.append({
            "id": f"pkg_{pkg['import']}",
            "category": "packages",
            "name": f"依赖库：{pkg['label']}",
            "ok": ok,
            "severity": "ok" if ok else "error",
            "detail": f"{pkg['pip']} 已安装" if ok else f"缺少 {pkg['pip']}",
            "pip": pkg["pip"],
        })

    # 3. 开源 OCR 模型权重 (PaddleOCR ONNX)
    paddle_missing = [rel for rel in PADDLE_FILES if not (root / rel).is_file()]
    items.append({
        "id": "paddle_ocr",
        "category": "weights",
        "name": "PaddleOCR 印刷题号权重",
        "ok": len(paddle_missing) == 0,
        "severity": "ok" if not paddle_missing else "error",
        "detail": "PP-OCRv6 ONNX 权重齐全" if not paddle_missing else "缺少: " + ", ".join(paddle_missing),
    })

    # 4. 部署模型权重 (orient / M1 / M2 / M3 / choice / digit)
    missing_weights = []
    for task, label, fallback_rel in WEIGHT_SPECS:
        wp = _resolve_weight_path(root, task, fallback_rel)
        ok = wp.is_file()
        if not ok:
            missing_weights.append(label)
        size_mb = round(wp.stat().st_size / (1024 * 1024), 1) if ok else 0
        items.append({
            "id": f"weight_{task}",
            "category": "weights",
            "name": label,
            "ok": ok,
            "severity": "ok" if ok else "error",
            "detail": f"就绪 ({size_mb} MB)" if ok else f"未找到 {wp.name}",
            "path": str(wp),
        })

    # 5. 大模型视觉 API 配置（豆包 / 小米 MiMo / DeepSeek / 通义 / 智谱 / 自定义中转站）
    vlm_cfg = get_vlm_settings(root)
    has_key = bool(vlm_cfg.get("has_key"))
    masked = vlm_cfg.get("api_key_masked") or ""
    items.append({
        "id": "ark_key",
        "category": "config",
        "name": f"大模型视觉 API ({vlm_cfg.get('provider_label') or vlm_cfg.get('provider')})",
        "ok": has_key,
        "severity": "ok" if has_key else "warn",
        "detail": f"已配置 {vlm_cfg.get('model')} ({masked})" if has_key else "尚未配置 API Key（点击「大模型 API 设置」配置豆包/MiMo/DeepSeek/中转站）",
    })

    # 6. 加密权重包与 7z / opencode 探测（辅助信息）
    archive_001 = find_weight_archive(root)
    seven_zip = find_7z()
    opencode_path = find_opencode(cfg)

    errors = [x for x in items if x["severity"] == "error"]
    warnings = [x for x in items if x["severity"] == "warn"]

    return {
        "checked_at": now_str(),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "ok": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "missing_packages": missing_pkgs,
        "missing_weights": missing_weights + (["PaddleOCR 权重"] if paddle_missing else []),
        "has_encrypted_weights": archive_001 is not None,
        "encrypted_archive": str(archive_001) if archive_001 else "",
        "seven_zip": seven_zip or "",
        "opencode_path": opencode_path or "",
        "items": items,
    }


def benchmark_mirrors(timeout: float = 3.0) -> list[dict]:
    """并发测速 PyPI 官方源与国内镜像源。"""
    def probe(m: dict) -> dict:
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(m["url"], method="HEAD", headers={"User-Agent": "AutoGradeWorkbench/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.status
            ms = round((time.perf_counter() - t0) * 1000, 1)
            return {**m, "ok": 200 <= code < 400, "latency_ms": ms, "error": ""}
        except Exception as exc:
            return {**m, "ok": False, "latency_ms": 99999.0, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=len(PIP_MIRRORS)) as pool:
        results = list(pool.map(probe, PIP_MIRRORS))
    results.sort(key=lambda r: (not r["ok"], r["latency_ms"]))
    return results


def install_packages(packages: list[str], progress_cb=None) -> dict:
    """测速选源后安装缺失的 Python 包。"""
    if not packages:
        return {"ok": True, "message": "没有缺失的依赖包", "mirrors": [], "installed": []}
    mirrors = benchmark_mirrors()
    chosen = mirrors[0] if mirrors and mirrors[0]["ok"] else PIP_MIRRORS[1]
    if progress_cb:
        progress_cb(f"测速完成，选择最快源：{chosen['name']} ({chosen.get('latency_ms', '?')} ms)")

    cmd = [
        sys.executable, "-m", "pip", "install",
        "-i", chosen["url"],
        "--trusted-host", chosen["host"],
        *packages,
    ]
    if progress_cb:
        progress_cb("执行安装：" + " ".join(packages))
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return {
        "ok": proc.returncode == 0,
        "mirror": chosen,
        "mirrors": mirrors,
        "installed": packages if proc.returncode == 0 else [],
        "returncode": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


def save_env_key(key: str, value: str, root: Path = ROOT) -> dict:
    """安全写入或更新项目根目录 .env 中的单个键值（保留其余配置行）。"""
    k = (key or "").strip()
    v = (value or "").strip()
    if not re.fullmatch(r"[A-Z0-9_]{2,64}", k):
        return {"ok": False, "error": "配置项名称不合法"}
    if not v:
        return {"ok": False, "error": "请输入有效的密钥内容"}
    env_file = root / ".env"
    lines = env_file.read_text(encoding="utf-8", errors="ignore").splitlines() if env_file.is_file() else []
    replaced = False
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == k:
            out.append(f"{k}={v}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{k}={v}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[k] = v
    return {"ok": True, "message": f"已保存 {k} 到 .env"}


def find_weight_archive(root: Path = ROOT) -> Path | None:
    """查找 weights/ 目录下的加密权重压缩包（优先单文件 .7z，兼容旧版 .7z.001 分卷）。"""
    wdir = root / "weights"
    if not wdir.is_dir():
        return None
    single = sorted(wdir.glob("*.7z"), key=lambda p: p.stat().st_mtime, reverse=True)
    if single:
        return single[0]
    parts = sorted(wdir.glob("*.7z.001"), key=lambda p: p.stat().st_mtime, reverse=True)
    return parts[0] if parts else None


def unpack_weights(password: str, root: Path = ROOT) -> dict:
    """用本机 7-Zip 解压加密权重包到项目根目录，并按需自动拉取百度官方 PP-OCRv6。"""
    if not password:
        return {"ok": False, "error": "请输入解压密码"}
    seven_zip = find_7z()
    if not seven_zip:
        return {"ok": False, "error": "未找到 7-Zip，请先安装 7-Zip 或求助 AI"}
    archive = find_weight_archive(root)
    if not archive:
        return {"ok": False, "error": "weights/ 目录下未找到加密权重包 (.7z)，请先运行 python tools/下载权重.py 或从 GitHub Releases 下载"}
    cmd = [seven_zip, "x", str(archive), f"-o{root}", f"-p{password}", "-y"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return {"ok": False, "error": "解压失败（请检查密码是否正确）", "detail": (proc.stderr or proc.stdout)[-1500:]}

    # 若缺少官方 PP-OCRv6 权重，尝试自动从百度飞桨官方源补齐
    paddle_missing = [rel for rel in PADDLE_FILES if not (root / rel).is_file()]
    if paddle_missing:
        try:
            import importlib.util
            dl_script = root / "tools" / "下载权重.py"
            if dl_script.is_file():
                spec = importlib.util.spec_from_file_location("_dl_weights", str(dl_script))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.ensure_paddle_ocr(root)
        except Exception as exc:
            return {
                "ok": True,
                "message": f"自训推理权重已解压就绪；官方 PP-OCRv6 自动下载失败（{exc}），请稍后运行 python tools/下载权重.py --only-paddle",
            }
    return {"ok": True, "message": "模型权重已全部就绪"}


def build_agent_prompt(report: dict, root: Path = ROOT, extra_note: str = "") -> str:
    """生成给本地 Agent (opencode 等) 的结构化诊断提示词。"""
    problems = [x for x in report.get("items", []) if x.get("severity") != "ok"]
    lines = [
        "请帮我诊断并修复「自动改卷」项目的本地运行环境问题。",
        "",
        f"- 项目路径：`{root}`",
        f"- 操作系统：{sys.platform}",
        f"- 当前 Python：`{sys.executable}`",
        f"- 检查时间：{report.get('checked_at', now_str())}",
        "",
        "### 检测到的问题",
    ]
    if not problems:
        lines.append("- 环境自检全部通过，用户希望咨询或排查其他使用问题。")
    else:
        for p in problems:
            tag = "错误" if p["severity"] == "error" else "提醒"
            lines.append(f"- [{tag}] {p['name']}：{p['detail']}")
    if extra_note.strip():
        lines.extend(["", "### 用户补充说明", extra_note.strip()])
    lines.extend([
        "",
        "### 期望操作",
        "1. 优先说明问题原因（用通俗中文），再给出或直接执行最小化修复命令。",
        "2. 若需安装 Python 依赖，优先测试并使用国内镜像源（清华/阿里云）。",
        "3. 修复完成后可运行 `python tools/工作台.py check` 验证全部变绿。",
    ])
    return "\n".join(lines)


def copy_to_clipboard(text: str) -> bool:
    """Windows 下写入系统剪贴板（UTF-16LE via PowerShell Set-Clipboard，避免中文乱码）。"""
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
                input=text,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=5,
            )
            return proc.returncode == 0
    except Exception:
        pass
    return False


def launch_opencode(root: Path, prompt: str, batches_root: Path = DEFAULT_BATCHES_ROOT) -> dict:
    """复制提示词到剪贴板，并在项目目录打开 opencode 终端（不劫持键盘，安全可靠）。"""
    copied = copy_to_clipboard(prompt)
    cfg = load_config(batches_root)
    oc = find_opencode(cfg)
    launched = False
    launch_msg = ""
    if oc and os.name == "nt":
        try:
            # 用新终端窗口在项目根目录打开 opencode
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "OpenCode AI 助手", "/D", str(root), oc],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            launched = True
            launch_msg = f"已打开 {oc}，诊断提示词已复制到剪贴板，直接 Ctrl+V 粘贴发送即可。"
        except Exception as exc:
            launch_msg = f"自动拉起 opencode 失败（{exc}），但诊断提示词已复制到剪贴板，请手动打开 opencode 后粘贴。"
    elif not oc:
        launch_msg = "未自动检测到 opencode 路径（可在设置中指定），但诊断提示词已复制到剪贴板，打开你的 AI 助手后直接 Ctrl+V 粘贴即可。"
    return {
        "ok": True,
        "copied": copied,
        "launched": launched,
        "opencode_path": oc or "",
        "message": launch_msg,
        "prompt": prompt,
    }


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sanitize_token(text: str, fallback: str = "未分类") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", (text or "").strip())
    return cleaned[:24] or fallback


def ingest_batch(
    source_dir: str | Path,
    subject: str,
    batches_root: Path = DEFAULT_BATCHES_ROOT,
    batch_name: str | None = None,
    progress_cb=None,
) -> dict:
    """非破坏性导入新一批试卷图片：SHA256 去重 + 统一时间戳/学科/序号命名 + manifest.csv。"""
    src = Path(source_dir).expanduser().resolve()
    if not src.is_dir():
        raise FileNotFoundError(f"找不到源文件夹：{src}")

    subj = _sanitize_token(subject, "其他")
    day = datetime.now().strftime("%Y%m%d")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_id = _sanitize_token(batch_name, f"{stamp}_{subj}") if batch_name else f"{stamp}_{subj}"

    batch_dir = batches_root / batch_id
    images_dir = batch_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # 收集源目录图片（支持一层及子目录，按相对路径稳定排序）
    src_files = sorted(
        p for p in src.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if not src_files:
        raise ValueError(f"文件夹中没有找到图片文件（支持 jpg/png/bmp/webp）：{src}")

    # 读取已有批次（如果是追加到同一批次）的去重集合
    manifest_path = batch_dir / "manifest.csv"
    seen_hashes: set[str] = set()
    existing_rows: list[dict] = []
    if manifest_path.is_file():
        with manifest_path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                existing_rows.append(row)
                if row.get("sha256"):
                    seen_hashes.add(row["sha256"])

    seq = len(existing_rows)
    imported = 0
    skipped_dup = 0
    new_rows = []
    imported_at = now_str()

    # 延迟导入 Pillow，仅在需要转格式（png/bmp/webp -> jpg）时使用
    pil_image = None
    for idx, fpath in enumerate(src_files, 1):
        digest = _file_sha256(fpath)
        if digest in seen_hashes:
            skipped_dup += 1
            continue
        seen_hashes.add(digest)
        seq += 1
        new_name = f"{day}_{subj}_{seq:04d}.jpg"
        dest = images_dir / new_name
        if fpath.suffix.lower() in (".jpg", ".jpeg"):
            shutil.copy2(fpath, dest)
        else:
            if pil_image is None:
                from PIL import Image
                pil_image = Image
            with pil_image.open(fpath) as im:
                im.convert("RGB").save(dest, format="JPEG", quality=95, subsampling=0)
        row = {
            "new_name": new_name,
            "batch": batch_id,
            "subject": subj,
            "source": str(src),
            "orig_name": str(fpath.relative_to(src)),
            "sha256": digest,
            "imported_at": imported_at,
        }
        new_rows.append(row)
        imported += 1
        if progress_cb and (idx % 10 == 0 or idx == len(src_files)):
            progress_cb(f"整理入库 {idx}/{len(src_files)}（新增 {imported}，跳过重复 {skipped_dup}）")

    all_rows = existing_rows + new_rows
    fields = ["new_name", "batch", "subject", "source", "orig_name", "sha256", "imported_at"]
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(all_rows)

    meta = {
        "batch_id": batch_id,
        "subject": subj,
        "source": str(src),
        "created_at": imported_at,
        "total_images": len(all_rows),
        "last_imported": imported,
        "last_skipped_dup": skipped_dup,
        "images_dir": str(images_dir),
        "recognize_dir": str(batch_dir / "recognized"),
    }
    write_json(batch_dir / "batch.json", meta)
    set_active_batch(batch_id, batches_root=batches_root)
    return meta


def list_batches(batches_root: Path = DEFAULT_BATCHES_ROOT) -> list[dict]:
    if not batches_root.is_dir():
        return []
    out = []
    for p in sorted(batches_root.iterdir(), reverse=True):
        meta = read_json(p / "batch.json")
        if isinstance(meta, dict):
            rec_dir = Path(meta.get("recognize_dir") or (p / "recognized"))
            meta["has_recognition"] = (rec_dir / "results.json").is_file()
            out.append(meta)
    return out


def set_active_batch(batch_id: str, batches_root: Path = DEFAULT_BATCHES_ROOT) -> dict | None:
    meta = read_json(batches_root / batch_id / "batch.json")
    if not meta:
        return None
    write_json(batches_root / "active.json", {"batch_id": batch_id, "updated_at": now_str()})
    return meta


def get_active_batch(batches_root: Path = DEFAULT_BATCHES_ROOT) -> dict | None:
    active = read_json(batches_root / "active.json")
    if isinstance(active, dict) and active.get("batch_id"):
        meta = read_json(batches_root / active["batch_id"] / "batch.json")
        if meta:
            rec_dir = Path(meta.get("recognize_dir") or (batches_root / active["batch_id"] / "recognized"))
            meta["has_recognition"] = (rec_dir / "results.json").is_file()
            return meta
    batches = list_batches(batches_root)
    return batches[0] if batches else None


def _load_vlm_module(root: Path = ROOT):
    pipe_dir = str(root / "pipeline")
    if pipe_dir not in sys.path:
        sys.path.insert(0, pipe_dir)
    from common import recognition_api as rec_api  # noqa: E402
    return rec_api


def get_vlm_settings(root: Path = ROOT) -> dict:
    """获取当前大模型视觉 API 配置（对前端脱敏返回 Key）及所有运营商预设。"""
    rec_api = _load_vlm_module(root)
    cfg = rec_api.load_vlm_config(root)
    key = cfg.get("api_key") or ""
    masked = f"{key[:4]}****{key[-4:]}" if len(key) >= 8 else ("已配置" if key else "")
    return {
        "provider": cfg["provider"],
        "provider_label": cfg["provider_label"],
        "base_url": cfg["base_url"],
        "endpoint": cfg["endpoint"],
        "model": cfg["model"],
        "has_key": bool(key),
        "api_key_masked": masked,
        "presets": list(rec_api.VLM_PRESETS.values()),
    }


def save_vlm_settings(
    provider: str = "doubao",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    root: Path = ROOT,
) -> dict:
    """保存多运营商/自定义中转站视觉 API 配置到项目 .env（留空 api_key 则保留原 Key）。"""
    rec_api = _load_vlm_module(root)
    prov = (provider or "doubao").strip()
    if prov not in rec_api.VLM_PRESETS:
        prov = "custom"
    preset = rec_api.VLM_PRESETS[prov]
    eff_base = (base_url or "").strip() or preset["base_url"]
    eff_model = (model or "").strip() or preset["model"]
    if not eff_base:
        return {"ok": False, "error": "请填写站点地址（Base URL）"}
    if not eff_model:
        return {"ok": False, "error": "请填写模型名称"}

    save_env_key("VLM_PROVIDER", prov, root=root)
    save_env_key("VLM_BASE_URL", eff_base, root=root)
    save_env_key("VLM_MODEL", eff_model, root=root)
    new_key = (api_key or "").strip()
    if new_key:
        save_env_key("VLM_API_KEY", new_key, root=root)
        if prov == "doubao":
            save_env_key("ARK_API_KEY", new_key, root=root)

    return {"ok": True, "message": f"已保存 {preset['label']} 配置（模型：{eff_model}）", "config": get_vlm_settings(root)}


def test_vlm_settings(
    provider: str = "doubao",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    root: Path = ROOT,
) -> dict:
    """测试当前或表单填写的视觉 API 连通性（未传 api_key 时自动使用已保存 Key）。"""
    rec_api = _load_vlm_module(root)
    eff_key = (api_key or "").strip()
    if not eff_key:
        eff_key = rec_api.load_vlm_config(root).get("api_key", "")
    return rec_api.test_vlm_connection(
        provider=provider,
        base_url=base_url,
        api_key=eff_key,
        model=model,
    )


def _count_recognition_progress(rec_dir: Path) -> tuple[dict, Counter]:
    """统计某次识别结果目录的审阅进度（按整页确认计数）与人员贡献。"""
    contributors = Counter()
    results = read_json(rec_dir / "results.json")
    if not isinstance(results, dict):
        return {"available": False, "total": 0, "done": 0, "remaining": 0, "pages": 0, "done_pages": 0, "total_items": 0}, contributors
    pages = results.get("pages") or []
    fb = read_json(rec_dir / "feedback.json", {}) or {}
    fb_items = fb.get("items") or {}

    total_items = 0
    done_pages = 0
    for p in pages:
        p_items = p.get("items") or []
        total_items += len(p_items)
        page_rec = fb_items.get(p["id"])
        if page_rec and page_rec.get("label"):
            done_pages += 1
            who = (page_rec.get("reviewer") or "未署名").strip()
            contributors[who] += 1
        elif p_items and all(bool((fb_items.get(it["id"]) or {}).get("label")) for it in p_items):
            # 兼容旧版逐题打卡记录
            done_pages += 1
            first_rec = fb_items.get(p_items[0]["id"]) or {}
            who = (first_rec.get("reviewer") or "未署名").strip()
            contributors[who] += 1
    total_pages = len(pages)
    return {
        "available": True,
        "dir": str(rec_dir),
        "total": total_pages,
        "done": done_pages,
        "remaining": max(0, total_pages - done_pages),
        "pages": total_pages,
        "done_pages": done_pages,
        "total_items": total_items,
    }, contributors


def _count_annotate_progress(root: Path, m_name: str) -> tuple[dict, Counter]:
    """快速统计 data/<m_name> 的标框进度与审核人贡献。"""
    contributors = Counter()
    base = root / "data" / m_name
    img_dir = base / "images"
    if not img_dir.is_dir():
        return {"available": False, "total": 0, "done": 0, "remaining": 0}, contributors
    total = sum(1 for f in os.listdir(img_dir) if f.lower().endswith(".jpg"))
    done = 0
    prog = base / "progress.csv"
    if prog.is_file():
        with prog.open("r", encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("status") in ("已审", "特殊"):
                    done += 1
                    who = (r.get("reviewer") or "").strip()
                    if who:
                        contributors[who] += 1
    return {
        "available": total > 0,
        "total": total,
        "done": done,
        "remaining": max(0, total - done),
    }, contributors


_DIR_STAT_CACHE: dict[str, tuple[float, float, dict]] = {}


def _count_choice_or_digit(root: Path, folder: str, csv_name: str) -> dict:
    """快速统计 choice/digit 审核进度（带目录 mtime + 60 秒内存缓存，避免万级小文件反复扫盘）。"""
    base = root / "data" / folder
    csv_path = base / csv_name
    if not csv_path.is_file():
        return {"available": False, "total": 0, "done": 0, "remaining": 0}
    log_dir = base / "log"
    try:
        dir_mtime = log_dir.stat().st_mtime if log_dir.is_dir() else 0.0
    except OSError:
        dir_mtime = 0.0
    cache_key = str(base)
    now_ts = time.time()
    cached = _DIR_STAT_CACHE.get(cache_key)
    if cached and cached[0] == dir_mtime and (now_ts - cached[1]) < 60.0:
        return dict(cached[2])

    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        total = sum(1 for _ in csv.DictReader(fh))
    done = 0
    if log_dir.is_dir():
        # 快速判定：只读每个日志最后 256 字节即可判断是否存在「审核/确认」记录
        with os.scandir(log_dir) as it:
            for entry in it:
                if not entry.name.endswith(".log"):
                    continue
                try:
                    with open(entry.path, "rb") as fh:
                        fh.seek(0, os.SEEK_END)
                        size = fh.tell()
                        fh.seek(max(0, size - 512))
                        tail = fh.read().decode("utf-8", errors="ignore")
                    if "审核" in tail or "确认" in tail:
                        done += 1
                except OSError:
                    pass
    res = {
        "available": total > 0,
        "total": total,
        "done": min(done, total),
        "remaining": max(0, total - done),
    }
    _DIR_STAT_CACHE[cache_key] = (dir_mtime, now_ts, res)
    return dict(res)


def collect_tasks_and_leaderboard(
    root: Path = ROOT,
    batches_root: Path = DEFAULT_BATCHES_ROOT,
    services: dict | None = None,
    presence: dict | None = None,
) -> dict:
    """汇总当前批次、4 类审核任务进度与人员贡献榜。"""
    services = services or {}
    presence = presence or {}
    active = get_active_batch(batches_root)
    leaderboard = Counter()

    # 1. 识别验收（优先看当前活跃批次的 recognized/）
    rec_dir = Path(active["recognize_dir"]) if active else (root / "batches" / "_none_")
    rec_stat, rec_contrib = _count_recognition_progress(rec_dir)
    leaderboard.update(rec_contrib)

    # 2. 标框审核（M1 / M2 / M3 汇总）
    m_stats = {}
    m_total = m_done = 0
    for m in ("m1", "m2", "m3"):
        st, contrib = _count_annotate_progress(root, m)
        m_stats[m] = st
        m_total += st["total"]
        m_done += st["done"]
        leaderboard.update(contrib)

    # 3. 涂黑审核 & 数字审核
    choice_stat = _count_choice_or_digit(root, "choice", "manifest.csv")
    digit_stat = _count_choice_or_digit(root, "digit", "labels.csv")

    tasks = [
        {
            "id": "review",
            "tier": "primary",
            "name": "人工复审（核对答案 + 调框）",
            "desc": "逐张核对识别结果，点错字直接改，按 E 调框，按 D 保存翻下一页",
            "guide": "",
            "port": 8904,
            "available": rec_stat["available"],
            "total": rec_stat["total"],
            "done": rec_stat["done"],
            "remaining": rec_stat["remaining"],
            "unit": "页",
            "extra": f"共 {rec_stat.get('total_items', 0)} 道题" if rec_stat["available"] else "请先导入试卷并完成自动识别",
            "running": bool(services.get("review", {}).get("running")),
        },
        {
            "id": "choice",
            "tier": "primary",
            "name": "选择题涂卡审核",
            "desc": "按选项分组批量检查选择题填涂，划过即确认，认错的点一下修改",
            "guide": "",
            "port": 8905,
            "available": choice_stat["available"],
            "total": choice_stat["total"],
            "done": choice_stat["done"],
            "remaining": choice_stat["remaining"],
            "unit": "条",
            "extra": "选择题涂卡题库" if choice_stat["available"] else "暂无涂卡审核数据",
            "running": bool(services.get("choice", {}).get("running")),
        },
        {
            "id": "annotate",
            "tier": "secondary",
            "name": "批量标框工具",
            "desc": "集中批量修改版面、选择题和大题区域框（平时改单张请在「人工复审」里按 E 调框）",
            "guide": "",
            "port": 8900,
            "available": m_total > 0,
            "total": m_total,
            "done": m_done,
            "remaining": max(0, m_total - m_done),
            "unit": "张",
            "extra": f"版面 {m_stats['m1']['done']}/{m_stats['m1']['total']} · 选择题 {m_stats['m2']['done']}/{m_stats['m2']['total']} · 大题 {m_stats['m3']['done']}/{m_stats['m3']['total']}",
            "running": bool(services.get("annotate", {}).get("running")),
        },
    ]

    # 合并在线人员与历史贡献
    now_ts = time.time()
    online_users = []
    for cid, info in list(presence.items()):
        if now_ts - info.get("seen", 0) <= 65:
            online_users.append({
                "name": info.get("name") or "成员",
                "role": info.get("role") or "collaborator",
                "task": info.get("task") or "总览",
            })

    people = [
        {"name": name, "count": count}
        for name, count in leaderboard.most_common(20)
    ]

    return {
        "active_batch": active,
        "batches": list_batches(batches_root)[:15],
        "tasks": tasks,
        "online_users": online_users,
        "leaderboard": people,
    }


def detect_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        finally:
            s.close()
    except OSError:
        pass
    return "127.0.0.1"


def emit_event(step: str, status: str, message: str, detail: dict | None = None, hub_url: str = "http://127.0.0.1:8888") -> bool:
    """CLI 执行动作时向本地工作台推送事件，触发网页实时高亮动效。"""
    payload = json.dumps({
        "source": "cli",
        "step": step,
        "status": status,
        "message": message,
        "detail": detail or {},
        "ts": now_str(),
    }, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{hub_url.rstrip('/')}/api/event",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=1.2):
            return True
    except Exception:
        return False

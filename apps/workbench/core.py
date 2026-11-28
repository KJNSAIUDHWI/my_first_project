# -*- coding: utf-8 -*-
"""工作台核心逻辑（CLI 与 Web 控制中心共用）。

职责：
1. 快速环境自检：Python、核心依赖包、部署模型权重（含 PaddleOCR ONNX）、.env 密钥、opencode 探测。
2. 双通道修复：
   - 自动测速官方 PyPI vs 清华/阿里镜像并安装缺失依赖；
   - 生成结构化诊断提示词、写入剪贴板并尝试唤起本地 opencode。
3. 标准化数据入库：非破坏性复制、SHA256 去重、按 `YYYYMMDD_学科_0001.jpg` 统一命名并写 manifest.csv。
4. 进度与多人工作量汇总：读取当前批次识别验收、M1-M3 标框、涂卡审核、数字审核进度与人员贡献榜。
5. CLI -> GUI 事件上报（供 SSE 实时高亮）。
"""
from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
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
    {"import": "torch", "pip": "torch", "dist": "torch", "label": "PyTorch 深度学习框架"},
    {"import": "ultralytics", "pip": "ultralytics==8.4.96", "dist": "ultralytics", "label": "YOLO 检测框架"},
    {"import": "cv2", "pip": "opencv-contrib-python==4.10.0.84", "dist": "opencv-contrib-python", "label": "OpenCV 图像处理"},
    {"import": "PIL", "pip": "Pillow", "dist": "Pillow", "label": "Pillow 图像库"},
    {"import": "numpy", "pip": "numpy>=2.0.0,<2.4", "dist": "numpy", "label": "NumPy 数值计算"},
    {"import": "scipy", "pip": "scipy", "dist": "scipy", "label": "SciPy 匈牙利匹配算法"},
    {"import": "yaml", "pip": "PyYAML", "dist": "PyYAML", "label": "PyYAML 配置文件解析"},
    {"import": "onnxruntime", "pip": "onnxruntime", "dist": "onnxruntime", "label": "ONNX Runtime (PaddleOCR 后端)"},
    {"import": "paddleocr", "pip": "paddleocr>=3.7.0,<3.8.0", "dist": "paddleocr", "label": "PaddleOCR 印刷题号识别"},
    {"import": "requests", "pip": "requests", "dist": "requests", "label": "Requests 网络请求"},
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
    ("choice", "选择题涂卡识别模型", "models/cnn/choice.pt"),
]
LEGACY_WEIGHT_FALLBACKS = {}

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


def write_json(path: Path, data, retries: int = 80, delay: float = 0.04) -> None:
    """原子写 JSON（PID+线程唯一临时文件 + Windows PermissionError 重试）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.writing.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    last_exc = None
    for attempt in range(max(1, int(retries))):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(delay)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    raise last_exc  # type: ignore[misc]


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


def _discover_zcode_registry_dirs() -> list[Path]:
    """从 Windows 卸载注册表读取 ZCode 自定义安装目录（如 D:\\Dev\\Zcode）。"""
    if os.name != "nt":
        return []
    found: list[Path] = []
    seen: set[str] = set()
    try:
        import winreg
    except Exception:
        return []
    roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, subkey in roots:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                n_sub, _, _ = winreg.QueryInfoKey(key)
                for idx in range(n_sub):
                    try:
                        name = winreg.EnumKey(key, idx)
                        with winreg.OpenKey(key, name) as sk:
                            try:
                                disp, _ = winreg.QueryValueEx(sk, "DisplayName")
                            except OSError:
                                disp = ""
                            if "zcode" not in str(disp or "").lower():
                                continue
                            for val_name in ("InstallLocation", "DisplayIcon", "UninstallString"):
                                try:
                                    raw, _ = winreg.QueryValueEx(sk, val_name)
                                except OSError:
                                    continue
                                s = str(raw or "").strip()
                                if not s:
                                    continue
                                if s.startswith('"') and '"' in s[1:]:
                                    s = s[1 : s.index('"', 1)]
                                else:
                                    s = s.split(",")[0].strip()
                                    if val_name == "UninstallString" and " /" in s:
                                        s = s.split(" /", 1)[0].strip()
                                p = Path(s)
                                cand = p if p.is_dir() else p.parent
                                key_norm = str(cand).lower()
                                if cand.is_dir() and key_norm not in seen:
                                    seen.add(key_norm)
                                    found.append(cand)
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def find_zcode(cfg: dict | None = None, *, probe_version: bool = False) -> dict:
    """探测本机 ZCode CLI（支持桌面端 Electron node-bundle、注册表自定义路径与独立 CLI）。"""
    cfg = cfg or {}
    custom_exe = str(cfg.get("zcode_exe") or "").strip()
    custom_cli = str(cfg.get("zcode_cli") or "").strip()
    result = {
        "available": False,
        "mode": "",
        "exe": "",
        "cli": "",
        "builtin_provider_config": "",
        "version": "",
    }

    def _detect_builtin_cfg(cli_path: str) -> str:
        try:
            p = Path(cli_path).resolve()
            # 典型结构：<ZCode>/resources/glm/zcode.cjs -> <ZCode>/resources/config/provider/zcode-builtin.json
            for cand in (
                p.parent / "provider" / "zcode-builtin.json",
                p.parent.parent / "config" / "provider" / "zcode-builtin.json",
            ):
                if cand.is_file():
                    return str(cand)
        except Exception:
            pass
        return ""

    # 1. 配置显式覆盖
    if custom_exe and custom_cli and Path(custom_exe).is_file() and Path(custom_cli).is_file():
        result.update({
            "available": True,
            "mode": "electron_bundle",
            "exe": custom_exe,
            "cli": custom_cli,
            "builtin_provider_config": _detect_builtin_cfg(custom_cli),
        })
    elif custom_cli and Path(custom_cli).is_file() and custom_cli.lower().endswith((".exe", ".cmd", ".bat")):
        result.update({"available": True, "mode": "cli", "exe": custom_cli, "cli": custom_cli})

    # 2. 常见桌面端 Electron bundle 安装路径 + Windows 卸载注册表自定义目录（ZCode.exe + resources/glm/zcode.cjs）
    if not result["available"]:
        local_app = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        prog_files = [
            *_discover_zcode_registry_dirs(),
            Path(os.environ.get("ProgramFiles") or "C:/Program Files") / "ZCode",
            Path(os.environ.get("ProgramFiles(x86)") or "C:/Program Files (x86)") / "ZCode",
            local_app / "Programs" / "ZCode",
            Path("C:/Program Files/ZCode"),
        ]
        for base in prog_files:
            exe_p = base / "ZCode.exe"
            cli_p = base / "resources" / "glm" / "zcode.cjs"
            if exe_p.is_file() and cli_p.is_file():
                result.update({
                    "available": True,
                    "mode": "electron_bundle",
                    "exe": str(exe_p),
                    "cli": str(cli_p),
                    "builtin_provider_config": _detect_builtin_cfg(str(cli_p)),
                })
                break

    # 3. 独立 PATH 或 ~/.zcode/bin 包装脚本
    if not result["available"]:
        for name in ("zcode", "zcode.cmd", "zcode.exe", "zcodecli", "zcodecli.cmd", "zcodecli.exe"):
            found = shutil.which(name)
            if found:
                result.update({"available": True, "mode": "cli", "exe": found, "cli": found})
                break
    if not result["available"]:
        for cand_cmd in (
            Path.home() / ".zcode" / "bin" / "zcode.cmd",
            Path.home() / ".zcode" / "bin" / "zcodecli.cmd",
        ):
            if cand_cmd.is_file():
                result.update({"available": True, "mode": "cli", "exe": str(cand_cmd), "cli": str(cand_cmd)})
                break

    if result["available"] and probe_version:
        try:
            cmd = [result["exe"], result["cli"], "--version"] if result["mode"] == "electron_bundle" else [result["exe"], "--version"]
            env = dict(os.environ, ELECTRON_RUN_AS_NODE="1") if result["mode"] == "electron_bundle" else dict(os.environ)
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, env=env)
            if proc.returncode == 0:
                lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
                if lines:
                    result["version"] = lines[-1][:40]
        except Exception:
            pass
    return result


def build_zcode_command(
    prompt: str,
    root: Path = ROOT,
    *,
    mode: str = "plan",
    session_id: str = "",
    attach: list[str] | None = None,
    cfg: dict | None = None,
) -> dict:
    """构造安全的 ZCode headless 调用契约（严禁默认 yolo，强制 --mode plan 或 guarded）。"""
    safe_mode = mode if mode in ("plan", "guarded") else "plan"
    zc = find_zcode(cfg)
    if not zc.get("available"):
        return {
            "ok": False,
            "provider": "clipboard",
            "mode": safe_mode,
            "error": "未检测到本机 ZCode CLI（可安装 ZCode 桌面端或在配置中指定 zcode_exe/zcode_cli）",
            "zcode": zc,
        }
    cwd = str(Path(root).resolve())
    if zc["mode"] == "electron_bundle":
        cmd = [zc["exe"], zc["cli"]]
        env_patch = {"ELECTRON_RUN_AS_NODE": "1"}
        builtin_cfg = str(zc.get("builtin_provider_config") or "").strip()
        if builtin_cfg and Path(builtin_cfg).is_file():
            if not os.environ.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"):
                env_patch["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = builtin_cfg
            if not os.environ.get("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"):
                base_dir = Path(os.environ.get("ZCODE_DATA_BASE_DIR") or Path.home())
                env_patch["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = str(base_dir / ".zcode" / "v2" / "provider_config.json")
    else:
        cmd = [zc["exe"]]
        env_patch = {}
    cmd.extend([
        "-p", prompt,
        "--cwd", cwd,
        "--json",
        "--no-color",
        "--locale", "zh-CN",
        "--mode", safe_mode,
    ])
    if session_id:
        cmd.extend(["--resume", str(session_id).strip()])
    for ap in (attach or []):
        p = Path(ap)
        if p.is_file():
            cmd.extend(["--attach", str(p.resolve())])
    return {
        "ok": True,
        "provider": "zcode",
        "mode": safe_mode,
        "cwd": cwd,
        "cmd": cmd,
        "env_patch": env_patch,
        "zcode": zc,
    }


def _pid_alive(pid: int) -> bool:
    """检查本地进程 PID 是否仍存活（Windows/POSIX 兼容）。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok and code.value == STILL_ACTIVE)
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_pipeline_lease(batches_root: Path = DEFAULT_BATCHES_ROOT) -> dict | None:
    """读取当前任务租约文件，并自动标记是否过期/进程已退出。"""
    lock_path = Path(batches_root) / ".pipeline.lock"
    data = read_json(lock_path)
    if not isinstance(data, dict):
        return None
    pid = int(data.get("pid") or 0)
    ts = float(data.get("ts") or 0.0)
    ttl = float(data.get("ttl_seconds") or 1800.0)
    expired = (time.time() - ts) > ttl if ts > 0 else True
    alive = _pid_alive(pid) if pid else False
    data["stale"] = bool(expired or not alive)
    return data


def acquire_pipeline_lease(
    kind: str,
    batch_id: str = "",
    *,
    owner: str = "workbench",
    batches_root: Path = DEFAULT_BATCHES_ROOT,
    ttl_seconds: int = 1800,
) -> dict:
    """获取跨进程共享任务租约（防 GUI/CLI/AI 同时跑同一批次触发重复扣费与结果覆盖）。"""
    batches_root = Path(batches_root)
    batches_root.mkdir(parents=True, exist_ok=True)
    lock_path = batches_root / ".pipeline.lock"
    token = f"{os.getpid()}_{threading.get_ident()}_{int(time.time() * 1000)}"
    payload = {
        "token": token,
        "pid": os.getpid(),
        "owner": owner,
        "kind": kind,
        "batch_id": batch_id,
        "acquired_at": now_str(),
        "ts": time.time(),
        "ttl_seconds": int(ttl_seconds),
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    for _ in range(3):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
            return {"ok": True, "lease": payload}
        except FileExistsError:
            cur = read_pipeline_lease(batches_root)
            if cur is None or cur.get("stale"):
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            return {
                "ok": False,
                "error": f"已有任务运行中（{cur.get('owner')}/{cur.get('kind')}，批次 {cur.get('batch_id') or '-'}，PID {cur.get('pid')}），请等待完成后再试",
                "lease": cur,
            }
        except OSError as exc:
            return {"ok": False, "error": f"无法创建任务租约锁：{exc}"}
    return {"ok": False, "error": "已有任务运行中，获取任务租约失败"}


def release_pipeline_lease(token: str | None = None, batches_root: Path = DEFAULT_BATCHES_ROOT) -> bool:
    """释放本进程持有的任务租约（若传入 token 则严格校验匹配后再删）。"""
    lock_path = Path(batches_root) / ".pipeline.lock"
    if not lock_path.is_file():
        return True
    cur = read_json(lock_path)
    if token and isinstance(cur, dict) and cur.get("token") and cur.get("token") != token:
        return False
    try:
        lock_path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def collect_machine_info(root: Path = ROOT, batches_root: Path = DEFAULT_BATCHES_ROOT, *, probe_zcode_version: bool = False) -> dict:
    """采集本机硬件/Python/依赖/ZCode/项目与服务信息（脱敏返回，供 AI 帮手与 doctor 读取）。"""
    import platform
    diag_mod = _load_diag_module(root)
    hw = diag_mod.probe_compute_hardware()
    cfg = load_config(batches_root)
    zc = find_zcode(cfg, probe_version=probe_zcode_version)
    vlm = get_vlm_settings(root)
    active = get_active_batch(batches_root)
    pkg_versions = {}
    for p in REQUIRED_PACKAGES:
        try:
            pkg_versions[p["dist"]] = importlib.metadata.version(p["dist"])
        except Exception:
            pkg_versions[p["dist"]] = ""
    return {
        "host": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "arch": platform.machine(),
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "hardware": hw,
        "packages": pkg_versions,
        "tools": {
            "seven_zip": find_7z() or "",
            "zcode": zc,
            "opencode": find_opencode(cfg) or "",
        },
        "project_root": str(Path(root).resolve()),
        "batches_root": str(Path(batches_root).resolve()),
        "active_batch": {
            "batch_id": active.get("batch_id"),
            "subject": active.get("subject"),
            "total_images": active.get("total_images"),
            "recognize_dir": active.get("recognize_dir"),
        } if active else None,
        "vlm": {
            "provider": vlm.get("provider"),
            "provider_label": vlm.get("provider_label"),
            "model": vlm.get("model"),
            "has_key": vlm.get("has_key"),
            "api_key_masked": vlm.get("api_key_masked"),
        },
        "ports": {"workbench": 8888, "review": 8904, "annotate": 8900, "choice": 8905, "monitor": 8901},
    }


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
    primary = (root / fallback_rel).resolve()
    if primary.is_file():
        return primary
    for legacy_rel in LEGACY_WEIGHT_FALLBACKS.get(task, ()):
        cand = (root / legacy_rel).resolve()
        if cand.is_file():
            return cand
    return primary


def _read_env_key(root: Path, key: str) -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    env_file = root / ".env"
    if not env_file.is_file():
        return ""
    for line in env_file.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return ""


def _load_diag_module(root: Path = ROOT):
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    from engine import diagnostics as diag_mod  # noqa: E402
    return diag_mod


def batch_root_dir(batch: dict) -> Path:
    """稳健获取批次根目录（兼容 batch_dir / recognize_dir / images_dir）。"""
    if isinstance(batch, dict):
        if batch.get("batch_dir"):
            return Path(batch["batch_dir"]).resolve()
        if batch.get("recognize_dir"):
            return Path(batch["recognize_dir"]).resolve().parent
        if batch.get("images_dir"):
            return Path(batch["images_dir"]).resolve().parent
    return DEFAULT_BATCHES_ROOT.resolve()


def get_work_mode(batches_root: Path = DEFAULT_BATCHES_ROOT) -> str:
    cfg = load_config(batches_root)
    mode = str(cfg.get("work_mode") or "grading").strip().lower()
    return mode if mode in ("grading", "iteration") else "grading"


def resolve_training_db_dir(root: Path = ROOT, batches_root: Path = DEFAULT_BATCHES_ROOT) -> Path:
    cfg = load_config(batches_root)
    raw = str(cfg.get("training_db_dir") or "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = (root / p).resolve()
        return p
    return (root / "data").resolve()


def get_training_db_status(root: Path = ROOT, batches_root: Path = DEFAULT_BATCHES_ROOT) -> dict:
    """返回本地训练数据库位置、各子库样本数量与当前回流开关状态。"""
    cfg = load_config(batches_root)
    mode = get_work_mode(batches_root)
    db_dir = resolve_training_db_dir(root, batches_root)
    default_dir = (root / "data").resolve()
    enabled = bool(cfg.get("training_db_enabled", True))

    counts: dict[str, int] = {"m1": 0, "m2": 0, "m3": 0, "choice": 0}
    for task in ("m1", "m2", "m3"):
        img_dir = db_dir / task / "images"
        if img_dir.is_dir():
            try:
                counts[task] = sum(1 for f in img_dir.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS)
            except OSError:
                counts[task] = 0
    choice_csv = db_dir / "choice" / "labels.csv"
    if choice_csv.is_file():
        try:
            with choice_csv.open("r", encoding="utf-8", errors="ignore") as fh:
                counts["choice"] = max(0, sum(1 for _ in fh) - 1)
        except OSError:
            counts["choice"] = 0

    return {
        "work_mode": mode,
        "training_db_dir": str(db_dir),
        "default_dir": str(default_dir),
        "is_default": db_dir == default_dir,
        "exists": db_dir.is_dir(),
        "enabled": enabled,
        "allow_sync_data": bool(mode == "iteration" and enabled),
        "counts": counts,
        "total_samples": sum(counts.values()),
    }


def get_infer_settings(batches_root: Path = DEFAULT_BATCHES_ROOT) -> dict:
    """读取推理设备、并发、内存暂存 Crop 与工作模式配置（默认 auto 优先 GPU）。"""
    cfg = load_config(batches_root)
    device = str(cfg.get("infer_device") or "auto").strip() or "auto"
    mode = get_work_mode(batches_root)
    try:
        yolo_batch = max(0, min(16, int(cfg.get("yolo_batch", 0) or 0)))
    except (TypeError, ValueError):
        yolo_batch = 0
    try:
        cpu_workers = max(0, min(16, int(cfg.get("cpu_workers", 0) or 0)))
    except (TypeError, ValueError):
        cpu_workers = 0
    try:
        api_concurrency = max(1, min(12, int(cfg.get("api_concurrency", 4) or 4)))
    except (TypeError, ValueError):
        api_concurrency = 4
    if "memory_crops" in cfg:
        mem_crops = bool(cfg.get("memory_crops"))
    else:
        mem_crops = mode == "grading"
    return {
        "infer_device": device,
        "yolo_batch": yolo_batch,
        "cpu_workers": cpu_workers,
        "api_concurrency": api_concurrency,
        "work_mode": mode,
        "memory_crops": mem_crops,
        "last_benchmark": cfg.get("last_benchmark") or None,
    }


def _pkg_version(dist_name: str) -> str:
    try:
        return importlib.metadata.version(dist_name)
    except Exception:
        return ""


_IMPORT_PROBE_CACHE: dict[str, tuple[float, dict]] = {}


def verify_runtime_imports(force: bool = False, timeout: float = 45.0) -> dict:
    """在独立子进程中执行真实 import 校验（抓出 DLL/依赖树缺失与版本冲突），带 120 秒缓存。"""
    key = sys.executable
    now_ts = time.time()
    cached = _IMPORT_PROBE_CACHE.get(key)
    if cached and not force and (now_ts - cached[0]) < 120.0:
        return dict(cached[1])
    code = (
        "import json, os, sys\n"
        "os.environ.setdefault('PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK', 'True')\n"
        "mods = ['torch', 'ultralytics', 'cv2', 'PIL', 'numpy', 'scipy', 'yaml', 'onnxruntime', 'paddleocr', 'requests']\n"
        "out = {'ok': True, 'modules': {}, 'gpu': {'cuda': False, 'gpu_name': '', 'vram_gb': 0.0}}\n"
        "for m in mods:\n"
        "    try:\n"
        "        mod = __import__(m)\n"
        "        ver = getattr(mod, '__version__', '')\n"
        "        out['modules'][m] = {'ok': True, 'version': str(ver)}\n"
        "    except Exception as exc:\n"
        "        out['ok'] = False\n"
        "        out['modules'][m] = {'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)[:220]}\n"
        "if out['modules'].get('torch', {}).get('ok'):\n"
        "    try:\n"
        "        import torch\n"
        "        if torch.cuda.is_available() and torch.cuda.device_count() > 0:\n"
        "            p = torch.cuda.get_device_properties(0)\n"
        "            out['gpu'] = {'cuda': True, 'gpu_name': torch.cuda.get_device_name(0), 'vram_gb': round(p.total_memory / (1024**3), 1)}\n"
        "    except Exception:\n"
        "        pass\n"
        "print('__PROBE_JSON__' + json.dumps(out, ensure_ascii=False), flush=True)\n"
    )
    env = dict(
        os.environ,
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True",
    )
    creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=creation,
        )
        prefix = "__PROBE_JSON__"
        matched = [ln.strip()[len(prefix):] for ln in (proc.stdout or "").splitlines() if ln.strip().startswith(prefix)]
        if proc.returncode == 0 and matched:
            data = json.loads(matched[-1])
            data["probed"] = True
            _IMPORT_PROBE_CACHE[key] = (now_ts, data)
            return dict(data)
        return {"ok": False, "probed": False, "modules": {}, "error": (proc.stderr or proc.stdout)[-400:]}
    except Exception as exc:
        return {"ok": False, "probed": False, "modules": {}, "error": str(exc)}


def _parse_ver_tuple(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:3])


def check_environment(root: Path = ROOT, batches_root: Path = DEFAULT_BATCHES_ROOT, deep: bool = False) -> dict:
    """运行环境、依赖版本冲突、硬件 GPU、模型权重与视觉 API 自检。

    deep=True 时会在子进程执行真实 import 校验；默认使用 find_spec + metadata 并在缓存命中时合并真实导入结果。
    """
    t0 = time.perf_counter()
    cfg = load_config(batches_root)
    infer_cfg = get_infer_settings(batches_root)
    items = []

    # 1. Python 解释器
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 10)
    items.append({
        "id": "python",
        "category": "runtime",
        "module": "Python 运行环境",
        "name": "Python 解释器",
        "ok": py_ok,
        "severity": "ok" if py_ok else "error",
        "detail": f"Python {py_ver} ({sys.executable})",
        "fix": None if py_ok else "请安装 Python 3.10~3.13，并优先在项目根目录建立 .venv 虚拟环境。",
    })

    # 2. 核心依赖库（结合真实导入缓存/深度探测 + 发行版版本冲突检查）
    probe = None
    if deep:
        probe = verify_runtime_imports(force=True)
    else:
        cached = _IMPORT_PROBE_CACHE.get(sys.executable)
        if cached and (time.time() - cached[0]) < 120.0:
            probe = cached[1]
    probed_mods = (probe or {}).get("modules") or {}

    missing_pkgs = []
    for pkg in REQUIRED_PACKAGES:
        imp_name = pkg["import"]
        spec = importlib.util.find_spec(imp_name)
        ok = spec is not None
        ver = _pkg_version(pkg.get("dist") or imp_name)
        err_msg = ""
        if imp_name in probed_mods:
            pm = probed_mods[imp_name]
            if not pm.get("ok"):
                ok = False
                err_msg = pm.get("error") or "导入失败"
            elif pm.get("version"):
                ver = pm["version"]
        if not ok:
            missing_pkgs.append(pkg["pip"])
        fix_tip = None
        if not ok:
            fix_tip = f"点击上方「自动测速并安装缺失依赖」，或在终端运行：pip install \"{pkg['pip']}\""
        detail = f"{pkg['pip']} 已就绪" + (f" (v{ver})" if ver else "") if ok else (
            f"导入失败：{err_msg}" if err_msg else f"缺少必需依赖 {pkg['pip']}"
        )
        items.append({
            "id": f"pkg_{imp_name}",
            "category": "packages",
            "module": pkg["label"],
            "name": f"依赖库：{pkg['label']}",
            "ok": ok,
            "severity": "ok" if ok else "error",
            "detail": detail,
            "fix": fix_tip,
            "pip": pkg["pip"],
        })

    # 2b. 版本兼容性检查（numpy < 2.4 与双 OpenCV 冲突）
    np_ver = _pkg_version("numpy")
    if np_ver and _parse_ver_tuple(np_ver) >= (2, 4, 0):
        items.append({
            "id": "compat_numpy",
            "category": "packages",
            "module": "NumPy 版本兼容",
            "name": "依赖兼容：NumPy 版本检查",
            "ok": False,
            "severity": "warn",
            "detail": f"当前 numpy=={np_ver}，PaddleOCR (paddlex) 要求 numpy<2.4，可能引发兼容性问题。",
            "fix": "建议运行：pip install \"numpy>=2.0.0,<2.4\"",
        })
    cv_std = _pkg_version("opencv-python")
    cv_contrib = _pkg_version("opencv-contrib-python")
    if cv_std and cv_contrib:
        items.append({
            "id": "compat_opencv",
            "category": "packages",
            "module": "OpenCV 发行版冲突",
            "name": "依赖兼容：OpenCV 重复安装",
            "ok": False,
            "severity": "warn",
            "detail": f"同时安装了 opencv-python ({cv_std}) 与 opencv-contrib-python ({cv_contrib})，两者同名 cv2 可能互相覆盖。",
            "fix": "建议运行：pip uninstall -y opencv-python opencv-contrib-python && pip install opencv-contrib-python==4.10.0.84",
        })

    # 3. 计算设备（GPU / CPU）探测
    diag_mod = _load_diag_module(root)
    hw = None
    if probe and isinstance(probe.get("gpu"), dict):
        g = probe["gpu"]
        hw = {
            "cuda": bool(g.get("cuda")),
            "gpu_name": g.get("gpu_name") or "",
            "vram_gb": float(g.get("vram_gb") or 0.0),
            "cpu_count": os.cpu_count() or 4,
            "recommended_device": "cuda:0" if g.get("cuda") else "cpu",
            "recommended_label": f"GPU: {g.get('gpu_name')} ({g.get('vram_gb')} GB)" if g.get("cuda") else f"CPU ({os.cpu_count() or 4} 线程)",
        }
    else:
        hw = diag_mod.probe_compute_hardware()
    eff_dev, eff_label = diag_mod.resolve_infer_device(infer_cfg["infer_device"])
    items.append({
        "id": "compute_device",
        "category": "hardware",
        "module": "计算设备与推理加速",
        "name": "计算设备（GPU 优先）",
        "ok": True,
        "severity": "ok",
        "detail": f"当前生效：{eff_label}（策略：{infer_cfg['infer_device']}）",
        "fix": None if hw["cuda"] else "当前未检测到 CUDA 显卡，将使用 CPU 模式运行；如有 NVIDIA 显卡可安装 CUDA 版 PyTorch 开启加速。",
    })

    # 4. 开源 OCR 模型权重 (PaddleOCR ONNX)
    paddle_missing = [rel for rel in PADDLE_FILES if not (root / rel).is_file()]
    items.append({
        "id": "paddle_ocr",
        "category": "weights",
        "module": "PaddleOCR 题号权重",
        "name": "PaddleOCR 印刷题号权重",
        "ok": len(paddle_missing) == 0,
        "severity": "ok" if not paddle_missing else "error",
        "detail": "PP-OCRv6 ONNX 权重齐全" if not paddle_missing else "缺少: " + ", ".join(paddle_missing),
        "fix": None if not paddle_missing else "请运行：python scripts/download_weights.py --only-paddle，或在工作台重新解压权重自动补齐。",
    })

    # 5. 部署模型权重 (orient / M1 / M2 / M3 / choice)
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
            "module": label,
            "name": label,
            "ok": ok,
            "severity": "ok" if ok else "error",
            "detail": f"就绪 ({size_mb} MB)" if ok else f"未找到 {wp.name}",
            "fix": None if ok else "请点击上方「输入密码解压加密权重」，或先运行 python scripts/download_weights.py 下载权重包。",
            "path": str(wp),
        })

    # 6. 大模型视觉 API 配置（豆包 / 小米 MiMo / DeepSeek / 通义 / 智谱 / 自定义中转站）
    vlm_cfg = get_vlm_settings(root)
    has_key = bool(vlm_cfg.get("has_key"))
    placeholder_key = bool(vlm_cfg.get("placeholder_detected"))
    masked = vlm_cfg.get("api_key_masked") or ""
    if has_key:
        api_detail = f"已配置 {vlm_cfg.get('model')} ({masked})"
        api_fix = None
    elif placeholder_key:
        api_detail = "检测到 .env 中仍是模板占位文字（your_...），未配置真实 API Key"
        api_fix = "请点击右上角「⚙ 大模型 API」填入真实的视觉模型密钥并测试连通性。"
    else:
        api_detail = "尚未配置有效 API Key（未配置前无法启动自动识别）"
        api_fix = "请点击右上角「⚙ 大模型 API」选择运营商（豆包/MiMo/DeepSeek/通义等）并填入 API Key。"
    items.append({
        "id": "vlm_api_key",
        "category": "config",
        "module": "视觉大模型 API",
        "name": f"大模型视觉 API ({vlm_cfg.get('provider_label') or vlm_cfg.get('provider')})",
        "ok": has_key,
        "severity": "ok" if has_key else "warn",
        "detail": api_detail,
        "fix": api_fix,
    })

    # 7. 加密权重包与 7z / opencode / ZCode 探测（辅助信息）
    archive_001 = find_weight_archive(root)
    seven_zip = find_7z()
    opencode_path = find_opencode(cfg)
    zcode_info = find_zcode(cfg)

    errors = [x for x in items if x["severity"] == "error"]
    warnings = [x for x in items if x["severity"] == "warn"]

    return {
        "checked_at": now_str(),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "deep_verified": bool(probe and probe.get("probed")),
        "ok": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "has_valid_api_key": has_key,
        "placeholder_api_key": placeholder_key,
        "hardware": hw,
        "infer_settings": infer_cfg,
        "effective_device": eff_dev,
        "effective_device_label": eff_label,
        "missing_packages": missing_pkgs,
        "missing_weights": missing_weights + (["PaddleOCR 权重"] if paddle_missing else []),
        "has_encrypted_weights": archive_001 is not None,
        "encrypted_archive": str(archive_001) if archive_001 else "",
        "seven_zip": seven_zip or "",
        "opencode_path": opencode_path or "",
        "zcode": zcode_info,
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


def save_env_keys(updates: dict[str, str], root: Path = ROOT) -> dict:
    """原子批量写入或更新项目根目录 .env 中的键值（兼容 UTF-8 BOM 并保留其余配置行）。"""
    clean: dict[str, str] = {}
    for raw_k, raw_v in (updates or {}).items():
        k = (raw_k or "").strip()
        v = (raw_v or "").strip()
        if not re.fullmatch(r"[A-Z0-9_]{2,64}", k):
            return {"ok": False, "error": f"配置项名称不合法：{k}"}
        if not v:
            return {"ok": False, "error": f"配置项 {k} 内容不能为空"}
        clean[k] = v
    if not clean:
        return {"ok": True, "message": "无需更新 .env"}
    env_file = root / ".env"
    lines = env_file.read_text(encoding="utf-8-sig", errors="ignore").splitlines() if env_file.is_file() else []
    replaced: set[str] = set()
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            lk = s.split("=", 1)[0].strip()
            if lk in clean:
                out.append(f"{lk}={clean[lk]}")
                replaced.add(lk)
                continue
        out.append(line)
    for k, v in clean.items():
        if k not in replaced:
            out.append(f"{k}={v}")
    tmp = env_file.with_suffix(f".writing.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    last_exc = None
    for attempt in range(40):
        try:
            os.replace(tmp, env_file)
            last_exc = None
            break
        except PermissionError as exc:
            last_exc = exc
            time.sleep(0.04)
    if last_exc is not None:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise last_exc
    for k, v in clean.items():
        os.environ[k] = v
    return {"ok": True, "message": f"已保存 {', '.join(clean.keys())} 到 .env"}


def save_env_key(key: str, value: str, root: Path = ROOT) -> dict:
    """安全写入或更新项目根目录 .env 中的单个键值（保留其余配置行）。"""
    return save_env_keys({key: value}, root=root)


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
        return {"ok": False, "error": "weights/ 目录下未找到加密权重包 (.7z)，请先运行 python scripts/download_weights.py 或从 GitHub Releases 下载"}
    cmd = [seven_zip, "x", str(archive), f"-o{root}", f"-p{password}", "-y"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return {"ok": False, "error": "解压失败（请检查密码是否正确）", "detail": (proc.stderr or proc.stdout)[-1500:]}

    # 若缺少官方 PP-OCRv6 权重，尝试自动从百度飞桨官方源补齐
    paddle_missing = [rel for rel in PADDLE_FILES if not (root / rel).is_file()]
    if paddle_missing:
        try:
            import importlib.util
            dl_script = root / "scripts" / "download_weights.py"
            if dl_script.is_file():
                spec = importlib.util.spec_from_file_location("_dl_weights", str(dl_script))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.ensure_paddle_ocr(root)
        except Exception as exc:
            return {
                "ok": True,
                "message": f"自训推理权重已解压就绪；官方 PP-OCRv6 自动下载失败（{exc}），请稍后运行 python scripts/download_weights.py --only-paddle",
            }
    return {"ok": True, "message": "模型权重已全部就绪"}


def build_agent_prompt(report: dict, root: Path = ROOT, extra_note: str = "") -> str:
    """生成给本地 Agent (ZCode / opencode) 的结构化脱敏诊断提示词。"""
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    from engine.run_log import mask_secret
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
            lines.append(f"- [{tag}] {p['name']}：{mask_secret(str(p['detail']))}")
    if extra_note.strip():
        lines.extend(["", "### 用户补充说明", mask_secret(extra_note.strip())])
    lines.extend([
        "",
        "### 期望操作",
        "1. 优先说明问题原因（用通俗中文），再给出最小化修复步骤（只读诊断模式不要直接修改无关文件）。",
        "2. 若需安装 Python 依赖，优先测试并使用国内镜像源（清华/阿里云）。",
        "3. 修复完成后可运行 `python -m apps.workbench check --json` 验证全部变绿。",
    ])
    return mask_secret("\n".join(lines))


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


def _clean_path_str(raw: str | Path) -> str:
    s = str(raw or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    return s


def inspect_ingest_path(
    source_path: str | Path,
    batches_root: Path = DEFAULT_BATCHES_ROOT,
) -> dict:
    """快速探测用户粘贴或选中的路径：支持文件夹（批量试卷）或单张图片文件（单张测试）。"""
    cleaned = _clean_path_str(source_path)
    if not cleaned:
        return {"ok": False, "error": "路径为空"}
    src = Path(cleaned).expanduser()
    try:
        src = src.resolve()
    except OSError:
        return {"ok": False, "error": f"无效路径：{cleaned}"}
    if src.is_file():
        if src.suffix.lower() not in IMAGE_EXTS:
            return {"ok": False, "error": f"不是支持的试卷图片格式（支持 jpg/png/bmp/webp）：{src.name}"}
        return {
            "ok": True,
            "kind": "file",
            "path": str(src),
            "name": src.name,
            "image_count": 1,
            "sample_names": [src.name],
            "size_bytes": src.stat().st_size,
        }
    if not src.is_dir():
        return {"ok": False, "error": f"找不到该文件夹或图片文件：{src}"}
    b_root_resolved = batches_root.resolve()
    if src == b_root_resolved or b_root_resolved.is_relative_to(src):
        return {"ok": False, "error": "不能选择包含 batches/ 目录的根目录，请直接选择存放试卷照片的子文件夹"}
    imgs = sorted(
        p for p in src.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if not imgs:
        return {"ok": False, "error": f"文件夹中没有找到图片文件（支持 jpg/png/bmp/webp）：{src.name}"}
    return {
        "ok": True,
        "kind": "folder",
        "path": str(src),
        "name": src.name,
        "image_count": len(imgs),
        "sample_names": [p.name for p in imgs[:4]],
    }


def _auto_batch_id(label: str, batches_root: Path) -> str:
    base_tok = _sanitize_token(label, "试卷")
    hm = datetime.now().strftime("%m%d_%H%M")
    cand = f"{base_tok}_{hm}"
    if (batches_root / cand).exists():
        cand = f"{base_tok}_{datetime.now().strftime('%m%d_%H%M%S')}"
    return cand


def ingest_batch(
    source_dir: str | Path,
    subject: str = "通用",
    batches_root: Path = DEFAULT_BATCHES_ROOT,
    batch_name: str | None = None,
    progress_cb=None,
) -> dict:
    """非破坏性导入新一批试卷图片：支持文件夹批量或单张图片文件，SHA256 去重 + manifest.csv。"""
    cleaned = _clean_path_str(source_dir)
    src = Path(cleaned).expanduser().resolve()
    is_single_file = src.is_file()
    if not is_single_file and not src.is_dir():
        raise FileNotFoundError(f"找不到源文件夹或图片文件：{src}")
    if is_single_file and src.suffix.lower() not in IMAGE_EXTS:
        raise ValueError(f"不支持的图片格式（支持 jpg/png/bmp/webp）：{src.name}")
    b_root_resolved = batches_root.resolve()
    if not is_single_file and (src == b_root_resolved or b_root_resolved.is_relative_to(src)):
        raise ValueError("不能选择包含 batches/ 目录的项目根目录作为源文件夹，请直接选择存放试卷照片的子文件夹。")

    raw_subj = (subject or "").strip()
    subj = _sanitize_token(raw_subj, "通用")
    day = datetime.now().strftime("%Y%m%d")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if batch_name:
        batch_id = _sanitize_token(batch_name, f"{stamp}_{subj}")
    elif not raw_subj or subj in ("通用", "试卷"):
        stem_lbl = f"单张_{src.stem}" if is_single_file else src.name
        batch_id = _auto_batch_id(stem_lbl, batches_root)
    else:
        batch_id = f"{stamp}_{subj}"

    batch_dir = batches_root / batch_id
    images_dir = batch_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # 收集源图片（支持单文件或文件夹递归）
    if is_single_file:
        src_files = [src]
        src_base = src.parent
    else:
        src_files = sorted(
            p for p in src.rglob("*")
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        )
        src_base = src
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
    skipped_bad = 0
    new_rows = []
    imported_at = now_str()

    # 延迟导入 Pillow，仅在需要转格式（png/bmp/webp -> jpg）时使用
    pil_image = None
    for idx, fpath in enumerate(src_files, 1):
        try:
            digest = _file_sha256(fpath)
        except OSError:
            skipped_bad += 1
            continue
        if digest in seen_hashes:
            skipped_dup += 1
            continue
        seq += 1
        new_name = f"{day}_{subj}_{seq:04d}.jpg"
        dest = images_dir / new_name
        try:
            if fpath.suffix.lower() in (".jpg", ".jpeg"):
                shutil.copy2(fpath, dest)
            else:
                if pil_image is None:
                    from PIL import Image
                    pil_image = Image
                with pil_image.open(fpath) as im:
                    im.convert("RGB").save(dest, format="JPEG", quality=95, subsampling=0)
        except Exception:
            seq -= 1
            skipped_bad += 1
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            continue
        seen_hashes.add(digest)
        row = {
            "new_name": new_name,
            "batch": batch_id,
            "subject": subj,
            "source": str(src),
            "orig_name": str(fpath.relative_to(src_base)),
            "sha256": digest,
            "imported_at": imported_at,
        }
        new_rows.append(row)
        imported += 1
        if progress_cb and (idx % 10 == 0 or idx == len(src_files)):
            progress_cb(f"整理入库 {idx}/{len(src_files)}（新增 {imported}，跳过重复 {skipped_dup}）")

    if imported == 0 and not existing_rows and skipped_bad > 0:
        raise ValueError(f"选中的 {skipped_bad} 张图片均已损坏或无法读取，请检查图片文件是否完整。")

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


def ingest_uploaded_images(
    files: list[tuple[str, bytes]],
    subject: str = "通用",
    batches_root: Path = DEFAULT_BATCHES_ROOT,
    batch_name: str | None = None,
    progress_cb=None,
) -> dict:
    """支持拖拽单张/多张试卷照片或 Ctrl+V 剪贴板贴图直接入库（SHA256 去重 + JPEG 标准化）。"""
    if not files:
        raise ValueError("未收到任何试卷图片文件")
    from io import BytesIO
    from PIL import Image

    raw_subj = (subject or "").strip()
    subj = _sanitize_token(raw_subj, "通用")
    day = datetime.now().strftime("%Y%m%d")
    if batch_name:
        batch_id = _sanitize_token(batch_name, _auto_batch_id("试卷", batches_root))
    elif len(files) == 1:
        stem = Path(files[0][0] or "剪贴板试卷").stem
        batch_id = _auto_batch_id(f"单张_{stem}", batches_root)
    else:
        batch_id = _auto_batch_id(f"试卷_{len(files)}张", batches_root)

    batch_dir = batches_root / batch_id
    images_dir = batch_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

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
    skipped_bad = 0
    new_rows = []
    imported_at = now_str()

    for idx, (orig_name, raw_bytes) in enumerate(files, 1):
        if not raw_bytes:
            skipped_bad += 1
            continue
        digest = hashlib.sha256(raw_bytes).hexdigest()
        if digest in seen_hashes:
            skipped_dup += 1
            continue
        safe_orig = Path(orig_name or f"upload_{idx}.png").name
        ext = Path(safe_orig).suffix.lower()
        seq += 1
        new_name = f"{day}_{subj}_{seq:04d}.jpg"
        dest = images_dir / new_name
        try:
            with Image.open(BytesIO(raw_bytes)) as im:
                if ext in (".jpg", ".jpeg") and im.format == "JPEG" and im.mode in ("RGB", "L"):
                    dest.write_bytes(raw_bytes)
                else:
                    im.convert("RGB").save(dest, format="JPEG", quality=95, subsampling=0)
        except Exception:
            seq -= 1
            skipped_bad += 1
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            continue
        seen_hashes.add(digest)
        new_rows.append({
            "new_name": new_name,
            "batch": batch_id,
            "subject": subj,
            "source": "upload",
            "orig_name": safe_orig,
            "sha256": digest,
            "imported_at": imported_at,
        })
        imported += 1
        if progress_cb and (idx % 5 == 0 or idx == len(files)):
            progress_cb(f"整理上传图片 {idx}/{len(files)}（新增 {imported}，跳过重复 {skipped_dup}）")

    if imported == 0 and not existing_rows:
        raise ValueError("上传的文件均不是有效图片或无法读取，请检查图片格式。")

    all_rows = existing_rows + new_rows
    fields = ["new_name", "batch", "subject", "source", "orig_name", "sha256", "imported_at"]
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(all_rows)

    meta = {
        "batch_id": batch_id,
        "subject": subj,
        "source": "upload",
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


def _safe_batch_dir(batch_id: str, batches_root: Path) -> Path | None:
    bid = str(batch_id or "").strip()
    if not bid or "/" in bid or "\\" in bid or bid in (".", ".."):
        return None
    root_res = batches_root.resolve()
    cand = (root_res / bid).resolve()
    if cand.parent != root_res:
        return None
    return cand


def set_active_batch(batch_id: str, batches_root: Path = DEFAULT_BATCHES_ROOT) -> dict | None:
    bdir = _safe_batch_dir(batch_id, batches_root)
    if bdir is None:
        return None
    meta = read_json(bdir / "batch.json")
    if not meta:
        return None
    write_json(batches_root / "active.json", {"batch_id": bdir.name, "updated_at": now_str()})
    return meta


def get_active_batch(batches_root: Path = DEFAULT_BATCHES_ROOT) -> dict | None:
    active = read_json(batches_root / "active.json")
    if isinstance(active, dict) and active.get("batch_id"):
        bdir = _safe_batch_dir(active["batch_id"], batches_root)
        meta = read_json(bdir / "batch.json") if bdir else None
        if meta:
            rec_dir = Path(meta.get("recognize_dir") or (bdir / "recognized"))
            meta["has_recognition"] = (rec_dir / "results.json").is_file()
            return meta
    batches = list_batches(batches_root)
    return batches[0] if batches else None


def _load_vlm_module(root: Path = ROOT):
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    from engine import vlm_client as rec_api  # noqa: E402
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
        "placeholder_detected": bool(cfg.get("placeholder_detected")),
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

    updates = {
        "VLM_PROVIDER": prov,
        "VLM_BASE_URL": eff_base,
        "VLM_MODEL": eff_model,
    }
    new_key = (api_key or "").strip()
    if new_key:
        updates["VLM_API_KEY"] = new_key
        if prov == "doubao":
            updates["ARK_API_KEY"] = new_key
    save_env_keys(updates, root=root)

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
        root=root,
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
    unmatched_items = 0
    done_pages = 0
    for p in pages:
        p_items = p.get("items") or []
        for it in p_items:
            if it.get("kind") == "unmatched":
                unmatched_items += 1
            else:
                total_items += 1
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
        "unmatched_items": unmatched_items,
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
                st_str = (r.get("status") or "").strip()
                if st_str.startswith("已审") or st_str == "特殊":
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
    """极速统计 choice/digit 审核进度（内存+mtime 缓存；万级日志用 scandir 毫秒估算，绝不阻塞 HTTP）。"""
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
    if cached and cached[0] == dir_mtime and (now_ts - cached[1]) < 300.0:
        return dict(cached[2])

    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        total = max(0, sum(1 for _ in fh) - 1)

    done = 0
    if log_dir.is_dir():
        log_entries = [e for e in os.scandir(log_dir) if e.name.endswith(".log")]
        # 小目录直接精确读取；超 1500 个小文件时不再同步 open() 万次（避免 Windows Defender 下卡死 190+ 秒）
        if len(log_entries) <= 1500:
            for entry in log_entries:
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
        else:
            # 抽样 80 个文件估算已审比例，结合文件总数毫秒返回，避免阻塞工作台首屏
            step = max(1, len(log_entries) // 80)
            sample = log_entries[::step][:80]
            hit = 0
            for entry in sample:
                try:
                    with open(entry.path, "rb") as fh:
                        fh.seek(0, os.SEEK_END)
                        size = fh.tell()
                        fh.seek(max(0, size - 512))
                        tail = fh.read().decode("utf-8", errors="ignore")
                    if "审核" in tail or "确认" in tail:
                        hit += 1
                except OSError:
                    pass
            done = round(len(log_entries) * (hit / len(sample))) if sample else 0
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

    # 3. 涂卡审核（移除未展示的 digit 万级扫盘开销）
    choice_stat = _count_choice_or_digit(root, "choice", "manifest.csv")

    tasks = [
        {
            "id": "review",
            "tier": "primary",
            "name": "人工复审",
            "desc": "核对答案、点击改字，按 E 调框，按 D 翻页",
            "guide": "",
            "port": 8904,
            "available": rec_stat["available"],
            "total": rec_stat["total"],
            "done": rec_stat["done"],
            "remaining": rec_stat["remaining"],
            "unit": "页",
            "extra": (
                f"共 {rec_stat.get('total_items', 0)} 题"
                + (f"（另 {rec_stat.get('unmatched_items', 0)} 未配对框）" if rec_stat.get("unmatched_items") else "")
            ) if rec_stat["available"] else "待完成识别",
            "running": bool(services.get("review", {}).get("running")),
        },
        {
            "id": "choice",
            "tier": "secondary",
            "name": "选择题涂卡审核（可选）",
            "desc": "按选项分组核查涂卡，一般无需重训涂卡 CNN",
            "guide": "",
            "port": 8905,
            "available": choice_stat["available"],
            "total": choice_stat["total"],
            "done": choice_stat["done"],
            "remaining": choice_stat["remaining"],
            "unit": "条",
            "extra": "可选 · 涂卡训练用" if choice_stat["available"] else "暂无数据",
            "running": bool(services.get("choice", {}).get("running")),
        },
        {
            "id": "annotate",
            "tier": "secondary",
            "name": "批量标框工具",
            "desc": "批量调整 M1/M2/M3 题框",
            "guide": "",
            "port": 8900,
            "available": m_total > 0,
            "total": m_total,
            "done": m_done,
            "remaining": max(0, m_total - m_done),
            "unit": "张",
            "extra": f"M1 {m_stats['m1']['done']}/{m_stats['m1']['total']} · M2 {m_stats['m2']['done']}/{m_stats['m2']['total']} · M3 {m_stats['m3']['done']}/{m_stats['m3']['total']}",
            "running": bool(services.get("annotate", {}).get("running")),
        },
    ]

    # 合并在线人员与历史贡献（按称呼+角色去重，避免同一人在本机开多标签页重复出现多个芯片）
    now_ts = time.time()
    seen_online: dict[tuple[str, str], dict] = {}
    for cid, info in list(presence.items()):
        seen_at = float(info.get("seen", 0) or 0)
        if now_ts - seen_at <= 65:
            uname = str(info.get("name") or "成员").strip() or "成员"
            urole = str(info.get("role") or "collaborator")
            key = (uname, urole)
            prev = seen_online.get(key)
            if not prev or seen_at > prev["_seen"]:
                seen_online[key] = {
                    "name": uname,
                    "role": urole,
                    "task": info.get("task") or "总览",
                    "_seen": seen_at,
                }
    online_users = [
        {"name": v["name"], "role": v["role"], "task": v["task"]}
        for v in seen_online.values()
    ]

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

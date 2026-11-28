# -*- coding: utf-8 -*-
"""多 Agent CLI 统一适配、跨环境快探（可选深度扫描）与防竞争冒险多会话调度器。

首批原生支持：
- zcode       : 智谱 ZCode CLI（桌面 Electron bundle + 独立 CLI，自动注入内置 provider 环境变量）
- opencode    : OpenCode CLI（opencode run --format json）
- workbuddy   : 腾讯 WorkBuddy / CodeBuddy CLI（codebuddy -p）
- doubao      : 字节豆包 / Trae CLI（trae-cli / trae-agent / trae / doubao）
- antigravity : Google Antigravity CLI（agy）
- custom      : 用户自定义 CLI 命令模板
"""
from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import run_log as _run_log  # noqa: E402

PROVIDER_ORDER = ("zcode", "opencode", "workbuddy", "doubao", "antigravity", "custom")

PROVIDER_META: dict[str, dict[str, Any]] = {
    "zcode": {
        "id": "zcode",
        "label": "ZCode CLI（智谱）",
        "short": "ZCode",
        "region": "cn",
        "supports_attach": True,
        "supports_resume": True,
        "supports_json": True,
        "default_mode": "yolo",
    },
    "opencode": {
        "id": "opencode",
        "label": "OpenCode CLI",
        "short": "OpenCode",
        "region": "cn",
        "supports_attach": True,
        "supports_resume": True,
        "supports_json": True,
        "default_mode": "yolo",
    },
    "workbuddy": {
        "id": "workbuddy",
        "label": "WorkBuddy / CodeBuddy CLI（腾讯）",
        "short": "CodeBuddy",
        "region": "cn",
        "supports_attach": False,
        "supports_resume": True,
        "supports_json": False,
        "default_mode": "yolo",
    },
    "doubao": {
        "id": "doubao",
        "label": "豆包 / Trae CLI（字节）",
        "short": "豆包/Trae",
        "region": "cn",
        "supports_attach": False,
        "supports_resume": False,
        "supports_json": False,
        "default_mode": "yolo",
    },
    "antigravity": {
        "id": "antigravity",
        "label": "Antigravity CLI (agy)",
        "short": "Antigravity",
        "region": "global",
        "supports_attach": False,
        "supports_resume": False,
        "supports_json": False,
        "default_mode": "yolo",
    },
    "custom": {
        "id": "custom",
        "label": "自定义 CLI 模板",
        "short": "自定义",
        "region": "custom",
        "supports_attach": False,
        "supports_resume": False,
        "supports_json": False,
        "default_mode": "yolo",
    },
}

# 用于快速提取用户消息中可能包含的临时密钥，仅用于脱敏日志/响应（不阻断传递给本地 Agent 执行配置导入）
_INLINE_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|ark|ak|pk|api|key|token)-[A-Za-z0-9._-]{10,}\b", re.IGNORECASE),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|密钥|密码|password|token)\s*[:：=是为]\s*['\"]?([^\s'\",;，；]{6,})['\"]?", re.IGNORECASE),
]


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def extract_inline_secrets(text: str) -> list[str]:
    """提取文本中可能是密钥/密码的字面量，供后续日志和 UI 回显精准脱敏。"""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for pat in _INLINE_SECRET_PATTERNS:
        for m in pat.finditer(text):
            val = m.group(1) if m.lastindex else m.group(0)
            val = (val or "").strip()
            if len(val) >= 6 and val not in seen:
                seen.add(val)
                found.append(val)
    return found


def scrub_text(text: str, extra_secrets: list[str] | None = None) -> str:
    """结合 run_log.mask_secret 与本次对话临时密钥清单执行严格脱敏。"""
    s = _run_log.mask_secret(str(text or ""))
    for sec in (extra_secrets or []):
        if sec and len(sec) >= 6 and sec in s:
            masked = sec[:3] + "***" + sec[-2:] if len(sec) >= 10 else "***"
            s = s.replace(sec, masked)
    return s


def _scan_windows_uninstall_registry(keywords: tuple[str, ...]) -> list[Path]:
    """读取 Windows 卸载注册表中匹配的自定义安装目录（毫秒级，零子进程）。"""
    if os.name != "nt":
        return []
    try:
        import winreg
    except Exception:
        return []
    kws = tuple(k.lower() for k in keywords)
    found: list[Path] = []
    seen: set[str] = set()
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
                            low = str(disp or "").lower()
                            if not any(k in low for k in kws):
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
                                norm = str(cand).lower()
                                if cand.is_dir() and norm not in seen:
                                    seen.add(norm)
                                    found.append(cand)
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _common_extra_bin_dirs(deep: bool = False) -> list[Path]:
    """返回轻量级常见 CLI 工具目录；仅 deep=True 时额外扫描常见开发目录（绝不扫全盘）。"""
    home = Path.home()
    dirs: list[Path] = []
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
        local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        dirs.extend([
            appdata / "npm",
            local_app / "Programs",
            home / ".local" / "bin",
            home / ".zcode" / "bin",
            home / ".opencode" / "bin",
            home / ".codebuddy" / "bin",
        ])
        if deep:
            dirs.extend([
                home / ".cargo" / "bin",
                home / "scoop" / "shims",
                Path("D:/Dev"),
                Path("C:/Dev"),
                Path("D:/Tools"),
                Path("D:/Program Files"),
                Path("E:/Dev"),
                local_app / "Volta" / "bin",
                local_app / "pnpm",
            ])
    else:
        dirs.extend([
            home / ".local" / "bin",
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
            home / ".npm-global" / "bin",
        ])
        if deep:
            dirs.extend([
                home / ".cargo" / "bin",
                home / ".volta" / "bin",
                Path("/Applications"),
            ])
    return [d for d in dirs if d.is_dir()]


def _which_any(names: tuple[str, ...]) -> str:
    for nm in names:
        found = shutil.which(nm)
        if found:
            return found
    return ""


def _find_in_dirs(dirs: list[Path], exe_names: tuple[str, ...], *, deep: bool = False) -> str:
    for d in dirs:
        for nm in exe_names:
            cand = d / nm
            if cand.is_file():
                return str(cand)
        if deep:
            # 最多下探 1 层子目录（如 D:\Dev\Tool\tool.exe 或 D:\Dev\Tool\bin\tool.exe）
            try:
                for sub in d.iterdir():
                    if not sub.is_dir():
                        continue
                    for nm in exe_names:
                        c1 = sub / nm
                        if c1.is_file():
                            return str(c1)
                        c2 = sub / "bin" / nm
                        if c2.is_file():
                            return str(c2)
            except OSError:
                continue
    return ""


def _detect_zcode_builtin_cfg(cli_path: str) -> str:
    try:
        p = Path(cli_path).resolve()
        for cand in (
            p.parent / "provider" / "zcode-builtin.json",
            p.parent.parent / "config" / "provider" / "zcode-builtin.json",
        ):
            if cand.is_file():
                return str(cand)
    except Exception:
        pass
    return ""


def detect_zcode(cfg: dict | None = None, *, deep: bool = False, probe_version: bool = False) -> dict[str, Any]:
    cfg = cfg or {}
    agent_paths = cfg.get("agent_paths") if isinstance(cfg.get("agent_paths"), dict) else {}
    custom_manual = str(agent_paths.get("zcode") or "").strip()
    custom_exe = str(cfg.get("zcode_exe") or "").strip()
    custom_cli = str(cfg.get("zcode_cli") or "").strip()

    res: dict[str, Any] = {
        **PROVIDER_META["zcode"],
        "available": False,
        "mode": "",
        "exe": "",
        "cli": "",
        "builtin_provider_config": "",
        "source": "",
        "version": "",
    }

    # 1. 手动配置覆盖（支持填单个目录、单个 cli 可执行文件、或 exe+cli 双路径）
    if custom_manual:
        p = Path(custom_manual)
        if p.is_dir() and (p / "ZCode.exe").is_file() and (p / "resources" / "glm" / "zcode.cjs").is_file():
            cli_s = str(p / "resources" / "glm" / "zcode.cjs")
            res.update({
                "available": True, "mode": "electron_bundle", "exe": str(p / "ZCode.exe"),
                "cli": cli_s, "builtin_provider_config": _detect_zcode_builtin_cfg(cli_s), "source": "manual",
            })
        elif p.is_file():
            if p.name.lower() == "zcode.cjs" and (p.parent.parent.parent / "ZCode.exe").is_file():
                res.update({
                    "available": True, "mode": "electron_bundle",
                    "exe": str(p.parent.parent.parent / "ZCode.exe"), "cli": str(p),
                    "builtin_provider_config": _detect_zcode_builtin_cfg(str(p)), "source": "manual",
                })
            elif p.name.lower() == "zcode.exe" and (p.parent / "resources" / "glm" / "zcode.cjs").is_file():
                cli_s = str(p.parent / "resources" / "glm" / "zcode.cjs")
                res.update({
                    "available": True, "mode": "electron_bundle", "exe": str(p),
                    "cli": cli_s, "builtin_provider_config": _detect_zcode_builtin_cfg(cli_s), "source": "manual",
                })
            else:
                res.update({"available": True, "mode": "cli", "exe": str(p), "cli": str(p), "source": "manual"})

    if not res["available"] and custom_exe and custom_cli and Path(custom_exe).is_file() and Path(custom_cli).is_file():
        res.update({
            "available": True, "mode": "electron_bundle", "exe": custom_exe, "cli": custom_cli,
            "builtin_provider_config": _detect_zcode_builtin_cfg(custom_cli), "source": "manual",
        })
    elif not res["available"] and custom_cli and Path(custom_cli).is_file():
        res.update({"available": True, "mode": "cli", "exe": custom_cli, "cli": custom_cli, "source": "manual"})

    # 2. 注册表 + 默认桌面安装路径（Electron bundle）
    if not res["available"]:
        local_app = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        candidates = [
            *_scan_windows_uninstall_registry(("zcode",)),
            Path(os.environ.get("ProgramFiles") or "C:/Program Files") / "ZCode",
            Path(os.environ.get("ProgramFiles(x86)") or "C:/Program Files (x86)") / "ZCode",
            local_app / "Programs" / "ZCode",
            Path("C:/Program Files/ZCode"),
        ]
        if deep:
            candidates.extend([Path("D:/Dev/Zcode"), Path("D:/Dev/ZCode"), Path("D:/ZCode"), Path("D:/Program Files/ZCode")])
        for base in candidates:
            exe_p = base / "ZCode.exe"
            cli_p = base / "resources" / "glm" / "zcode.cjs"
            if exe_p.is_file() and cli_p.is_file():
                res.update({
                    "available": True, "mode": "electron_bundle", "exe": str(exe_p), "cli": str(cli_p),
                    "builtin_provider_config": _detect_zcode_builtin_cfg(str(cli_p)), "source": "auto",
                })
                break

    # 3. PATH / 常见 CLI 目录
    if not res["available"]:
        names = ("zcode", "zcode.cmd", "zcode.exe", "zcodecli", "zcodecli.cmd", "zcodecli.exe")
        found = _which_any(names) or _find_in_dirs(_common_extra_bin_dirs(deep=deep), names, deep=deep)
        if found:
            res.update({"available": True, "mode": "cli", "exe": found, "cli": found, "source": "auto"})

    if res["available"] and probe_version:
        res["version"] = _probe_version(res)
    return res


def _detect_simple_cli(
    provider_id: str,
    names: tuple[str, ...],
    *,
    cfg: dict | None = None,
    reg_keywords: tuple[str, ...] = (),
    extra_candidates: list[Path] | None = None,
    legacy_cfg_key: str = "",
    deep: bool = False,
    probe_version: bool = False,
) -> dict[str, Any]:
    cfg = cfg or {}
    agent_paths = cfg.get("agent_paths") if isinstance(cfg.get("agent_paths"), dict) else {}
    manual = str(agent_paths.get(provider_id) or (cfg.get(legacy_cfg_key) if legacy_cfg_key else "") or "").strip()

    res: dict[str, Any] = {
        **PROVIDER_META[provider_id],
        "available": False,
        "mode": "cli",
        "exe": "",
        "cli": "",
        "source": "",
        "version": "",
    }
    if manual and Path(manual).is_file():
        res.update({"available": True, "exe": manual, "cli": manual, "source": "manual"})
    elif manual and Path(manual).is_dir():
        f = _find_in_dirs([Path(manual)], names, deep=False)
        if f:
            res.update({"available": True, "exe": f, "cli": f, "source": "manual"})

    if not res["available"]:
        found = _which_any(names)
        if found:
            res.update({"available": True, "exe": found, "cli": found, "source": "path"})

    if not res["available"]:
        dirs = list(_common_extra_bin_dirs(deep=deep))
        if reg_keywords:
            for rdir in _scan_windows_uninstall_registry(reg_keywords):
                dirs.extend([rdir, rdir / "bin", rdir / "resources" / "bin"])
        for c in (extra_candidates or []):
            if c.is_file():
                res.update({"available": True, "exe": str(c), "cli": str(c), "source": "default"})
                break
            if c.is_dir():
                dirs.append(c)
        if not res["available"]:
            found = _find_in_dirs(dirs, names, deep=deep)
            if found:
                res.update({"available": True, "exe": found, "cli": found, "source": "auto"})

    if res["available"] and probe_version:
        res["version"] = _probe_version(res)
    return res


def detect_opencode(cfg: dict | None = None, *, deep: bool = False, probe_version: bool = False) -> dict[str, Any]:
    home = Path.home()
    return _detect_simple_cli(
        "opencode",
        ("opencode", "opencode.exe", "opencode.cmd"),
        cfg=cfg,
        reg_keywords=("opencode",),
        extra_candidates=[
            home / ".opencode" / "bin" / "opencode.exe",
            home / "AppData" / "Local" / "Programs" / "opencode" / "opencode.exe",
            Path("C:/Program Files/opencode/opencode.exe"),
        ],
        legacy_cfg_key="opencode_path",
        deep=deep,
        probe_version=probe_version,
    )


def detect_workbuddy(cfg: dict | None = None, *, deep: bool = False, probe_version: bool = False) -> dict[str, Any]:
    home = Path.home()
    return _detect_simple_cli(
        "workbuddy",
        ("codebuddy", "codebuddy.cmd", "codebuddy.exe", "workbuddy", "workbuddy.cmd", "workbuddy.exe"),
        cfg=cfg,
        reg_keywords=("workbuddy", "codebuddy"),
        extra_candidates=[
            home / ".codebuddy" / "bin" / "codebuddy.exe",
            home / "AppData" / "Local" / "Programs" / "WorkBuddy",
            home / "AppData" / "Local" / "Programs" / "CodeBuddy",
        ],
        deep=deep,
        probe_version=probe_version,
    )


def detect_doubao(cfg: dict | None = None, *, deep: bool = False, probe_version: bool = False) -> dict[str, Any]:
    home = Path.home()
    return _detect_simple_cli(
        "doubao",
        ("trae-cli", "trae-cli.exe", "trae-cli.cmd", "trae-agent", "trae-agent.exe", "doubao-cli", "doubao-cli.exe"),
        cfg=cfg,
        reg_keywords=("trae", "doubao"),
        extra_candidates=[
            home / ".trae" / "bin",
            home / "AppData" / "Local" / "Programs" / "Trae",
        ],
        deep=deep,
        probe_version=probe_version,
    )


def detect_antigravity(cfg: dict | None = None, *, deep: bool = False, probe_version: bool = False) -> dict[str, Any]:
    home = Path.home()
    return _detect_simple_cli(
        "antigravity",
        ("agy", "agy.exe", "agy.cmd"),
        cfg=cfg,
        reg_keywords=("antigravity",),
        extra_candidates=[
            home / ".gemini" / "antigravity-cli" / "bin" / "agy.exe",
            home / "AppData" / "Local" / "Programs" / "antigravity" / "bin",
        ],
        deep=deep,
        probe_version=probe_version,
    )


def detect_custom(cfg: dict | None = None) -> dict[str, Any]:
    cfg = cfg or {}
    tpl = str(cfg.get("agent_custom_cmd") or "").strip()
    return {
        **PROVIDER_META["custom"],
        "available": bool(tpl),
        "mode": "template",
        "exe": tpl,
        "cli": tpl,
        "source": "manual" if tpl else "",
        "version": "",
    }


def _probe_version(agent: dict[str, Any]) -> str:
    if not agent.get("available") or agent.get("id") == "custom":
        return ""
    try:
        if agent.get("mode") == "electron_bundle":
            cmd = [agent["exe"], agent["cli"], "--version"]
            env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
        else:
            cmd = [agent["exe"], "--version"]
            env = dict(os.environ)
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8, env=env,
        )
        out = (proc.stdout or proc.stderr or "").strip()
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return lines[-1][:48] if lines else ""
    except Exception:
        return ""


def detect_all_agents(
    cfg: dict | None = None,
    *,
    deep: bool = False,
    probe_version: bool = False,
) -> dict[str, Any]:
    """探测全部内置 Agent（默认快探 <50ms 不扫全盘；deep=True 时做受限扩展扫描）。"""
    t0 = time.monotonic()
    agents = {
        "zcode": detect_zcode(cfg, deep=deep, probe_version=probe_version),
        "opencode": detect_opencode(cfg, deep=deep, probe_version=probe_version),
        "workbuddy": detect_workbuddy(cfg, deep=deep, probe_version=probe_version),
        "doubao": detect_doubao(cfg, deep=deep, probe_version=probe_version),
        "antigravity": detect_antigravity(cfg, deep=deep, probe_version=probe_version),
        "custom": detect_custom(cfg),
    }
    ordered = [agents[k] for k in PROVIDER_ORDER]
    available_ids = [a["id"] for a in ordered if a["available"]]
    pref = str((cfg or {}).get("ai_helper_provider") or "auto").strip().lower()
    recommended = pref if (pref in agents and agents[pref]["available"]) else (available_ids[0] if available_ids else "clipboard")
    return {
        "ok": True,
        "deep": bool(deep),
        "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
        "preferred": pref,
        "recommended": recommended,
        "available": available_ids,
        "agents": agents,
        "list": ordered,
    }


def test_agent_provider(provider_id: str, cfg: dict | None = None) -> dict[str, Any]:
    """实测指定 Agent 可执行文件能否响应 --version / --help。"""
    t0 = time.monotonic()
    info = detect_all_agents(cfg, deep=False, probe_version=False)["agents"].get(provider_id)
    if not info:
        return {"ok": False, "provider": provider_id, "error": f"不支持的 Agent 类型：{provider_id}"}
    if not info.get("available"):
        return {"ok": False, "provider": provider_id, "error": f"未检测到 {info['label']}，请确认已安装或在上方填入可执行路径"}
    if provider_id == "custom":
        return {"ok": True, "provider": "custom", "version": "自定义模板已配置", "latency_ms": round((time.monotonic() - t0) * 1000, 1)}
    ver = _probe_version(info)
    if ver:
        return {"ok": True, "provider": provider_id, "version": ver, "exe": info["exe"], "cli": info["cli"], "latency_ms": round((time.monotonic() - t0) * 1000, 1)}
    # 部分 CLI 不支持 --version，退回 --help 探测
    try:
        cmd = [info["exe"], info["cli"], "--help"] if info.get("mode") == "electron_bundle" else [info["exe"], "--help"]
        env = dict(os.environ, ELECTRON_RUN_AS_NODE="1") if info.get("mode") == "electron_bundle" else dict(os.environ)
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8, env=env)
        if proc.returncode == 0:
            return {"ok": True, "provider": provider_id, "version": "CLI 响应正常", "exe": info["exe"], "cli": info["cli"], "latency_ms": round((time.monotonic() - t0) * 1000, 1)}
        return {"ok": False, "provider": provider_id, "error": f"CLI 退出码 {proc.returncode}：{(proc.stderr or proc.stdout or '')[:200]}"}
    except Exception as exc:
        return {"ok": False, "provider": provider_id, "error": str(exc)}


def build_agent_command(
    prompt: str,
    root: Path = ROOT,
    *,
    provider: str = "auto",
    mode: str = "yolo",
    session_id: str = "",
    attach: list[str] | None = None,
    cfg: dict | None = None,
) -> dict[str, Any]:
    """根据选定的 Agent 构造统一的 headless CLI 启动契约（默认全权 yolo，支持切换 guarded / plan）。"""
    safe_mode = mode if mode in ("yolo", "guarded", "plan") else "yolo"
    det = detect_all_agents(cfg, deep=False, probe_version=False)
    agents = det["agents"]

    chosen_id = provider.strip().lower() if provider else "auto"
    if chosen_id == "auto":
        chosen_id = det["recommended"]
    if chosen_id not in agents or not agents[chosen_id]["available"]:
        return {
            "ok": False,
            "provider": "clipboard",
            "mode": safe_mode,
            "error": "未检测到可用的本地 CLI Agent（请在面板中选择路径、执行扩展扫描或安装 ZCode / OpenCode / CodeBuddy）",
            "detected": det,
        }

    ag = agents[chosen_id]
    cwd = str(Path(root).resolve())
    valid_attach = [str(Path(a).resolve()) for a in (attach or []) if a and Path(a).is_file()]
    env_patch: dict[str, str] = {}
    cmd: list[str] = []

    if chosen_id == "zcode":
        if ag["mode"] == "electron_bundle":
            cmd = [ag["exe"], ag["cli"]]
            env_patch["ELECTRON_RUN_AS_NODE"] = "1"
            builtin_cfg = str(ag.get("builtin_provider_config") or "").strip()
            if builtin_cfg and Path(builtin_cfg).is_file():
                if not os.environ.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"):
                    env_patch["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = builtin_cfg
                if not os.environ.get("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"):
                    base_dir = Path(os.environ.get("ZCODE_DATA_BASE_DIR") or Path.home())
                    env_patch["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = str(base_dir / ".zcode" / "v2" / "provider_config.json")
        else:
            cmd = [ag["exe"]]
        cmd.extend(["-p", prompt, "--cwd", cwd, "--json", "--no-color", "--locale", "zh-CN", "--mode", safe_mode])
        if session_id:
            cmd.extend(["--resume", str(session_id).strip()])
        for ap in valid_attach:
            cmd.extend(["--attach", ap])

    elif chosen_id == "opencode":
        cmd = [ag["exe"], "run", "--format", "json"]
        if session_id:
            cmd.extend(["--session", str(session_id).strip()])
        for ap in valid_attach:
            cmd.extend(["-f", ap])
        cmd.append(prompt)

    elif chosen_id == "workbuddy":
        cmd = [ag["exe"]]
        if safe_mode == "plan":
            cmd.extend(["--permission-mode", "plan"])
        if session_id:
            cmd.extend(["-r", str(session_id).strip()])
        final_prompt = prompt
        if valid_attach:
            final_prompt += "\n\n[已附带本地截图文件，请直接读取查看]\n" + "\n".join(f"- {p}" for p in valid_attach)
        cmd.extend(["-p", final_prompt])

    elif chosen_id == "doubao":
        exe_low = Path(ag["exe"]).name.lower()
        final_prompt = prompt
        if valid_attach:
            final_prompt += "\n\n[已附带本地截图文件，请直接读取查看]\n" + "\n".join(f"- {p}" for p in valid_attach)
        if "trae" in exe_low:
            cmd = [ag["exe"], "run", final_prompt, "--working-dir", cwd]
        else:
            cmd = [ag["exe"], "-p", final_prompt]

    elif chosen_id == "antigravity":
        final_prompt = prompt
        if valid_attach:
            final_prompt += "\n\n[已附带本地截图文件，请直接读取查看]\n" + "\n".join(f"- {p}" for p in valid_attach)
        cmd = [ag["exe"], "-p", final_prompt]

    elif chosen_id == "custom":
        tpl = str(ag["cli"]).strip()
        if not tpl:
            return {"ok": False, "provider": "clipboard", "mode": safe_mode, "error": "自定义 CLI 模板为空"}
        attach_str = " ".join(valid_attach)
        tokens = shlex.split(tpl, posix=(os.name != "nt"))
        cmd = [
            tok.replace("{prompt}", prompt)
            .replace("{cwd}", cwd)
            .replace("{mode}", safe_mode)
            .replace("{session_id}", session_id)
            .replace("{attach}", attach_str)
            for tok in tokens
        ]

    return {
        "ok": True,
        "provider": chosen_id,
        "provider_label": ag["label"],
        "mode": safe_mode,
        "cwd": cwd,
        "cmd": cmd,
        "env_patch": env_patch,
        "attach": valid_attach,
        "agent": ag,
    }


def build_computer_use_prompt(
    *,
    user_message: str,
    surface: str = "workbench",
    root: Path = ROOT,
    hub_url: str = "http://127.0.0.1:8888",
    review_url: str = "http://127.0.0.1:8904",
    context_snapshot: dict[str, Any] | None = None,
    attached_images: list[str] | None = None,
    attached_elements_path: str = "",
    mode: str = "yolo",
) -> str:
    """构造同构 Computer-Use 系统提示词（支持按需截图与预附带截图双模式自动切换）。"""
    snap = context_snapshot or {}
    has_attached = bool(attached_images)
    surf_name = "8904 审阅与调框界面" if surface == "review" else "8888 统一工作台控制中心"

    lines = [
        f"你是「自动改卷」系统的内置同构 AI 帮手（当前触发入口：{surf_name}，项目目录：{Path(root).resolve()}）。",
        "所有界面状态与数据均可经由同构 CLI 直接查询与操控，且 CLI 改动会通过 SSE 实时同步映射到浏览器 GUI。",
        "",
        "【当前发送瞬间已冻结的现场快照】（用户发送后可能已切换页面，请默认针对此冻结现场处理，除非用户要求看最新状态）：",
    ]
    if snap.get("batch_id"):
        lines.append(f"- 当前批次：{snap['batch_id']}（状态：{snap.get('rec_status') or '未知'}）")
    if snap.get("ui_state"):
        lines.append("- 发送时刻界面焦点：" + json.dumps(snap["ui_state"], ensure_ascii=False))
    if snap.get("check_summary"):
        lines.append(f"- 运行环境摘要：{snap['check_summary']}")
    if snap.get("recent_errors"):
        lines.append("- 最近报错摘要：")
        for err in snap["recent_errors"][:8]:
            lines.append(f"  * {scrub_text(str(err))[:260]}")

    lines.append("")
    if has_attached:
        lines.append("【视觉画面说明】")
        lines.append("- 已为你附带发送瞬间的实时离屏界面截图（见附件）。请直接结合附图判断，**不要重复截一次同样的整页图**；如果某个局部字迹、题框或按钮没看清，再调用下方的 `capture` 命令按题号/区域/选择器精准放大裁切。")
        if attached_elements_path:
            lines.append(f"- 本次附带的界面可交互元素清单 JSON：`{attached_elements_path}`（含每个按钮/题框的 selector、文案与坐标）。")
    else:
        lines.append("【按需离屏视觉能力】")
        lines.append("- 当前仅提供文字状态快照以节省 Token。如果你觉得有必要查看界面报错、按钮位置或高清手写试卷原图，随时调用同构截图 CLI（支持后台离屏渲染，无需浏览器在前台）：")
        lines.append(f"  `python -m apps.workbench capture --target {surface} --with-elements --json`")
        lines.append("  或看审阅页高清试卷/单题原图：`python -m apps.workbench capture --target review --mode canvas [--page <pid>] [--item <iid>] --json`")

    lines.extend([
        "",
        "【同构 CLI 操作清单（所有操作会自动映射同步到 GUI）】",
        f"- 控制台与状态：`python -m apps.workbench status|doctor|runs|logs --hub {hub_url} --json`",
        f"- 跑批与运维：`python -m apps.workbench ingest|run|retry|stop|benchmark|vlm-test --hub {hub_url} --json`",
        f"- 试卷答案/结构配置、成绩统计与内存预检（用户上传答案 PDF 或要求修改多选题漏选比例时优先调用）：",
        f"  `python -m apps.workbench schema get|set|parse-pdf|grade|export-csv [--batch <id>] [--file <json_or_pdf>] [--partial-ratio 0.5] --hub {hub_url} --json`",
        f"  `python -m apps.workbench memory preflight [--batch <id>] --hub {hub_url} --json`",
        f"- 配置与密钥导入（支持直接写入 .env/配置并刷新界面，切勿在回复中回显明文密钥）：",
        f"  `python -m apps.workbench config get|set|set-vlm|import|export --hub {hub_url} --json`",
        f"  `python -m apps.workbench weights unpack --password <密码> --hub {hub_url} --json`",
        f"- 8904 审阅数据 CRUD（页级锁防竞争冒险，修改实时推给前端）：",
        f"  `python -m apps.workbench review status|page|set-text|set-blank|set-choice|delete-box|re-ocr|re-ocr-subbox|purge-unmatched|judge-blank|grading|confirm|nav --review-url {review_url} --json`",
        f"- 界面聚焦/投送焦点/元素操作（让前台自动高亮、滚动并聚焦到指定按钮或题框）：",
        f"  `python -m apps.workbench ui focus|navigate|click|state --target {surface} --hub {hub_url} --review-url {review_url} --json`",
        "",
        "【权限与两类改动规范】",
        f"- 当前权限模式：`{mode}`（默认全权执行，直接帮用户干活并给出清晰结果）。",
        "- 第一类（业务数据/配置/密钥/跑批/审阅修改）：直接通过上述同构 CLI 执行，自动落盘并映射 GUI；`batches/` 与 `.env` 均已忽略，不要提交到 Git。",
        "- 第二类（修改项目源代码修 Bug / 改提示词 / 改前端）：允许直接修复；凡改动了 Git 跟踪的源码文件，完成后必须：",
        "  1) 运行对应单元测试确认通过（如 `python -B -m unittest discover -s tests`）；",
        "  2) 检查 `git status` 确保绝不提交 `.env`、密钥、模型权重或临时文件；",
        "  3) 自动执行 `git add`、`git commit` 并 `git push origin main`，在最终回复里附上 commit 哈希；若修改了后端 Python，提醒用户重启工作台/审阅服务生效。",
        "- 安全底线：严禁在回复文本、日志或 Git 提交中打印或泄露任何明文 API Key / 密码。",
        "",
        "【用户自然语言指令】",
        user_message.strip() or "请结合当前运行环境与最近报错日志，自动排查问题并给出或直接执行修复方案。",
    ])
    return "\n".join(lines)


def _extract_last_json_obj(raw_text: str) -> dict[str, Any] | None:
    s = (raw_text or "").strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    dec = json.JSONDecoder()
    pos = s.find("{")
    last_ok: dict[str, Any] | None = None
    while pos != -1:
        try:
            obj, end = dec.raw_decode(s, pos)
            if isinstance(obj, dict):
                last_ok = obj
            pos = s.find("{", max(end, pos + 1))
        except ValueError:
            pos = s.find("{", pos + 1)
    return last_ok


def parse_agent_json_output(provider: str, raw_stdout: str) -> dict[str, Any]:
    """统一抽取不同 Agent CLI 的 JSON 输出（session_id / response / usage）。"""
    out: dict[str, Any] = {"session_id": "", "response": "", "usage": {}}
    obj = _extract_last_json_obj(raw_stdout)
    if not isinstance(obj, dict):
        return out
    sid = obj.get("sessionId") or obj.get("session_id") or obj.get("sessionID") or ""
    if sid:
        out["session_id"] = str(sid)
    if isinstance(obj.get("usage"), dict):
        out["usage"] = obj["usage"]
    for key in ("response", "result", "text", "content", "message"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            out["response"] = val
            break
    return out


class AiSessionManager:
    """防竞争冒险的多会话 Agent 管理器：
    - 同一 chat_id 严格单实例串行（队内顺序执行 + --resume 续上下文），绝不并发打架；
    - 不同 chat_id（多标签页或 8888 + 8904 同时开启）各自独立子进程真正并发执行。
    """

    def __init__(
        self,
        root: Path,
        batches_root: Path,
        *,
        publish_cb: Callable[[dict[str, Any]], None] | None = None,
        config_loader: Callable[[], dict[str, Any]] | None = None,
        env_builder: Callable[[], dict[str, str]] | None = None,
    ):
        self.root = Path(root).resolve()
        self.batches_root = Path(batches_root).resolve()
        self._publish = publish_cb or (lambda _ev: None)
        self._load_cfg = config_loader or (lambda: {})
        self._base_env = env_builder or (lambda: dict(os.environ))
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._queues: dict[str, queue.Queue] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._last_chat_id: str = ""

    def _new_session_record(self, chat_id: str, *, surface: str = "workbench", provider: str = "auto", mode: str = "yolo", title: str = "") -> dict[str, Any]:
        return {
            "chat_id": chat_id,
            "title": title or ("审阅助手" if surface == "review" else "控制台助手"),
            "surface": surface,
            "running": False,
            "queued": 0,
            "provider": provider,
            "provider_label": "",
            "mode": mode,
            "status": "idle",
            "session_id": "",
            "response": "",
            "usage": {},
            "logs": [],
            "messages": [],
            "error": "",
            "started_at": "",
            "finished_at": "",
            "updated_at": _now_str(),
        }

    def snapshot_session(self, chat_id: str) -> dict[str, Any] | None:
        with self._lock:
            s = self._sessions.get(chat_id)
            if not s:
                return None
            return self._copy_session(s)

    @staticmethod
    def _copy_session(s: dict[str, Any]) -> dict[str, Any]:
        out = dict(s)
        out["logs"] = list(s.get("logs") or [])
        out["messages"] = [dict(m) for m in (s.get("messages") or [])[-40:]]
        out["usage"] = dict(s.get("usage") or {})
        return out

    def list_sessions(self, *, surface: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._sessions.values())
            if surface:
                items = [s for s in items if s.get("surface") == surface]
            items.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
            return [self._copy_session(s) for s in items]

    def legacy_snapshot(self) -> dict[str, Any]:
        """向后兼容旧 /api/ai/status 的单 ai_job 快照字段。"""
        with self._lock:
            if self._last_chat_id and self._last_chat_id in self._sessions:
                return self._copy_session(self._sessions[self._last_chat_id])
            if self._sessions:
                first = next(iter(self._sessions.values()))
                return self._copy_session(first)
        return self._new_session_record("default")

    def create_session(self, *, surface: str = "workbench", provider: str = "auto", mode: str = "yolo", title: str = "") -> dict[str, Any]:
        cid = f"chat_{uuid.uuid4().hex[:8]}"
        with self._lock:
            rec = self._new_session_record(cid, surface=surface, provider=provider, mode=mode, title=title)
            self._sessions[cid] = rec
            self._last_chat_id = cid
            return self._copy_session(rec)

    def delete_session(self, chat_id: str) -> dict[str, Any]:
        self.stop_session(chat_id)
        with self._lock:
            existed = chat_id in self._sessions
            self._sessions.pop(chat_id, None)
            self._queues.pop(chat_id, None)
            if self._last_chat_id == chat_id:
                self._last_chat_id = next(iter(self._sessions.keys()), "")
        return {"ok": True, "deleted": existed, "chat_id": chat_id}

    def submit_turn(
        self,
        *,
        chat_id: str = "",
        surface: str = "workbench",
        provider: str = "auto",
        mode: str = "yolo",
        user_message: str = "",
        full_prompt: str = "",
        session_id: str = "",
        attach: list[str] | None = None,
        command_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """向指定对话投递一轮指令：同 chat_id 严格单实例队列串行，不同 chat_id 真正并发。"""
        cfg = self._load_cfg()
        safe_mode = mode if mode in ("yolo", "guarded", "plan") else "yolo"
        inline_secrets = extract_inline_secrets(user_message)
        masked_user_msg = scrub_text(user_message or "(自动诊断)", inline_secrets)

        with self._lock:
            cid = str(chat_id or "").strip()
            if not cid:
                cid = f"chat_{uuid.uuid4().hex[:8]}"
            rec = self._sessions.get(cid)
            if rec is None:
                title = (masked_user_msg[:18] + "…") if len(masked_user_msg) > 18 else masked_user_msg
                rec = self._new_session_record(cid, surface=surface, provider=provider, mode=safe_mode, title=title)
                self._sessions[cid] = rec
            if session_id:
                rec["session_id"] = session_id
            effective_sid = str(rec.get("session_id") or "")
            self._last_chat_id = cid

        # 先校验 CLI 是否可用（支持测试注入 command_override）
        spec = command_override or build_agent_command(
            full_prompt or user_message,
            self.root,
            provider=provider,
            mode=safe_mode,
            session_id=effective_sid,
            attach=attach,
            cfg=cfg,
        )
        if not spec.get("ok"):
            return {
                "ok": False,
                "headless": False,
                "chat_id": cid,
                "provider": spec.get("provider") or "clipboard",
                "error": spec.get("error") or "未检测到可用 CLI Agent",
                "detected": spec.get("detected"),
            }

        turn_payload = {
            "chat_id": cid,
            "surface": surface,
            "provider": spec["provider"],
            "provider_label": spec.get("provider_label") or spec["provider"],
            "mode": safe_mode,
            "user_message": user_message,
            "masked_user_msg": masked_user_msg,
            "full_prompt": full_prompt or user_message,
            "attach": list(attach or []),
            "inline_secrets": inline_secrets,
            "command_override": command_override,
        }

        with self._lock:
            rec = self._sessions[cid]
            rec["provider"] = spec["provider"]
            rec["provider_label"] = spec.get("provider_label") or spec["provider"]
            rec["mode"] = safe_mode
            rec["messages"].append({
                "role": "user",
                "text": masked_user_msg,
                "attach": [Path(a).name for a in (attach or []) if a],
                "ts": _now_str(),
            })
            rec["updated_at"] = _now_str()
            q = self._queues.get(cid)
            if q is None:
                q = queue.Queue()
                self._queues[cid] = q
            was_running = bool(rec.get("running"))
            q.put(turn_payload)
            rec["queued"] = q.qsize()
            worker = self._workers.get(cid)
            if worker is None or not worker.is_alive():
                t = threading.Thread(target=self._session_worker_loop, args=(cid,), daemon=True, name=f"ai-{cid}")
                self._workers[cid] = t
                t.start()
            snap = self._copy_session(rec)

        return {
            "ok": True,
            "headless": True,
            "queued": was_running,
            "chat_id": cid,
            "provider": spec["provider"],
            "provider_label": spec.get("provider_label") or spec["provider"],
            "mode": safe_mode,
            "session": snap,
            "message": (
                f"同一对话正在执行中，已自动排入单实例队列（防竞争冒险，排队 {snap['queued']} 条）"
                if was_running
                else f"已启动 {spec.get('provider_label') or spec['provider']}（{safe_mode} 模式）"
            ),
        }

    def _session_worker_loop(self, chat_id: str) -> None:
        while True:
            with self._lock:
                q = self._queues.get(chat_id)
                rec = self._sessions.get(chat_id)
            if q is None or rec is None:
                return
            try:
                turn = q.get(timeout=0.5)
            except queue.Empty:
                with self._lock:
                    if chat_id in self._queues and self._queues[chat_id].empty():
                        self._workers.pop(chat_id, None)
                        return
                continue
            try:
                self._execute_one_turn(chat_id, turn)
            finally:
                q.task_done()
                with self._lock:
                    if chat_id in self._sessions and chat_id in self._queues:
                        self._sessions[chat_id]["queued"] = self._queues[chat_id].qsize()

    def _execute_one_turn(self, chat_id: str, turn: dict[str, Any]) -> None:
        t0 = time.monotonic()
        secrets_list = list(turn.get("inline_secrets") or [])
        with self._lock:
            rec = self._sessions.get(chat_id)
            if rec is None:
                return
            cur_sid = str(rec.get("session_id") or "")
            rec["running"] = True
            rec["status"] = "running"
            rec["error"] = ""
            rec["response"] = ""
            rec["started_at"] = _now_str()
            rec["finished_at"] = ""
            rec["updated_at"] = _now_str()
            rec["logs"].append(f"[{_now_str()}] 启动 {turn['provider_label']}（模式：{turn['mode']}，会话：{chat_id}）...")
            if len(rec["logs"]) > 200:
                rec["logs"] = rec["logs"][-200:]
            snap0 = self._copy_session(rec)

        self._publish({
            "source": "ai",
            "step": "ai",
            "chat_id": chat_id,
            "surface": turn["surface"],
            "status": "running",
            "message": f"{turn['provider_label']} 正在处理（{turn['mode']}）...",
            "ai_snapshot": snap0,
        })

        # 重新构造命令，确保队列中后续轮次携带上一轮刚拿到的 --resume session_id！
        cfg = self._load_cfg()
        spec = turn.get("command_override") or build_agent_command(
            turn["full_prompt"],
            self.root,
            provider=turn["provider"],
            mode=turn["mode"],
            session_id=cur_sid,
            attach=turn.get("attach"),
            cfg=cfg,
        )
        if not spec.get("ok"):
            with self._lock:
                rec = self._sessions.get(chat_id)
                if rec:
                    rec["running"] = False
                    rec["status"] = "error"
                    rec["error"] = str(spec.get("error") or "无法构造 CLI 命令")
                    rec["finished_at"] = _now_str()
                    rec["updated_at"] = _now_str()
                    snap_err = self._copy_session(rec)
                else:
                    snap_err = None
            if snap_err:
                self._publish({"source": "ai", "step": "ai", "chat_id": chat_id, "surface": turn["surface"], "status": "error", "message": snap_err["error"], "ai_snapshot": snap_err})
            return

        cmd = spec["cmd"]
        env = {**self._base_env(), **(spec.get("env_patch") or {})}
        creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        out_lines: list[str] = []
        err_lines: list[str] = []
        rc = -1
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=spec["cwd"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=creation,
            )
            with self._lock:
                self._procs[chat_id] = proc

            def _drain_err():
                assert proc.stderr is not None
                for ln in proc.stderr:
                    t = scrub_text(ln.rstrip(), secrets_list)
                    if not t:
                        continue
                    err_lines.append(t)
                    with self._lock:
                        r = self._sessions.get(chat_id)
                        if r is not None:
                            r["logs"].append(f"[stderr] {t[:300]}")
                            if len(r["logs"]) > 200:
                                r["logs"] = r["logs"][-200:]

            eth = threading.Thread(target=_drain_err, daemon=True, name=f"ai-err-{chat_id}")
            eth.start()

            assert proc.stdout is not None
            for line in proc.stdout:
                txt = scrub_text(line.rstrip("\r\n"), secrets_list)
                if not txt.strip():
                    continue
                out_lines.append(txt)
                stripped = txt.strip()
                if stripped.startswith("{") and stripped.endswith("}"):
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, dict):
                            with self._lock:
                                r = self._sessions.get(chat_id)
                                if r is not None:
                                    sid = parsed.get("sessionId") or parsed.get("session_id")
                                    if sid:
                                        r["session_id"] = str(sid)
                                    if isinstance(parsed.get("usage"), dict):
                                        r["usage"] = parsed["usage"]
                                    if isinstance(parsed.get("response"), str):
                                        r["response"] = scrub_text(parsed["response"], secrets_list)
                                    elif isinstance(parsed.get("delta"), str):
                                        r["response"] += scrub_text(parsed["delta"], secrets_list)
                                    snap_live = self._copy_session(r)
                                else:
                                    snap_live = None
                            if snap_live:
                                self._publish({
                                    "source": "ai", "step": "ai", "chat_id": chat_id,
                                    "surface": turn["surface"], "status": "running",
                                    "message": f"{turn['provider_label']} 输出更新",
                                    "ai_snapshot": snap_live,
                                })
                    except ValueError:
                        pass

            rc = proc.wait()
            eth.join(timeout=1.5)
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:
                    pass
            whole_stdout = "\n".join(out_lines)
            parsed_final = parse_agent_json_output(turn["provider"], whole_stdout)

            with self._lock:
                self._procs.pop(chat_id, None)
                r = self._sessions.get(chat_id)
                if r is None:
                    return
                was_stopped = r.get("status") == "stopped"
                if parsed_final["session_id"]:
                    r["session_id"] = parsed_final["session_id"]
                if parsed_final["usage"]:
                    r["usage"] = parsed_final["usage"]
                if parsed_final["response"]:
                    r["response"] = scrub_text(parsed_final["response"], secrets_list)
                elif not r["response"] and out_lines:
                    r["response"] = scrub_text("\n".join(out_lines[-40:]), secrets_list)
                r["running"] = False
                r["finished_at"] = _now_str()
                r["updated_at"] = _now_str()
                if was_stopped:
                    r["status"] = "stopped"
                elif rc == 0:
                    r["status"] = "done"
                else:
                    r["status"] = "error"
                    err_tail = "\n".join(err_lines[-6:]).strip()
                    r["error"] = f"{turn['provider_label']} 退出码 {rc}" + (f"：{err_tail}" if err_tail else "")
                    if not r["response"] and err_tail:
                        r["response"] = err_tail
                r["messages"].append({
                    "role": "assistant",
                    "text": r["response"] or r["error"] or "(无文本回复)",
                    "status": r["status"],
                    "ts": _now_str(),
                })
                final_snap = self._copy_session(r)

            dur_ms = round((time.monotonic() - t0) * 1000.0, 1)
            _run_log.append_cli_log(
                self.batches_root,
                command=f"ai.{turn['provider']}",
                argv=[cmd[0], "--mode", turn["mode"], "--surface", turn["surface"]],
                source="ai",
                actor=turn["provider"],
                exit_code=rc,
                duration_ms=dur_ms,
                ok=(rc == 0),
                session_id=final_snap.get("session_id") or chat_id,
                message=f"{turn['provider']} ({turn['mode']}) 完成" if rc == 0 else f"{turn['provider']} 失败 (rc={rc})",
                detail={"chat_id": chat_id, "surface": turn["surface"], "usage": final_snap.get("usage") or {}},
            )
            self._publish({
                "source": "ai",
                "step": "ai",
                "chat_id": chat_id,
                "surface": turn["surface"],
                "status": final_snap["status"],
                "message": f"{turn['provider_label']} 执行完成" if rc == 0 else f"{turn['provider_label']} 结束（{final_snap['status']}）",
                "ai_snapshot": final_snap,
            })
        except Exception as exc:
            err_s = scrub_text(str(exc), secrets_list)
            with self._lock:
                self._procs.pop(chat_id, None)
                r = self._sessions.get(chat_id)
                if r is not None:
                    r["running"] = False
                    r["status"] = "error"
                    r["error"] = err_s
                    r["finished_at"] = _now_str()
                    r["updated_at"] = _now_str()
                    r["messages"].append({"role": "assistant", "text": f"启动异常：{err_s}", "status": "error", "ts": _now_str()})
                    snap_exc = self._copy_session(r)
                else:
                    snap_exc = None
            if snap_exc:
                self._publish({"source": "ai", "step": "ai", "chat_id": chat_id, "surface": turn["surface"], "status": "error", "message": f"启动失败：{err_s}", "ai_snapshot": snap_exc})

    def stop_session(self, chat_id: str = "") -> dict[str, Any]:
        with self._lock:
            target = str(chat_id or self._last_chat_id or "").strip()
            if not target or target not in self._sessions:
                return {"ok": True, "message": "当前没有运行中的 AI 对话"}
            rec = self._sessions[target]
            proc = self._procs.get(target)
            # 清空待执行队列
            q = self._queues.get(target)
            if q is not None:
                while not q.empty():
                    try:
                        q.get_nowait()
                        q.task_done()
                    except Exception:
                        break
            rec["queued"] = 0
            if not rec.get("running") and proc is None:
                return {"ok": True, "chat_id": target, "message": "当前对话未在运行"}
            rec["status"] = "stopped"
            rec["updated_at"] = _now_str()
        if proc and proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=5)
                else:
                    proc.terminate()
                    proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._publish({"source": "ai", "step": "ai", "chat_id": target, "status": "stopped", "message": "已停止 AI 对话任务"})
        return {"ok": True, "chat_id": target, "message": "已停止 AI 对话任务"}

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._sessions.keys())
        for cid in ids:
            try:
                self.stop_session(cid)
            except Exception:
                pass

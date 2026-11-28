# -*- coding: utf-8 -*-
"""统一中文报错诊断、语义化退出码、硬件设备探测与错误日志落盘。

设计原则：
1. 任何异常暴露给用户时都必须具备三要素：【出错模块】+【中文原因】+【建议修复方向】；
2. 退出码不再只给冷冰冰的 1/2/3/4，统一映射到可读中文说明；
3. 自动检测 GPU（CUDA），有显卡优先用 GPU，无显卡自动回退 CPU。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_TZ_CN = timezone(timedelta(hours=8))
_ERROR_LOG_LOCK = threading.RLock()

_JSON_STRUCT_ERROR_HINTS = (
    "json重复键",
    "extra data",
    "expecting",
    "unterminated string",
    "响应未完整结束",
    "顶层必须为图块对象",
    "图块类型或结构不符",
    "没有逐空结构",
    "小题号非法",
    "小题缺空位",
    "空序号非法",
    "空位编号与子层级冲突",
    "空序号不连续",
    "小题号规范化后重复",
    "空位完整编号重复",
    "结构异常",
    "缺图块输出",
)


def is_json_or_struct_error(text: str | None) -> bool:
    """判断错误文本是否属于模型输出 JSON/结构类异常（适合单题降级重试，而非网络/鉴权故障）。"""
    s = (text or "").strip()
    if not s:
        return False
    low = s.lower()
    if any(k in low for k in ("http 401", "http 403", "http 429", "http 5", "timeout", "urlopen", "connection", "余额", "费用限制", "setlimitexceeded", "insufficientbalance", "accountoverdue", "api key")):
        return False
    return any(k in low for k in _JSON_STRUCT_ERROR_HINTS)

# ==================== 语义化退出码 ====================
EXIT_OK = 0
EXIT_GENERAL_ERROR = 1
EXIT_ALL_PAGES_FAILED = 2
EXIT_ENV_MISSING = 3
EXIT_API_CONFIG_ERROR = 4
EXIT_IO_PERMISSION = 5
EXIT_MANIFEST_CONFLICT = 6
EXIT_WEIGHTS_MISSING = 7

EXIT_CODE_MAP: dict[int, dict[str, str]] = {
    EXIT_OK: {
        "title": "正常结束",
        "reason": "识别程序已运行结束。",
        "fix": "如仍有部分失败页或失败图块，可直接使用「补跑失败项」重试。",
    },
    EXIT_GENERAL_ERROR: {
        "title": "运行中发生未预期异常",
        "reason": "程序运行期间抛出未分类错误并提前终止。",
        "fix": "请查看下方中文错误详情或 recognized/logs/errors.log，也可点击「求助 AI」定位。",
    },
    EXIT_ALL_PAGES_FAILED: {
        "title": "全部试卷本地检测失败",
        "reason": "本批次所有页面在本地检测阶段（方向/YOLO/PaddleOCR）均执行失败，未能产出有效识别结果。",
        "fix": "最常见原因是缺少 paddleocr 依赖、OpenCV/NumPy 版本冲突或模型权重损坏。请先回到「第 1 步：运行环境」点击重新检查并一键修复。",
    },
    EXIT_ENV_MISSING: {
        "title": "运行环境或核心依赖缺失/冲突",
        "reason": "缺少运行所需的 Python 依赖库（如 paddleocr、ultralytics、torch、onnxruntime），或库版本存在冲突。",
        "fix": "请回到「第 1 步：运行环境」点击「自动测速并安装缺失依赖」，或在终端运行 pip install -r requirements.txt。",
    },
    EXIT_API_CONFIG_ERROR: {
        "title": "大模型视觉 API 未配置或鉴权失败",
        "reason": "未配置有效的 API Key（或仍为 .env.example 中的占位符），或服务商拒绝了该密钥/余额不足。",
        "fix": "请点击右上角「⚙ 大模型 API」填入有效密钥并点击「测试连通性」（确认支持看图）后再启动识别。",
    },
    EXIT_IO_PERMISSION: {
        "title": "文件读写权限冲突或磁盘异常",
        "reason": "目标目录被其他程序占用、权限不足或磁盘空间已满，导致无法保存识别结果。",
        "fix": "请检查磁盘剩余空间、关闭正在占用结果文件的程序，避免把项目放在受保护的系统目录（如 C:\\Program Files）。",
    },
    EXIT_MANIFEST_CONFLICT: {
        "title": "输出目录与历史批次配置不一致",
        "reason": "当前输出目录中已有历史运行记录，但输入图片、模型权重或推理代码/设备配置与之前不同，无法直接复用旧断点。",
        "fix": "请点击「重新导入/新建批次」，或在命令行指定新的 --out 输出目录，也可清理该批次 recognized/ 目录后重新识别。",
    },
    EXIT_WEIGHTS_MISSING: {
        "title": "模型权重文件缺失或未解压",
        "reason": "models/ 目录中缺少必要的推理权重文件（orient/M1/M2/M3/choice 或 PP-OCRv6 ONNX 模型）。",
        "fix": "请回到「第 1 步：运行环境」输入密码解压加密权重，或在终端运行 python scripts/download_weights.py 拉取官方与自训权重。",
    },
}


def describe_exit_code(code: int | None) -> dict[str, Any]:
    """将进程退出码翻译为中文标题、原因和修复建议。"""
    if code is None:
        return {
            "code": None,
            "title": "运行中或未返回退出码",
            "reason": "任务尚未结束或进程被外部强制中止。",
            "fix": "可重新点击启动，或检查后台任务状态。",
        }
    info = EXIT_CODE_MAP.get(int(code))
    if info:
        return {"code": int(code), **info}
    if int(code) < 0 or int(code) in (3221225786, 130, 143):
        return {
            "code": int(code),
            "title": "任务被手动中断或系统终止",
            "reason": f"进程收到终止信号（退出码 {code}），可能是人为停止或显存/内存被系统回收。",
            "fix": "如需继续，可直接重新点击「开始自动识别」，已完成的页面与图块会自动断点续跑。",
        }
    if int(code) == 3221225477:
        return {
            "code": int(code),
            "title": "底层扩展库内存访问异常（闪退）",
            "reason": "C++/CUDA/ONNX 底层库发生崩溃（0xC0000005），常见于显卡驱动老旧或 OpenCV/Paddle/PyTorch DLL 冲突。",
            "fix": "建议先在「第 1 步：运行环境」把推理设备临时切为 CPU 验证，并检查 requirements.txt 中 opencv-contrib-python 与 numpy 版本是否一致。",
        }
    return {
        "code": int(code),
        "title": f"异常退出（退出码 {code}）",
        "reason": f"子进程返回非零退出码 {code}。",
        "fix": "请查看下方中文错误卡片或 recognized/logs/errors.log 中的详细原因。",
    }


# ==================== 占位符 API Key 识别 ====================
_PLACEHOLDER_PATTERNS = (
    r"^your[_-]",
    r"api[_-]?key[_-]?here$",
    r"^sk-x{3,}$",
    r"^replace[_-]?me",
    r"^changeme$",
    r"^<.+>$",
    r"^\*{4,}$",
    r"^请填写",
    r"^示例",
)


def is_placeholder_key(value: str | None) -> bool:
    """判断 API Key 是否为模板占位符（避免复制 .env.example 后被误判为已配置）。"""
    s = (value or "").strip()
    if not s:
        return False
    low = s.lower()
    if low in ("none", "null", "empty", "todo", "xxx", "test_key", "your_key"):
        return True
    return any(re.search(pat, low) for pat in _PLACEHOLDER_PATTERNS)


# ==================== 硬件与推理设备自动探测 ====================
_HW_CACHE: dict[str, Any] | None = None


def probe_compute_hardware(force: bool = False) -> dict[str, Any]:
    """探测本机 CUDA GPU 与 CPU 核心信息（结果缓存，避免反复初始化）。"""
    global _HW_CACHE
    if _HW_CACHE is not None and not force:
        return dict(_HW_CACHE)
    info: dict[str, Any] = {
        "cuda": False,
        "gpu_count": 0,
        "gpu_name": "",
        "vram_gb": 0.0,
        "cpu_count": os.cpu_count() or 4,
        "recommended_device": "cpu",
        "recommended_label": f"CPU ({os.cpu_count() or 4} 线程)",
        "torch_cuda_version": "",
    }
    try:
        import torch  # type: ignore

        info["torch_cuda_version"] = str(getattr(torch.version, "cuda", "") or "")
        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            props = torch.cuda.get_device_properties(0)
            vram_gb = round(props.total_memory / (1024 ** 3), 1)
            gpu_name = torch.cuda.get_device_name(0)
            info.update({
                "cuda": True,
                "gpu_count": torch.cuda.device_count(),
                "gpu_name": gpu_name,
                "vram_gb": vram_gb,
                "recommended_device": "cuda:0",
                "recommended_label": f"GPU: {gpu_name} ({vram_gb} GB)",
            })
    except Exception:
        pass
    _HW_CACHE = dict(info)
    return info


def resolve_infer_device(requested: str | None = "auto") -> tuple[str, str]:
    """解析推理设备（auto 优先使用 GPU，无可用 GPU 自动降级 CPU）。

    返回：(torch/ultralytics 设备字符串如 'cuda:0' 或 'cpu', 可读中文设备说明)
    """
    req = (requested or "auto").strip().lower()
    hw = probe_compute_hardware()
    if req in ("", "auto", "gpu", "cuda"):
        if hw["cuda"]:
            return "cuda:0", hw["recommended_label"]
        return "cpu", f"CPU 自动回退（未检测到可用 CUDA 显卡，{hw['cpu_count']} 线程）"
    if req == "cpu":
        return "cpu", f"CPU 手动指定（{hw['cpu_count']} 线程）"
    if req.isdigit():
        req = f"cuda:{req}"
    if req.startswith("cuda"):
        if hw["cuda"]:
            return req, f"GPU ({req}): {hw['gpu_name']} ({hw['vram_gb']} GB)"
        return "cpu", f"请求 {req} 但本机无可用 CUDA 显卡，已自动回退 CPU"
    return "cpu", f"未识别设备 {requested}，已回退 CPU"


# ==================== 中文报错诊断引擎 ====================
_MODULE_INSTALL_HINTS: dict[str, tuple[str, str, str]] = {
    "paddleocr": (
        "PaddleOCR 印刷题号模块",
        "缺少必需的 Python 依赖包 `paddleocr`（每张试卷读取题号都会调用它）。",
        "请在工作台「第 1 步：运行环境」点击一键安装缺失依赖，或在终端运行：pip install \"paddleocr>=3.7.0,<3.8.0\" \"numpy>=2.0.0,<2.4\" \"opencv-contrib-python==4.10.0.84\"",
    ),
    "paddlex": (
        "PaddleOCR 运行底座 (paddlex)",
        "缺少 `paddlex` 依赖或安装不完整。",
        "请在终端重新安装：pip install --upgrade \"paddleocr>=3.7.0,<3.8.0\"",
    ),
    "onnxruntime": (
        "ONNX Runtime 推理引擎",
        "缺少 `onnxruntime`，无法加载 PP-OCRv6 ONNX 印刷题号模型。",
        "请运行：pip install onnxruntime",
    ),
    "ultralytics": (
        "YOLO 版面与题框检测模块",
        "缺少 `ultralytics`，无法加载方向/M1/M2/M3 检测模型。",
        "请运行：pip install ultralytics==8.4.96",
    ),
    "torch": (
        "PyTorch 深度学习框架",
        "缺少 `torch` 或 PyTorch DLL 加载失败。",
        "请运行：pip install torch torchvision（NVIDIA 显卡用户建议安装 CUDA 版 PyTorch）。",
    ),
    "cv2": (
        "OpenCV 图像处理模块",
        "缺少 `cv2`（OpenCV），或 opencv-python 与 opencv-contrib-python 同时安装导致冲突。",
        "建议先卸载冲突包再装锁定版本：pip uninstall -y opencv-python opencv-contrib-python && pip install opencv-contrib-python==4.10.0.84",
    ),
    "PIL": (
        "Pillow 图像读写模块",
        "缺少 `Pillow` 图像库。",
        "请运行：pip install Pillow",
    ),
    "scipy": (
        "SciPy 题号配对算法模块",
        "缺少 `scipy`，无法执行题号与选项框的匈牙利匹配。",
        "请运行：pip install scipy",
    ),
}


def diagnose_exception(exc_or_text: BaseException | str, default_module: str = "识别流水线") -> dict[str, Any]:
    """将异常或错误文本翻译为结构化中文诊断（模块 + 原因 + 修复方向 + 退出码）。"""
    if isinstance(exc_or_text, BaseException):
        exc_type = type(exc_or_text).__name__
        raw = f"{exc_type}: {exc_or_text}"
        msg = str(exc_or_text)
    else:
        exc_type = ""
        raw = str(exc_or_text or "").strip()
        msg = raw
    low = raw.lower()

    # 1. 缺包 ModuleNotFoundError / No module named 'xxx'
    m_mod = re.search(r"No module named ['\"]([^'\".]+)", raw, re.IGNORECASE)
    if m_mod or exc_type == "ModuleNotFoundError":
        pkg = m_mod.group(1) if m_mod else "未知模块"
        if pkg in _MODULE_INSTALL_HINTS:
            mod_name, reason, fix = _MODULE_INSTALL_HINTS[pkg]
        else:
            mod_name = f"Python 依赖库 ({pkg})"
            reason = f"当前 Python 环境中未安装 `{pkg}` 模块。"
            fix = f"请运行 `pip install {pkg}` 或执行 `pip install -r requirements.txt` 补齐依赖。"
        return {
            "module": mod_name,
            "title": f"缺少依赖库：{pkg}",
            "reason": reason,
            "fix": fix,
            "exit_code": EXIT_ENV_MISSING,
            "raw": raw[:320],
        }

    # 2. 权限 / 文件被占用 WinError 5 / PermissionError
    if "permissionerror" in low or "winerror 5" in low or "拒绝访问" in raw or "permission denied" in low:
        return {
            "module": "文件读写与结果保存",
            "title": "文件被占用或目录无写入权限",
            "reason": "写入结果文件时被系统拒绝（常见原因：杀毒软件/审阅台并发占用文件，或项目位于 C:\\Program Files 等受保护路径）。",
            "fix": "请稍后重试（新版已内置 5 秒自动重试）；若频繁出现，请将项目移至 D:\\ 或桌面普通目录，不要放在系统保护文件夹中。",
            "exit_code": EXIT_IO_PERMISSION,
            "raw": raw[:320],
        }

    # 3. 磁盘空间不足
    if "no space left" in low or "winerror 112" in low or "磁盘空间不足" in raw:
        return {
            "module": "磁盘存储",
            "title": "磁盘剩余空间不足",
            "reason": "保存转正图片、裁剪题图或识别结果时磁盘空间已满。",
            "fix": "请清理项目所在磁盘空间（建议至少预留 2 GB 以上可用空间）后重新运行。",
            "exit_code": EXIT_IO_PERMISSION,
            "raw": raw[:320],
        }

    # 4. 权重缺失
    if (
        ("models" in low and ("not found" in low or "filenotfound" in low or "不存在" in raw or "no such file" in low))
        or "pp-ocrv6" in low
        or "weights" in low and "不存在" in raw
    ):
        return {
            "module": "模型权重加载",
            "title": "缺少必要的模型权重文件",
            "reason": f"未能找到或读取所需权重文件：{msg[:160]}",
            "fix": "请回到「第 1 步：运行环境」输入密码解压加密权重，或在终端运行 `python scripts/download_weights.py` 补齐官方 PP-OCRv6 与自训模型权重。",
            "exit_code": EXIT_WEIGHTS_MISSING,
            "raw": raw[:320],
        }

    # 5. manifest 冲突
    if "不同于原运行" in raw or "manifest" in low and ("conflict" in low or "不同" in raw):
        return {
            "module": "断点续跑校验",
            "title": "当前输出目录与历史批次配置不一致",
            "reason": "该输出目录已存在旧的 manifest.json，但图片、模型权重或代码配置已发生变化，为防结果错乱拒绝混写。",
            "fix": "请重新导入为新批次，或备份并清空当前批次下的 recognized/ 文件夹后再点「开始自动识别」。",
            "exit_code": EXIT_MANIFEST_CONFLICT,
            "raw": raw[:320],
        }

    # 6. CUDA / 显存不足
    if "cuda out of memory" in low or "out of memory" in low or "cublas" in low or "cudnn" in low:
        return {
            "module": "GPU 显存与 CUDA 推理",
            "title": "显卡显存不足或 CUDA 推理异常",
            "reason": "当前批大小（YOLO Batch）或并发任务占用了过多显存，或显卡驱动与 PyTorch CUDA 版本不匹配。",
            "fix": "请在「第 1 步：运行环境」中减小 YOLO 批大小（如改为 1 或 2），或点击「一键本机测速」重新选配，也可临时切回 CPU 模式。",
            "exit_code": EXIT_GENERAL_ERROR,
            "raw": raw[:320],
        }

    # 7. API Key 缺失 / 鉴权 / 余额 / 限流 / 网络
    if "api key" in low or "ark_api_key" in low or "占位符" in raw or "凭据或权限失败" in raw or "http 401" in low or "http 403" in low:
        return {
            "module": "视觉大模型 API 鉴权",
            "title": "API Key 无效、未配置或无调用权限",
            "reason": "服务商返回 401/403 鉴权失败，或尚未配置有效 API Key（含占位符）。",
            "fix": "请点击右上角「⚙ 大模型 API」，检查运营商、模型名称与 API Key，并点「测试连通性」通过后再试。",
            "exit_code": EXIT_API_CONFIG_ERROR,
            "raw": raw[:320],
        }
    if "setlimitexceeded" in low or "insufficientbalance" in low or "accountoverdue" in low or "余额" in raw or "费用限制" in raw:
        return {
            "module": "视觉大模型 API 额度",
            "title": "API 账号余额不足或达到用量上限",
            "reason": "大模型服务商已暂停当前 Key 的调用额度。",
            "fix": "请登录对应模型平台充值或调高限额，或在「⚙ 大模型 API」切换到其他可用服务商后点「补跑失败图块」。",
            "exit_code": EXIT_API_CONFIG_ERROR,
            "raw": raw[:320],
        }
    if "http 429" in low or "rate limit" in low or "限流" in raw:
        return {
            "module": "视觉大模型 API 并发",
            "title": "触发接口频率限制（HTTP 429）",
            "reason": "当前并发请求数超过了服务商允许的速率上限。",
            "fix": "程序已自动降级为串行并重试；如仍频繁出现，请在「第 1 步：运行环境」把 API 并发数调低到 2，稍后点「补跑失败图块」。",
            "exit_code": EXIT_GENERAL_ERROR,
            "raw": raw[:320],
        }
    if "timeout" in low or "urlopen error" in low or "connection" in low or "getaddrinfo" in low:
        return {
            "module": "视觉大模型网络连接",
            "title": "连接大模型接口超时或网络中断",
            "reason": "无法连通配置的 API 站点，或单次视觉推理等待超过 180 秒。",
            "fix": "请检查网络/代理连接，点击右上角「⚙ 大模型 API」核对站点地址并测试连通性；已成功图块已缓存，可直接「补跑失败图块」。",
            "exit_code": EXIT_GENERAL_ERROR,
            "raw": raw[:320],
        }

    # 8. NumPy / OpenCV 二进制不兼容
    if ("numpy" in low and ("dtype size changed" in low or "incompatible" in low or "_array_api" in low)) or (
        "cv2" in low and "dll" in low
    ):
        return {
            "module": "NumPy / OpenCV 依赖兼容",
            "title": "NumPy 或 OpenCV 版本不兼容",
            "reason": "PaddleOCR/paddlex 要求 numpy<2.4 且 opencv-contrib-python==4.10.0.84，当前环境可能混装了不兼容版本。",
            "fix": "请执行：pip uninstall -y opencv-python opencv-contrib-python && pip install \"numpy>=2.0.0,<2.4\" \"opencv-contrib-python==4.10.0.84\"",
            "exit_code": EXIT_ENV_MISSING,
            "raw": raw[:320],
        }

    # 8.5 视觉大模型 JSON 解析 / 重复键 / 结构异常（告别循环指引）
    if is_json_or_struct_error(raw):
        return {
            "module": "视觉大模型输出解析",
            "title": "大模型返回 JSON 结构异常或存在重复键",
            "reason": f"模型在多题拼图输出中产生了重复小题号或结构异常（{msg[:200]}），并非 API 密钥或网络故障。",
            "fix": "新版已支持同层重复键自动合并（冲突项标为待复核），且点击「补跑失败图块」会自动降级为单题模式重试，通常一轮即可清零。",
            "exit_code": EXIT_GENERAL_ERROR,
            "raw": raw[:320],
        }

    # 默认兜底
    return {
        "module": default_module,
        "title": f"{default_module}出现异常" + (f"（{exc_type}）" if exc_type else ""),
        "reason": msg[:260] or "发生未明确分类的运行错误。",
        "fix": "请检查具体原始报错信息，或点击工作台「求助 AI」结合最新运行日志定位修复。",
        "exit_code": EXIT_GENERAL_ERROR,
        "raw": raw[:320],
    }


def append_error_log(
    out_dir: Path | str,
    module: str,
    exc: BaseException | str,
    diag: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    retries: int = 20,
    delay: float = 0.02,
) -> Path | None:
    """将结构化中文诊断与完整 traceback 线程安全追加写入 <out_dir>/logs/errors.log。"""
    log_path = Path(out_dir) / "logs" / "errors.log"
    d = diag or diagnose_exception(exc, default_module=module)
    ts = datetime.now(_TZ_CN).isoformat(timespec="seconds")
    tb = ""
    if isinstance(exc, BaseException) and exc.__traceback__ is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    lines = [
        f"================ [{ts}] {d['module']} | {d['title']} ================",
        f"原因：{d['reason']}",
        f"建议修复：{d['fix']}",
    ]
    if context:
        lines.append("上下文：" + json.dumps(context, ensure_ascii=False))
    if d.get("raw"):
        lines.append(f"原始信息：{d['raw']}")
    if tb:
        lines.append("完整堆栈：\n" + tb.rstrip())
    lines.append("")
    payload = "\n".join(lines) + "\n"

    last_exc: Exception | None = None
    with _ERROR_LOG_LOCK:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as mk_exc:
            print(f"[WARN] 创建错误日志目录失败 ({log_path.parent}): {mk_exc}", file=sys.stderr)
            return None
        for attempt in range(max(1, int(retries))):
            try:
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                return log_path
            except OSError as io_exc:
                last_exc = io_exc
                if attempt + 1 < retries:
                    time.sleep(delay)
    print(f"[WARN] 写入 errors.log 失败 ({log_path}): {last_exc}", file=sys.stderr)
    return None

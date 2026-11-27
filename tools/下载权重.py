# -*- coding: utf-8 -*-
"""一键准备模型权重（零第三方依赖，可在 Windows CMD 直接运行）。

功能：
1. 自动从百度飞桨官方源下载 PP-OCRv6 印刷题号 ONNX 权重（约 73 MB）并校验 SHA256；
2. 自动从 GitHub Releases 下载本项目精简推理权重加密包（约 45.7 MB）到 weights/ 目录；
3. 如传入 --password 或本机装有 7-Zip，可自动校验并解压到 models/ 目录。

用法：
  python tools/下载权重.py                  # 下载官方 PP-OCRv6 + GitHub Release 权重包（并提示输入密码解压）
  python tools/下载权重.py --only-paddle    # 仅下载百度飞桨官方 PP-OCRv6 权重
  python tools/下载权重.py --no-extract     # 只下载不解压（随后可在桌面工作台输入密码解压）
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS_DIR = ROOT / "weights"
PADDLE_DIR = ROOT / "models" / "pretrained" / "PP-OCRv6_medium_rec_onnx"

RELEASE_TAG = "v2026.11.27"
ARCHIVE_NAME = "autograde_weights_v20261127.7z"
ARCHIVE_SHA256 = "79a91ac256ee5ff48f66225eeecc8d17bbbf5595bc7bb3da9c1c25190b677f2c"
RELEASE_URL = (
    f"https://github.com/KJNSAIUDHWI/my_first_project/releases/download/{RELEASE_TAG}/{ARCHIVE_NAME}"
)

PADDLE_URL = (
    "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/"
    "paddle3.0.0/PP-OCRv6_medium_rec_onnx_infer.tar"
)
PADDLE_SHA256 = {
    "inference.onnx": "9c09abf0957f7968c7586464b7397b84ad2387a0497a351af40e9acc71b673ba",
    "inference.yml": "991b700facf5b50a7de193468207d5f4255b538dde0d312ae3b7c7a9b6873129",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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


def download_file(url: str, dst: Path, label: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "AutoGradeDownloader/1.0"})
    print(f"[下载] {label}")
    print(f"       来源: {url}")
    with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last_pct = -1
        while True:
            buf = resp.read(256 * 1024)
            if not buf:
                break
            out.write(buf)
            done += len(buf)
            if total > 0:
                pct = int(done * 100 / total)
                if pct >= last_pct + 5 or pct == 100:
                    last_pct = pct
                    print(
                        f"\r       进度: {pct:3d}% ({done / 1048576:.1f} / {total / 1048576:.1f} MB)",
                        end="",
                        flush=True,
                    )
        print()
    os.replace(tmp, dst)


def ensure_paddle_ocr(root: Path = ROOT) -> bool:
    target_dir = root / "models" / "pretrained" / "PP-OCRv6_medium_rec_onnx"
    all_ok = True
    for name, expected in PADDLE_SHA256.items():
        p = target_dir / name
        if not p.is_file() or sha256_file(p) != expected:
            all_ok = False
            break
    if all_ok:
        print(f"[跳过] 官方 PP-OCRv6 权重已就绪：{target_dir}")
        return True

    with tempfile.TemporaryDirectory(prefix="paddle_ocr_") as td:
        tar_path = Path(td) / "PP-OCRv6_medium_rec_onnx_infer.tar"
        download_file(PADDLE_URL, tar_path, "百度飞桨官方 PP-OCRv6 印刷题号模型 (约 73 MB)")
        target_dir.mkdir(parents=True, exist_ok=True)
        extracted = {}
        with tarfile.open(tar_path, "r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                base = Path(member.name).name
                if base in PADDLE_SHA256:
                    src_f = tf.extractfile(member)
                    if src_f is None:
                        continue
                    dst_p = target_dir / base
                    with dst_p.open("wb") as out:
                        shutil.copyfileobj(src_f, out)
                    extracted[base] = dst_p
        for name, expected in PADDLE_SHA256.items():
            p = target_dir / name
            if not p.is_file():
                raise RuntimeError(f"官方压缩包内未找到 {name}")
            actual = sha256_file(p)
            if actual != expected:
                raise RuntimeError(f"{name} SHA256 校验失败: 期望 {expected}, 实际 {actual}")
        prov = {
            "model": "PP-OCRv6_medium_rec",
            "engine": "ONNX Runtime CPU",
            "source": PADDLE_URL,
            "sha256": PADDLE_SHA256,
        }
        (target_dir / "provenance.json").write_text(
            json.dumps(prov, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"[完成] 官方 PP-OCRv6 权重已就绪：{target_dir}")
    return True


def ensure_release_archive(root: Path = ROOT, url: str = RELEASE_URL) -> Path:
    dst = root / "weights" / ARCHIVE_NAME
    if dst.is_file() and sha256_file(dst) == ARCHIVE_SHA256:
        print(f"[跳过] 精简推理权重包已存在且校验通过：{dst}")
        return dst
    download_file(url, dst, f"GitHub Release 自训推理权重包 ({ARCHIVE_NAME}, 约 45.7 MB)")
    actual = sha256_file(dst)
    if actual != ARCHIVE_SHA256:
        raise RuntimeError(f"{ARCHIVE_NAME} SHA256 校验失败: 期望 {ARCHIVE_SHA256}, 实际 {actual}")
    print(f"[完成] 权重包下载并校验通过：{dst}")
    return dst


def extract_archive(archive: Path, password: str, root: Path = ROOT) -> None:
    seven_zip = find_7z()
    if not seven_zip:
        raise RuntimeError("未检测到 7-Zip，请先安装 7-Zip（https://www.7-zip.org/）后再解压。")
    cmd = [seven_zip, "x", str(archive), f"-o{root}", f"-p{password}", "-y"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError("解压失败（请检查密码是否正确）：\n" + (proc.stderr or proc.stdout)[-800:])
    print(f"[完成] 6 个自训推理权重已解压至：{root / 'models'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="下载官方 PP-OCRv6 与 GitHub Release 精简推理权重")
    parser.add_argument("--only-paddle", action="store_true", help="仅下载百度飞桨官方 PP-OCRv6 模型")
    parser.add_argument("--skip-paddle", action="store_true", help="跳过官方 PP-OCRv6 模型下载")
    parser.add_argument("--no-extract", action="store_true", help="下载权重压缩包后不自动解压")
    parser.add_argument("--password", default="", help="加密权重包解压密码（可选，留空则在终端提示输入）")
    parser.add_argument("--release-url", default=RELEASE_URL, help="自定义权重包下载链接")
    args = parser.parse_args()

    try:
        if not args.skip_paddle:
            ensure_paddle_ocr(ROOT)
        if args.only_paddle:
            return 0
        archive = ensure_release_archive(ROOT, url=args.release_url)
        if args.no_extract:
            print("已跳过解压。你可以稍后打开「自动改卷工作台」输入密码一键解压。")
            return 0
        pwd = args.password
        if not pwd and sys.stdin.isatty():
            pwd = getpass.getpass("请输入权重压缩包解压密码（直接回车则稍后在工作台解压）: ").strip()
        if not pwd:
            print("未输入密码，已跳过解压。稍后运行「自动改卷工作台.exe」输入密码即可自动解压。")
            return 0
        extract_archive(archive, pwd, ROOT)
        return 0
    except urllib.error.URLError as exc:
        print(f"[网络错误] 下载失败：{exc}", file=sys.stderr)
        print("提示：若 GitHub 访问较慢，可手动在浏览器打开 Releases 页面下载附件放入 weights/ 目录：", file=sys.stderr)
        print(f"      https://github.com/KJNSAIUDHWI/my_first_project/releases/tag/{RELEASE_TAG}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

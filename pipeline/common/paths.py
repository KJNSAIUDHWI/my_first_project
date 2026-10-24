# -*- coding: utf-8 -*-
"""统一路径。所有脚本只从这里取路径，不要自己拼。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # 自动改卷/
DATA = ROOT / "data"
RAW = DATA / "raw"
OLD_LABELS = DATA / "旧标注"
MODELS = ROOT / "models"
TRAIN = ROOT / "train"

def m_dir(m):
    return DATA / m

def m_images(m):
    return DATA / m / "images"

def m_labels(m):
    return DATA / m / "labels"

def m_dataset(m):
    return DATA / m / "dataset"

def m_manifest(m):
    return DATA / m / "manifest.csv"

def m_progress(m):
    return DATA / m / "progress.csv"

def m_eval(m):
    return m_dataset(m) / "data.yaml"

def model_weight(m, name=None):
    """m1/m2/m3/m4/orient/cnn 的当前权重；name 指定历史版本"""
    if name:
        return MODELS / m / name
    from .model_registry import current
    return current('digit' if m == 'cnn' else m)


def pretrained(name="yolo11n.pt"):
    return MODELS / "pretrained" / name

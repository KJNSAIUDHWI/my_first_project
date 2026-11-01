"""M3复用检测训练器、独立快照和数值保护；默认YOLO11s、1024输入。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from m2.train_base import main

if __name__ == '__main__':
    main(task='m3')

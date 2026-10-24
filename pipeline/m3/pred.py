# -*- coding: utf-8 -*-
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.pred_base import pred

if __name__ == "__main__":
    pred("m3", {0: 5, 1: 6})

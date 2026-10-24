# -*- coding: utf-8 -*-
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.pred_base import pred

if __name__ == "__main__":
    pred("m4", {0: 7, 1: 9})

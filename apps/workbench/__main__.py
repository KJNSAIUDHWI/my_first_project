# -*- coding: utf-8 -*-
"""支持 `python -m apps.workbench <子命令>` 方式调用。"""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

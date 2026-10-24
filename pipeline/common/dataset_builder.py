"""历史dataset构建接口；训练准入统一由迭代snapshot负责。"""

def build(m, cls_map, names, val_ratio=0.1, seed=42, stage=None):
    raise RuntimeError('历史构建接口已停用，请使用 tools/迭代.py snapshot；不重建共享dataset。')

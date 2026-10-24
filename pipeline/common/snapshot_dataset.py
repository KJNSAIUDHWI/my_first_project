"""检测快照：图片读 data 原图，标签读本 run 的 labels/train 或 labels/val。"""
from pathlib import Path

from ultralytics.data import YOLODataset
from ultralytics.data.build import build_yolo_dataset
from ultralytics.models.yolo.detect import DetectionValidator


class SnapshotDataset(YOLODataset):
    def __init__(self, *args, label_dir, **kwargs):
        self.label_dir = Path(label_dir)
        super().__init__(*args, **kwargs)

    def get_labels(self):
        self.label_files = [str(self.label_dir / (Path(p).stem + '.txt')) for p in self.im_files]
        for path in self.label_files:
            if not Path(path).is_file():
                raise FileNotFoundError('快照标签缺失：' + path)
        labels = self.cache_labels(self.label_dir.with_suffix('.cache'))['labels']
        if not labels:
            raise ValueError('快照没有可读取的图片和标签')
        self.im_files = [row['im_file'] for row in labels]
        return labels


def build_snapshot_dataset(cfg, img_path, batch, data, mode='train', rect=False, stride=32):
    path = Path(img_path)
    if path.name not in ('train_list.txt', 'val_list.txt'):
        return build_yolo_dataset(cfg, img_path, batch, data, mode=mode, rect=rect, stride=stride)
    split = path.stem.split('_')[0]
    return SnapshotDataset(
        img_path=img_path, label_dir=path.parent / 'labels' / split,
        imgsz=cfg.imgsz, batch_size=batch, augment=mode == 'train', hyp=cfg,
        rect=cfg.rect or rect, cache=cfg.cache or None, single_cls=cfg.single_cls,
        stride=stride, pad=0.0 if mode == 'train' else 0.5, prefix=f'{mode}: ',
        task=cfg.task, classes=cfg.classes, data=data,
        fraction=cfg.fraction if mode == 'train' else 1.0)


class SnapshotValidator(DetectionValidator):
    def build_dataset(self, img_path, mode='val', batch=None):
        return build_snapshot_dataset(self.args, img_path, batch, self.data,
                                      mode=mode, stride=self.stride)

"""收尾后固定验证身份、无raw裁图及M4移除；仅写显式临时工作目录。"""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import yolo_snapshot, model_registry, events
from cascade.crop import validate_parent_sources


def main(workdir):
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=False)
    assert 'm4' not in yolo_snapshot.MAPS
    assert 'm4' not in yolo_snapshot.NAMES
    assert 'm4' not in model_registry.DEFAULTS
    assert 'm4' not in events.CLASS_NAMES
    old_box = {'类别': '小题号', '中心X': .5, '中心Y': .5, '宽': .1, '高': .1}
    assert events.boxes_from_cn('m3', [old_box]) == [[7, .5, .5, .1, .1]]
    folder = root / 'data/m1'
    folder.mkdir(parents=True)
    names = ['v1', 'v2']
    file = folder / 'validation_baseline.json'
    record = dict(task='m1', source_run='old_run', count=2, names=names,
                  names_sha256=hashlib.sha256('\n'.join(names).encode()).hexdigest())
    file.write_text(json.dumps(record), 'utf-8')
    plan = {name: {'split': 'val'} for name in names}
    with patch.object(yolo_snapshot, 'ROOT', root), patch.object(yolo_snapshot, 'TRAIN', root/'train'), patch.dict(yolo_snapshot.VALIDATION_BASELINES, m1=('old_run', 2)):
        assert yolo_snapshot.validation_names('m1', plan) == set(names)
        # A historical cleanup copy must never substitute for missing formal configuration.
        old = root / 'models/清理/20261031_项目收尾_1245/m1_validation_baseline.json'
        old.parent.mkdir(parents=True)
        old.write_bytes(file.read_bytes())
        file.unlink()
        try:
            yolo_snapshot.validation_names('m1', plan)
        except ValueError as error:
            assert '固定验证基线登记缺失' in str(error)
        else:
            raise AssertionError('不得从清理目录读取运行配置')
        file.write_bytes(old.read_bytes())
        record['names'] = ['v1', 'other']
        file.write_text(json.dumps(record), 'utf-8')
        try:
            yolo_snapshot.validation_names('m1', plan)
        except ValueError as error:
            assert '指纹' in str(error)
        else:
            raise AssertionError('不可静默替换固定验证名单')
    image, raw = root/'working.jpg', root/'raw.jpg'
    image.write_bytes(b'preserved working image')
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    validate_parent_sources(raw, image, {'working_sha256': digest}, 'page')
    for row in ({}, {'working_sha256': 'wrong'}):
        try:
            validate_parent_sources(raw, image, row, 'page')
        except ValueError:
            pass
        else:
            raise AssertionError('raw缺失时不能跳过工作图指纹')
    raw.write_bytes(b'original')
    try:
        validate_parent_sources(raw, image, {'working_sha256': digest, 'raw_sha256': 'wrong'}, 'page')
    except ValueError as error:
        assert 'raw哈希' in str(error)
    else:
        raise AssertionError('raw仍存在时继续校验')
    print('M4 removed; preserved validation identities; no-raw crop hashes: passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', required=True)
    main(parser.parse_args().workdir)

"""退休入口与权重索引边界；仅在显式D盘临时假库写文件。"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main(workdir):
    folder = Path(workdir).resolve()
    assert folder.is_relative_to(Path('D:/temp').resolve()) and 'codex' in str(folder)
    folder.mkdir(parents=True, exist_ok=False)
    retired = [f'pipeline/{m}/build_dataset.py' for m in ('m1', 'm2', 'm3')]
    retired += ['pipeline/m2/build_snapshot.py', 'pipeline/cnn/build_digit_dataset.py',
                'pipeline/cnn/build_digit_legacy.py', 'pipeline/cnn/build_choice_dataset.py',
                'tools/补多选区样本.py', 'tools/生成历史断言.py']
    for name in ('pipeline', 'tools'):
        shutil.copytree(ROOT / name, folder / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in retired:
        target = folder / name
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(target)],
                                cwd=folder, capture_output=True)
        assert result.returncode != 0 and '历史'.encode() in result.stderr, name
        assert b'ModuleNotFoundError' not in result.stderr, (name, result.stderr)
    assert not any((folder / d).exists() for d in ('data', 'models', 'train'))
    spec = importlib.util.spec_from_file_location('weight_index', ROOT / 'tools/权重索引.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = b'fake model bytes; never loaded'
    for name in ('models/m3/version.pt', 'models/m3/m3.pt', 'train/run/weights/best.pt'):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    deployed = {'m3': dict(path='models/m3/version.pt', sha256=digest)}
    (folder / 'models/current.json').write_text(json.dumps({'m3': {'alias': 'models/m3/m3.pt'}}))
    (folder / 'train/run/candidate.json').write_text(json.dumps(dict(
        path='models/m3/version.pt', sha256=digest, finite=True, task='m3')))
    index = module.build_index(folder, deployed)
    assert index['file_count'] == 3 and index['unique_sha_count'] == 1
    entries = {row['path']: row for row in index['entries']}
    assert entries['models/m3/m3.pt']['role'] == 'deployment_alias'
    assert entries['models/m3/version.pt']['role'] == 'deployed'
    assert entries['models/m3/version.pt']['source_records'][0]['recorded_sha_matches']
    assert len(entries['train/run/weights/best.pt']['identical_paths']) == 2
    assert '[models/m3/version.pt](../models/m3/version.pt)' in module.markdown(index)
    (folder / 'models/m3/version.pt').write_bytes(b'tampered')
    try:
        module.build_index(folder, deployed)
    except ValueError as error:
        assert 'SHA变化' in str(error)
    else:
        raise AssertionError('索引不得掩盖现用权重变化')
    print('9 original-position retired guards; weight roles, sources, duplicate hashes and deployment mutation: passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', required=True)
    main(parser.parse_args().workdir)

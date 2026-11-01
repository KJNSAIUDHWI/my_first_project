"""仅在D盘codex临时目录检查故障现场保存，不读写正式训练数据。"""
import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch
import torch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.numerical_trainer import GradGuardTrainer

ap=argparse.ArgumentParser();ap.add_argument('--workdir',type=Path,required=True);args=ap.parse_args()
work=args.workdir.resolve()
assert work.is_relative_to(Path('D:/temp').resolve()) and any('codex' in p for p in work.parts)
work.mkdir(exist_ok=False)
for fail_capture in [False,True]:
    obj=GradGuardTrainer.__new__(GradGuardTrainer);obj.save_dir=work/str(fail_capture);obj.save_dir.mkdir()
    obj.model=torch.nn.BatchNorm1d(2);obj.model.running_mean[0]=float('nan')
    obj.guard_step=1;obj.epoch=0;obj.guard_batch_files=['fixture.jpg'];obj.guard_batch=None
    def injected(*args,**kwargs):raise RuntimeError('capture test only')
    try:
        if fail_capture:
            with patch('torch.save',side_effect=injected):obj._fail('fixture.loss','NaN/Inf')
        else:obj._fail('fixture.loss','NaN/Inf')
    except FloatingPointError as error:assert 'fixture.loss' in str(error)
    else:raise AssertionError('guard must still stop')
    record=json.loads((obj.save_dir/'numerical_failure.jsonl').read_text('utf-8'))
    if fail_capture:assert 'capture test only' in record['model_state_capture_error']
    else:
        saved=torch.load(obj.save_dir/'failure_model_state.pt',map_location='cpu',weights_only=False)
        assert torch.isnan(saved['state_dict']['running_mean'][0]) and torch.isnan(obj.model.running_mean[0])
print('PASS: raw failed state preserved; capture failure still raises original numerical fault')

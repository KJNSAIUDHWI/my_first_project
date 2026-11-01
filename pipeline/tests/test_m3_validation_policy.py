"""验证名单登记、父页分区检查与M3方形验证输入。假数据只写显式工作目录。"""
import argparse,csv,json,hashlib,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import yolo_snapshot as snapshot
from common import numerical_trainer as trainer
import torch

def main(workdir):
    root=Path(workdir);root.mkdir(parents=True,exist_ok=False)
    file=root/'baseline.csv'
    file.write_text('new_name\nv1\nv2\n',encoding='utf-8')
    meta={'count':2,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
    file.with_suffix('.json').write_text(json.dumps(meta),encoding='utf-8')
    plan={'v1':{'split':'val'},'v2':{'split':'val'}}
    with patch.object(snapshot,'ROOT',root),patch.dict(snapshot.VALIDATION_BASELINES,m3=('baseline.csv',None)):
        assert snapshot.validation_names('m3',plan)=={'v1','v2'}
        file.write_text('new_name\nv1\n',encoding='utf-8')
        try:snapshot.validation_names('m3',plan)
        except ValueError as e:assert '指纹' in str(e)
        else:raise AssertionError('不能静默缩小验证名单')
        file.write_text('new_name\nv1\nv2\n',encoding='utf-8');plan['v2']['split']='train'
        try:snapshot.validation_names('m3',plan)
        except ValueError as e:assert '身份不一致' in str(e)
        else:raise AssertionError('训练身份不得进入val')
    model=torch.nn.Linear(1,1);model.stride=torch.tensor([32])
    fake=SimpleNamespace(model=model,save_dir=root,args=SimpleNamespace(rect=False),data={})
    for task,expected in [('m3',False),('m2',True)]:
        (root/'snapshot_audit.json').write_text(json.dumps({'task':task}),encoding='utf-8')
        with patch.object(trainer,'build_snapshot_dataset') as build:
            trainer.GradGuardTrainer.build_dataset(fake,'val_list.txt',mode='val')
            assert build.call_args.kwargs['rect'] is expected
    print('M3 validation metadata, split rejection, square validation; M2 unchanged: passed')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--workdir',required=True);main(parser.parse_args().workdir)

"""入口保护回归：所有业务测试在D:/temp的codex专属副本运行，不启动模型或服务。"""
import argparse
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import types
import unittest

PROJECT = Path(__file__).resolve().parents[2]
WORK = None


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class EntryBoundaries(unittest.TestCase):
    def setUp(self):
        self.root = WORK / self._testMethodName
        if self.root.exists():
            raise RuntimeError('测试目录已存在，请使用新的workdir：' + str(self.root))
        for folder in ('pipeline','tools'):
            shutil.copytree(PROJECT/folder, self.root/folder,
                            ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for name in list(sys.modules):
            if name == 'common' or name.startswith('common.'):
                del sys.modules[name]
        sys.path.insert(0, str(self.root/'pipeline'))
        from common.paths import ROOT
        self.assertEqual(ROOT, self.root)

    def detection(self):
        from PIL import Image
        from common.records import write_csv, sha, append
        from common import yolo_snapshot
        base = self.root/'data/m2'
        rows = []
        for name, split in [('t','train'),('v','val')]:
            p = base/'images'/(name+'.jpg');p.parent.mkdir(parents=True,exist_ok=True)
            Image.new('RGB',(40,40),(80,90,100)).save(p)
            append(base/'log'/(name+'.jsonl'), {'e':'review','boxes':[[2,.5,.5,.2,.2]],'image_sha256':sha(p)})
            rows.append(dict(new_name=name,split=split,stage=1,group=name,subject_original='物理'))
        write_csv(base/'splits.csv',rows,list(rows[0]))
        val = self.root/'train/base/dataset_snapshot/images/val';val.mkdir(parents=True)
        shutil.copy2(base/'images/v.jpg',val/'v.jpg')
        yolo_snapshot.VALIDATION_BASELINES={'m2':('base',1,hashlib.sha256(b'v').hexdigest())}
        return yolo_snapshot

    def test_legacy_cli_stops(self):
        env = {**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'}
        scripts = [f'pipeline/{m}/build_dataset.py' for m in ('m1','m2','m3','m4')]
        scripts += ['pipeline/m2/build_snapshot.py','tools/补多选区样本.py']
        for script in scripts:
            p = subprocess.run([sys.executable,'-B',str(self.root/script)],cwd=self.root,
                               env=env,capture_output=True,text=True,encoding='utf-8')
            self.assertNotEqual(p.returncode,0,script)
            self.assertIn('历史',p.stderr,script)
        self.assertFalse((self.root/'data').exists())
        self.assertFalse((self.root/'models').exists())
        self.assertFalse((self.root/'train').exists())

    def test_shared_legacy_functions_stop(self):
        from common.fixed_dataset import build_fixed
        from common.dataset_builder import build
        with self.assertRaisesRegex(RuntimeError,'已停用'):
            build_fixed('m2',{},[],1)
        with self.assertRaisesRegex(RuntimeError,'已停用'):
            build('m2',{},[],stage=1)
        self.assertFalse((self.root/'data').exists())

    def test_detection_snapshot_and_training_validation(self):
        y = self.detection();out=self.root/'train/new/dataset_snapshot'
        y.freeze('m2',None,out,stage=1)
        self.assertEqual(y.validate_training_input('m2',out.parent,out/'data.yaml'),out)
        with self.assertRaisesRegex(ValueError,'本run'):
            y.validate_training_input('m2',out.parent,self.root/'other.yaml')
        label=out/'labels/train/t.txt';label.write_text('1 .1 .1 .1 .1\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'标签已变化'):
            y.validate_training_input('m2',out.parent,out/'data.yaml')

    def test_detection_image_mutation_rejected(self):
        y=self.detection();out=self.root/'train/new/dataset_snapshot';y.freeze('m2',None,out,stage=1)
        p=out/'images/train/t.jpg';st=p.stat();raw=bytearray(p.read_bytes());raw[100]^=1;p.write_bytes(raw)
        os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns))
        with self.assertRaisesRegex(ValueError,'图片或标签已变化'):
            y.validate_training_input('m2',out.parent,out/'data.yaml')

    def test_detection_log_change_during_copy_rejected(self):
        y=self.detection();out=self.root/'train/new/dataset_snapshot';real=y.shutil.copy2
        def copy(src,dst):
            result=real(src,dst)
            if Path(src).stem=='t':
                from common.records import append
                append(self.root/'data/m2/log/t.jsonl',{'e':'unreview'})
            return result
        y.shutil.copy2=copy
        try:
            with self.assertRaisesRegex(ValueError,'审核日志'):
                y.freeze('m2',None,out,stage=1)
            self.assertFalse(out.exists());self.assertFalse((out.parent/'snapshot_audit.json').exists())
        finally:y.shutil.copy2=real

    def test_old_run_cannot_receive_a_new_snapshot(self):
        stub=types.ModuleType('common.run_training');stub.supervise=None;sys.modules['common.run_training']=stub
        m=load('old_run_iteration',self.root/'tools/迭代.py')
        run=self.root/'train/m2_old';run.mkdir(parents=True)
        launch=run/'launch.json';launch.write_text('original launch',encoding='utf-8')
        with self.assertRaisesRegex(FileExistsError,'已有训练现场'):
            m.cmd_snapshot(types.SimpleNamespace(task='m2',run='m2_old',check_only=False))
        self.assertEqual(launch.read_text(encoding='utf-8'),'original launch')
        self.assertFalse((run/'dataset_snapshot').exists())

    def test_passthrough_rejected_before_training(self):
        calls=[];stub=types.ModuleType('common.run_training');stub.supervise=lambda *a,**k:calls.append(a)
        sys.modules['common.run_training']=stub
        m=load('entry_iteration',self.root/'tools/迭代.py')
        args=types.SimpleNamespace(arg=['--data=unreviewed.yaml'])
        with self.assertRaisesRegex(ValueError,'--arg 已停用'):m.cmd_train(args)
        self.assertFalse(calls);self.assertFalse((self.root/'train').exists())

    def test_normal_training_command_uses_checked_snapshot(self):
        y=self.detection();out=self.root/'train/m2_new/dataset_snapshot';y.freeze('m2',None,out,stage=1)
        calls=[];stub=types.ModuleType('common.run_training')
        def supervise(*args,**kw):calls.append((args,kw));return {'probe_only':True}
        stub.supervise=supervise;sys.modules['common.run_training']=stub
        w=self.root/'models/m2/m2.pt';w.parent.mkdir(parents=True);w.write_bytes(b'FAKE, never loaded')
        m=load('normal_iteration',self.root/'tools/迭代.py')
        args=types.SimpleNamespace(arg=None,run='m2_new',task='m2',scratch=False,epochs=None,batch=None,lr=None)
        m.cmd_train(args)
        self.assertEqual(len(calls),1);command=calls[0][0][2]
        self.assertEqual(command[command.index('--data')+1],str(out/'data.yaml'))
        self.assertFalse(calls[0][1]['deploy'])

    def test_candidate_path_and_run_are_bounded(self):
        from common import model_registry as reg
        from common.records import write_json, sha
        model=self.root/'models/m2/candidate.pt';model.parent.mkdir(parents=True);model.write_bytes(b'fake model')
        outside=self.root/'data/other.pt';outside.parent.mkdir();outside.write_bytes(b'fake model')
        with self.assertRaisesRegex(ValueError,'models目录'):
            reg.model_record_path(str(outside))
        write_json(self.root/'models/current.json', {'m2':{'path':'data/other.pt','sha256':sha(outside)}})
        with self.assertRaisesRegex(ValueError,'models目录'):
            reg.current('m2')
        write_json(self.root/'models/current.json', {'m2':{'path':'models/m2/candidate.pt','sha256':sha(model)}})
        self.assertEqual(reg.current('m2'),model)
        stub=types.ModuleType('common.run_training');stub.supervise=None;sys.modules['common.run_training']=stub
        m=load('candidate_iteration',self.root/'tools/迭代.py')
        run=self.root/'train/m2_candidate';run.mkdir(parents=True)
        candidate={'task':'m2','path':'data/other.pt','sha256':sha(outside),'run':str(run)}
        write_json(run/'completion_check.json',{'success':True,'candidate':candidate})
        with self.assertRaisesRegex(ValueError,'models目录'):
            m.cmd_deploy(types.SimpleNamespace(run=run.name,task='m2'))
        candidate.update(path='models/m2/candidate.pt',run=str(self.root/'train/another'))
        write_json(run/'completion_check.json',{'success':True,'candidate':candidate})
        with self.assertRaisesRegex(ValueError,'来源run'):
            m.cmd_deploy(types.SimpleNamespace(run=run.name,task='m2'))
        self.assertIn('来源run',m._deploy_note(run,{'success':True},candidate,False))
        self.assertEqual(model.read_bytes(),b'fake model')

    def test_metrics_collision_rejected_before_model_import(self):
        target=self.root/'existing.json';target.write_text('original',encoding='utf-8')
        m=load('entry_eval',self.root/'pipeline/m1/eval.py');old=sys.argv
        sys.argv=['eval','--weights','unused','--data','unused','--results',str(self.root/'new_results'),'--json',str(target)]
        try:
            with self.assertRaisesRegex(SystemExit,'汇总JSON已存在'):m.main()
        finally:sys.argv=old
        self.assertEqual(target.read_text(encoding='utf-8'),'original')
        self.assertFalse((self.root/'new_results').exists())


def main():
    global WORK
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--workdir',type=Path,required=True)
    args=ap.parse_args();WORK=args.workdir.resolve();base=Path('D:/temp').resolve()
    guard = load('entry_scratch_guard', PROJECT/'tools/验证交接与准入.py')
    try:
        WORK = guard.validate_workdir(args.workdir)
    except ValueError as error:
        ap.error(str(error))
    if WORK==PROJECT or WORK.is_relative_to(PROJECT) or PROJECT.is_relative_to(WORK):
        ap.error('workdir不得与项目重叠')
    WORK.mkdir(parents=True,exist_ok=True)
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(EntryBoundaries)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1

if __name__=='__main__':
    sys.exit(main())

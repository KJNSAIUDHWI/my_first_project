"""Freeze explicitly selected reviewed YOLO data, preserving fixed splits."""
from pathlib import Path
from collections import Counter
import math
import shutil
import hashlib
from PIL import Image
from .paths import DATA, TRAIN, ROOT
from .fixed_dataset import read_plan
from . import events, fixed_dataset
from .records import read_csv, read_json, write_json, write_csv

MAPS = {'m1':{0:0,1:1,4:2},'m2':{2:0,3:1},'m3':{5:0,6:1}}
NAMES = {'m1':['info','choice','fill'],'m2':['num','option'],'m3':['block','number']}

# M3按已审val更新名单，配套元数据锁定本次数量与指纹；m1/m2沿用原基线。
VALIDATION_BASELINES = {'m1': ('m1_20261023_164732_全学科累计140页每科最终候选', 147), 'm2': ('m2_20261023_181744_全学科首轮120裁图微调', 115), 'm3': ('data/m3/validation_baseline.csv', None)}


def validation_names(task, plan):
    if task not in VALIDATION_BASELINES:
        raise ValueError('该模块尚未建立固定验证基线：' + task)
    run, count = VALIDATION_BASELINES[task]
    folder = ROOT / run if task == 'm3' else TRAIN / run / 'dataset_snapshot/images/val'
    if task == 'm3' and count is None:
        metadata = read_json(folder.with_suffix('.json'), {})
        count = metadata.get('count')
        if not isinstance(count, int) or count <= 0 or not folder.is_file():
            raise ValueError('M3验证名单缺少有效数量登记')
        if hashlib.sha256(folder.read_bytes()).hexdigest() != metadata.get('sha256'):
            raise ValueError('M3验证名单与登记指纹不一致')
    if task != 'm3' and not folder.is_dir():
        # 清理旧训练快照后仍使用完全相同的固定验证身份，绝不重抽。
        baseline = ROOT / 'data' / task / 'validation_baseline.json'
        metadata = read_json(baseline, {})
        names = metadata.get('names', [])
        digest = hashlib.sha256('\n'.join(names).encode()).hexdigest()
        if metadata.get('source_run') != run or metadata.get('task') != task or metadata.get('count') != count or metadata.get('names_sha256') != digest:
            raise ValueError('固定验证基线登记缺失或指纹不一致：' + str(baseline))
    else:
        names = [r['new_name'] for r in read_csv(folder)] if task == 'm3' else sorted(p.stem for p in folder.glob('*.jpg'))
    if len(names) != count or len(set(names)) != count:
        raise ValueError('固定验证基线缺失或名单变化：' + str(folder))
    if any(n not in plan or plan[n]['split'] != 'val' for n in names):
        raise ValueError('固定验证名单与splits身份不一致')
    return set(names)


def read_deferrals(path, plan, stage, task):
    deferred = {}
    for row in read_csv(path) if path else []:
        name = row['new_name']
        identity = plan.get(name)
        if name in deferred or not identity or identity['split'] != 'train' or int(row['stage']) != identity['stage']:
            raise ValueError('暂缓身份未知、重复或不一致：' + name)
        if stage is not None and identity['stage'] > stage:
            raise ValueError('暂缓名单超过本次stage：' + name)
        if not row.get('reason') or not row.get('decision'):
            raise ValueError('暂缓缺少用户决定或原因：' + name)
        st = events.load_state(task, name)
        if st['reviewed'] or st['special']:
            raise ValueError('暂缓项已审核或特殊，请更新名单：' + name)
        deferred[name] = row
    return deferred


def freeze(task, subset, output, decisions=None, check_only=False, stage=None, skip_invalid=False, defer_list=None):
    output = Path(output)
    if output.exists():
        raise FileExistsError('快照已存在：'+str(output))
    if not check_only and (output.parent/'snapshot_audit.json').exists():
        raise FileExistsError('快照审计已存在，拒绝覆盖；请另建run')
    fixed_dataset._PLAN_CACHE.pop(task, None)
    plan = read_plan(task)
    if not plan:
        raise ValueError('缺少固定splits.csv')
    if stage is None:
        raise ValueError('必须显式给 --stage；subset只约束本批审核，不能代替累计范围')
    val_names = validation_names(task, plan)
    deferred = read_deferrals(defer_list, plan, stage, task)
    requested = {r['new_name'] for r in read_csv(subset)} if subset else set()
    if task == 'm3' and not requested:
        raise ValueError('M3逐批迭代必须用--subset显式指定本轮已审名单')
    if requested-set(plan):
        raise ValueError('名单里有身份未知的图：' + ', '.join(sorted(requested-set(plan))[:5]))
    decisions = read_json(decisions,{}) if decisions else {}
    selected, excluded, clips, errors = [], [], [], []
    invalid = []
    skipped = Counter()
    # 累计训练必须已审或明确暂缓；验证名单不可随审核进度变化。
    for name, identity in plan.items():
        if identity['split'] not in ('train','val'):
            if name in requested:
                errors.append('请求包含预留或排除身份：' + name)
            continue
        if identity['split'] == 'val' and name not in val_names:
            skipped['reserved_val'] += 1
            if name in requested:
                errors.append('请求包含预留验证页：' + name)
            continue
        if identity['split'] == 'train' and identity['stage'] > stage:
            if name in requested:
                errors.append('请求包含超出 stage %d 的图：%s' % (stage, name))
            else:
                skipped['stage'] += 1
            continue
        if task == 'm3' and identity['split'] == 'train' and name not in requested:
            skipped['outside_review_batch'] += 1
            continue
        if name in deferred:
            excluded.append({'name': name, **deferred[name]})
            continue
        st = events.load_state(task,name)
        if not st['reviewed'] or st['special']:
            decision = decisions.get(name,{})
            valid = bool(decision.get('reason'))
            if identity['split'] == 'train' and st['special'] and valid and decision.get('action') == 'exclude':
                excluded.append({'name': name, **decision})
                continue
            errors.append('未审或特殊样本未明确排除：' + name)
            continue
        image = DATA/task/'images'/(name+'.jpg')
        if not image.exists():
            errors.append('图片缺失：%s' % name)
            continue
        try:
            with Image.open(image) as im:
                size = im.size
                im.verify()
            labels = []
            row_clips = []
            for i,box in enumerate(st['boxes']):
                if len(box)!=5 or not all(math.isfinite(float(v)) for v in box):
                    raise ValueError('非有限或损坏标签（第%d框）：%s' % (i, name))
                c,x,y,w,h = map(float,box)
                if c not in MAPS[task] or w<=0 or h<=0:
                    raise ValueError('非法类别或面积（类别 %g）：%s' % (c, name))
                x1,y1,x2,y2=x-w/2,y-h/2,x+w/2,y+h/2
                tx,ty=max(.01,2/size[0]),max(.01,2/size[1])
                if x1 < -tx or y1 < -ty or x2 > 1+tx or y2 > 1+ty:
                    raise ValueError('标签超出裁剪界限（第%d框）：%s' % (i, name))
                bounds = [max(0,x1),max(0,y1),min(1,x2),min(1,y2)]
                if bounds[2]<=bounds[0] or bounds[3]<=bounds[1]:
                    raise ValueError('裁剪后无面积（第%d框）：%s' % (i, name))
                if bounds != [x1,y1,x2,y2]: row_clips.append({'name':name,'index':i,'source':box,'export_bounds':bounds})
                a,b,c2,d=bounds
                labels.append([MAPS[task][int(c)],(a+c2)/2,(b+d)/2,c2-a,d-b])
        except (OSError, ValueError) as exc:
            if skip_invalid and identity['split'] == 'train':
                invalid.append({'name': name, 'error': str(exc)})
            else:
                errors.append(str(exc))
            continue
        clips.extend(row_clips)
        selected.append({'new_name':name,**identity,'labels':labels})
    if errors:
        raise ValueError('快照未执行，共 %d 项问题（按 00 规范修日志后重跑）：\n%s'
                         % (len(errors), '\n'.join(errors[:30])))
    counts=Counter(r['split'] for r in selected)
    if not counts['train'] or not counts['val']:
        raise ValueError('已审训练和固定验证均需非空')
    audit={'task':task,'counts':dict(counts),'requested':sorted(requested),
           'excluded':excluded,'clips':clips,'stage':stage,
           'validation_baseline':VALIDATION_BASELINES[task][0],
           'reserved_val':skipped['reserved_val'],
           'train_subject_counts':dict(Counter(r['subject_original'] for r in selected if r['split']=='train')),
           'skipped_unreviewed':skipped['unreviewed'],'skipped_stage':skipped['stage'],
           'skipped_invalid':invalid}
    if task == 'm3':
        audit['outside_review_batch'] = skipped['outside_review_batch']
    if check_only: return audit
    output.mkdir(parents=True, exist_ok=False)
    try:
        for split in ('train','val'):
            (output/'labels'/split).mkdir(parents=True)
        for r in selected:
            name,split=r['new_name'],r['split']
            text = ''.join('%d %.8f %.8f %.8f %.8f\n'%tuple(b) for b in r['labels'])
            (output/'labels'/split/(name+'.txt')).write_text(text,encoding='utf-8')
        for split in ('train','val'):
            names = [r['new_name'] for r in selected if r['split']==split]
            lines = [str((DATA/task/'images'/(n+'.jpg')).resolve()) for n in names]
            (output/(split+'_list.txt')).write_text('\n'.join(lines)+'\n',encoding='utf-8')
        fields=['new_name','split','stage','group','subject_original']
        write_csv(output/'selection.csv',[{k:r[k] for k in fields} for r in selected],fields)
        yaml = 'path: %s\ntrain: %s\nval: %s\nnames: %s\n' % (
            ROOT.resolve().as_posix(), (output/'train_list.txt').resolve().as_posix(),
            (output/'val_list.txt').resolve().as_posix(), str(NAMES[task]))
        (output/'data.yaml').write_text(yaml,encoding='utf-8')
        write_json(output.parent/'snapshot_audit.json',audit)
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return audit


def validate_training_input(task, run, data):
    """训练前核对本run的独立检测快照；只看名单、分区和文件在不在。"""
    run = Path(run).resolve()
    if run.parent != TRAIN.resolve():
        raise ValueError('run必须在train下一级')
    snapshot = run / 'dataset_snapshot'
    if Path(data).resolve() != snapshot / 'data.yaml':
        raise ValueError('训练数据必须是本run的dataset_snapshot/data.yaml')
    audit = read_json(run/'snapshot_audit.json', {})
    if audit.get('task') != task:
        raise ValueError('缺少当前任务快照审计；不能使用旧dataset绕过准入')
    rows = read_csv(snapshot/'selection.csv')
    seen, counts, groups = set(), Counter(), {}
    for row in rows:
        name, split = row['new_name'], row['split']
        if not name or Path(name).name != name or '/' in name or '\\' in name or split not in ('train','val'):
            raise ValueError('快照身份非法')
        if name in seen:
            raise ValueError('快照图片重复：' + name)
        seen.add(name); counts[split] += 1
        group = row.get('group') or name
        if groups.setdefault(group, split) != split:
            raise ValueError('快照卷组跨分区：' + group)
    if not counts['train'] or not counts['val'] or dict(counts) != audit.get('counts'):
        raise ValueError('快照数量与审计不符')
    for split in ('train','val'):
        lp = snapshot/(split+'_list.txt')
        if not lp.is_file():
            raise ValueError('缺少图片名单：' + str(lp))
        listed = [Path(x.strip()) for x in lp.read_text(encoding='utf-8').splitlines() if x.strip()]
        names = {r['new_name'] for r in rows if r['split'] == split}
        if len(listed) != len(names) or {p.stem for p in listed} != names:
            raise ValueError('名单与selection.csv不一致：' + str(lp))
        for p in listed:
            if not p.is_file():
                raise ValueError('训练图缺失：' + str(p))
            copy = snapshot/'labels'/split/(p.stem+'.txt')
            if not copy.is_file():
                raise ValueError('run内标签副本缺失：' + str(copy))
    return snapshot

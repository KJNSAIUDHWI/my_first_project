"""已审整页→M1→M2→题号/答案回归验收；只写新报告，不改标注和部署。"""
import argparse
import copy
import hashlib
import html
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from scipy.optimize import linear_sum_assignment

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import events, row_log
from common.cnn_data import data_bases, digit_image
from common.model_registry import current
from common.paths import ROOT, DATA
from common.records import read_csv, read_json, write_json, sha, now
from stages.choice import read_choice_zone

SUBJECTS = ['物理', '化学', '数学', '英语', '语文', '历史', '地理', '生物', '道德与法治']


def xyxy(box, width, height):
    _, x, y, w, h = box[:5]
    return [(x-w/2)*width, (y-h/2)*height, (x+w/2)*width, (y+h/2)*height]


def iou(a, b):
    area = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
    union = max(0, a[2]-a[0])*max(0, a[3]-a[1]) + max(0, b[2]-b[0])*max(0, b[3]-b[1])-area
    return area/union if union else 0.


def assign(gt, pred, threshold=.5):
    """全局一对一匹配；拒绝低重叠，不以题号/答案参与配对。"""
    if not gt or not pred:
        return []
    scores = np.array([[iou(a, b) for b in pred] for a in gt])
    # 先最大化达到阈值的匹配数，然后才最大化重叠。
    rows, cols = linear_sum_assignment(-(scores >= threshold).astype(float)*100 - scores)
    return [(int(i), int(j), float(scores[i, j])) for i, j in zip(rows, cols) if scores[i, j] >= threshold]


def detection(gt, pred):
    stats = {}
    for cls in sorted({b['cls'] for b in gt+pred}):
        a = [b['box'] for b in gt if b['cls'] == cls]
        b = [b['box'] for b in pred if b['cls'] == cls]
        hits = len(assign(a, b))
        stats[str(cls)] = {'truth': len(a), 'prediction': len(b), 'matched': hits,
                           'missed': len(a)-hits, 'extra': len(b)-hits}
    return stats


def load_image(path):
    img = cv2.imdecode(np.frombuffer(Path(path).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError('图片无法读取：'+str(path))
    return img


def indexes():
    choices, digits, explicit_digit = defaultdict(list), defaultdict(list), set()
    for task, target in [('choice', choices), ('digit', digits)]:
        for base, _ in data_bases(task):
            for r in read_csv(base/('manifest.csv' if task == 'choice' else 'labels.csv')):
                target[r['stem']].append((base, r))
                if task == 'digit' and r.get('box'):
                    explicit_digit.add(r['stem'].split('.选择题区')[0])
    return choices, digits, explicit_digit


def select_pages(count, seed, choices, explicit_digit):
    sampling = {r['new_name']: r for r in read_csv(DATA/'m1/sampling.csv')}
    plan = {r['new_name']: r for r in read_csv(DATA/'m1/splits.csv')}
    by_parent = defaultdict(dict)
    for z in read_csv(DATA/'m2/crop清单.csv'):
        if z['区域'] == '选择题区':
            by_parent[z['源图']][Path(z['文件名']).stem] = z
    pools = defaultdict(list)
    for parent, zones in by_parent.items():
        identity = plan.get(parent, {})
        subject = sampling.get(parent, {}).get('subject_original') or parent.split('.')[-1]
        if subject not in SUBJECTS or parent not in explicit_digit:
            continue
        st = events.load_state('m1', parent)
        if not st['reviewed'] or st['special'] or identity.get('split') not in ('train', 'val', 'test'):
            continue
        if not all(stem in choices and events.load_state('m2', stem)['reviewed']
                   and not events.load_state('m2', stem)['special'] for stem in zones):
            continue
        pools[subject].append({'name': parent, 'subject': subject, **identity,
                               'zones': [{'stem': stem, **z} for stem, z in zones.items()],
                               'm1_boxes': st['boxes'], 'm1_reviewed_at': st['reviewed_at'],
                               'layout': sampling.get(parent, {}).get('layout', 'legacy'),
                               'session': sampling.get(parent, {}).get('session', 'legacy')})
    selected = []
    for subject in SUBJECTS:
        # 验证优先；同卷组只抽一页，避免正反面/重复扫描占满名额。
        pool = sorted(pools[subject], key=lambda r: (r['split'] == 'train',
                      hashlib.sha256((str(seed)+r['name']).encode()).hexdigest()))
        used, chosen = set(), []
        for row in pool:
            if row['group'] in used:
                continue
            used.add(row['group'])
            chosen.append(row)
            if len(chosen) == count:
                break
        if len(chosen) != count:
            raise ValueError(f'{subject}不足{count}个可核对卷组，实际{len(chosen)}')
        selected.extend(chosen)
    return selected, {s: len(pools[s]) for s in SUBJECTS}


def locate_zone(page, zone, record):
    if all(record.get(k) for k in ('x1', 'y1', 'x2', 'y2')):
        x, y, x2, y2 = [int(record[k]) for k in ('x1', 'y1', 'x2', 'y2')]
        if (x2-x, y2-y) != (zone.shape[1], zone.shape[0]):
            raise ValueError('历史裁图坐标与尺寸不一致：'+record['stem'])
        return [x, y, x2, y2], {'basis': 'crop清单坐标'}
    # legacy清单无坐标：从原页定位实际存档裁图，不能用本轮预测框充当真值。
    gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
    templ = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
    scale = .4
    a = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    b = cv2.resize(templ, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    score = cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)
    _, peak, _, pos = cv2.minMaxLoc(score)
    x, y = [round(v/scale) for v in pos]
    x0, y0 = max(0, x-8), max(0, y-8)
    search = gray[y0:min(gray.shape[0], y+templ.shape[0]+9),
                  x0:min(gray.shape[1], x+templ.shape[1]+9)]
    score = cv2.matchTemplate(search, templ, cv2.TM_CCOEFF_NORMED)
    _, peak, _, pos = cv2.minMaxLoc(score)
    if peak < .9:
        raise ValueError(f'历史裁图定位无法可靠核实：{record["stem"]} score={peak:.3f}')
    x, y = x0+pos[0], y0+pos[1]
    return [x, y, x+zone.shape[1], y+zone.shape[0]], {'basis': '存档裁图模板定位', 'score': peak}


def reference_rows(stem, image, origin, boxes, choices, digits):
    height, width = image.shape[:2]
    nums = [b for b in boxes if b[0] == 2]
    opts = [b for b in boxes if b[0] == 3]
    nboxes = [xyxy(b, width, height) for b in nums]
    oboxes = [xyxy(b, width, height) for b in opts]
    number_candidates = []
    for base, r in digits.get(stem, []):
        if not r.get('box'):
            continue  # 不从旧文件序号、模型读数或旧CSV标签推断题号真值。
        state = row_log.digit_state(row_log.read(base, Path(r['file']).stem))
        if not state['reviewed'] or state['special'] or not state['label']:
            continue
        number_candidates.append({'box': xyxy([2, *json.loads(r['box'])], width, height),
                                  'label': state['label'], 'source': str(base/'log'/(Path(r['file']).stem+'.log'))})
    ntruth = {i: number_candidates[j] for i, j, _ in
              assign(nboxes, [r['box'] for r in number_candidates], .3)}
    answers = []
    for base, r in choices.get(stem, []):
        name = '%s_%03d' % (stem, int(r['row']))
        state = row_log.choice_state(row_log.read(base, name))
        if not state['reviewed'] or state['special']:
            continue
        bounds = xyxy([3, *json.loads(r['box'])], width, height) if r.get('box') else [float(r[k]) for k in ('x1', 'y1', 'x2', 'y2')]
        answers.append({'box': bounds, 'answer': state['answer'],
                         'source': str(base/'log'/(name+'.log'))})
    atruth = {i: answers[j] for i, j, _ in assign(oboxes, [r['box'] for r in answers])}
    rows = []
    for oi, opt in enumerate(oboxes):
        # 独立的人工框几何参照，不调用生产配对算法生成“正确答案”。
        candidates = []
        for ni, num in enumerate(nboxes):
            overlap = max(0, min(num[3], opt[3])-max(num[1], opt[1]))
            if overlap < .45*min(num[3]-num[1], opt[3]-opt[1]) or (num[0]+num[2])/2 > (opt[0]+opt[2])/2:
                continue
            cost = abs((num[1]+num[3]-opt[1]-opt[3])/2)/max(1, num[3]-num[1])*5 + max(0, opt[0]-num[2])/max(1, num[3]-num[1])
            candidates.append((cost, ni))
        candidates.sort()
        ni = candidates[0][1] if candidates and (len(candidates) == 1 or candidates[1][0]-candidates[0][0] > .2) else None
        number = ntruth.get(ni, {})
        row = {'zone': stem, 'option_index': oi, 'local_box': opt,
               'box': [opt[0]+origin[0], opt[1]+origin[1], opt[2]+origin[0], opt[3]+origin[1]],
               'answer': atruth.get(oi, {}).get('answer'), 'answer_log': atruth.get(oi, {}).get('source'),
               'no': number.get('label'), 'number_log': number.get('source'),
               'num_box': nboxes[ni] if ni is not None else None}
        rows.append(row)
    return rows


def infer_boxes(model, image, mapping, confidence, min_size):
    result = model(image, imgsz=1024, conf=confidence, iou=.5, verbose=False)[0]
    out = []
    for b in result.boxes:
        cls = int(b.cls[0])
        bounds = b.xyxy[0].tolist()
        if cls in mapping and bounds[2]-bounds[0] >= min_size and bounds[3]-bounds[1] >= min_size:
            out.append({'cls': mapping[cls], 'box': bounds, 'confidence': float(b.conf[0])})
    return sorted(out, key=lambda b: (b['box'][1], b['box'][0]))


def normalized(boxes, width, height):
    return [[b['cls'], (b['box'][0]+b['box'][2])/2/width,
             (b['box'][1]+b['box'][3])/2/height,
             (b['box'][2]-b['box'][0])/width,
             (b['box'][3]-b['box'][1])/height] for b in boxes]


def row_global(row, bounds):
    x, y, right, bottom = bounds
    a = xyxy([3, *row['opt_box']], right-x, bottom-y)
    return {**row, 'box': [a[0]+x, a[1]+y, a[2]+x, a[3]+y]}


def score_rows(truth, predictions):
    pairs = assign([r['box'] for r in truth], [r['box'] for r in predictions])
    matched = {i: predictions[j] for i, j, _ in pairs}
    totals = Counter()
    for i, row in enumerate(truth):
        pred = matched.get(i)
        row['prediction'] = pred
        totals['rows'] += 1
        totals['matched_rows'] += int(pred is not None)
        if row['answer'] is not None:
            totals['answer_known'] += 1
            totals['answer_correct'] += int(pred is not None and pred['answer'] == row['answer'])
        if row['no'] is not None:
            totals['number_known'] += 1
            totals['number_raw_correct'] += int(pred is not None and pred['no_raw'] == row['no'])
            totals['number_correct'] += int(pred is not None and pred['no'] == row['no'])
        if row['answer'] is not None and row['no'] is not None:
            totals['joint_known'] += 1
            ok = pred is not None and pred['answer'] == row['answer'] and pred['no'] == row['no']
            totals['joint_correct'] += int(ok)
            totals['usable_correct'] += int(ok and pred['valid'])
    totals['extra_rows'] = len(predictions)-len(pairs)
    return dict(totals)


def render_assets(out, pi, page, gt1, pred1, zones, rows):
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 19)
    small = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 16)
    im = Image.fromarray(cv2.cvtColor(page, cv2.COLOR_BGR2RGB))
    scale = min(1, 1100/im.width)
    overlay = im.resize((round(im.width*scale), round(im.height*scale)))
    draw = ImageDraw.Draw(overlay)
    for b in gt1:
        draw.rectangle([v*scale for v in b['box']], outline='#16a34a', width=2)
    for b in pred1:
        draw.rectangle([v*scale for v in b['box']], outline='#a855f7', width=3)
        label = {0: '姓名学号', 1: '选择题区', 4: '填空题区'}[b['cls']]
        draw.text((b['box'][0]*scale, b['box'][1]*scale), label, font=font, fill='#7e22ce', stroke_width=1, stroke_fill='white')
    prefix = f'p{pi:03d}'
    overlay.save(out/'images'/(prefix+'_page.jpg'), quality=88)
    thumb = ImageOps.contain(overlay, (270, 360))
    thumb.save(out/'images'/(prefix+'_thumb.jpg'), quality=82)
    zone_assets = []
    for zi, z in enumerate(zones):
        image = Image.fromarray(cv2.cvtColor(z['image'], cv2.COLOR_BGR2RGB))
        sc = min(1, 1500/image.width)
        image = image.resize((round(image.width*sc), round(image.height*sc)))
        zd = ImageDraw.Draw(image)
        for b in z['truth']:
            a = [(b['box'][0]-z['bounds'][0])*sc, (b['box'][1]-z['bounds'][1])*sc,
                 (b['box'][2]-z['bounds'][0])*sc, (b['box'][3]-z['bounds'][1])*sc]
            zd.rectangle(a, outline='#16a34a', width=2)
        for b in z['predicted']:
            zd.rectangle([v*sc for v in b['box']], outline='#a855f7' if b['cls']==3 else '#f97316', width=2)
        file = prefix+f'_zone{zi:02d}.jpg'
        image.save(out/'images'/file, quality=91)
        zone_assets.append('images/'+file)
    cards = []
    zone_lookup = {z['stem']: z for z in zones if z.get('stem')}
    for index, row in enumerate(rows):
        # 行图来自存档人工参照裁图；即使新链路漏检也能显示遗漏内容。
        source = load_image(DATA/'m2/images'/(row['zone']+'.jpg'))
        bounds = row['local_box']
        if row['num_box']:
            bounds = [min(bounds[0],row['num_box'][0]), min(bounds[1],row['num_box'][1]),
                      max(bounds[2],row['num_box'][2]), max(bounds[3],row['num_box'][3])]
        x1,y1,x2,y2 = [int(v) for v in bounds]
        strip = source[max(0,y1-3):min(source.shape[0],y2+3), max(0,x1-3):min(source.shape[1],x2+3)]
        strip = Image.fromarray(cv2.cvtColor(strip, cv2.COLOR_BGR2RGB))
        strip = ImageOps.contain(strip, (446, 65))
        candidate = row.get('candidate'); deployed = row.get('deployed')
        truth = f"{row['no'] if row['no'] is not None else '?'} / {row['answer'] if row['answer'] is not None else '?'}"
        ok = candidate is not None and row['answer'] is not None and candidate['answer']==row['answer'] and (row['no'] is None or candidate['no']==row['no'])
        card = Image.new('RGB',(470,157),'#ecfdf5' if ok else '#fff1f2')
        d = ImageDraw.Draw(card)
        d.text((8,3),f'#{index+1} 人工 {truth or "空白"}',font=font,fill='#111827')
        card.paste(strip,(8,31))
        def label(p):
            return '未配出' if p is None else f"{p['no_raw']}→{p['no']} / {p['answer'] or '空'}"+(' [待核对]' if not p['valid'] else '')
        d.text((8,99),'当前 '+label(deployed),font=small,fill='#475569')
        d.text((8,126),'候选 '+label(candidate),font=small,fill='#166534' if ok else '#b91c1c')
        cards.append(card)
    sheets = []
    for start in range(0,len(cards),24):
        chunk = cards[start:start+24]
        board = Image.new('RGB',(1410,157*math.ceil(len(chunk)/3)), '#e2e8f0')
        for j,card in enumerate(chunk): board.paste(card,((j%3)*470,(j//3)*157))
        name=prefix+f'_rows{start//24:02d}.jpg'; board.save(out/'images'/name,quality=91)
        sheets.append('images/'+name)
    return {'page':'images/'+prefix+'_page.jpg','thumb':'images/'+prefix+'_thumb.jpg',
            'zones':zone_assets,'rows':sheets}


def total_counter(records, key):
    result=Counter()
    for r in records: result.update(r[key])
    return dict(result)


def summarize(pages):
    result={}
    for subject in ['全部']+SUBJECTS:
        rows=pages if subject=='全部' else [p for p in pages if p['subject']==subject]
        dets={}
        for stage in ('m1','m2'):
            classes=defaultdict(Counter)
            for p in rows:
                for cls,st in p[stage].items(): classes[cls].update(st)
            dets[stage]={k:dict(v) for k,v in classes.items()}
        result[subject]={'pages':len(rows),'page_splits':dict(Counter(p['split'] for p in rows)),
                         'deployed':total_counter(rows,'deployed'),'candidate':total_counter(rows,'candidate'),**dets}
    return result


def make_findings(out, data):
    """保留真实候选输入的错误图集；不改人工答案或生产纠错规则。"""
    from stages.choice import _box_px, crop_option_row
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 18)
    small = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 16)
    issues, categories, cards = [], Counter(), []
    unknown = 0
    for page in data['pages']:
        original = None
        for ri, row in enumerate(page['truth_rows']):
            pred = row['candidate']
            if row['no'] is None or row['answer'] is None:
                unknown += 1
            tags = []
            if pred is None:
                tags.append('未配出答题行')
            else:
                if row['answer'] is not None and pred['answer'] != row['answer']:
                    tags.append('答案不符')
                if row['no'] is not None and pred['no'] != row['no']:
                    tags.append('题号纠正误改' if pred['no_raw'] == row['no'] else '题号读数不符')
            if not tags:
                continue
            categories.update(tags)
            issue = {'page_index': page['index'], 'name': page['name'], 'subject': page['subject'],
                     'row_index': ri, 'truth': {'no': row['no'], 'answer': row['answer']},
                     'candidate': pred, 'tags': tags, 'answer_log': row['answer_log'],
                     'number_log': row['number_log']}
            issues.append(issue)
            card = Image.new('RGB', (900, 225), '#fff1f2')
            draw = ImageDraw.Draw(card)
            draw.text((10, 5), f"{len(issues):02d} {page['subject']} {page['name']} / 行{ri+1}", font=font, fill='#111827')
            expected = f"人工 {row['no'] if row['no'] is not None else '?'} / {row['answer'] or '空'}"
            actual = '候选：未配出' if pred is None else f"候选 {pred['no_raw']}→{pred['no']} / {pred['answer'] or '空'}"
            draw.text((10, 34), expected+'    '+actual, font=font, fill='#b91c1c')
            draw.text((10, 62), '、'.join(tags), font=small, fill='#b91c1c')
            draw.text((10, 88), '人工参照行（含题号）', font=small, fill='#475569')
            draw.text((455, 88), '候选实际收到的涂黑图片', font=small, fill='#475569')
            reference = load_image(DATA/'m2/images'/(row['zone']+'.jpg'))
            a = list(row['local_box'])
            if row['num_box']:
                b = row['num_box']; a = [min(a[0],b[0]),min(a[1],b[1]),max(a[2],b[2]),max(a[3],b[3])]
            strip = reference[max(0,int(a[1])-2):min(reference.shape[0],math.ceil(a[3])+2),
                              max(0,int(a[0])-2):min(reference.shape[1],math.ceil(a[2])+2)]
            left = ImageOps.contain(Image.fromarray(cv2.cvtColor(strip,cv2.COLOR_BGR2RGB)),(430,85))
            card.paste(left,(10,116))
            if pred is not None:
                if original is None: original = load_image(DATA/'m1/images'/(page['name']+'.jpg'))
                # 从全页框与区内归一化框还原当次M1裁区，避免用人工裁图替代真实输入。
                ob=pred['opt_box']; gb=pred['box']
                width=round((gb[2]-gb[0])/ob[2]); height=round((gb[3]-gb[1])/ob[3])
                x=round((gb[0]+gb[2])/2-ob[0]*width); y=round((gb[1]+gb[3])/2-ob[1]*height)
                zone=original[y:y+height,x:x+width]
                nb=pred['num_box']; paired=(nb[0]*width,nb[1]*height,nb[2]*width,nb[3]*height)
                crop=crop_option_row(zone,[],*_box_px(*ob,width,height),paired=paired)
                if crop.size:
                    right=ImageOps.contain(Image.fromarray(cv2.cvtColor(crop,cv2.COLOR_BGR2RGB)),(430,85))
                    card.paste(right,(455,116))
            else:
                draw.text((455,136),'该行未进入答案识别',font=font,fill='#b91c1c')
            cards.append(card)
    for start in range(0,len(cards),10):
        chunk=cards[start:start+10]; board=Image.new('RGB',(1800,225*math.ceil(len(chunk)/2)),'#e2e8f0')
        for i,card in enumerate(chunk):board.paste(card,((i%2)*900,(i//2)*225))
        board.save(out/'images'/f'failures_{start//10:02d}.jpg',quality=94)
    report={'row_cases':len(issues),'category_counts':dict(categories),'unknown_reference_rows':unknown,
            'note':'同一行可有多个问题类别；答案不符可能来自裁切或CNN，不能只凭输出断言根因。',
            'cases':issues,'images':[f'images/failures_{i:02d}.jpg' for i in range(math.ceil(len(cards)/10))]}
    # 已知题号+答案的错误必须与同一份逐行指标吻合。
    known_errors=sum(i['truth']['no'] is not None and i['truth']['answer'] is not None for i in issues)
    c=data['summary']['全部']['candidate']
    assert known_errors == c.get('joint_known',0)-c.get('joint_correct',0)
    write_json(out/'findings.json',report)
    return report


def write_report(out, data):
    from chain_report import write_report as render_report
    render_report(out, data)


def main():
    ap=argparse.ArgumentParser(__doc__)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--per-subject',type=int,default=15)
    ap.add_argument('--seed',type=int,default=20261027)
    ap.add_argument('--candidate-digit',type=Path,required=True)
    ap.add_argument('--candidate-choice',type=Path,required=True)
    ap.add_argument('--english-choice',type=Path,required=True)
    args=ap.parse_args(); out=args.output.resolve()
    if out.exists(): raise FileExistsError(out)
    if args.per_subject<1: raise ValueError('每科抽样数量必须为正')
    choices,digits,explicit=indexes(); selection,pools=select_pages(args.per_subject,args.seed,choices,explicit)
    models={k:{'path':str(current(k)), 'sha256':sha(current(k))} for k in ('m1','m2','digit','choice')}
    for label,path in [('candidate_digit',args.candidate_digit),('candidate_choice',args.candidate_choice),('english_choice',args.english_choice)]:
        models[label]={'path':str(path.resolve()),'sha256':sha(path)}
    out.mkdir(parents=True); (out/'images').mkdir(); (out/'pages').mkdir()
    write_json(out/'selection.json',{'seed':args.seed,'per_subject':args.per_subject,'pool_counts':pools,'pages':selection})
    write_json(out/'status.json',{'phase':'starting','total':len(selection),'models':models,'time':now()})
    import torch
    torch.set_num_threads(4); cv2.setNumThreads(0)
    from ultralytics import YOLO
    from cnn.digit import DigitReader
    from cnn.choice import ChoiceReader
    m1=YOLO(models['m1']['path']); m2=YOLO(models['m2']['path'])
    readers={'deployed':(DigitReader(models['digit']['path']),ChoiceReader(models['choice']['path'])),
             'candidate':(DigitReader(args.candidate_digit),ChoiceReader(args.candidate_choice))}
    en=ChoiceReader(args.english_choice)
    pages=[]
    for pi,entry in enumerate(selection):
        page=load_image(DATA/'m1/images'/(entry['name']+'.jpg')); height,width=page.shape[:2]
        gt1=[{'cls':b[0],'box':xyxy(b,width,height)} for b in entry['m1_boxes']]
        pred1=infer_boxes(m1,page,{0:0,1:1,2:4},.2,16)
        truth_rows=[]; gt2=[]; reference_notes=[]
        for record in entry['zones']:
            stem=record['stem']; zone=load_image(DATA/'m2/images'/(stem+'.jpg'))
            origin,location=locate_zone(page,zone,record)
            if location.get('score'): reference_notes.append(stem+f' 存档裁图区定位分数{location["score"]:.4f}')
            st=events.load_state('m2',stem)
            truth_rows.extend(reference_rows(stem,zone,origin,st['boxes'],choices,digits))
            for b in st['boxes']:
                bounds=xyxy(b,zone.shape[1],zone.shape[0]); bounds=[bounds[0]+origin[0],bounds[1]+origin[1],bounds[2]+origin[0],bounds[3]+origin[1]]
                gt2.append({'cls':b[0],'box':bounds,'stem':stem})
        predicted_rows={'deployed':[],'candidate':[]}; pred2=[]; zones=[]
        for area in [b for b in pred1 if b['cls']==1]:
            x1,y1,x2,y2=area['box']; bounds=[max(0,int(x1)),max(0,int(y1)),min(width,math.ceil(x2)),min(height,math.ceil(y2))]
            x1,y1,x2,y2=bounds; image=page[y1:y2,x1:x2]
            boxes=infer_boxes(m2,image,{0:2,1:3},.5,6)
            for b in boxes:
                pred2.append({**b,'box':[b['box'][0]+x1,b['box'][1]+y1,b['box'][2]+x1,b['box'][3]+y1]})
            for stack,(dr,cr) in readers.items():
                if stack=='candidate' and entry['subject']=='英语': cr=en
                rows=read_choice_zone(image,normalized(boxes,image.shape[1],image.shape[0]),dr,cr,is_en=entry['subject']=='英语')
                predicted_rows[stack].extend(row_global(row,bounds) for row in rows)
            relevant=[b for b in gt2 if iou(b['box'],bounds)>0 or (bounds[0]<b['box'][0]<bounds[2] and bounds[1]<b['box'][1]<bounds[3])]
            zones.append({'image':image,'bounds':bounds,'predicted':boxes,'truth':relevant})
        item={'index':pi,'name':entry['name'],'subject':entry['subject'],'split':entry['split'],
              'group':entry['group'],'layout':entry['layout'],'reference_notes':reference_notes,
              'm1':detection(gt1,pred1),'m2':detection(gt2,pred2),'predictions':predicted_rows}
        combined=copy.deepcopy(truth_rows)
        for stack in readers:
            truth=copy.deepcopy(truth_rows); item[stack]=score_rows(truth,predicted_rows[stack])
            for row,scored in zip(combined,truth): row[stack]=scored['prediction']
        item['truth_rows']=combined
        item['assets']=render_assets(out,pi,page,gt1,pred1,zones,combined)
        pages.append(item)
        write_json(out/'status.json',{'phase':'running','completed':len(pages),'total':len(selection),'last':entry['name'],'time':now()})
        write_json(out/'pages'/f'p{pi:03d}.json',item)
        print(f'{pi+1}/{len(selection)} {entry["subject"]} {entry["name"]} 当前{item["deployed"].get("joint_correct",0)}/{item["deployed"].get("joint_known",0)} 候选{item["candidate"].get("joint_correct",0)}/{item["candidate"].get("joint_known",0)}',flush=True)
    summary=summarize(pages); all_=summary['全部']; c=all_['candidate']
    reasons=[]
    if c.get('joint_known',0)<c['rows']: reasons.append('部分答题行缺少可可靠对应的人工题号或答案真值')
    if c.get('joint_correct',0)<c.get('joint_known',0): reasons.append('候选存在题号或答案错误/漏配')
    if c.get('extra_rows',0): reasons.append('候选存在额外配出的答题行')
    if c.get('usable_correct',0)<c.get('joint_correct',0): reasons.append('部分正确读数被有效性阈值拒绝，仍需人工核对')
    for stage in ('m1','m2'):
        if any(r['missed'] or r['extra'] for r in all_[stage].values()): reasons.append(stage.upper()+'存在漏框或多框')
    result={'created':now(),'models':models,'pages':pages,'summary':summary,'gate':{'pass':not reasons,'reasons':reasons},
            'limitations':['本轮是已审素材回归验收，多数学科来自训练集；不能当作独立泛化测试。',
                           '从已转正的M1工作页开始，不包含原始扫描方向转正的独立验收。',
                           '人工答案来自审核日志；旧manifest题号和n_opts不当真值，题号必须有已审小图及位置对应。',
                           '未知人工真值不计正确；真值已知但新链路漏检/漏配计错误。检测使用IoU≥0.5，框命中不代表裁切内容完全正确。',
                           '候选只离线对照，没有替换部署。M3现有2037张图、无标注/审核记录，尚不能重训。']}
    result['findings']=make_findings(out,result)
    write_json(out/'results.json',result); write_report(out,result)
    text=['# 全学科整页到选择题识别回归验收', '',f'时间：{result["created"]}；共{len(pages)}页，每原始学科{args.per_subject}页。',
          '', '结论：'+('满足本轮严格核对门槛' if not reasons else '未通过，'+ '；'.join(reasons)), '',
          '| 学科 | 页数 | 当前答案 | 候选答案 | 候选题号 | 候选题号+答案 |', '|---|---:|---:|---:|---:|---:|']
    for subject,r in summary.items():
        d=r['deployed']; cc=r['candidate']; text.append(f'| {subject} | {r["pages"]} | {d.get("answer_correct",0)}/{d.get("answer_known",0)} | {cc.get("answer_correct",0)}/{cc.get("answer_known",0)} | {cc.get("number_correct",0)}/{cc.get("number_known",0)} | {cc.get("joint_correct",0)}/{cc.get("joint_known",0)} |')
    text+=['','## 范围与限制','']+['- '+x for x in result['limitations']]
    (out/'summary.md').write_text('\n'.join(text)+'\n',encoding='utf-8')
    write_json(out/'completion.json',{'time':now(),'success':True,'pages':len(pages),'gate':result['gate'],'report':'index.html'})
    write_json(out/'status.json',{'phase':'complete','completed':len(pages),'total':len(selection),'time':now()})
    print('DONE',out,flush=True)


if __name__=='__main__':
    main()

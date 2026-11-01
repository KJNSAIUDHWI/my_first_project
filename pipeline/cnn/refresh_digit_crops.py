"""用当前M2重新裁未审数字批次，独立保存、不改原M2人工框与旧小图。"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
import sys
import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment
from ultralytics import YOLO

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import row_log
from common.paths import ROOT,DATA
from common.records import read_csv,read_json,write_csv,write_json,now,lock
from common.model_registry import current
from cnn.prepare_increment import save_png
from stages.choice import _crop


def match_boxes(old, new):
    if not old or not new:return {}
    scores=np.zeros((len(old),len(new)))
    for i,a in enumerate(old):
        for j,b in enumerate(new):
            left=max(a[0]-a[2]/2,b[0]-b[2]/2);right=min(a[0]+a[2]/2,b[0]+b[2]/2)
            top=max(a[1]-a[3]/2,b[1]-b[3]/2);bottom=min(a[1]+a[3]/2,b[1]+b[3]/2)
            intersection=max(0,right-left)*max(0,bottom-top)
            scores[i,j]=intersection/(a[2]*a[3]+b[2]*b[3]-intersection)
    ii,jj=linear_sum_assignment(-scores)
    return {int(i):int(j) for i,j in zip(ii,jj) if scores[i,j]>=.3}


def ensure_unreviewed(base, rows):
    with ThreadPoolExecutor(max_workers=12) as pool:
        states=pool.map(lambda r:row_log.digit_state(row_log.read(base,Path(r['file']).stem)),rows)
        for r,st in zip(rows,states):
            if st['reviewed']:raise ValueError('批次已有人工审核，停止替换：'+r['file'])


def refresh(base,batch):
    base=Path(base).resolve()
    if not base.is_relative_to((DATA/'digit/increments').resolve()):raise ValueError('只允许重裁增量批次')
    if Path(batch).name!=batch or '/' in batch or '\\' in batch:raise ValueError('非法批次名')
    output=DATA/'digit/increments'/batch
    if output.exists():raise FileExistsError(output)
    meta=read_json(base/'batch.json');rows=read_csv(base/'labels.csv')
    if not meta['complete'] or meta.get('enabled') is False:raise ValueError('来源批次不可用')
    ensure_unreviewed(base,rows)
    weight=current('m2');model=YOLO(str(weight))
    (output/'images').mkdir(parents=True)
    by={}
    for r in rows:by.setdefault(r['stem'],[]).append(r)
    result=[];unmatched=[];sources=[]
    for count,source in enumerate(meta['sources'],1):
        stem=source['stem'];old=[b[1:] for b in source['boxes'] if b[0]==2]
        img=cv2.imdecode(np.frombuffer(Path(source['image']).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
        if img is None:raise ValueError('无法读来源图：'+stem)
        prediction=model(img,conf=.5,iou=.5,imgsz=1024,verbose=False)[0]
        boxes=[b.xywhn[0].tolist() for b in prediction.boxes if int(b.cls[0])==0]
        matching=match_boxes(old,boxes)
        for r in by[stem]:
            i=int(Path(r['file']).stem.rsplit('_',1)[1])-1
            box=boxes[matching[i]] if i in matching else old[i]
            crop=_crop(img,*box,pad=0)
            if not crop.size:raise ValueError('裁图为空：'+r['file'])
            name='%s_m2new01_%03d'%(stem,i+1)
            save_png(output/'images'/(name+'.png'),crop)
            result.append({**r,'file':name+'.png','old_file':r['file'],'box':json.dumps(box),
                           'recrop_matched':i in matching})
            row_log.append(output,name,{'事件':'预标注','标签':int(r['label']),'模型':'旧CNN建议（待MiMo重标）',
                '来源图':stem,'来源框':box,'裁图模型':str(weight.relative_to(ROOT)),
                '裁图异常':i not in matching,'说明':'当前M2框无外扩重裁；保留旧框兜底的样本待核查；仍未人工审核'})
            if i not in matching:unmatched.append(name+'.png')
        sources.append({**source,'recrop_boxes':boxes,'old_to_new':matching})
        if count%50==0:print('重裁 %d/%d，小图%d，待核查%d'%(count,len(meta['sources']),len(result),len(unmatched)),flush=True)
    ensure_unreviewed(base,rows)
    write_csv(output/'labels.csv',result,list(result[0]))
    write_json(output/'batch.json',{**meta,'batch':batch,'created':now(),'enabled':True,'samples':len(result),
        'sources':sources,'recrop_model':str(weight.relative_to(ROOT)),'pad':0,'unmatched':unmatched,
        'replaces':str(base.relative_to(ROOT)),'prelabel_status':'等待MiMo预标注'})
    # 原图与日志原样保留；只退休未审旧批次，防止同来源新旧图同时参加训练。
    write_json(base/'batch.json',{**meta,'enabled':False,'superseded_by':str(output.relative_to(ROOT)),
        'superseded_at':now(),'superseded_reason':'用户指出裁图与审核负担，改当前M2无外扩重裁和MiMo预标注'})
    print('重裁完成：%d张，未匹配%d'%(len(result),len(unmatched)),flush=True)
    return output


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data',required=True);ap.add_argument('--batch',required=True)
    args=ap.parse_args()
    with lock(DATA/'.cnn_increment.lock'):refresh(args.data,args.batch)


if __name__=='__main__':main()

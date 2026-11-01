"""从已保存的链路结果生成逐题对照界面；不重算指标、不写审核。"""
import copy
import json
import math
from pathlib import Path

import cv2
from PIL import Image, ImageOps

from common.paths import DATA
from stages.choice import crop_option_row, _box_px


def prediction_geometry(p):
    """恢复当次裁区与题号框，不以人工裁区替代模型输入。"""
    ob, gb = p['opt_box'], p['box']
    w = round((gb[2]-gb[0])/ob[2]); h = round((gb[3]-gb[1])/ob[3])
    x = round((gb[0]+gb[2])/2-ob[0]*w); y = round((gb[1]+gb[3])/2-ob[1]*h)
    cx, cy, bw, bh = p['num_box']
    return (x, y, w, h), [x+(cx-bw/2)*w, y+(cy-bh/2)*h,
                            x+(cx+bw/2)*w, y+(cy+bh/2)*h]


def write_report(out, data):
    from eval_chain import load_image
    out = Path(out); dest = out/'reader'; dest.mkdir(exist_ok=True)
    display = copy.deepcopy(data)
    def save(img, name, maximum):
        target = dest/name
        if not target.exists():
            im = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            ImageOps.contain(im, maximum).save(target, quality=90)
        return 'reader/'+name
    for p in display['pages']:
        image = load_image(DATA/'m1/images'/(p['name']+'.jpg'))
        h,w = image.shape[:2]; prefix=f'p{p["index"]:03d}'
        p['view']={'raw':save(image,prefix+'.jpg',(1800,2600)), 'width':w,'height':h}
        sources={}
        for ri,r in enumerate(p['truth_rows']):
            if r['zone'] not in sources: sources[r['zone']]=load_image(DATA/'m2/images'/(r['zone']+'.jpg'))
            ref=sources[r['zone']]; b=list(r['local_box']); nb=r['num_box']
            origin=[r['box'][0]-b[0],r['box'][1]-b[1]]
            r['view']={'num_global':None if not nb else [nb[0]+origin[0],nb[1]+origin[1],nb[2]+origin[0],nb[3]+origin[1]]}
            if nb:b=[min(b[0],nb[0]),min(b[1],nb[1]),max(b[2],nb[2]),max(b[3],nb[3])]
            strip=ref[max(0,int(b[1])-3):min(ref.shape[0],math.ceil(b[3])+3),max(0,int(b[0])-3):min(ref.shape[1],math.ceil(b[2])+3)]
            r['view']['reference']=save(strip,f'{prefix}_r{ri:03d}.jpg',(700,100))
            for k in ('deployed','candidate'):
                pred=r[k]
                if pred is None:continue
                (x,y,zw,zh), nbox=prediction_geometry(pred);pred['num_global']=nbox
                zone=image[y:y+zh,x:x+zw]; n=pred['num_box']
                paired=(n[0]*zw,n[1]*zh,n[2]*zw,n[3]*zh)
                crop=crop_option_row(zone,[],*_box_px(*pred['opt_box'],zw,zh),paired=paired)
                if crop.size:r['view'][k]=save(crop,f'{prefix}_r{ri:03d}_{k}.jpg',(700,100))
        for k in ('deployed','candidate'):
            for pred in p['predictions'][k]:pred['num_global']=prediction_geometry(pred)[1]
    payload=json.dumps(display,ensure_ascii=False).replace('</','<\\/')
    template=Path(__file__).with_suffix('.html').read_text(encoding='utf-8')
    (out/'index.html').write_text(template.replace('__DATA__',payload),encoding='utf-8')


if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser(__doc__);ap.add_argument('report',type=Path);args=ap.parse_args()
    write_report(args.report,json.loads((args.report/'results.json').read_text(encoding='utf-8')))

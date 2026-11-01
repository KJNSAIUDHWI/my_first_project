"""MiMo数字预标注：编号拼图逐批请求，只写建议；人工审核状态保持未审。"""
import argparse
import base64
import io
import json
import os
from pathlib import Path
import sys
import urllib.request
import urllib.error

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import row_log
from common.cnn_data import digit_image
from common.digit_classes import MAX_DIGIT
from common.records import read_csv, write_json


def sheet(base, rows):
    canvas=Image.new('RGB',(1200,((len(rows)+5)//6)*130),'#e8e8e8')
    draw=ImageDraw.Draw(canvas)
    font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',20)
    for i,r in enumerate(rows):
        x,y=(i%6)*200,(i//6)*130
        draw.rectangle((x+3,y+3,x+196,y+126),fill='white')
        draw.text((x+8,y+6),str(i+1),font=font,fill='blue')
        with Image.open(digit_image(base,r)) as im:
            im=im.convert('RGB')
            scale=min(170/im.width,75/im.height)
            im=im.resize((max(1,round(im.width*scale)),max(1,round(im.height*scale))))
            canvas.paste(im,(x+(200-im.width)//2,y+42))
    return canvas


def parse_reply(text, count):
    text=text.strip()
    if text.startswith('```'):
        text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
    rows=json.loads(text)
    if not isinstance(rows,list) or len(rows)!=count:
        raise ValueError('MiMo返回条数不符')
    by={}
    for r in rows:
        ident,label=r.get('id'),r.get('label')
        if type(ident)!=int or not 1<=ident<=count or ident in by:
            raise ValueError('MiMo返回编号缺失、重复或越界')
        if label is not None and (type(label)!=int or not 0<=label<=MAX_DIGIT):
            raise ValueError('MiMo返回非法题号')
        if type(r.get('uncertain'))!=bool or type(r.get('crop_issue'))!=bool:
            raise ValueError('MiMo未返回疑难和裁图标记')
        by[ident]=r
    return [by[i] for i in range(1,count+1)]


def predict(image, model, key, base_url):
    buffer=io.BytesIO();image.save(buffer,format='PNG')
    prompt=('Read printed question numbers in the numbered grid. Blue numbers are cell IDs, NOT question numbers. '
            'Ignore adjacent brackets, punctuation, option letters and black answer marks. Do not infer a sequence. '
            'Return ONLY a JSON array with one object per cell: '
            '{"id":1,"label":12,"uncertain":false,"crop_issue":true}. '
            'label must be an integer 1..60 for a readable question number, 0 for definitely non-numeric, '
            'or null when unreadable/ambiguous. crop_issue=true if neighboring option strokes, brackets or clipped '
            'digits contaminate the crop. uncertain=true whenever the number is not clearly readable. '
            'Never guess missing digits. Return every cell exactly once.')
    body={'model':model,'max_tokens':4096,'thinking':{'type':'disabled'},'messages':[{'role':'user','content':[
        {'type':'image','source':{'type':'base64','media_type':'image/png','data':base64.b64encode(buffer.getvalue()).decode()}},
        {'type':'text','text':prompt}]}]}
    request=urllib.request.Request(base_url.rstrip('/')+'/v1/messages',json.dumps(body).encode(),
        {'Content-Type':'application/json','x-api-key':key,'anthropic-version':'2023-06-01'})
    try:
        with urllib.request.urlopen(request,timeout=120) as response:
            result=json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError('MiMo HTTP %d；未写预标注，请核对端点、模型权限和额度'%error.code) from None
    text='\n'.join(c.get('text','') for c in result.get('content',[]) if c.get('type')=='text')
    if result.get('stop_reason')!='end_turn':
        raise ValueError('MiMo输出未正常结束（%s），不接入；usage=%s'%(result.get('stop_reason'),result.get('usage',{})))
    return text,result.get('usage',{})


def run(base, rows, output, model='mimo-v2.6-flash', base_url='https://api.xiaomimimo.com/anthropic', apply=False):
    base,output=Path(base),Path(output)
    if output.exists():raise FileExistsError(output)
    if not rows:raise ValueError('清单为空')
    if len({r['file'] for r in rows})!=len(rows):raise ValueError('重复样本')
    for r in rows:
        if row_log.digit_state(row_log.read(base,Path(r['file']).stem))['reviewed']:
            raise ValueError('拒绝覆盖已审样本：'+r['file'])
    key=os.environ.get('MIMO_API_KEY','').strip()
    if not key:raise ValueError('缺少MIMO_API_KEY环境变量；不要把密钥写进项目源码或日志')
    output.mkdir(parents=True)
    results=[];usage=[]
    for start in range(0,len(rows),24):
        chunk=rows[start:start+24];image=sheet(base,chunk)
        image.save(output/('sheet_%04d.png'%start))
        text,used=predict(image,model,key,base_url)
        (output/('reply_%04d.txt'%start)).write_text(text,encoding='utf-8')
        parsed=parse_reply(text,len(chunk))
        items=[{'file':r['file'],'stem':r['stem'],'previous_label':row_log.digit_state(row_log.read(base,Path(r['file']).stem))['label'],
                **{k:v for k,v in p.items() if k!='id'}} for r,p in zip(chunk,parsed)]
        results.extend(items);usage.append(used)
        write_json(output/'results.json',{'model':model,'items':results,'usage':usage,'applied':False})
        print('MiMo预标注 %d/%d'%(len(results),len(rows)),flush=True)
    if apply:
        # 全部响应通过检查后才接入；空值保留旧建议并进入疑难，绝不冒充非数字。
        for r in results:
            name=Path(r['file']).stem
            if row_log.digit_state(row_log.read(base,name))['reviewed']:
                raise ValueError('请求期间样本被审核，停止接入：'+r['file'])
        for r in results:
            label=r['previous_label'] if r['label'] is None else r['label']
            row_log.append(base,Path(r['file']).stem,{'事件':'预标注','标签':label,'模型':model,
                '疑难':r['uncertain'] or r['label'] is None,'裁图异常':r['crop_issue'],
                'MiMo读数':r['label'],'说明':'MiMo模型建议，仍待人工确认；不自动审核'})
    write_json(output/'results.json',{'model':model,'items':results,'usage':usage,'applied':apply})
    return results


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data',required=True)
    ap.add_argument('--subset',help='file列CSV，默认当前库前limit条')
    ap.add_argument('--limit',type=int,default=24)
    ap.add_argument('--output',required=True)
    ap.add_argument('--model',default='mimo-v2.6-flash')
    ap.add_argument('--base-url',default='https://api.xiaomimimo.com/anthropic')
    ap.add_argument('--apply',action='store_true',help='只追加未审预标注，绝不写审核')
    args=ap.parse_args()
    if args.limit<1:raise ValueError('limit必须为正数')
    rows=read_csv(Path(args.data)/'labels.csv')
    if args.subset:
        names=[r['file'] for r in read_csv(args.subset)]
        if len(set(names))!=len(names):raise ValueError('subset重复')
        by={r['file']:r for r in rows}
        rows=[by[name] for name in names]
    run(args.data,rows[:args.limit],args.output,args.model,args.base_url,args.apply)


if __name__=='__main__':main()

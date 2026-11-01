"""识别拼图/并发/反馈边界，全部写显式D盘临时目录。"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util
import io
import json
import re
from pathlib import Path
import sys
import time
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.recognition_api import SquarePacker, DoubaoClient, strict_json, validate_item, save_json, normalize_item, PROMPT
from recognize import pair_fill, apply_responses

BASE = None


def response(ids):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(
        {i: {'kind': 'fill', 'question_number': '13', 'answers': {'无小题号': {'1': '答案'+i, '2': ''}}} for i in ids})}}],
        'model': 'test', 'usage': {'prompt_tokens': 1280, 'completion_tokens': 50}}


class RecognitionTests(unittest.TestCase):
    def test_prompt_formula_json_roundtrip(self):
        example = PROMPT.split('完整合法JSON示例：', 1)[1].split('。这只是格式示例', 1)[0]
        item = strict_json(example)['图块ID']
        validate_item(item, 'fill')
        formula = item['answers']['3']['1']
        self.assertEqual(formula, r'$\frac{F_{1}L_{1}}{F_{2}}=L_{2}$')
        self.assertFalse(any(ord(c) < 32 and c not in '\r\n' for c in PROMPT))
        hierarchy = PROMPT.split('层级格式示例：', 1)[1].split('。没有印刷子题号', 1)[0]
        answers = strict_json(hierarchy)
        self.assertEqual(answers, {'4.1': {'1': '某答案'}, '4.2': {'1': ''}})
        validate_item({'kind': 'fill', 'question_number': '22', 'answers': answers}, 'fill')
        self.assertEqual(normalize_item({'kind': 'fill', 'answers': answers})['answers'], answers)

    def folder(self):
        path = BASE/self._testMethodName
        path.mkdir()
        return path

    def test_strict_nested_duplicates_and_slots(self):
        with self.assertRaises(ValueError):
            strict_json('{"x":{"1":"a","1":"b"}}')
        with self.assertRaises(ValueError):
            validate_item({'kind': 'fill', 'question_number': '13', 'answers': {'1': {'2': '不能前移'}}}, 'fill')
        validate_item({'kind': 'fill', 'question_number': '13', 'answers': {'1': {'1': '', '2': '看不清'}}}, 'fill')
        validate_item({'kind':'fill','question_number':'20','answers':{'1':{'舍弃':'舍弃'},'2':{'1':'文字'}}},'fill')
        with self.assertRaises(ValueError):
            validate_item({'kind':'fill','question_number':'20','answers':{'1':{'舍弃':'舍弃','1':'不能混入答案'}}},'fill')
        normalized=normalize_item({'kind':'fill','question_number':'13','answers':{'（1）':{'1':'5'}}})
        self.assertEqual(normalized['answers'],{'1':{'1':'5'}})
        self.assertEqual(normalize_item({'kind':'fill','answers':{'（1）①':{'1':'5'}}})['answers'],{'1.1':{'1':'5'}})
        with self.assertRaises(ValueError):
            normalize_item({'kind':'fill','answers':{'(1)':{'1':'a'},'1':{'1':'b'}}})
        multilevel=normalize_item({'kind':'fill','question_number':'23','answers':{'4':{'1':{'1':'1.0','2':'说明'}}}})
        self.assertEqual(multilevel['answers'],{'4':{'1.1':'1.0','1.2':'说明'}})
        validate_item(multilevel,'fill')
        with self.assertRaises(ValueError):
            validate_item({'kind':'fill','question_number':'23','answers':{'4':{'1':'a','1.1':'b'}}},'fill')

    def test_packing_geometry_and_same_question_distinct_ids(self):
        folder = self.folder()
        packer = SquarePacker(folder)
        grids = []
        for i in range(9):
            finished = packer.add({'id': f'p{i:03d}_000', 'kind': 'fill', 'number': 13}, Image.new('RGB', (1300, 310), (i*20, 0, 0)))
            if finished:
                grids.append(finished)
        grids.append(packer.finish())
        self.assertEqual(len(grids), 3)
        self.assertEqual(len({i['id'] for g in grids for i in g['items']}), 9)
        for grid in grids:
            with Image.open(folder/grid['image']) as im:
                self.assertEqual(im.size, (1500,1500))
            for tile in grid['items']:
                x,y,r,b = tile['grid_box']
                self.assertTrue(0 <= x < r <= 1500 and 0 <= y < b <= 1500)
                self.assertEqual(tile['scale'], 1)

    def test_concurrent_out_of_order_and_resume(self):
        folder = self.folder()
        packer = SquarePacker(folder)
        grids = []
        for i in range(4):
            packer.add({'id':f'p{i}_0', 'kind':'fill'}, Image.new('RGB',(1400,1300),'white'))
            grids.append(packer.finish())
        called = []
        def transport(body):
            text = body['messages'][0]['content'][0]['text']
            ident = next('p'+str(i)+'_0' for i in range(4) if 'p'+str(i)+'_0' in text)
            time.sleep((4-int(ident[1]))*.025)
            called.append(ident)
            return response([ident])
        client = DoubaoClient(folder, transport=transport)
        with ThreadPoolExecutor(4) as pool:
            futures = {pool.submit(client.call, g):g['id'] for g in grids}
            results = {futures[f]:f.result() for f in as_completed(futures)}
        self.assertNotEqual(called, ['p0_0','p1_0','p2_0','p3_0'])
        for i,g in enumerate(grids):
            self.assertIn(f'p{i}_0', results[g['id']]['items'])
            self.assertTrue(client.call(g)['reused'])
        self.assertEqual(len(called), 4)

    def test_rate_limit_retry_and_auth_stop(self):
        folder = self.folder()
        packer = SquarePacker(folder)
        packer.add({'id':'a','kind':'fill'},Image.new('RGB',(200,200)))
        grid = packer.finish()
        counter = []
        def throttled(_):
            counter.append(1)
            if len(counter)==1:
                raise urllib.error.HTTPError('test',429,'limited',{},None)
            return response(['a'])
        client = DoubaoClient(folder,transport=throttled)
        with patch('common.recognition_api.time.sleep'):
            value=client.call(grid)
        self.assertTrue(value['ok'])
        self.assertTrue(client.throttled.is_set())
        self.assertEqual(len(value['attempts']),2)
        def denied(_):
            raise urllib.error.HTTPError('test',401,'denied',{},None)
        client=DoubaoClient(folder,transport=denied)
        self.assertFalse(client.call(grid,refresh=True)['ok'])
        self.assertTrue(client.stop.is_set())

    def test_unmatched_and_failed_never_become_blank(self):
        boxes=[{'cls':5,'box':[0,0,100,100]},{'cls':6,'box':[10,10,20,20]}, {'cls':6,'box':[200,200,210,210]}]
        self.assertEqual(pair_fill(boxes),[(0,1,3.)])
        pages=[{'id':'p0','items':[{'id':'a','kind':'fill','number':13,'issues':[],'status':'等待豆包'}],'issues':[]}]
        grids=[{'id':'g000','image':'grid.png','items':[{'id':'a','grid_box':[0,0,10,10],'scale':1}]}]
        apply_responses(pages,grids,[{'ok':False,'error':'接口失败'}])
        self.assertNotIn('transcription',pages[0]['items'][0])
        self.assertEqual(pages[0]['items'][0]['status'],'调用失败')

    def test_account_usage_cap_is_not_retried_as_transient_rate_limit(self):
        folder=self.folder();packer=SquarePacker(folder)
        packer.add({'id':'a','kind':'fill'},Image.new('RGB',(200,200)));grid=packer.finish()
        calls=[]
        def limited(_):
            calls.append(1)
            payload=json.dumps({'error':{'code':'SetLimitExceeded','message':'service paused'}}).encode()
            raise urllib.error.HTTPError('test',429,'limit',{},io.BytesIO(payload))
        client=DoubaoClient(folder,transport=limited)
        value=client.call(grid)
        self.assertEqual(value['error_code'],'SetLimitExceeded')
        self.assertTrue(client.stop.is_set());self.assertEqual(len(calls),1)
        self.assertTrue(client.call(grid)['not_sent']);self.assertEqual(len(calls),1)

    def test_feedback_survives_reload_and_rejects_stale_update(self):
        folder=self.folder()
        save_json(folder/'manifest.json',{'input':'fake'})
        save_json(folder/'results.json',{'pages':[{'id':'p0','items':[{'id':'p0_0'}]}]})
        spec=importlib.util.spec_from_file_location('review_server',Path(__file__).resolve().parents[2]/'tools/识别验收.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        store=module.ReviewStore(folder)
        state=store.update({'id':'p0_0','label':'识别错误','correction':'5','revision':0})
        self.assertEqual(module.ReviewStore(folder).read(),state)
        with self.assertRaises(ValueError):
            store.update({'id':'p0_0','label':'正确','revision':0})
        with self.assertRaises(ValueError):
            store.update({'id':'not-a-target','label':'正确','revision':1})
        self.assertEqual(store.read()['items']['p0_0']['correction'],'5')

    def test_http_feedback_and_asset_boundary(self):
        folder=self.folder()
        save_json(folder/'manifest.json',{'input':'fake'})
        save_json(folder/'results.json',{'pages':[{'id':'p0','items':[{'id':'p0_0'}]}]})
        spec=importlib.util.spec_from_file_location('review_http',Path(__file__).resolve().parents[2]/'tools/识别验收.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        server=module.make_server(folder,0)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with urllib.request.urlopen(base) as r:
                html=r.read().decode()
            token=re.search("const TOKEN='([^']+)'",html).group(1)
            body=json.dumps({'id':'p0_0','label':'漏题／漏空','correction':'第2空遗漏','revision':0}).encode()
            req=urllib.request.Request(base+'/api/feedback',body,{'Content-Type':'application/json','X-Review-Token':token,'Origin':base})
            with urllib.request.urlopen(req) as r:
                self.assertEqual(json.load(r)['revision'],1)
            with urllib.request.urlopen(base+'/api/state') as r:
                self.assertEqual(json.load(r)['feedback']['items']['p0_0']['label'],'漏题／漏空')
            with self.assertRaises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(urllib.request.Request(base+'/api/feedback',body,{'X-Review-Token':'wrong'}))
            self.assertEqual(exc.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base+'/asset/%2e%2e/%2e%2e/.env')
        finally:
            server.shutdown();server.server_close();worker.join()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--temp',type=Path,required=True);args,rest=parser.parse_known_args()
    BASE=args.temp.resolve()
    if not str(BASE).lower().startswith('d:\\temp\\') or 'codex' not in str(BASE):
        raise ValueError('测试必须放D:/temp带codex目录')
    BASE.mkdir(parents=True,exist_ok=False)
    unittest.main(argv=[sys.argv[0],*rest])

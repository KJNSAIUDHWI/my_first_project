"""识别拼图/并发/反馈边界，全部写显式D盘临时目录。"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util
import io
import json
import re
from pathlib import Path
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from engine.vlm_client import SquarePacker, VlmClient, DoubaoClient, strict_json, validate_item, save_json, normalize_item, PROMPT
from engine.recognizer import pair_fill, apply_responses

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
        if BASE is None:
            td = tempfile.TemporaryDirectory()
            self.addCleanup(td.cleanup)
            return Path(td.name)
        path = BASE / self._testMethodName
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
        self.assertIs(DoubaoClient, VlmClient)
        client = VlmClient(folder, transport=transport)
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
        client = VlmClient(folder,transport=throttled)
        with patch('engine.vlm_client.time.sleep'):
            value=client.call(grid)
        self.assertTrue(value['ok'])
        self.assertTrue(client.throttled.is_set())
        self.assertEqual(len(value['attempts']),2)
        def denied(_):
            raise urllib.error.HTTPError('test',401,'denied',{},None)
        client=VlmClient(folder,transport=denied)
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
        client=VlmClient(folder,transport=limited)
        value=client.call(grid)
        self.assertEqual(value['error_code'],'SetLimitExceeded')
        self.assertTrue(client.stop.is_set());self.assertEqual(len(calls),1)
        self.assertTrue(client.call(grid)['not_sent']);self.assertEqual(len(calls),1)

    def test_feedback_survives_reload_and_rejects_stale_update(self):
        folder=self.folder()
        save_json(folder/'manifest.json',{'input':'fake'})
        save_json(folder/'results.json',{'pages':[{'id':'p0','items':[{'id':'p0_0'}]}]})
        spec=importlib.util.spec_from_file_location('review_server',ROOT/'apps/review/server.py')
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
        spec=importlib.util.spec_from_file_location('review_http',ROOT/'apps/review/server.py')
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

    def test_duplicate_key_p141_tolerant_merge_and_isolation(self):
        from engine.vlm_client import parse_record
        raw = (
            '{"p141_001":{"kind":"fill","question_number":"21","answers":{'
            '"4":{"1":"变大"},'
            '"1":{"1":"不相同","2":"开启"},'
            '"2":{"1":"B"},'
            '"3":{"1":"A","2":"0.3"},'
            '"4":{"1":"在A、B和C点处都安装一个电流表"}'
            '}},'
            '"p141_002":{"kind":"fill","question_number":"22","answers":{"1":{"1":"正常答案"}}}}'
        )
        grid = {
            'id': 'g001',
            'image': 'grids/g001.png',
            'items': [
                {'id': 'p141_001', 'kind': 'fill', 'grid_box': [0, 0, 100, 100], 'scale': 1.0},
                {'id': 'p141_002', 'kind': 'fill', 'grid_box': [0, 120, 100, 220], 'scale': 1.0},
            ],
        }
        rec = {'raw_text': raw, 'ok': True}
        parse_record(rec, grid)
        self.assertEqual(rec['errors'], {})
        self.assertIn('p141_001', rec['items'])
        self.assertIn('p141_002', rec['items'])
        ans1 = rec['items']['p141_001']['answers']
        self.assertEqual(set(ans1.keys()), {'1', '2', '3', '4'})
        self.assertEqual(ans1['4']['1'], '在A、B和C点处都安装一个电流表')
        # 冲突告警严格隔离在 p141_001，不污染同图兄弟题块 p141_002
        self.assertIn('p141_001', rec['dup_warnings'])
        self.assertNotIn('p141_002', rec['dup_warnings'])
        self.assertTrue(any('重复键' in w and '变大' in w for w in rec['dup_warnings']['p141_001']))

        pages = [{'id': 'p141', 'items': [
            {'id': 'p141_001', 'page_id': 'p141', 'kind': 'fill', 'number': 21, 'issues': [], 'status': '等待视觉转录'},
            {'id': 'p141_002', 'page_id': 'p141', 'kind': 'fill', 'number': 22, 'issues': [], 'status': '等待视觉转录'},
        ], 'issues': []}]
        apply_responses(pages, [grid], [rec], provider_label='豆包')
        it1, it2 = pages[0]['items']
        self.assertEqual(it1['status'], '待复核')
        self.assertTrue(any('重复键' in x for x in it1['issues']))
        self.assertEqual(it2['status'], '已识别')
        self.assertFalse(any('重复键' in x for x in it2['issues']))
        self.assertEqual(pages[0]['status'], '识别完成，待人工验收')

    def test_validate_item_reports_key_and_packer_max_items(self):
        with self.assertRaises(ValueError) as ctx:
            validate_item({'kind': 'fill', 'question_number': '20', 'answers': {'2': {'舍弃': '舍弃', '1': '错'}}}, 'fill')
        self.assertIn('舍弃', str(ctx.exception))
        self.assertIn('2', str(ctx.exception))

        folder = self.folder()
        pk = SquarePacker(folder, prefix='single_', max_items=1)
        out_grids = []
        for i in range(3):
            g = pk.add({'id': f'p0_00{i}', 'kind': 'fill'}, Image.new('RGB', (200, 120), 'white'))
            if g:
                out_grids.append(g)
        tail = pk.finish()
        if tail:
            out_grids.append(tail)
        self.assertEqual(len(out_grids), 3)
        self.assertTrue(all(len(g['items']) == 1 for g in out_grids))

    def test_append_error_log_concurrent_400_no_loss(self):
        from engine.diagnostics import append_error_log, diagnose_exception
        folder = self.folder()
        diag_dup = diagnose_exception('JSON重复键：4')
        self.assertEqual(diag_dup['module'], '视觉大模型输出解析')
        self.assertNotIn('获取完整堆栈', diag_dup['fix'])

        def worker(tid):
            for k in range(50):
                p = append_error_log(folder, '并发测试', f'JSON重复键：{tid}_{k}', context={'t': tid, 'k': k})
                self.assertIsNotNone(p)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        txt = (folder / 'logs' / 'errors.log').read_text(encoding='utf-8')
        blocks = [ln for ln in txt.splitlines() if ln.startswith('================ [')]
        self.assertEqual(len(blocks), 400)

    def test_retry_failed_tiles_single_mode_stats_tokens_and_logs(self):
        from scripts.retry_failed_tiles import retry_failed_grids
        folder = self.folder()
        (folder / 'crops').mkdir(parents=True)
        (folder / 'api').mkdir(parents=True)
        for name in ('p0_000.png', 'p0_001.png', 'p0_002.png'):
            Image.new('RGB', (200, 100), 'white').save(folder / 'crops' / name)
        # 历史已成功拼图 g000 + 历史失败拼图 g001（含 2 个 JSON 重复键失败题块）
        save_json(folder / 'api' / 'g000_old.json', {
            'grid': 'g000', 'ok': True, 'attempts': [{'attempt': 1, 'status': 200}],
            'usage': {'prompt_tokens': 1000, 'completion_tokens': 200},
        })
        save_json(folder / 'api' / 'g001_old.json', {
            'grid': 'g001', 'ok': False, 'error': 'JSON重复键：4', 'attempts': [{'attempt': 1, 'status': 200}],
            'usage': {'prompt_tokens': 1200, 'completion_tokens': 300},
        })
        save_json(folder / 'results.json', {
            'pages': [{
                'id': 'p0', 'name': 'page0', 'status': '部分识别失败', 'issues': [],
                'items': [
                    {'id': 'p0_000', 'page_id': 'p0', 'kind': 'fill', 'number': 13, 'crop': 'crops/p0_000.png', 'status': '已识别', 'issues': [], 'transcription': {'kind': 'fill', 'question_number': '13', 'answers': {'无小题号': {'1': 'A'}}}},
                    {'id': 'p0_001', 'page_id': 'p0', 'kind': 'fill', 'number': 14, 'crop': 'crops/p0_001.png', 'status': '调用失败', 'issues': ['豆包：JSON重复键：4']},
                    {'id': 'p0_002', 'page_id': 'p0', 'kind': 'fill', 'number': 15, 'crop': 'crops/p0_002.png', 'status': '调用失败', 'issues': ['豆包：JSON重复键：4']},
                ],
            }],
            'grids': [
                {'id': 'g000', 'image': 'grids/g000.png', 'items': [{'id': 'p0_000', 'kind': 'fill', 'grid_box': [0, 0, 10, 10], 'scale': 1}]},
                {'id': 'g001', 'image': 'grids/g001.png', 'items': [
                    {'id': 'p0_001', 'kind': 'fill', 'grid_box': [0, 0, 10, 10], 'scale': 1},
                    {'id': 'p0_002', 'kind': 'fill', 'grid_box': [0, 20, 10, 30], 'scale': 1},
                ]},
            ],
            'stats': {'pages': 1, 'grids': 2, 'api_done': 2, 'api_success': 1, 'api_failures': 1, 'prompt_tokens': 2200, 'completion_tokens': 500},
            'status': '有未完成调用，待补跑',
        })

        seen_batch_sizes = []
        fail_once = {'p0_002': True}

        def mock_transport(body):
            txt = body['messages'][0]['content'][0]['text']
            ids = re.findall(r'"id":\s*"([^"]+)"', txt)
            seen_batch_sizes.append(len(ids))
            if ids == ['p0_002'] and fail_once['p0_002']:
                fail_once['p0_002'] = False
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'not-json'}}], 'model': 'test', 'usage': {'prompt_tokens': 400, 'completion_tokens': 50}}
            return response(ids)

        def factory(out_d):
            return VlmClient(out_d, key='k', transport=mock_transport)

        # 第一轮补跑：2 个 JSON 失败题块应降级为 2 张单题图（1 成功 1 失败），失败写入 errors.log
        r1 = retry_failed_grids(folder, concurrency=2, client_factory=factory)
        self.assertTrue(r1['ok'])
        self.assertEqual(r1['retried_grids'], 2)
        self.assertEqual(seen_batch_sizes, [1, 1])
        self.assertEqual(r1['remaining_failures'], 1)
        self.assertEqual(r1['stats']['api_done'], r1['stats']['api_success'] + r1['stats']['api_failures'])
        self.assertEqual(r1['stats']['superseded_grids'], 1)
        self.assertTrue((folder / 'logs' / 'errors.log').is_file())
        self.assertTrue((folder / 'runs' / r1['run_id'] / 'events.jsonl').is_file())

        # 第二轮补跑：剩余 1 题成功，总失败清零，且总 Token 累计所有历史+补跑记录
        r2 = retry_failed_grids(folder, concurrency=2, client_factory=factory)
        self.assertTrue(r2['ok'])
        self.assertEqual(r2['remaining_failures'], 0)
        self.assertEqual(r2['stats']['api_failures'], 0)
        self.assertEqual(r2['stats']['api_done'], r2['stats']['api_success'])
        self.assertGreaterEqual(r2['stats']['prompt_tokens'], 1000 + 1200 + 1280 + 400 + 1280)

    def test_row_layout_normalization_vlm_vote_and_cv_fallback(self):
        from PIL import ImageDraw
        from engine.row_layout import build_batch_row_layouts, detect_crop_underline_rows, normalize_row_layout

        # 1) normalize_item 规范化合法 rows，并容忍畸形 rows 不拒收有效答案
        n1 = normalize_item({'kind': 'fill', 'question_number': '20', 'rows': ['2', 3], 'answers': {'1': {'1': 'a', '2': 'b'}}})
        self.assertEqual(n1['rows'], [2, 3])
        n2 = normalize_item({'kind': 'fill', 'question_number': '20', 'rows': [0, -1], 'answers': {'1': {'1': 'a'}}})
        self.assertNotIn('rows', n2)
        self.assertIsNone(normalize_row_layout([True, 2]))

        # 2) VLM 多数投票聚合 + 老批次 OpenCV 印刷横线兜底
        folder = self.folder()
        crops = folder / 'crops'
        crops.mkdir(parents=True, exist_ok=True)
        # 构造一道 2 行印刷横线题图块：第 1 行 2 条横线，第 2 行 3 条横线
        im = Image.new('L', (1200, 200), 255)
        dr = ImageDraw.Draw(im)
        for x1, x2 in ((120, 380), (480, 740)):
            dr.line([(x1, 75), (x2, 75)], fill=0, width=3)
        for x1, x2 in ((120, 340), (420, 640), (760, 1020)):
            dr.line([(x1, 155), (x2, 155)], fill=0, width=3)
        crop_p = crops / 'q20.png'
        im.save(crop_p)
        self.assertEqual(detect_crop_underline_rows(crop_p), [2, 3])

        fake_result = {
            'pages': [
                {'id': 'p0', 'items': [
                    {'id': 'p0_0', 'kind': 'fill', 'number': 20, 'crop': 'crops/q20.png',
                     'transcription': {'kind': 'fill', 'question_number': '20', 'answers': {'1': {'1': 'a', '2': 'b'}, '2': {'1': 'c', '2': 'd'}, '3': {'1': 'e'}}}},
                    {'id': 'p0_1', 'kind': 'fill', 'number': 21,
                     'transcription': {'kind': 'fill', 'question_number': '21', 'rows': [4, 2], 'answers': {'1': {'1': '1', '2': '2', '3': '3', '4': '4'}, '2': {'1': '5', '2': '6'}}}},
                ]},
                {'id': 'p1', 'items': [
                    {'id': 'p1_1', 'kind': 'fill', 'number': 21,
                     'transcription': {'kind': 'fill', 'question_number': '21', 'rows': [4, 2], 'answers': {'1': {'1': '1', '2': '2', '3': '3', '4': '4'}, '2': {'1': '5', '2': '6'}}}},
                ]},
                {'id': 'p2', 'items': [
                    {'id': 'p2_1', 'kind': 'fill', 'number': 21,
                     'transcription': {'kind': 'fill', 'question_number': '21', 'rows': [6], 'answers': {'1': {'1': '1', '2': '2', '3': '3', '4': '4'}, '2': {'1': '5', '2': '6'}}}},
                ]},
            ]
        }
        save_json(folder / 'results.json', fake_result)
        layouts = build_batch_row_layouts(fake_result, folder, allow_cv_fallback=True, use_cache=True)
        self.assertEqual(layouts.get('20'), [2, 3])
        self.assertEqual(layouts.get('21'), [4, 2])
        self.assertTrue((folder / 'row_layouts_cache.json').is_file())


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--temp',type=Path,default=None);args,rest=parser.parse_known_args()
    if args.temp is not None:
        BASE=args.temp.resolve()
        BASE.mkdir(parents=True,exist_ok=True)
    unittest.main(argv=[sys.argv[0],*rest])

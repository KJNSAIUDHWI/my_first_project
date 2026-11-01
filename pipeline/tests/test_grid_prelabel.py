"""三路拼图失败重试、队列去重及不自动审核。"""
from pathlib import Path
import sys,unittest,tempfile
from datetime import datetime
from unittest.mock import patch
import json
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cnn import grid_prelabel as grid
from common.records import write_csv,read_json
from common import row_log


class GridTest(unittest.TestCase):
    def test_gemini_gateway_line_wrap_does_not_change_numbers(self):
        content='[{"i\nd":1,"la\nbel":6\n0,"uncer\ntain":false,"crop_\nissue":false}]'
        payload=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':content}}],'usage':{},'model':'gemini-3.6-flash'}).encode()
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self):return payload
        class ImageStub:
            def save(self,buffer,format):buffer.write(b'unit-test-image')
        with patch.dict('os.environ',{'HAPPY_GEMINI_KEY':'unit-test'}),patch.object(grid.urllib.request,'urlopen',return_value=Response()):
            text,usage=grid.request_grid('gemini',ImageStub())
        self.assertEqual(grid.mimo_digit.parse_reply(text,1)[0]['label'],60)
        self.assertTrue(usage['transport_line_unwrap'])

    def test_bad_grid_is_retried_and_not_taken_as_blank(self):
        rows=[dict(file='one.png',split='train')]
        with patch.object(grid.mimo_digit,'sheet',return_value=None),patch.object(grid,'request_grid',return_value=('',{})),patch.object(grid.time,'sleep'):
            result=grid.recognize('.',rows,'gemini',attempts=2)
        self.assertFalse(result['ok']);self.assertEqual(len(result['errors']),2);self.assertEqual(result['items'],[])

    def test_full_keeps_file_identity_and_does_not_review(self):
        prefix=datetime.now().strftime('%Y-%m-%d-%H-%M')+'_codex_grid_test_'
        with tempfile.TemporaryDirectory(prefix=prefix,dir='D:/temp') as tmp:
            base=Path(tmp)/'data';base.mkdir();rows=[]
            for i in range(30):
                row=dict(file='%03d.png'%i,stem='page',split='train');rows.append(row)
                row_log.append(base,Path(row['file']).stem,{'事件':'预标注','标签':0})
            write_csv(base/'labels.csv',rows,list(rows[0]))
            def fake(base,chunk,route):
                return dict(route=route,model=grid.ROUTES[route]['model'],ok=True,errors=[],usage={},
                    items=[dict(file=r['file'],label=60,uncertain=False,crop_issue=False) for r in chunk])
            env={c['env']:'unit-test' for c in grid.ROUTES.values()}
            with patch.dict('os.environ',env),patch.object(grid,'recognize',side_effect=fake):
                grid.full(base,Path(tmp)/'out')
                grid.full(base,Path(tmp)/'out')
            self.assertEqual(read_json(Path(tmp)/'out/status.json')['done'],30)
            for r in rows:
                ev=row_log.read(base,Path(r['file']).stem)
                self.assertEqual(len(ev),2)
                state=row_log.digit_state(ev)
                self.assertEqual(state['label'],60);self.assertFalse(state['reviewed'])


if __name__=='__main__':unittest.main()

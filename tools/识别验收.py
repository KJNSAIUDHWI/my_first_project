"""本地识别验收页。仅在本次结果目录保存人工反馈，不写正式data。"""
import argparse
from datetime import datetime
import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import sys
import threading
import tarfile
import urllib.request
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'pipeline'))
from common.recognition_api import save_json

LABELS = ('正确', '识别错误', '漏题／漏空', '对应错误', '看不清')


def prepare_math_assets(folder):
    """固定版本静态公式资源放在本次结果内；以后看报告不依赖外部CDN。"""
    target = Path(folder).resolve()/'vendor/katex'
    if (target/'katex.min.js').is_file() and (target/'katex.min.css').is_file():
        return
    try:
        raw = urllib.request.urlopen('https://registry.npmjs.org/katex/-/katex-0.16.22.tgz', timeout=30).read()
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            for member in archive.getmembers():
                if member.name in ('package/dist/katex.min.js', 'package/dist/katex.min.css') or (member.name.startswith('package/dist/fonts/') and member.name.endswith('.woff2')):
                    dest = (target/member.name.removeprefix('package/dist/')).resolve()
                    if not dest.is_relative_to(target):
                        raise ValueError('公式资源路径异常')
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(archive.extractfile(member).read())
    except (OSError, ValueError, tarfile.TarError):
        print('公式资源未准备完成，公式暂显示原始转录；重启服务可重试。', flush=True)


class ReviewStore:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.path = self.folder/'feedback.json'
        self.lock = threading.Lock()
        # Bind feedback to immutable input/model manifest, not changing completion status.
        self.identity = hashlib.sha256((self.folder/'manifest.json').read_bytes()).hexdigest()

    def read(self):
        if not self.path.exists():
            return {'run': self.identity, 'revision': 0, 'items': {}}
        value = json.loads(self.path.read_text(encoding='utf-8'))
        if value['run'] != self.identity:
            raise ValueError('反馈属于另一批输入')
        return value

    def update(self, request):
        results = json.loads((self.folder/'results.json').read_text(encoding='utf-8'))
        allowed = {p['id'] for p in results['pages']} | {i['id'] for p in results['pages'] for i in p['items']}
        ident, label = request.get('id'), request.get('label')
        if ident not in allowed or label not in LABELS + ('',):
            raise ValueError('反馈目标或类型不合法')
        correction = request.get('correction', '')
        if not isinstance(correction, str) or len(correction) > 20000:
            raise ValueError('修正文字不合法或过长')
        with self.lock:
            state = self.read()
            if request.get('revision') != state['revision']:
                raise ValueError('反馈已被另一页面更新，请刷新后重试；当前草稿保留')
            state['items'][ident] = {'label': label, 'correction': correction, 'updated': datetime.now().isoformat(timespec='seconds')}
            state['revision'] += 1
            save_json(self.path, state)
            with (self.folder/'feedback_history.jsonl').open('a', encoding='utf-8') as file:
                file.write(json.dumps({'id': ident, 'revision': state['revision'], **state['items'][ident]}, ensure_ascii=False) + '\n')
            return state


def make_server(folder, port=8903):
    store = ReviewStore(folder)
    token = secrets.token_urlsafe(24)
    template_path = Path(__file__).with_suffix('.html')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, value, code=200, content_type='application/json; charset=utf-8'):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            name = unquote(urlparse(self.path).path)
            if name == '/':
                template = template_path.read_text(encoding='utf-8')
                return self.reply(template.replace('__TOKEN__', token).encode(), content_type='text/html; charset=utf-8')
            if name == '/api/state':
                try:
                    result = json.loads((store.folder/'results.json').read_text(encoding='utf-8'))
                    live = store.folder/'benchmark_live.json'
                    if live.is_file() and not result.get('benchmark'):
                        result['benchmark'] = json.loads(live.read_text(encoding='utf-8'))
                    return self.reply({'result': result, 'feedback': store.read()})
                except (OSError, ValueError) as exc:
                    return self.reply({'error': str(exc)}, 409)
            if name.startswith('/asset/'):
                path = (store.folder/name.removeprefix('/asset/')).resolve()
                if not path.is_relative_to(store.folder) or path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.svg', '.js', '.css', '.woff', '.woff2', '.ttf') or not path.is_file():
                    return self.reply({'error': '文件不存在'}, 404)
                return self.reply(path.read_bytes(), content_type=mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
            self.reply({'error': '接口不存在'}, 404)

        def do_POST(self):
            if self.path != '/api/feedback' or self.headers.get('X-Review-Token') != token:
                return self.reply({'error': '请求无效'}, 403)
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self.reply({'error': '禁止跨站修改'}, 403)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 100000:
                    raise ValueError('请求过长或为空')
                value = json.loads(self.rfile.read(size))
                self.reply(store.update(value))
            except (ValueError, KeyError, TypeError) as exc:
                self.reply({'error': str(exc)}, 409)
            except OSError:
                self.reply({'error': '保存失败，草稿保留，请重试'}, 500)
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8903)
    args = parser.parse_args()
    prepare_math_assets(args.dir)
    server = make_server(args.dir, args.port)
    print(f'识别验收 http://127.0.0.1:{server.server_port}/', flush=True)
    server.serve_forever()

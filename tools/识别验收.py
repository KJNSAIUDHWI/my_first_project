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
from common.lan_collab import ClaimManager, resolve_host, print_access_urls

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
            req_rev = request.get('revision')
            existing = state['items'].get(ident)
            # 细粒度并发控制：若两人同时改不同题目则自动合并；改同一题且版本过期则拦截。
            if req_rev != state['revision']:
                if not isinstance(req_rev, int) or req_rev < 0 or req_rev > state['revision']:
                    raise ValueError('反馈已被另一页面更新，请刷新后重试；当前草稿保留')
                if existing is not None and int(existing.get('revision', state['revision'])) > req_rev:
                    raise ValueError('反馈已被另一页面更新，请刷新后重试；当前草稿保留')
            state['revision'] += 1
            reviewer = str(request.get('reviewer') or '').strip()[:32]
            item_record = {
                'label': label,
                'correction': correction,
                'updated': datetime.now().isoformat(timespec='seconds'),
                'revision': state['revision'],
            }
            if reviewer:
                item_record['reviewer'] = reviewer
            state['items'][ident] = item_record
            save_json(self.path, state)
            with (self.folder/'feedback_history.jsonl').open('a', encoding='utf-8') as file:
                file.write(json.dumps({'id': ident, **item_record}, ensure_ascii=False) + '\n')
            return state


def make_server(folder, port=8903, host='127.0.0.1', lock_ttl=300):
    store = ReviewStore(folder)
    claims = ClaimManager(ttl_seconds=lock_ttl)
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
                    return self.reply({'result': result, 'feedback': store.read(), 'locks': claims.snapshot()})
                except (OSError, ValueError) as exc:
                    return self.reply({'error': str(exc)}, 409)
            if name == '/api/locks':
                return self.reply({'ok': True, 'locks': claims.snapshot()})
            if name.startswith('/asset/'):
                path = (store.folder/name.removeprefix('/asset/')).resolve()
                if not path.is_relative_to(store.folder) or path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.svg', '.js', '.css', '.woff', '.woff2', '.ttf') or not path.is_file():
                    return self.reply({'error': '文件不存在'}, 404)
                return self.reply(path.read_bytes(), content_type=mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
            self.reply({'error': '接口不存在'}, 404)

        def do_POST(self):
            path = unquote(urlparse(self.path).path)
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self.reply({'error': '禁止跨站修改'}, 403)
            # release 允许 sendBeacon 携带 body 内 token
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 100000:
                    raise ValueError('请求过长或为空')
                value = json.loads(self.rfile.read(size))
            except (ValueError, KeyError, TypeError) as exc:
                return self.reply({'error': str(exc)}, 400)
            req_token = self.headers.get('X-Review-Token') or (value.get('token') if isinstance(value, dict) else None)
            if req_token != token:
                return self.reply({'error': '请求无效'}, 403)
            if path == '/api/claim':
                client_id = str(value.get('client_id') or '').strip()
                user = str(value.get('user') or '成员').strip() or '成员'
                exclude = value.get('exclude') or []
                result = json.loads((store.folder/'results.json').read_text(encoding='utf-8'))
                fb = store.read().get('items', {})
                pages = result.get('pages', [])
                page_map = {p['id']: p for p in pages}
                def page_done(pid):
                    p = page_map[pid]
                    if not fb.get(p['id'], {}).get('label'):
                        return False
                    return all(bool(fb.get(i['id'], {}).get('label')) for i in p.get('items', []))
                res = claims.claim_next(client_id, user, [p['id'] for p in pages], page_done, exclude=exclude)
                res['locks'] = claims.snapshot()
                return self.reply(res)
            if path == '/api/acquire':
                pid = str(value.get('page_id') or '').strip()
                client_id = str(value.get('client_id') or '').strip()
                user = str(value.get('user') or '成员').strip() or '成员'
                res = claims.acquire(pid, client_id, user, force=bool(value.get('force', False)))
                res['locks'] = claims.snapshot()
                return self.reply(res, 200 if res.get('ok') else 409)
            if path == '/api/heartbeat':
                pid = str(value.get('page_id') or '').strip()
                client_id = str(value.get('client_id') or '').strip()
                user = str(value.get('user') or '成员').strip() or '成员'
                res = claims.heartbeat(pid, client_id, user)
                res['locks'] = claims.snapshot()
                return self.reply(res)
            if path == '/api/release':
                client_id = str(value.get('client_id') or '').strip()
                pid = value.get('page_id')
                return self.reply(claims.release(client_id, pid if pid else None))
            if path != '/api/feedback':
                return self.reply({'error': '请求无效'}, 403)
            try:
                self.reply(store.update(value))
            except (ValueError, KeyError, TypeError) as exc:
                self.reply({'error': str(exc)}, 409)
            except OSError:
                self.reply({'error': '保存失败，草稿保留，请重试'}, 500)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8903)
    parser.add_argument('--host', default=None, help='监听地址，默认 127.0.0.1；多人局域网协作传 --lan 或 --host 0.0.0.0')
    parser.add_argument('--lan', action='store_true', help='开启局域网监听（等价于 --host 0.0.0.0）')
    parser.add_argument('--lock-ttl', type=int, default=300, help='多人协作单页锁定超时秒数，默认 300 秒')
    args = parser.parse_args()
    prepare_math_assets(args.dir)
    host = resolve_host(args.host, args.lan)
    server = make_server(args.dir, args.port, host=host, lock_ttl=args.lock_ttl)
    print('识别验收服务已启动', flush=True)
    print_access_urls(host, server.server_port, label='识别验收')
    server.serve_forever()

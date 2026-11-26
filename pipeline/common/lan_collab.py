# -*- coding: utf-8 -*-
"""局域网多人协作公共模块：局域网地址探测 + 动态抢占租约锁。

规则：
- 谁先进入/领图，就锁定第一张未审且未被他人锁定的样本；
- 审核完成、切图或关闭页面时主动释放锁；
- 超过 ttl_seconds（默认 300 秒）无心跳自动过期释放，避免断网/关机死锁。
"""
import socket
import threading
import time


def detect_lan_ips():
    """探测本机可用的局域网 IPv4 地址（优先默认路由出口）。"""
    ips = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and not ip.startswith("169.254.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def resolve_host(host=None, lan=False):
    """解析监听地址：--lan 开启 0.0.0.0，否则默认 127.0.0.1。"""
    if host:
        return host
    return "0.0.0.0" if lan else "127.0.0.1"


def print_access_urls(host, port, label="服务"):
    """在终端清晰打印本机及局域网访问链接，便于另一台电脑直接打开。"""
    print(f"  本机访问: http://127.0.0.1:{port}/", flush=True)
    if host in ("0.0.0.0", "::", ""):
        lan_ips = detect_lan_ips()
        if lan_ips:
            for ip in lan_ips:
                print(f"  局域网访问（另一台电脑浏览器打开）: http://{ip}:{port}/", flush=True)
        else:
            print(f"  局域网已监听 0.0.0.0:{port}（未自动检出局域网IP，可用 ipconfig 查看 IPv4）", flush=True)
        print(f"  提示: 若另一台电脑打不开，请确认两台电脑连同一路由器，并在 Windows 防火墙放行 TCP {port} 端口。", flush=True)
    elif host != "127.0.0.1":
        print(f"  指定地址访问: http://{host}:{port}/", flush=True)


class ClaimManager:
    """线程安全的样本动态抢占与租约锁管理器。"""

    def __init__(self, ttl_seconds=300):
        self.ttl_seconds = max(30, int(ttl_seconds))
        self._lock = threading.RLock()
        # key -> {"client_id": str, "user": str, "claimed_at": float, "expires_at": float}
        self._claims = {}

    def _prune(self, now=None):
        now = time.monotonic() if now is None else now
        expired = [k for k, v in self._claims.items() if v["expires_at"] <= now]
        for k in expired:
            del self._claims[k]

    def _release_client_locked(self, client_id, keep_key=None):
        if not client_id:
            return
        to_del = [
            k for k, v in self._claims.items()
            if v["client_id"] == client_id and k != keep_key
        ]
        for k in to_del:
            del self._claims[k]

    def snapshot(self):
        """返回当前所有未过期锁的只读快照。"""
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            return {
                k: {
                    "client_id": v["client_id"],
                    "user": v["user"],
                    "expires_in": max(0, int(v["expires_at"] - now)),
                }
                for k, v in self._claims.items()
            }

    def owner_of(self, key):
        with self._lock:
            self._prune()
            info = self._claims.get(key)
            return dict(info) if info else None

    def acquire(self, key, client_id, user="成员", force=False):
        """尝试锁定指定样本。已被他人锁定时返回 ok=False 及持锁人信息。"""
        if not key or not client_id:
            return {"ok": False, "error": "缺少样本标识或客户端标识"}
        user = (user or "成员").strip()[:32] or "成员"
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            cur = self._claims.get(key)
            if cur and cur["client_id"] != client_id and not force:
                return {
                    "ok": False,
                    "locked": True,
                    "locked_by": cur["user"],
                    "locked_client": cur["client_id"],
                    "expires_in": max(0, int(cur["expires_at"] - now)),
                }
            self._release_client_locked(client_id, keep_key=key)
            self._claims[key] = {
                "client_id": client_id,
                "user": user,
                "claimed_at": cur["claimed_at"] if (cur and cur["client_id"] == client_id) else now,
                "expires_at": now + self.ttl_seconds,
            }
            return {
                "ok": True,
                "key": key,
                "user": user,
                "ttl": self.ttl_seconds,
            }

    def claim_next(self, client_id, user, candidates, is_done_fn, exclude=None):
        """动态领取第一张未完成且未被他人锁定的样本。"""
        if not client_id:
            return {"ok": False, "error": "缺少客户端标识"}
        user = (user or "成员").strip()[:32] or "成员"
        exclude = set(exclude or ())
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            # 释放该用户之前持有的锁，准备领下一张
            self._release_client_locked(client_id)
            remaining_pending = 0
            locked_by_others = 0
            for key in candidates:
                if is_done_fn(key):
                    continue
                remaining_pending += 1
                if key in exclude:
                    continue
                cur = self._claims.get(key)
                if cur and cur["client_id"] != client_id:
                    locked_by_others += 1
                    continue
                self._claims[key] = {
                    "client_id": client_id,
                    "user": user,
                    "claimed_at": now,
                    "expires_at": now + self.ttl_seconds,
                }
                return {
                    "ok": True,
                    "key": key,
                    "user": user,
                    "ttl": self.ttl_seconds,
                    "remaining_pending": remaining_pending,
                }
            if remaining_pending == 0:
                return {"ok": True, "key": None, "reason": "all_done", "remaining_pending": 0}
            return {
                "ok": True,
                "key": None,
                "reason": "all_locked",
                "remaining_pending": remaining_pending,
                "locked_by_others": locked_by_others,
            }

    def heartbeat(self, key, client_id, user="成员"):
        """给当前持有的锁续租。"""
        if not key or not client_id:
            return {"ok": False}
        user = (user or "成员").strip()[:32] or "成员"
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            cur = self._claims.get(key)
            if cur and cur["client_id"] != client_id:
                return {"ok": False, "locked_by": cur["user"]}
            self._claims[key] = {
                "client_id": client_id,
                "user": user,
                "claimed_at": cur["claimed_at"] if cur else now,
                "expires_at": now + self.ttl_seconds,
            }
            return {"ok": True, "ttl": self.ttl_seconds}

    def release(self, client_id, key=None):
        """释放指定样本锁或该客户端的全部锁。"""
        if not client_id:
            return {"ok": True, "released": 0}
        with self._lock:
            self._prune()
            released = 0
            if key is not None:
                cur = self._claims.get(key)
                if cur and cur["client_id"] == client_id:
                    del self._claims[key]
                    released = 1
            else:
                to_del = [k for k, v in self._claims.items() if v["client_id"] == client_id]
                for k in to_del:
                    del self._claims[k]
                released = len(to_del)
            return {"ok": True, "released": released}

"""本机回环 HTTP 单源（仅标准库，不依赖 Qt）。

范围：客户端 → 本机后端（固定 127.0.0.1）。承载两处历史实现：
  1. 面板 ``cuktech_launcher.make_api`` —— 保活连接、命名管道、GET 单次重试；
  2. 构建 ``build_cuktech._http_get`` —— 一次性 GET，固定 127.0.0.1，异常吞掉返回 (None, b'')。

主机固定 v4：取证见 ``_verify/c46_b1/host_probe.json``——服务端只监听
127.0.0.1:8080，::1 连 TCP 都建不起来（TimeoutError，约 1009 ms）。即原 R25 的
::1 探测在任何场景都只走回落分支，纯增延迟，已删除；``CK_API_HOST`` 仍可覆盖。

不在范围：``ux_core.ai_run`` / ``ai_stream``。那是外网 HTTPS + Bearer + SSE + 429 退避，
失败面是中文提示而不是状态码，与本模块语义不同，合并会改行为（见内部评测记录）。

超时、重试次数、异常吞与不吞均由调用方显式传入，不在这里统一（理由表见内部评测记录）。
"""
import http.client
import json
import os
import threading

__all__ = ['Loopback', 'loopback_get_once']


def _noop(*_args, **_kw):
    return None


def loopback_get_once(host, port, path, token=None, timeout=5.0, max_bytes=None):
    """一次性 GET：建连 → 请求 → 读体 → 关连接。

    抛异常给调用方，本函数不吞（build_cuktech._http_get 自己吞，语义保持原样）。
    max_bytes 非空时按该上限截断响应体（构建门禁只用前 64 KB）。
    """
    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request('GET', path, headers={'X-Local-Token': token} if token else {})
        resp = conn.getresponse()
        body = resp.read(max_bytes) if max_bytes else resp.read()
        return resp.status, body
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


class Loopback:
    """本机后端客户端：线程内保活连接 + 可选命名管道。

    主机固定 v4（取证见模块 docstring）；四个入口（api / api_meta / get_bytes /
    管道）共用同一个 self.host，不再各自解析。
    swallow 由调用方注入（launcher 传 ck_swallow），本模块不自带日志。
    """

    def __init__(self, port, host='127.0.0.1', token='', pipe_name=None,
                 pipe_enabled=False, swallow=None):
        self.port = int(port)
        self.host = os.environ.get('CK_API_HOST') or host
        self.token = token or ''
        self._pipe_name = pipe_name or (r'\\.\pipe\ck_live_%d' % self.port)
        self._pipe_on = bool(pipe_enabled)
        self._swallow = swallow or _noop
        self._tl = threading.local()
        self.api = self._api_shim()

    # ── 线程内保活连接（R19，原 L4496–4510）───────────────────────────────
    def _tl_conn(self, timeout):
        c = getattr(self._tl, 'conn', None)
        if c is None:
            c = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
            self._tl.conn = c
        return c

    def _tl_drop(self):
        c = getattr(self._tl, 'conn', None)
        self._tl.conn = None
        try:
            if c is not None:
                c.close()
        except Exception as exc:
            self._swallow(exc, 'launcher:2407')

    # ── 命名管道通道（R25，原 L4512–4533）─────────────────────────────────
    def _tl_pipe(self):
        c = getattr(self._tl, 'pipe', None)
        if c is None:
            from multiprocessing.connection import Client
            c = Client(self._pipe_name)
            self._tl.pipe = c
        return c

    def _pipe_live(self, path, extra_headers):
        _tag = (extra_headers or {}).get('X-Chart-Tag') or ''
        _qs = path.split('?', 1)[1] if '?' in path else ''
        c = self._tl_pipe()
        c.send({'qs': _qs, 'tag': _tag})
        r = c.recv()
        if not isinstance(r, dict) or r.get('code') != 200:
            raise IOError('pipe_resp')
        # 该分支无 etag 语义：管道回包只带 qs/tag/code/obj/len/srv_ms，没有 ETag 字段。
        return 200, r.get('obj'), {'len': int(r.get('len') or 0),
                                   'srv_ms': r.get('srv_ms'), 'srv_ms_src': 'pipe',
                                   'etag': None}

    # ── 主入口（原 L4535–4584）───────────────────────────────────────────
    def api_meta(self, method, path, payload=None, timeout=8, extra_headers=None):
        """返回 (status, obj, meta)；meta 含响应体长度/服务端耗时头。"""
        if self._pipe_on and method == 'GET' and path.startswith('/api/live'):
            try:
                return self._pipe_live(path, extra_headers)
            except Exception as exc:
                self._swallow(exc, 'launcher:pipe-fallback')
                self._pipe_on = False
                self._tl.pipe = None
        body = json.dumps(payload) if payload is not None else None
        headers = {'Content-Type': 'application/json'} if body else {}
        if self.token:
            headers['X-Local-Token'] = self.token
        if extra_headers:
            headers.update(extra_headers)
        last = None
        for _attempt in (0, 1):
            c = self._tl_conn(timeout)
            try:
                c.request(method, path, body=body, headers=headers)
                r = c.getresponse()
                raw = r.read()
                # srv_ms 两个来源语义不同（HTTP 头 vs 同进程管道回包），显式标注来源，
                # 不合成一个字段；调用方按 srv_ms_src 决定可信度。
                meta = {'len': len(raw), 'srv_ms': r.getheader('X-Live-Ms'),
                        'srv_ms_src': 'http', 'etag': r.getheader('ETag')}
                try:
                    obj = json.loads(raw)
                except Exception:
                    obj = raw.decode('utf-8', 'replace')
                return r.status, obj, meta
            except Exception as e:
                last = e
                self._tl_drop()
                if method != 'GET' or _attempt:
                    break
        raise last

    def _api_shim(self):
        """二元返回入口。用闭包而非绑定方法：旧调用方会在 api 上挂 api_meta 属性。"""
        def api(method, path, payload=None, timeout=8):
            s, obj, _m = self.api_meta(method, path, payload, timeout)
            return s, obj
        api.api_meta = self.api_meta
        return api

    def get_bytes(self, path, timeout=10):
        """二进制下载。与 api/api_meta 共用同一个 host（固定 v4）。"""
        c = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        gheaders = {'X-Local-Token': self.token} if self.token else {}
        c.request('GET', path, headers=gheaders)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

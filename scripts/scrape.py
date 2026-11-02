"""用 Scrapling（https://github.com/D4Vinci/Scrapling）抓取东方财富和同花顺。

- 普通请求：Scrapling 的 FetcherSession（curl_cffi 模拟 Chrome 的 TLS 指纹和请求头）。同花顺对普通 HTTP 客户端直接返回 403，
  东方财富也更不容易限流。common.get 对这两个网站的请求都走这里，每个线程一个会话（curl 会话不能多线程共用）。
- 需要先在浏览器里运行网页 JS 生成 cookie 的页面（同花顺资金流向等）：browser() 打开 Scrapling 的 StealthySession，
  用本机已装的 Chrome / Chromium（GitHub Actions 自带 Chrome），不额外下载浏览器。"""
import glob
import json
import logging
import os
import re
import shutil
import threading
import time
from urllib.parse import urlsplit

try:
    from scrapling.fetchers import FetcherSession
    logging.getLogger("scrapling").setLevel(logging.CRITICAL)    # 每个请求都打一行日志太多；失败由调用方重试并记日志
except ImportError:                                              # 没装 Scrapling 时退回 requests
    FetcherSession = None

HOSTS = ("eastmoney.com", "10jqka.com.cn")
GAP = {"10jqka.com.cn": 0.3}                                     # 同一网站两次请求之间至少间隔几秒，避免被封
_tl = threading.local()
_last, _lock = {}, threading.Lock()


class FetchError(Exception):
    pass


class Resp:
    """Scrapling 的 Response 包成 requests.Response 的常用接口：status_code、content、text、encoding（可改）、json()、headers。"""

    def __init__(self, r):
        self.status_code, self.content, self.url = r.status, r.body if isinstance(r.body, bytes) else str(r.body).encode(), r.url
        self.headers = {k.lower(): v for k, v in (r.headers or {}).items()}
        m = re.search(r"charset=([\w-]+)", self.headers.get("content-type", ""), re.I)
        self.encoding = m.group(1) if m else None

    @property
    def text(self):
        return self.content.decode(self.encoding or "utf-8", "replace")

    def json(self):
        return json.loads(self.text)                             # 解析失败抛 ValueError，和 requests 一样


def handles(url):
    host = urlsplit(url).hostname or ""
    return FetcherSession is not None and any(host == h or host.endswith("." + h) for h in HOSTS)


def _session():
    s = getattr(_tl, "s", None)
    if s is None:
        s = _tl.s = FetcherSession(impersonate="chrome", timeout=30, retries=1).__enter__()   # 重试由调用方按状态码处理
    return s


def _wait(host):
    gap = next((g for h, g in GAP.items() if host == h or host.endswith("." + h)), 0)
    if not gap:
        return
    with _lock:
        t = max(time.time(), _last.get(host, 0) + gap)
        _last[host] = t
    time.sleep(max(0, t - time.time()))


def request(method, url, params=None, headers=None, timeout=30, json_body=None):
    """发一个请求，返回 Resp；网络错误抛 FetchError。"""
    _wait(urlsplit(url).hostname or "")
    kw = {k: v for k, v in (("params", params), ("headers", headers), ("timeout", timeout)) if v is not None}
    try:
        r = _session().post(url, json=json_body, **kw) if method == "POST" else _session().get(url, **kw)
    except Exception as e:                                       # curl_cffi 的网络错误
        raise FetchError(f"{type(e).__name__}: {e}") from e
    return Resp(r)


def post_json(url, body, headers=None, tries=3, timeout=30):
    """POST JSON 并解析返回的 JSON；失败返回 None。"""
    for k in range(tries):
        try:
            r = request("POST", url, headers=headers, timeout=timeout, json_body=body)
            if r.status_code == 200:
                return r.json()
        except (FetchError, ValueError):
            pass
        time.sleep(1.5 * (k + 1))
    return None


def chrome_path():
    """本机的 Chrome / Chromium：CHROME_PATH 环境变量、系统里的 Chrome、Playwright 自带的 Chromium。"""
    for p in (os.environ.get("CHROME_PATH"), shutil.which("google-chrome"), shutil.which("google-chrome-stable"), shutil.which("chromium"),
              shutil.which("chromium-browser"), *sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))):
        if p and os.path.exists(p):
            return p
    return None


def browser(**kw):
    """Scrapling 的 StealthySession（真实浏览器，会运行网页里的 JS）；没有浏览器或没装 Scrapling 时返回 None。用法：with browser() as b: b.fetch(url)。"""
    exe = chrome_path()
    if FetcherSession is None or not exe:
        return None
    from scrapling.fetchers import StealthySession
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    opts = {"headless": True, "executable_path": exe, "disable_resources": True, "google_search": False, "timeout": 45000}
    if proxy:
        opts["proxy"] = {"server": proxy}
    return StealthySession(**{**opts, **kw})

"""公共工具：HTTP 重试、数据目录读写、任务排程状态、腾讯行情批量查询。"""
import datetime as dt
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SITE = ROOT / "site"
DATA.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
S = requests.Session()
S.headers["User-Agent"] = UA
BJ = dt.timezone(dt.timedelta(hours=8))


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def get(url, params=None, headers=None, tries=4, timeout=30, stop=(400, 403, 404, 422)):
    """GET 并重试；返回 Response 或 None。stop 里的状态码不重试。"""
    for k in range(tries):
        try:
            r = S.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in stop:
                return None
            if r.status_code == 429:
                time.sleep(20 * (k + 1))
                continue
        except requests.RequestException:
            pass
        time.sleep(1.5 * (k + 1))
    return None


def jget(url, params=None, headers=None, **kw):
    r = get(url, params, headers, **kw)
    if r is None:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def load(name, default=None):
    p = DATA / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return default


def save(name, obj):
    tmp = DATA / (name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(DATA / name)


def now():
    return dt.datetime.now(dt.timezone.utc)


def bj_now():
    return now().astimezone(BJ)


# ----- 任务排程：data/state.json 记录每项任务上次成功的时间 -----
def state():
    return load("state.json", {})


def due(task, hours, force=False):
    """距离上次成功超过 hours 小时（或从没跑过）时返回 True。FORCE=1 时全部重跑。"""
    if force or os.environ.get("FORCE") == "1":
        return True
    last = state().get(task)
    if not last:
        return True
    return (now() - dt.datetime.fromisoformat(last)).total_seconds() >= hours * 3600 - 300


def done(task):
    st = state()
    st[task] = now().isoformat(timespec="seconds")
    save("state.json", st)


def num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def r2(x):
    return None if x is None else round(float(x), 2)


# ----- 腾讯行情 -----
def tx_quotes(codes, lanes=4):
    """批量查询 qt.gtimg.cn，返回 {代码: 字段列表}。每次 60 个，lanes 路并发；
    腾讯偶尔限流（返回 403 或只返回一部分），没拿到的再分成小批、降低并发重试两轮。"""
    def one(part):
        r = get("https://qt.gtimg.cn/q=" + ",".join(part), timeout=20, stop=(400, 404, 422))
        out = {}
        if r is None:
            return out
        for line in r.content.decode("gbk", "ignore").split(";"):
            m = re.match(r'\s*v_(\w[\w.]*)="([^"]*)"', line)
            if m and "~" in m.group(2):
                out[m.group(1)] = m.group(2).split("~")
        return out

    res, todo = {}, list(codes)
    for size, n, pause in ((60, lanes, 0), (30, 2, 5), (15, 1, 15)):
        if not todo:
            break
        time.sleep(pause)
        with ThreadPoolExecutor(n) as ex:
            for d in ex.map(one, [todo[i:i + size] for i in range(0, len(todo), size)]):
                res.update(d)
        todo = [c for c in todo if c not in res]
    return res


def close_date():
    """a.json 是一个交易日收盘后的完整行情时，返回那个交易日（YYYY-MM-DD），否则 None。
    收盘当天 15:30 之后、或者第二天开盘前运行都算，这样当天晚上没跑成的，第二天早上还能补上。"""
    a = load("a.json", {})
    asof = a.get("asof", "")
    if len(a.get("rows", [])) < 5000 or asof[11:] < "15:00":
        return None
    t = bj_now()
    if asof[:10] == t.strftime("%Y-%m-%d") and t.hour * 60 + t.minute < 15 * 60 + 30:   # 当天 15:30 之前资金等数据可能还没定稿
        return None
    return asof[:10]


def a_sym(code):
    """A 股代码 → 腾讯代码（sh/sz/bj 前缀）。"""
    if code.startswith(("92", "4", "8")):
        return "bj" + code
    if code.startswith(("6", "9")):
        return "sh" + code
    return "sz" + code


def et_ms(s):
    """美东时间字符串 → UTC 毫秒（3 月第二个周日至 11 月第一个周日为夏令时）。"""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):?(\d{2})?", s or "")
    if not m:
        return 0
    t = dt.datetime(*map(int, m.groups(default="0")))
    y = t.year
    second_sun_mar = 8 + (6 - dt.date(y, 3, 8).weekday()) % 7
    first_sun_nov = 1 + (6 - dt.date(y, 11, 1).weekday()) % 7
    dst = dt.datetime(y, 3, second_sun_mar, 2) <= t < dt.datetime(y, 11, first_sun_nov, 2)
    return int(t.replace(tzinfo=dt.timezone(dt.timedelta(hours=-4 if dst else -5))).timestamp() * 1000)


def bj_ms(s):
    m = re.match(r"(\d{4})[-/](\d{2})[-/](\d{2})\s+(\d{2}):(\d{2}):?(\d{2})?", s or "")
    if not m:
        return 0
    return int(dt.datetime(*map(int, m.groups(default="0")), tzinfo=BJ).timestamp() * 1000)


def tx_daily(sym, kind="newfqkline", count=640, pages=1, adj="qfq"):
    """腾讯日K，按截止日期向前翻页；返回 [[日期, 开, 收, 高, 低, 量, 额], ...]。"""
    out, end = [], ""
    for _ in range(pages):
        d = jget(f"https://web.ifzq.gtimg.cn/appstock/app/{kind}/get", {"param": f"{sym},day,,{end},{count},{adj}"})
        try:
            x = d["data"][sym]
            k = x.get("qfqday") or x.get("day") or []
        except (TypeError, KeyError):
            break
        k = [r for r in k if not out or r[0] < out[0][0]]
        if not k:
            break
        out = [[r[0], r2(r[1]), r2(r[2]), r2(r[3]), r2(r[4]), round(num(r[5]) or 0), round(num(r[8]) or 0) if len(r) > 8 else 0] for r in k] + out
        if len(k) < count - 10:
            break
        end = (dt.date.fromisoformat(k[0][0]) - dt.timedelta(days=1)).isoformat()
    return out


def merge_bars(old, new, keep=None):
    """按日期合并K线：新数据覆盖同日旧数据。keep 为保留的最近根数。"""
    by = {r[0]: r for r in old or []}
    for r in new or []:
        by[r[0]] = r
    rows = [by[k] for k in sorted(by)]
    return rows[-keep:] if keep else rows

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gp —— A 股数据命令行，把六个开源库包成一组中文命令。

  gp q 600519 000001          实时行情（easyquotation：新浪，失败换腾讯）；加 --book 看五档
  gp k 600519 -n 20           日K（akshare：腾讯，失败换新浪；--src tdx 走通达信 mootdx）
  gp tdx 600519               通达信行情（mootdx，需要能直连通达信服务器 TCP 7709）；--ticks 分笔，--minute 分时
  gp zt [20261113]            涨停池（akshare：东方财富）；--kind zb 炸板 / dt 跌停 / qs 强势
  gp lhb [20261113]           龙虎榜（akshare：东方财富）
  gp wc "今日涨停，非ST"       问财选股（pywencai，需要你自己的问财 cookie：环境变量 WENCAI_COOKIE）
  gp js 600519 | gp js 茅台    stock-api（Node）行情 / 搜索；--k 20 看日K
  gp bt 600519                操盘线 B/S 策略回测（akquant 引擎：次日开盘成交、T+1、手续费、整手）
  gp ak 函数名 参数=值 ...      直接调用任意 akshare 函数；gp ak --search 解禁 按关键词找函数
  gp doctor                   体检：六个库是否装好、各数据源能否连通

代码可以写 600519 / sh600519 / 600519.SH，也可以写名称或拼音首字母（茅台、gzmt）。
指数请带前缀：sh000001 上证指数、sz399001 深证成指、sz399006 创业板指。
所有命令加 --json 输出 JSON，方便程序或 Claude 调用。
数据来自公开行情接口，仅供研究参考，不构成投资建议。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import warnings

warnings.filterwarnings("ignore")
os.environ.setdefault("TQDM_DISABLE", "1")          # akshare 的进度条不打印
VERSION = "1.0.0"
HOME = os.environ.get("STOCK_TOOLS_HOME") or os.path.join(os.path.expanduser("~"), ".stock-tools")
try:
    sys.stdout.reconfigure(errors="replace")         # Windows 管道输出时遇到 GBK 编码不了的字符不报错
except Exception:
    pass


class GpError(Exception):
    pass


def bj_now():
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)


def ymd(d):
    return d.strftime("%Y%m%d")


# ---------- 代码 ----------
def norm(code):
    """600519 / sh600519 / SH600519 / 600519.SH / 600519.SS -> sh600519；不是代码返回 None。"""
    s = (code or "").strip().lower().replace(".", "")
    m = re.fullmatch(r"(sh|sz|bj)?(\d{6})(sh|sz|bj|ss)?", s)
    if not m:
        return None
    pre = m.group(1) or {"ss": "sh"}.get(m.group(3) or "", m.group(3))
    num = m.group(2)
    if not pre:
        if num.startswith("92") or num[0] in "48":
            pre = "bj"
        elif num[0] in "569":
            pre = "sh"
        else:
            pre = "sz"
    return pre + num


def search(q, n=8):
    """腾讯证券搜索：名称、拼音首字母都行。返回 [(代码, 名称, 类型)]，只保留沪深京。"""
    import codecs
    import requests
    r = requests.get("https://smartbox.gtimg.cn/s3/", params={"v": 2, "q": q, "t": "all", "c": 1}, timeout=8)
    s = codecs.decode(r.text, "unicode_escape")
    m = re.search(r'v_hint="(.*)"', s)
    out = []
    for item in (m.group(1).split("^") if m and m.group(1) and m.group(1) != "N" else []):
        f = item.split("~")
        if len(f) >= 5 and f[0] in ("sh", "sz", "bj"):
            out.append((f[0] + f[1], f[2], f[4]))
    return out[:n]


def resolve(arg):
    s = norm(arg)
    if s:
        return s
    hits = search(arg)
    if not hits:
        raise GpError(f"找不到“{arg}”。可以直接写 6 位代码，指数请带前缀（如 sh000001）。")
    pick = next((h for h in hits if h[1] == arg), None) or next((h for h in hits if h[2] == "GP-A"), None) or hits[0]
    return pick[0]


def lot_of(sym):
    return 200 if re.fullmatch(r"sh68[89]\d{3}", sym) else 100        # 科创板最少 200 股


# ---------- 输出 ----------
def is_index(sym):
    return bool(re.fullmatch(r"(sh000|sz399|bj899)\d{3}", sym or ""))


def dec(sym):
    """价格小数位：ETF / LOF / 可转债按交易所规则是三位，股票和指数两位。"""
    return 3 if re.fullmatch(r"(sh5\d|sz1[5-8]|sh11|sz12)\d{4}", sym or "") else 2


def fp(x, d=2):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{float(x):.{d}f}"


def fyuan(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    x = float(x); a = abs(x)
    return f"{x / 1e8:.2f}亿" if a >= 1e8 else f"{x / 1e4:.2f}万" if a >= 1e4 else f"{x:.2f}"


def fshou(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    x = float(x); a = abs(x)
    return f"{x / 1e8:.2f}亿手" if a >= 1e8 else f"{x / 1e4:.2f}万手" if a >= 1e4 else f"{x:.0f}手"


def fpct(x, sign=True):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{float(x):+.2f}%" if sign else f"{float(x):.2f}%"


def jsonable(v):
    if v is None:
        return None
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def show(rows, cols, a, title=None, raw=None):
    """rows：列表的列表（已格式化）；--json 时输出 raw（未格式化的字典列表）或按列名组装。"""
    if getattr(a, "json", False):
        data = raw if raw is not None else [dict(zip(cols, r)) for r in rows]
        print(json.dumps(data, ensure_ascii=False, default=jsonable, indent=1))
        return
    import pandas as pd
    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.width", 320)
    pd.set_option("display.max_columns", 60)
    pd.set_option("display.max_colwidth", 46)
    pd.set_option("display.max_rows", 2000)
    if title:
        print(title)
    if not rows:
        print("（没有数据）")
        return
    print(pd.DataFrame(rows, columns=cols).to_string(index=False))


def show_df(df, a, title=None, n=None, cols=None):
    if cols:
        cols = [c for c in cols if c in df.columns]
        if cols:
            df = df[cols]
    if n:
        df = df.head(n)
    if getattr(a, "json", False):
        print(json.dumps([{k: jsonable(v) for k, v in r.items()} for r in df.to_dict("records")], ensure_ascii=False, default=jsonable, indent=1))
        return
    show(df.values.tolist(), list(df.columns), a, title)


# ---------- 数据 ----------
def daily(sym, start, end, fq="qfq"):
    """日K（akshare）：腾讯，失败换新浪。返回 DataFrame：date open high low close volume(股) amount(元)。"""
    import akshare as ak
    import pandas as pd
    errs = []
    adj = "" if fq == "none" else fq
    for name, fn in (("腾讯", lambda: ak.stock_zh_a_hist_tx(symbol=sym, start_date=ymd(start), end_date=ymd(end), adjust=adj)),
                     ("新浪", lambda: ak.stock_zh_a_daily_sina(symbol=sym, start_date=ymd(start), end_date=ymd(end), adjust=adj))):
        try:
            df = fn()
            if df is not None and len(df):
                df = df.copy()
                df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                for c in ("open", "high", "low", "close", "volume", "amount"):
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                df.attrs["src"] = name
                return df[["date", "open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)
        except Exception as e:
            errs.append(f"{name}: {type(e).__name__} {str(e)[:80]}")
    raise GpError("日K 获取失败 —— " + "；".join(errs))


def tdx_client():
    from mootdx.consts import HQ_HOSTS
    from mootdx.quotes import Quotes
    for h in HQ_HOSTS[:16]:
        name, ip, port = h if len(h) == 3 else ("", *h)
        try:
            socket.create_connection((ip, port), timeout=1.5).close()
        except OSError:
            continue
        return Quotes.factory(market="std", server=(ip, port), timeout=10), f"{name} {ip}:{port}"
    raise GpError("连不上通达信行情服务器（TCP 7709）。Claude 云端会话不允许直连 TCP，请在自己电脑上运行；电脑上也连不上时检查防火墙或代理。")


def last_days(date, k=10):
    if date:
        return [date]
    d = bj_now().date()
    out = []
    while len(out) < k:
        if d.weekday() < 5:
            out.append(ymd(d))
        d -= dt.timedelta(days=1)
    return out


# ---------- 命令 ----------
def cmd_q(a):
    import easyquotation
    syms = [resolve(c) for c in a.codes]
    data, used, errs = {}, None, []
    for src in ("sina", "tencent"):
        try:
            data = easyquotation.use(src).real(syms, prefix=True) or {}
            if data:
                used = src
                break
        except Exception as e:
            errs.append(f"{src}: {e}")
    if not data:
        raise GpError("行情获取失败：" + "；".join(errs))
    rows, raw = [], []
    for s in syms:
        d = data.get(s) or data.get(s[2:])
        if not d:
            rows.append([s, "（没有行情）"] + ["-"] * 10)
            continue
        if used == "sina":
            vol, amt, ts = d.get("turnover"), d.get("volume"), f'{d.get("date", "")} {d.get("time", "")}'
        else:
            vol, amt, ts = d.get("volume"), d.get("成交额(万)"), str(d.get("datetime", ""))
        now, pc, k = d.get("now"), d.get("close"), dec(s)
        ch = (now - pc) if now and pc else None
        pct = ch / pc * 100 if ch is not None and pc else None
        shou = (vol if is_index(s) else vol / 100) if vol else None                 # 指数的量本来就是手，个股是股
        rows.append([s, d.get("name"), fp(now, k), (f"{ch:+.{k}f}" if ch is not None else "-"), fpct(pct), fp(d.get("open"), k), fp(d.get("high"), k), fp(d.get("low"), k), fp(pc, k),
                     fshou(shou), fyuan(amt), ts.strip()])
        raw.append({"代码": s, "名称": d.get("name"), "现价": now, "涨跌": round(ch, 3) if ch is not None else None, "涨跌幅%": round(pct, 2) if pct is not None else None,
                    "今开": d.get("open"), "最高": d.get("high"), "最低": d.get("low"), "昨收": pc, "成交量手": round(shou, 2) if shou else None, "成交额元": amt,
                    "时间": ts.strip(), "来源": used,
                    "五档": {"买": [[d.get(f"bid{i}"), round((d.get(f"bid{i}_volume") or 0) / 100, 2)] for i in range(1, 6)],
                            "卖": [[d.get(f"ask{i}"), round((d.get(f"ask{i}_volume") or 0) / 100, 2)] for i in range(1, 6)]}})
    show(rows, ["代码", "名称", "现价", "涨跌", "涨跌幅", "今开", "最高", "最低", "昨收", "成交量", "成交额", "时间"], a,
         f"实时行情（easyquotation · {'新浪' if used == 'sina' else '腾讯'}）", raw)
    if a.book and not a.json:
        for r in raw:
            print(f"\n{r['代码']} {r['名称']} 五档（手 / 金额）")
            k = dec(r["代码"])
            book = [[f"卖{i}", fp(p, k), f"{v:.2f}".rstrip("0").rstrip("."), fyuan((p or 0) * v * 100)] for i, (p, v) in reversed(list(enumerate(r["五档"]["卖"], 1)))]
            book += [[f"买{i}", fp(p, k), f"{v:.2f}".rstrip("0").rstrip("."), fyuan((p or 0) * v * 100)] for i, (p, v) in enumerate(r["五档"]["买"], 1)]
            show(book, ["档位", "价格", "手数", "金额"], a)


def cmd_k(a):
    s = resolve(a.code)
    if a.src == "tdx":
        client, host = tdx_client()
        df = client.bars(symbol=s[2:], frequency=9, offset=a.n)
        if df is None or not len(df):
            raise GpError("通达信没有返回 K 线")
        df = df.reset_index(drop=True)
        rows, prev = [], None
        for r in df.to_dict("records"):
            c = r["close"]
            k = dec(s)
            rows.append([str(r.get("datetime", ""))[:10], fp(r["open"], k), fp(r["high"], k), fp(r["low"], k), fp(c, k), fpct((c / prev - 1) * 100) if prev else "-",
                         fshou(r.get("vol")), fyuan(r.get("amount"))])
            prev = c
        show(rows, ["日期", "开盘", "最高", "最低", "收盘", "涨跌幅", "成交量", "成交额"], a, f"{s} 日K（mootdx · 通达信 {host}，不复权）")
        return
    end = bj_now().date()
    df = daily(s, end - dt.timedelta(days=int(a.n * 1.6) + 20), end, a.fq)
    df["pct"] = df["close"].pct_change() * 100
    df = df.tail(a.n)
    k = dec(s)
    rows = [[r["date"], fp(r["open"], k), fp(r["high"], k), fp(r["low"], k), fp(r["close"], k), fpct(r["pct"]), fshou(r["volume"] / 100), fyuan(r["amount"])] for r in df.to_dict("records")]
    raw = [{"日期": r["date"], "开盘": r["open"], "最高": r["high"], "最低": r["low"], "收盘": r["close"], "涨跌幅%": None if math.isnan(r["pct"]) else round(r["pct"], 2),
            "成交量手": round(r["volume"] / 100, 2), "成交额元": r["amount"]} for r in df.to_dict("records")]
    fq = {"qfq": "前复权", "hfq": "后复权", "none": "不复权"}[a.fq]
    show(rows, ["日期", "开盘", "最高", "最低", "收盘", "涨跌幅", "成交量", "成交额"], a, f"{s} 日K（akshare · {df.attrs.get('src', '')}，{fq}）", raw)


def cmd_tdx(a):
    client, host = tdx_client()
    syms = [resolve(c) for c in a.codes]
    if a.ticks or a.minute:
        s = syms[0]
        if a.ticks:
            df = client.transaction(symbol=s[2:], start=0, offset=a.n)
            show_df(df, a, f"{s} 分笔成交（mootdx · {host}）")
        else:
            df = client.minute(symbol=s[2:])
            show_df(df.tail(a.n) if df is not None else df, a, f"{s} 分时（mootdx · {host}）")
        return
    df = client.quotes(symbol=[s[2:] for s in syms])
    cols = [c for c in ("code", "price", "last_close", "open", "high", "low", "vol", "amount", "bid1", "bid_vol1", "ask1", "ask_vol1", "servertime") if c in df.columns]
    show_df(df[cols], a, f"通达信行情（mootdx · {host}）")


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def cmd_zt(a):
    import akshare as ak
    fns = {"zt": (ak.stock_zt_pool_em, "涨停池"), "zb": (ak.stock_zt_pool_zbgc_em, "炸板池"), "dt": (ak.stock_zt_pool_dtgc_em, "跌停池"), "qs": (ak.stock_zt_pool_strong_em, "强势股池")}
    fn, label = fns[a.kind]
    df, used = None, None
    for d in last_days(a.date):
        try:
            df = fn(date=d)
        except Exception:
            df = None
        if df is not None and len(df):
            used = d
            break
    if df is None or not len(df):
        raise GpError(f"{label}没有数据（非交易日或接口暂时不可用）")
    if a.kind == "zt" and "连板数" in df.columns:
        df = df.sort_values(["连板数", "首次封板时间"], ascending=[False, True])
    if a.json:
        show_df(df, a, n=a.n)
        return
    money = ("成交额", "封板资金", "封单资金", "流通市值", "总市值", "板上成交额")
    pref = ["代码", "名称", "连板数", "连续跌停", "涨跌幅", "最新价", "成交额", "封板资金", "封单资金", "首次封板时间", "最后封板时间", "炸板次数", "开板次数", "涨停统计", "换手率", "所属行业", "入选理由"]
    cols = [c for c in pref if c in df.columns]
    rows = []
    for r in df.head(a.n).to_dict("records"):
        row = []
        for c in cols:
            v = r[c]
            if c in money:
                v = fyuan(_num(v))
            elif c in ("涨跌幅", "换手率"):
                v = fpct(_num(v), c == "涨跌幅")
            elif c == "最新价":
                v = fp(_num(v), dec(norm(str(r.get("代码", ""))) or ""))
            elif c.endswith("时间") and isinstance(v, str) and len(v) == 6:
                v = f"{v[:2]}:{v[2:4]}:{v[4:]}"
            row.append(v)
        rows.append(row)
    show(rows, cols, a, f"{label} {used[:4]}-{used[4:6]}-{used[6:]}（akshare · 东方财富，共 {len(df)} 只）")


def cmd_lhb(a):
    import akshare as ak
    df, used = None, None
    for d in last_days(a.date):
        try:
            df = ak.stock_lhb_detail_em(start_date=d, end_date=d)
        except Exception:
            df = None
        if df is not None and len(df):
            used = d
            break
    if df is None or not len(df):
        raise GpError("龙虎榜没有数据（当天还没公布或非交易日）")
    g = df.groupby("代码", sort=False).agg({"名称": "first", "收盘价": "first", "涨跌幅": "first", "龙虎榜净买额": "first", "龙虎榜成交额": "first", "解读": "first",
                                          "上榜原因": lambda x: "；".join(dict.fromkeys(x))}).reset_index()
    g = g.sort_values("龙虎榜净买额", ascending=False)
    if a.json:
        show_df(g, a, n=a.n)
        return
    rows = [[r["代码"], r["名称"], fp(r["收盘价"]), fpct(r["涨跌幅"]), fyuan(r["龙虎榜净买额"]), fyuan(r["龙虎榜成交额"]), r["解读"], r["上榜原因"][:40]] for r in g.head(a.n).to_dict("records")]
    show(rows, ["代码", "名称", "收盘", "涨跌幅", "净买额", "龙虎榜成交", "解读", "上榜原因"], a, f"龙虎榜 {used[:4]}-{used[4:6]}-{used[6:]}（akshare · 东方财富，{len(g)} 只）")


WENCAI_HELP = """问财（pywencai）需要你自己的问财登录 cookie，gp 不会代为登录：
  1. 用浏览器打开 https://www.iwencai.com 并登录自己的账号；
  2. 按 F12 打开开发者工具 → 网络（Network）→ 随便搜一句 → 点一个请求 → 复制请求头里的 Cookie 整段；
  3. 设成环境变量 WENCAI_COOKIE（只保存在你自己的电脑上，不要发给别人）：
       Mac / Linux：export WENCAI_COOKIE='复制的内容'   （写进 ~/.zshrc 或 ~/.bashrc 可长期生效）
       Windows：    setx WENCAI_COOKIE "复制的内容"    （重新打开终端后生效）
cookie 过期后重新复制一次即可。"""


def cmd_wc(a):
    cookie = os.environ.get("WENCAI_COOKIE", "").strip()
    if not cookie:
        raise GpError(WENCAI_HELP)
    import pandas as pd
    import pywencai
    res = pywencai.get(query=a.query, cookie=cookie, loop=a.all, query_type=a.type)
    if res is None:
        raise GpError("问财没有返回结果：cookie 可能过期了，或者这句问法没有匹配。\n" + WENCAI_HELP)
    if isinstance(res, pd.DataFrame):
        show_df(res, a, f"问财：{a.query}（pywencai，共 {len(res)} 条）", n=a.n)
        return
    if isinstance(res, dict):
        for k, v in res.items():
            if isinstance(v, pd.DataFrame):
                show_df(v, a, f"【{k}】", n=a.n)
            elif not a.json:
                print(f"【{k}】{str(v)[:800]}")
        return
    print(res)


def stock_api_cmd():
    exe = shutil.which("stock-api")
    if not exe:
        for p in (os.path.join(HOME, "node", "node_modules", ".bin", "stock-api"), os.path.join(HOME, "node", "node_modules", ".bin", "stock-api.cmd")):
            if os.path.exists(p):
                exe = p
                break
    if exe:
        return [exe]
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", "stock-api"]
    raise GpError("没有找到 stock-api：需要 Node.js 18+，然后运行 npm install -g stock-api（或重新运行 gp 的安装脚本）。")


def cmd_js(a):
    base = stock_api_cmd()
    codes = [norm(x) for x in a.args]
    if all(codes):
        su = [c.upper() for c in codes]
        cmd = base + (["get-klines", su[0], "--period", "day", "--count", str(a.k)] if a.k else ["get-stocks"] + su)
    else:
        cmd = base + ["search-stocks", " ".join(a.args)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if p.returncode != 0:
        raise GpError("stock-api 出错：" + (p.stderr or p.stdout).strip()[:400])
    data = json.loads(p.stdout)
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return
    if not isinstance(data, list):
        data = [data]
    if not data:
        print("（没有数据）")
        return
    if "date" in data[0]:
        k = dec(codes[0])
        rows = [[d["date"], fp(d.get("open"), k), fp(d.get("high"), k), fp(d.get("low"), k), fp(d.get("close"), k), fshou(d.get("volume"))] for d in data]
        show(rows, ["日期", "开盘", "最高", "最低", "收盘", "成交量"], a, f"{su[0]} 日K（stock-api · {data[0].get('source', '')}）")
    elif "now" in data[0]:
        rows = []
        for d in data:
            k = dec(norm(d.get("code") or "") or "")
            rows.append([d.get("code"), d.get("name"), fp(d.get("now"), k), fpct((d.get("percent") or 0) * 100), fp(d.get("high"), k), fp(d.get("low"), k), fp(d.get("yesterday"), k), d.get("source")])
        show(rows, ["代码", "名称", "现价", "涨跌幅", "最高", "最低", "昨收", "来源"], a, "实时行情（stock-api）")
    else:
        keys = list(data[0].keys())
        show([[d.get(k) for k in keys] for d in data], keys, a, "搜索结果（stock-api）")


# ---------- 回测（akquant） ----------
def _ema(x, p):
    k = 2 / (p + 1)
    e = x[0]
    out = []
    for v in x:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def cpx_signals(close):
    """操盘线 B/S（和网站“操盘”页同一公式，只用当天及以前的收盘价）：
    操盘线 TL = EMA(收盘, 13)；B = 收盘上穿 TL 且 MACD 的 DIF > DEA；S = 收盘下破 TL 且 DIF < DEA。"""
    n = len(close)
    tl = _ema(close, 13)
    dif = [a - b for a, b in zip(_ema(close, 12), _ema(close, 26))]
    dea = _ema(dif, 9)
    sig = [0] * n
    for i in range(1, n):
        if close[i] > tl[i] and close[i - 1] <= tl[i - 1] and dif[i] > dea[i]:
            sig[i] = 1
        elif close[i] < tl[i] and close[i - 1] >= tl[i - 1] and dif[i] < dea[i]:
            sig[i] = -1
    return sig


def ma_signals(close, f=5, s=20):
    def ma(k):
        return [sum(close[i - k + 1:i + 1]) / k if i >= k - 1 else None for i in range(len(close))]
    a, b = ma(f), ma(s)
    sig = [0] * len(close)
    for i in range(1, len(close)):
        if None in (a[i], b[i], a[i - 1], b[i - 1]):
            continue
        if a[i] > b[i] and a[i - 1] <= b[i - 1]:
            sig[i] = 1
        elif a[i] < b[i] and a[i - 1] >= b[i - 1]:
            sig[i] = -1
    return sig


def backtest(df, sym, strategy="cpx", cash=1000000.0, start_idx=34, quiet=True):
    """用 akquant 跑一遍：信号在当天收盘后产生，下一根 K 线开盘成交；A 股 T+1、低佣金费率、整手。
    df 要从 start_idx 之前多带一段做指标预热：信号用全部 K 线算（只用当天及以前的收盘价），回测只在 start_idx 之后进行。
    返回 (指标字典, 交易明细 DataFrame, 权益曲线 Series, 回测区间的 DataFrame, 回测区间的信号)。"""
    import akquant as aq
    from akquant import Strategy
    if quiet:                                                            # akquant 没有日志 handler 时会往标准输出打 INFO，先挂一个只输出警告的
        import logging
        lg = logging.getLogger("akquant")
        if not any(not isinstance(h, logging.NullHandler) for h in lg.handlers):
            h = logging.StreamHandler(sys.stderr)
            h.setLevel(logging.WARNING)
            lg.addHandler(h)
        lg.setLevel(logging.WARNING)
    close_all = df["close"].astype(float).tolist()
    start_idx = max(0, min(start_idx, len(close_all) - 5))
    if len(close_all) - start_idx < 5 or (strategy != "hold" and len(close_all) < 40):
        raise GpError(f"K 线太少（{len(close_all)} 根），至少要 40 根")
    sig_all = cpx_signals(close_all) if strategy == "cpx" else ma_signals(close_all) if strategy == "ma" else None
    dfe = df.iloc[start_idx:].reset_index(drop=True)
    close = close_all[start_idx:]
    sig = sig_all[start_idx:] if sig_all else [1] + [0] * (len(close) - 1)
    lot = lot_of(sym)
    if close[-1] * lot > cash * 0.985:
        raise GpError(f"资金 {cash:,.0f} 买不起一手（最新价 {close[-1]:.2f} × {lot} 股），用 --cash 调大，例如 --cash {math.ceil(close[-1] * lot * 5 / 1e4) * 1e4:.0f}")
    by_day = dict(zip(dfe["date"].astype(str).str[:10], sig))

    class Cpx(Strategy):
        def on_bar(self, bar):
            s = by_day.get(str(bar.timestamp_iso)[:10], 0)
            pos = self.get_position(bar.symbol)
            if s == 1 and pos == 0:
                q = int(self.cash * 0.985 / bar.close / 100) * 100
                if q >= lot:
                    self.buy(symbol=bar.symbol, quantity=q)
            elif s == -1 and pos > 0:
                self.close_position(symbol=bar.symbol)

    res = aq.run_backtest(data=dfe, strategy=Cpx, initial_cash=float(cash), symbols=sym, broker_profile="cn_stock_t1_low_fee", show_progress=False)
    m = res.metrics_df["value"].to_dict()
    hold = (close[-1] / close[0] - 1) * 100 if close[0] else None
    out = {
        "代码": sym, "策略": {"cpx": "操盘线B/S", "ma": "均线5/20金叉死叉", "hold": "买入持有"}[strategy],
        "开始": str(dfe["date"].iloc[0])[:10], "结束": str(dfe["date"].iloc[-1])[:10], "K线数": len(dfe),
        "初始资金": float(cash), "期末资产": round(float(m.get("end_market_value") or 0), 2),
        "策略收益%": round(float(m.get("total_return_pct") or 0), 2), "年化收益%": round(float(m.get("annualized_return") or 0) * 100, 2),
        "最大回撤%": round(float(m.get("max_drawdown_pct") or 0), 2), "夏普": round(float(m.get("sharpe_ratio") or 0), 2),
        "平仓交易数": int(m.get("closed_trade_count") or 0), "胜率%": round(float(m.get("win_rate") or 0), 2),
        "手续费": round(float(m.get("total_commission") or 0), 2), "持仓中": int(m.get("open_position_count") or 0) > 0,
        "同期持有收益%": round(hold, 2) if hold is not None else None,
        "买点数": sum(1 for x in sig if x == 1), "卖点数": sum(1 for x in sig if x == -1),
    }
    return out, res.trades_df, res.equity_curve, dfe, sig


def cmd_bt(a):
    s = resolve(a.code)
    end = dt.datetime.strptime(a.end, "%Y%m%d").date() if a.end else bj_now().date()
    start = dt.datetime.strptime(a.start, "%Y%m%d").date() if a.start else end - dt.timedelta(days=730)
    df = daily(s, start - dt.timedelta(days=90), end, "qfq")                 # 多取 3 个月给指标预热
    warm = int((df["date"] < start.isoformat()).sum())
    out, trades, *_ = backtest(df, s, a.strategy, a.cash, start_idx=max(34, warm) if a.strategy != "hold" else warm)
    out["数据来源"] = f"akshare · {df.attrs.get('src', '')}（前复权）"
    if a.json:
        out["交易"] = [{k: jsonable(v) for k, v in r.items()} for r in trades.to_dict("records")] if a.trades else None
        print(json.dumps(out, ensure_ascii=False, default=jsonable, indent=1))
        return
    print(f"{s} {out['策略']} 回测（akquant 引擎：信号收盘产生、次日开盘成交，T+1，低佣金费率，整手）")
    print(f"  区间        {out['开始']} ~ {out['结束']}    数据：{out['数据来源']}")
    print(f"  初始资金    {out['初始资金']:,.0f}    期末资产 {out['期末资产']:,.2f}{'（期末仍持仓）' if out['持仓中'] else ''}")
    print(f"  策略收益    {out['策略收益%']:+.2f}%    年化 {out['年化收益%']:+.2f}%    同期买入持有 {out['同期持有收益%']:+.2f}%")
    print(f"  最大回撤    {out['最大回撤%']:.2f}%    夏普 {out['夏普']:.2f}")
    print(f"  交易        平仓 {out['平仓交易数']} 次，胜率 {out['胜率%']:.2f}%，手续费 {out['手续费']:,.2f}；区间内买点 {out['买点数']} 个、卖点 {out['卖点数']} 个")
    if a.trades and len(trades):
        k = dec(s)
        rows = [[str(r["entry_time"])[:10], fp(r["entry_price"], k), str(r["exit_time"])[:10], fp(r["exit_price"], k), f"{r['quantity']:.0f}", fyuan(r["net_pnl"]), fpct(r["return_pct"])]
                for r in trades.to_dict("records")]
        show(rows, ["买入日", "买价", "卖出日", "卖价", "股数", "净盈亏", "收益率"], a, "\n逐笔交易")
    print("历史回测不代表未来收益，仅供研究参考。")


def cmd_ak(a):
    import akshare as ak
    if a.search:
        kw = a.search.lower()
        hits = []
        for name in dir(ak):
            fn = getattr(ak, name)
            if name.startswith("_") or not callable(fn):
                continue
            doc = (fn.__doc__ or "").strip().splitlines()
            first = doc[0].strip() if doc else ""
            if kw in name.lower() or kw in first.lower():
                hits.append([name, first[:60]])
        show(hits[:a.n], ["函数", "说明"], a, f"akshare 里和“{a.search}”有关的函数（{len(hits)} 个）")
        return
    if not a.func:
        raise GpError("用法：gp ak 函数名 参数=值 ...，例如 gp ak stock_zt_pool_em date=20261113；或 gp ak --search 涨停")
    fn = getattr(ak, a.func, None)
    if not callable(fn):
        raise GpError(f"akshare 里没有函数 {a.func}（akshare 改名很频繁，可以用 gp ak --search 关键词 找一下）")
    kw = {}
    for p in a.params:
        if "=" not in p:
            raise GpError(f"参数要写成 名称=值：{p}")
        k, v = p.split("=", 1)
        kw[k] = v
    res = fn(**kw)
    import pandas as pd
    if isinstance(res, pd.DataFrame):
        show_df(res, a, f"ak.{a.func}（共 {len(res)} 行）", n=a.n)
    else:
        print(json.dumps(res, ensure_ascii=False, default=jsonable, indent=1) if a.json else res)


def cmd_doctor(a):
    from importlib.metadata import PackageNotFoundError, version
    rows = []

    def add(item, ok, note):
        rows.append([item, "✓" if ok is True else "✗" if ok is False else "-", note])

    for pkg, use in (("akshare", "日K、涨停池、龙虎榜等上千个接口"), ("easyquotation", "新浪 / 腾讯实时行情"), ("mootdx", "通达信行情"),
                     ("pywencai", "问财选股"), ("akquant", "回测引擎")):
        try:
            add(pkg, True, f"{version(pkg)} · {use}")
        except PackageNotFoundError:
            add(pkg, False, f"没装 · pip install {pkg}")
    node = shutil.which("node")
    add("Node.js", bool(node), subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip() if node else "没装（stock-api 和问财要用）")
    try:
        cmd = stock_api_cmd()
        add("stock-api", True, " ".join(cmd[-1:]) if len(cmd) == 1 else "未全局安装，使用 npx 临时下载")
    except GpError as e:
        cmd = None
        add("stock-api", False, str(e))

    import requests

    def http(name, url, test, note_ok):
        t0 = time.time()
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
            ok = r.status_code == 200 and test(r.text)
            add(name, ok, f"{note_ok}，{(time.time() - t0) * 1000:.0f}ms" if ok else f"HTTP {r.status_code}")
        except Exception as e:
            add(name, False, f"{type(e).__name__}: {str(e)[:60]}")

    http("新浪行情", "https://hq.sinajs.cn/list=sh600519", lambda t: "贵州茅台" in t or "," in t, "easyquotation、akshare 可用")
    http("腾讯行情", "https://qt.gtimg.cn/q=sh600519", lambda t: "600519" in t, "easyquotation、akshare、stock-api 可用")
    http("东方财富数据中心", "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LICO_FN_CPD&columns=SECURITY_CODE&pageSize=1",
         lambda t: "SECURITY_CODE" in t or '"success":true' in t, "涨停池、龙虎榜、解禁等可用")
    http("东方财富行情 push2", "https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=f43,f58", lambda t: '"data"' in t and "null" not in t[:60], "akshare 的东财实时 / 历史接口可用")
    try:
        from mootdx.consts import HQ_HOSTS
        ok = None
        for h in HQ_HOSTS[:8]:
            name, ip, port = h if len(h) == 3 else ("", *h)
            t0 = time.time()
            try:
                socket.create_connection((ip, port), timeout=1.5).close()
                ok = f"{name} {ip} {(time.time() - t0) * 1000:.0f}ms"
                break
            except OSError:
                pass
        add("通达信 TCP 7709", bool(ok), ok or "连不上（Claude 云端会话不允许直连 TCP，gp tdx 请在电脑上用）")
    except Exception as e:
        add("通达信 TCP 7709", False, f"mootdx 不可用：{e}")
    add("问财 cookie", bool(os.environ.get("WENCAI_COOKIE")), "已设置 WENCAI_COOKIE" if os.environ.get("WENCAI_COOKIE") else "没设置：gp wc 需要你自己的问财 cookie（gp wc 看说明）")
    if cmd:
        try:
            p = subprocess.run(cmd + ["get-stock", "SH600519"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
            ok = p.returncode == 0 and '"now"' in p.stdout
            add("stock-api 取数", ok, "能取到行情" if ok else (p.stderr or p.stdout).strip()[:80])
        except Exception as e:
            add("stock-api 取数", False, str(e)[:80])
    show(rows, ["检查项", "结果", "说明"], a, f"gp {VERSION} 体检（Python {sys.version.split()[0]}）")


def main(argv=None):
    p = argparse.ArgumentParser(prog="gp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-V", "--version", action="version", version=f"gp {VERSION}")
    sub = p.add_subparsers(dest="cmd", metavar="命令")

    def sp(name, help_, aliases=()):
        x = sub.add_parser(name, help=help_, aliases=list(aliases), description=help_)
        x.add_argument("--json", action="store_true", help="输出 JSON")
        return x

    x = sp("q", "实时行情（easyquotation）", ["行情"])
    x.add_argument("codes", nargs="+", help="代码或名称，可以多个")
    x.add_argument("--book", action="store_true", help="显示五档盘口（手 / 金额）")
    x.set_defaults(fn=cmd_q)
    x = sp("k", "日K（akshare；--src tdx 走 mootdx）", ["K线"])
    x.add_argument("code")
    x.add_argument("-n", type=int, default=30, help="显示最近 n 天（默认 30）")
    x.add_argument("--src", choices=["ak", "tdx"], default="ak")
    x.add_argument("--fq", choices=["qfq", "hfq", "none"], default="qfq", help="复权方式（默认前复权）")
    x.set_defaults(fn=cmd_k)
    x = sp("tdx", "通达信行情 / 分笔 / 分时（mootdx）", ["通达信"])
    x.add_argument("codes", nargs="+")
    x.add_argument("--ticks", action="store_true", help="分笔成交")
    x.add_argument("--minute", action="store_true", help="分时")
    x.add_argument("-n", type=int, default=30)
    x.set_defaults(fn=cmd_tdx)
    x = sp("zt", "涨停池 / 炸板 / 跌停 / 强势股（akshare）", ["涨停"])
    x.add_argument("date", nargs="?", help="YYYYMMDD，默认最近一个交易日")
    x.add_argument("--kind", choices=["zt", "zb", "dt", "qs"], default="zt")
    x.add_argument("-n", type=int, default=100)
    x.set_defaults(fn=cmd_zt)
    x = sp("lhb", "龙虎榜（akshare）", ["龙虎榜"])
    x.add_argument("date", nargs="?", help="YYYYMMDD，默认最近一个交易日")
    x.add_argument("-n", type=int, default=100)
    x.set_defaults(fn=cmd_lhb)
    x = sp("wc", "问财选股（pywencai，需要 WENCAI_COOKIE）", ["问财", "wencai"])
    x.add_argument("query", help="自然语言条件，例如：今日涨停，非ST，连板天数从大到小")
    x.add_argument("--type", default="stock", help="stock / zhishu / fund / hkstock / usstock ...")
    x.add_argument("--all", action="store_true", help="翻页取全部结果")
    x.add_argument("-n", type=int, default=50)
    x.set_defaults(fn=cmd_wc)
    x = sp("js", "stock-api（Node）行情 / K线 / 搜索", ["stockapi"])
    x.add_argument("args", nargs="+", help="代码（可多个）或搜索关键词")
    x.add_argument("--k", type=int, default=0, help="取最近 k 根日K")
    x.set_defaults(fn=cmd_js)
    x = sp("bt", "策略回测（akquant）", ["回测"])
    x.add_argument("code")
    x.add_argument("--start", help="YYYYMMDD，默认两年前")
    x.add_argument("--end", help="YYYYMMDD，默认今天")
    x.add_argument("--cash", type=float, default=1000000.0, help="初始资金（默认 100 万）")
    x.add_argument("--strategy", choices=["cpx", "ma", "hold"], default="cpx", help="cpx 操盘线B/S（默认）/ ma 均线5/20 / hold 买入持有")
    x.add_argument("--trades", action="store_true", help="列出逐笔交易")
    x.set_defaults(fn=cmd_bt)
    x = sp("ak", "直接调用 akshare 函数", ["akshare"])
    x.add_argument("func", nargs="?")
    x.add_argument("params", nargs="*", help="参数=值")
    x.add_argument("--search", help="按关键词找函数")
    x.add_argument("-n", type=int, default=50)
    x.set_defaults(fn=cmd_ak)
    x = sp("doctor", "体检：库是否装好、数据源能否连通", ["体检"])
    x.set_defaults(fn=cmd_doctor)

    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        p.print_help()
        return 0
    try:
        a.fn(a)
        return 0
    except GpError as e:
        print(str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except ImportError as e:
        print(f"缺少依赖：{e}。运行 gp doctor 看看哪些没装。", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"出错了：{type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

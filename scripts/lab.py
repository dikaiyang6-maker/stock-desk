"""量化页：把六个开源库接进网站。每个交易日收盘后（和精选名单、资金监控同一时段）跑一次，结果写进 data/lab.json。

  akquant        操盘线 B/S 策略回测：宽基 ETF、当天出 B 点的个股（成交额前 30）、精选名单，对比同期买入持有。
                 回测代码和命令行 gp bt 是同一份（tools/gp.py），网站上的数和终端里跑出来的一致。
  akshare        机构调研（近 30 天）、大宗交易、龙虎榜（最近交易日）、限售解禁（未来 30 天），东方财富数据中心。
  easyquotation  新浪全市场行情，和本站的腾讯行情逐只核对收盘价。
  stock-api      几只大盘股的行情（Node），和腾讯行情核对。
  mootdx         GitHub 机房能否连上通达信行情服务器（TCP 7709）；连得上就取几只股票的行情核对。
  pywencai       问财预设选股：仓库 Secrets 里设置了 WENCAI_COOKIE（你自己登录问财后的 cookie）才运行，不设就不跑。
任何一项失败都不影响其它项，失败的沿用上一次的结果并标注。"""
import datetime as dt
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError, version

sys.path.insert(0, os.path.dirname(__file__))
from common import ROOT, bj_now, close_date, jget, load, log, save   # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import gp   # noqa: E402  回测和 gp bt 用同一份代码

ETFS = [("sh510300", "沪深300ETF"), ("sh510500", "中证500ETF"), ("sh512100", "中证1000ETF"), ("sz159915", "创业板ETF"),
        ("sh588000", "科创50ETF"), ("sh510050", "上证50ETF"), ("sh518880", "黄金ETF"), ("sh513100", "纳指ETF")]
BIG = ["sh600519", "sz300750", "sh601318", "sz000858", "sh600036"]          # 行情核对用的几只大盘股
WC_QUERIES = ["今日涨停，非ST，连续涨停天数从大到小", "近5日主力资金净流入从大到小排名前20，非ST", "市盈率TTM小于25，ROE大于15%，营业收入同比增长大于20%，非ST"]
BT_DAYS, WARM = 500, 60                                                     # 回测近 500 个交易日（约两年），前面多取 60 根给指标预热
CURVE_PTS = 90


def lab_due():
    """有一个交易日收盘后的完整行情、那天还没跑过时运行（节假日没有新收盘，不跑）。"""
    d = close_date()
    return bool(d) and load("lab.json", {}).get("date") != d


def _ver(pkg):
    if pkg == "pywencai" and gp.wencai_enhanced():
        return f"enhanced {gp.wencai_enhanced()}"
    try:
        return version(pkg)
    except PackageNotFoundError:
        return None


# ---------- akquant：回测 ----------
def _bars(sym, n=BT_DAYS + WARM):
    """腾讯日K（前复权），不做四舍五入（ETF 价格有三位小数）。"""
    import pandas as pd
    d = jget("https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get", {"param": f"{sym},day,,,{n},qfq"})
    try:
        x = d["data"][sym]
        k = x.get("qfqday") or x.get("day") or []
    except (TypeError, KeyError):
        return None
    rows = [[r[0], float(r[1]), float(r[3]), float(r[4]), float(r[2]), float(r[5] or 0) * 100] for r in k if len(r) >= 6]
    if not rows:
        return None
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])


def _bt_one(sym, name, df):
    if df is None or len(df) < 120:
        return None
    start = max(34, len(df) - BT_DAYS)
    out, _trades, eq, dfe, sig = gp.backtest(df, sym, "cpx", 1e6, start_idx=start)
    dates, closes = dfe["date"].tolist(), dfe["close"].astype(float).tolist()
    eqd = {str(ts)[:10]: float(v) for ts, v in eq.items()}
    curve, last = [], 1e6
    for d in dates:                                                         # 权益按日期对齐，缺的沿用前值
        last = eqd.get(d, last)
        curve.append(last)
    step = max(1, len(dates) // CURVE_PTS)
    idx = list(range(0, len(dates), step))
    if idx[-1] != len(dates) - 1:
        idx.append(len(dates) - 1)
    li = next((i for i in range(len(sig) - 1, -1, -1) if sig[i]), None)
    return [sym, name, out["策略收益%"], out["同期持有收益%"], out["年化收益%"], out["最大回撤%"], out["夏普"], out["平仓交易数"], out["胜率%"],
            1 if out["持仓中"] else 0, dates[li] if li is not None else None, sig[li] if li is not None else 0, sig[-1],
            [round(curve[i] / 1e6 * 1000) for i in idx], [round(closes[i] / closes[0] * 1000) for i in idx], dates[0], dates[-1]]


BT_COLS = ["sym", "name", "ret", "hold", "ann", "mdd", "sharpe", "n", "win", "pos", "lastD", "lastS", "today", "cs", "ch", "d0", "d1"]


def run_backtests():
    a = load("a.json", {})
    I = {c: i for i, c in enumerate(a.get("cols", []))}
    by = {r[I["code"]]: r for r in a.get("rows", [])} if I else {}
    sc = load("screen.json", {})
    tc = sc.get("techCols") or []
    bs_i = tc.index("bs") if "bs" in tc else None
    bsl = [c for c, v in (sc.get("tech") or {}).items() if bs_i is not None and v[bs_i] == 1 and c in by]
    bsl.sort(key=lambda c: -(by[c][I["amt"]] or 0))
    pk, seen = [], set()
    for t in (load("picks.json", {}).get("tiers") or []):
        for it in t.get("items", []):
            if it[0] not in seen and it[0] in by:
                seen.add(it[0]); pk.append(it[0])
    groups = {"etf": [(s, n) for s, n in ETFS],
              "bs": [(gp.norm(c), by[c][I["name"]]) for c in bsl[:30]],
              "pk": [(gp.norm(c), by[c][I["name"]]) for c in pk[:20]]}
    import logging
    logging.getLogger("akquant").setLevel(logging.ERROR)                   # 网站任务里不打印逐笔的拒单警告
    syms = sorted({s for g in groups.values() for s, _ in g})
    with ThreadPoolExecutor(6) as ex:
        bars = dict(zip(syms, ex.map(_bars, syms)))
    out, fails = {}, []
    for g, items in groups.items():
        rows = []
        for s, n in items:
            try:
                r = _bt_one(s, n, bars.get(s))
                if r:
                    rows.append(r)
                else:
                    fails.append(s)
            except gp.GpError:
                fails.append(s)
            except Exception:
                traceback.print_exc(); fails.append(s)
        out[g] = rows
    n = sum(len(v) for v in out.values())
    return {"cols": BT_COLS, **out, "bsDate": sc.get("techDate"), "n": n, "fail": fails}, n > 0


# ---------- akshare：东方财富数据中心 ----------
def _f(x, k=2):
    try:
        v = float(x)
        return None if v != v else round(v, k)
    except (TypeError, ValueError):
        return None


def ak_data(d):
    import akshare as ak
    d8 = d.replace("-", "")
    res, errs = {}, []

    def one(key, fn):
        try:
            res[key] = fn()
        except Exception as e:
            errs.append(f"{key}: {type(e).__name__} {str(e)[:80]}")

    def jgdy():
        df = ak.stock_jgdy_tj_em(date=(dt.date.fromisoformat(d) - dt.timedelta(days=30)).strftime("%Y%m%d"))
        g = {}
        for r in df.to_dict("records"):
            x = g.setdefault(r["代码"], {"name": r["名称"], "n": 0, "org": 0, "last": "", "how": "", "p": r["最新价"], "chg": r["涨跌幅"]})
            x["n"] += 1; x["org"] += int(r["接待机构数量"] or 0)
            day = str(r["接待日期"])[:10]
            if day > x["last"]:
                x["last"], x["how"] = day, "、".join([w for w in str(r["接待方式"] or "").replace("，", ",").split(",") if w.strip()][:3])
        rows = sorted(g.items(), key=lambda kv: (-kv[1]["org"], -kv[1]["n"]))[:40]
        return {"t": f"近 30 天（截至 {d}）共 {len(g)} 家公司被调研", "cols": ["code", "name", "n", "org", "last", "how", "p", "chg"],
                "rows": [[c, x["name"], x["n"], x["org"], x["last"], x["how"], _f(x["p"]), _f(x["chg"])] for c, x in rows]}

    def dzjy():
        df = ak.stock_dzjy_mrmx(symbol="A股", start_date=d8, end_date=d8)
        df = df.sort_values("成交额", ascending=False)
        return {"t": f"{d} 共 {len(df)} 笔，成交 {df['成交额'].sum() / 1e8:.2f} 亿元", "cols": ["code", "name", "px", "close", "prem", "amt", "buy", "sell"],
                "rows": [[str(r["证券代码"]), r["证券简称"], _f(r["成交价"], 3), _f(r["收盘价"], 3), _f((r["折溢率"] or 0) * 100), _f(r["成交额"], 0),
                          str(r["买方营业部"] or "")[:24], str(r["卖方营业部"] or "")[:24]] for r in df.head(40).to_dict("records")]}

    def lhb():
        df = ak.stock_lhb_detail_em(start_date=d8, end_date=d8)
        g = df.groupby("代码", sort=False).agg({"名称": "first", "收盘价": "first", "涨跌幅": "first", "龙虎榜净买额": "first", "龙虎榜成交额": "first",
                                              "解读": "first", "上榜原因": lambda x: "；".join(dict.fromkeys(x))}).reset_index()
        g = g.sort_values("龙虎榜净买额", ascending=False)
        return {"t": f"{d} 共 {len(g)} 只上榜", "cols": ["code", "name", "close", "chg", "net", "amt", "jd", "why"],
                "rows": [[r["代码"], r["名称"], _f(r["收盘价"]), _f(r["涨跌幅"]), _f(r["龙虎榜净买额"], 0), _f(r["龙虎榜成交额"], 0), str(r["解读"] or "")[:30],
                          str(r["上榜原因"] or "")[:60]] for r in g.head(40).to_dict("records")]}

    def jj():
        t0 = bj_now().date()
        df = ak.stock_restricted_release_detail_em(start_date=t0.strftime("%Y%m%d"), end_date=(t0 + dt.timedelta(days=30)).strftime("%Y%m%d"))
        df = df.sort_values("实际解禁市值", ascending=False)
        return {"t": f"未来 30 天（{t0.isoformat()} 起）共 {len(df)} 批，合计 {df['实际解禁市值'].sum() / 1e8:.2f} 亿元",
                "cols": ["code", "name", "day", "kind", "mv", "pct"],
                "rows": [[str(r["股票代码"]), r["股票简称"], str(r["解禁时间"])[:10], str(r["限售股类型"] or "")[:14], _f(r["实际解禁市值"], 0),
                          _f((r["占解禁前流通市值比例"] or 0) * 100)] for r in df.head(40).to_dict("records")]}

    for k, fn in (("jgdy", jgdy), ("dzjy", dzjy), ("lhb", lhb), ("jj", jj)):
        one(k, fn)
    return res, errs


# ---------- 行情核对：easyquotation（新浪）、stock-api、mootdx ----------
def _tx_prices():
    a = load("a.json", {})
    I = {c: i for i, c in enumerate(a.get("cols", []))}
    return {r[I["code"]]: (r[I["name"]], r[I["price"]]) for r in a.get("rows", [])} if I else {}, (a.get("asof") or "")


def chk_sina(tx):
    import easyquotation
    codes = [gp.norm(c) for c in tx]
    q = easyquotation.use("sina")
    snap = q.real(codes, prefix=True) or {}
    n = same = 0
    diff = []
    for c, (name, p) in tx.items():
        s = snap.get(gp.norm(c))
        if not s or not p or not s.get("now"):
            continue
        n += 1
        if abs(float(s["now"]) - float(p)) < 0.0051:
            same += 1
        elif len(diff) < 8:
            diff.append([c, name, p, float(s["now"])])
    return {"n": n, "same": same, "diff": diff}


def stock_api_bin():
    for c in ("stock-api",):
        p = shutil.which(c)
        if p:
            return [p]
    npx = shutil.which("npx")
    return [npx, "-y", "stock-api"] if npx else None


def chk_stock_api(tx):
    cmd = stock_api_bin()
    if not cmd:
        raise RuntimeError("没有 Node.js / stock-api")
    p = subprocess.run(cmd + ["get-stocks"] + [s.upper() for s in BIG], capture_output=True, text=True, encoding="utf-8", timeout=90)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:120])
    import json
    rows = []
    for d in json.loads(p.stdout):
        c = (d.get("code") or "")[2:]
        name, txp = tx.get(c, (d.get("name"), None))
        rows.append([c, d.get("name") or name, d.get("now"), txp, d.get("source")])
    return rows


def chk_tdx(tx):
    """GitHub 机房一般能连上通达信（TCP 7709）。连上后取几只股票的行情；某个服务器连上却不给数据时换下一个。"""
    from mootdx.consts import HQ_HOSTS
    from mootdx.quotes import Quotes
    tried, errs = 0, []
    for h in HQ_HOSTS[:16]:
        name, ip, port = h if len(h) == 3 else ("", *h)
        tried += 1
        t0 = time.time()
        try:
            socket.create_connection((ip, port), timeout=2).close()
        except OSError:
            continue
        ms = round((time.time() - t0) * 1000)
        try:
            client = Quotes.factory(market="std", server=(ip, port), timeout=10)
            df = client.quotes(symbol=[s[2:] for s in BIG])
            rows = []
            for r in (df.to_dict("records") if df is not None else []):
                c = str(r.get("code") or "").zfill(6)
                nm, txp = tx.get(c, ("", None))
                if r.get("price"):
                    rows.append([c, nm, round(float(r["price"]), 2), txp])
            if not rows:                                                   # 实时行情为空时（休市日有的服务器不给），改取最近一根日K的收盘价
                for sym in BIG:
                    k = client.bars(symbol=sym[2:], frequency=9, offset=1)
                    if k is not None and len(k):
                        c = sym[2:]
                        nm, txp = tx.get(c, ("", None))
                        rows.append([c, nm, round(float(k["close"].iloc[-1]), 2), txp])
            if rows:
                return {"ok": 1, "host": f"{name} {ip}", "ms": ms, "rows": rows}
            errs.append(f"{ip} 没返回数据（行情 {0 if df is None else len(df)} 行）")
        except Exception as e:
            errs.append(f"{ip} {type(e).__name__}: {str(e)[:60]}")
        if len(errs) >= 3:
            break
    return {"ok": 2 if errs else 0, "tried": tried, "err": "；".join(errs)[:200]}


# ---------- pywencai ----------
WC_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"


def _wc_probe(cookie):
    """问句都失败时，自己发一次和 pywencai 一样的“问句解析”请求，记下问财返回的状态（只记状态码和提示，不记 cookie）。"""
    import requests
    from pywencai.headers import get_token
    data = {"add_info": "{\"urp\":{\"scene\":1,\"company\":1,\"business\":1},\"contentType\":\"json\",\"searchInfo\":true}", "perpage": "10", "page": 1,
            "source": "Ths_iwencai_Xuangu", "log_info": "{\"input_type\":\"click\"}", "version": "2.0", "secondary_intent": "stock", "question": WC_QUERIES[0]}
    try:
        r = requests.post("http://www.iwencai.com/customized/chart/get-robot-data", json=data, timeout=20,
                          headers={"hexin-v": get_token(), "User-Agent": WC_UA, "cookie": cookie})
        info = f"问句接口 HTTP {r.status_code}"
        try:
            j = r.json()
            if isinstance(j, dict):
                info += f"，status_code={j.get('status_code')}，{str(j.get('status_msg') or '')[:40]}"
        except ValueError:
            m = re.search(r"<title>(.*?)</title>", r.text, re.S)
            info += f"，{(m.group(1).strip() if m else r.text.strip()[:40])[:40]}"
    except Exception as e:
        info = f"{type(e).__name__}: {str(e)[:80]}"
    try:                                                                    # 首页不用登录：首页也被拒，说明是按服务器地址拦的
        h = requests.get("https://www.iwencai.com/unifiedwap/home/index", timeout=20, headers={"User-Agent": WC_UA})
        info += f"；首页 HTTP {h.status_code}"
    except Exception as e:
        info += f"；首页 {type(e).__name__}"
    names = [kv.split("=", 1)[0].strip() for kv in cookie.split(";") if "=" in kv]   # 只看字段名，不碰内容
    login = [n for n in names if n.lower() in ("userid", "user", "ticket", "u_name", "escapename", "user_status", "utk")]
    info += f"；cookie 共 {len(names)} 个字段，登录相关字段：{'、'.join(login) if login else '没有'}"
    return info


def _wc_cell(col, v):
    """问财给的是原始数字（涨跌幅 9.998509909104445、资金 4013433721.44 元），按列名整理成网页上的写法。"""
    if v is None or (isinstance(v, float) and v != v):
        return ""
    t = str(v).strip()
    try:
        x = float(t.replace(",", ""))
    except ValueError:
        return t[:24]
    if re.search(r"涨跌幅|增长率|收益率|roe|ROE|换手率|占比|振幅", col):
        return f"{x:.2f}%"
    if re.search(r"资金|金额|成交额|市值|净额", col):
        a = abs(x)
        return f"{x / 1e8:.2f}亿" if a >= 1e8 else f"{x / 1e4:.2f}万" if a >= 1e4 else f"{x:.2f}"
    if re.search(r"天数|家数|次数", col) and x == int(x):
        return str(int(x))
    return f"{x:.2f}"


def wencai():
    cookie = gp.wencai_cookie()
    if not cookie:
        return {"on": 0}
    import pandas as pd
    import pywencai
    enh = gp.wencai_enhanced()                                             # pywencai-enhanced 跟上了问财的新接口，自己处理请求头和 token
    if not enh:
        ok, why = gp.wencai_prepare(cookie, WC_QUERIES[0])                 # 原版：问财升级后要带 Referer/Origin，先挑能用的请求头
        log("wencai: 请求头检查", why)
        if not ok:
            return {"on": 1, "res": [{"q": q, "err": "问财拒绝了请求"} for q in WC_QUERIES], "diag": why + "；" + _wc_probe(cookie).split("；", 1)[-1]}
    out, errs = [], []
    for q in WC_QUERIES:
        try:
            df = (pywencai.get(query=q, cookie=cookie, loop=2, strict=True, log=False) if enh                     # 取前两页（20 条），总条数在 attrs 里
                  else pywencai.get(query=q, cookie=cookie, loop=False, user_agent=WC_UA, retry=3, sleep=1, request_params={"timeout": 20}))
            if isinstance(df, dict):
                df = next((v for v in df.values() if isinstance(v, pd.DataFrame)), None)
            if not isinstance(df, pd.DataFrame) or not len(df):
                out.append({"q": q, "err": "没有结果（cookie 可能过期）"}); continue
            cols = [c for c in df.columns if c not in ("market_code", "code")][:8]
            names = [re.sub(r"\[\d{8}(?:-\d{8})?\]", "", str(c)).strip() for c in cols]   # 去掉列名里的日期 [20261113] / [20261109-20261113]
            rows = []
            for r in df[cols].head(20).values.tolist():
                cells = [_wc_cell(n, v) for n, v in zip(names, r)]
                if names[:1] == ["股票代码"]:
                    cells[0] = str(r[0]).split(".")[0]                       # 605255.SH -> 605255，网页上点击打开个股
                rows.append(cells)
            total = ((getattr(df, "attrs", {}) or {}).get("wencai_query_coverage") or {}).get("expected_row_count") or len(df)
            out.append({"q": q, "cols": names, "rows": rows, "n": int(total), "shown": len(rows)})
        except Exception as e:
            msg = "问财没有返回结果" if "NoneType" in str(e) else f"{type(e).__name__}: {str(e)[:120]}"
            out.append({"q": q, "err": msg}); errs.append(msg)
        time.sleep(2)
    res = {"on": 1, "res": out, "lib": f"pywencai-enhanced {enh}" if enh else "pywencai"}
    if not any("rows" in x for x in out):
        res["diag"] = (f"{errs[0]}；" + _wc_probe(cookie).split("；", 1)[-1]) if enh and errs else _wc_probe(cookie)
        log("wencai: 问句都没有结果：", res["diag"])
    return res


def refresh_lab():
    d = close_date() or (load("a.json", {}).get("asof") or "")[:10]
    old = load("lab.json", {})
    lab = {"date": d, "t": bj_now().strftime("%Y-%m-%d %H:%M"), "libs": {}}
    libs = lab["libs"]

    def stat(k, ok, msg, ms=None):
        libs[k] = {"v": _ver(k) if k != "stock-api" else None, "ok": ok, "msg": msg, "ms": ms}

    t0 = time.time()
    try:
        bt, ok = run_backtests()
        lab["bt"] = bt
        stat("akquant", 1 if ok else 0, f"回测 {bt['n']} 个标的" + (f"，{len(bt['fail'])} 个取不到数据" if bt["fail"] else ""), round(time.time() - t0, 1))
    except Exception as e:
        traceback.print_exc(); lab["bt"] = old.get("bt"); stat("akquant", 0, f"{type(e).__name__}: {str(e)[:80]}（沿用上一次）")
    t0 = time.time()
    try:
        ak, errs = ak_data(d)
        lab["ak"] = {**{k: v for k, v in (old.get("ak") or {}).items() if k not in ak}, **ak}
        stat("akshare", 1 if ak and not errs else 0 if not ak else 2, f"取到 {len(ak)}/4 组数据" + (f"；失败：{'；'.join(errs)}" if errs else ""), round(time.time() - t0, 1))
    except Exception as e:
        traceback.print_exc(); lab["ak"] = old.get("ak"); stat("akshare", 0, f"{type(e).__name__}: {str(e)[:80]}（沿用上一次）")
    tx, asof = _tx_prices()
    lab["chk"] = {"asof": asof}
    t0 = time.time()
    try:
        r = chk_sina(tx)
        lab["chk"]["sina"] = r
        stat("easyquotation", 1 if r["n"] > 4000 else 2, f"新浪 {r['n']} 只，和腾讯收盘价一致 {r['same']} 只", round(time.time() - t0, 1))
    except Exception as e:
        stat("easyquotation", 0, f"{type(e).__name__}: {str(e)[:80]}")
    t0 = time.time()
    try:
        rows = chk_stock_api(tx)
        lab["chk"]["sa"] = rows
        stat("stock-api", 1 if rows else 0, f"取到 {len(rows)} 只行情", round(time.time() - t0, 1))
        libs["stock-api"]["v"] = _npm_ver()
    except Exception as e:
        stat("stock-api", 0, f"{type(e).__name__}: {str(e)[:80]}")
    t0 = time.time()
    try:
        r = chk_tdx(tx)
        lab["chk"]["tdx"] = r
        stat("mootdx", r["ok"], f"连上通达信 {r['host']}（{r['ms']}ms），取到 {len(r['rows'])} 只行情" if r["ok"] == 1
             else f"能连上通达信服务器，但没取到行情：{r['err']}" if r["ok"] == 2 else f"GitHub 机房连不上通达信行情服务器（试了 {r['tried']} 个，TCP 7709）", round(time.time() - t0, 1))
    except Exception as e:
        stat("mootdx", 0, f"{type(e).__name__}: {str(e)[:80]}")
    try:
        w = wencai()
        if not w["on"] and (old.get("wc") or {}).get("on"):
            w = {"on": 0}
        lab["wc"] = w
        stat("pywencai", None if not w["on"] else 1 if any("rows" in x for x in w["res"]) else 0,
             "未启用：仓库 Secrets 里没有 WENCAI_COOKIE" if not w["on"] else f"{sum(1 for x in w['res'] if 'rows' in x)}/{len(w['res'])} 条问句有结果" + (f"；问财返回 {w['diag']}" if w.get("diag") else ""))
    except Exception as e:
        stat("pywencai", 0, f"{type(e).__name__}: {str(e)[:80]}")
    save("lab.json", lab)
    log("lab", d, "|", {k: (v["ok"], v["msg"]) for k, v in libs.items()})
    return libs["akquant"]["ok"] == 1


def _npm_ver():
    cmd = stock_api_bin()
    if not cmd or len(cmd) > 1:
        return None
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=20).stdout.strip()
        import json
        return json.load(open(os.path.join(root, "stock-api", "package.json"), encoding="utf-8")).get("version")
    except Exception:
        return None


if __name__ == "__main__":
    refresh_lab()

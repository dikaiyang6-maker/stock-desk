"""全球市场：世界指数（Yahoo 日K/分钟线 + 腾讯报价）与成分股报价（腾讯美股港股、Naver 日韩、Yahoo 其余）。"""
import datetime as dt
import re
import time

import requests

from common import (S, UA, bj_ms, et_ms, get, jget, load, log, merge_bars, num, r2, save, tx_daily, tx_quotes)

# code, 中文名, 区域, 实时源, 实时代码, Yahoo 代码, 东方财富 secid
INDICES = [
    ("usDJI", "道琼斯工业平均", "美洲", "us", "usDJI", "^DJI", "100.DJIA"),
    ("usINX", "标普500", "美洲", "us", "usINX", "^GSPC", "100.SPX"),
    ("usIXIC", "纳斯达克综合", "美洲", "us", "usIXIC", "^IXIC", None),
    ("usNDX", "纳斯达克100", "美洲", "us", "usNDX", "^NDX", "100.NDX"),
    ("gzGSPTSE", "加拿大S&P/TSX", "美洲", "gz", "gzGSPTSE", "^GSPTSE", "100.TSX"),
    ("gzIBOV", "巴西IBOVESPA", "美洲", "gz", "gzIBOV", "^BVSP", "100.BVSP"),
    ("gzMXX", "墨西哥IPC", "美洲", "gz", "gzMXX", "^MXX", "100.MXX"),
    ("gzFTSE", "英国富时100", "欧洲", "gz", "gzFTSE", "^FTSE", "100.FTSE"),
    ("gzGDAXI", "德国DAX", "欧洲", "gz", "gzGDAXI", "^GDAXI", "100.GDAXI"),
    ("gzFCHI", "法国CAC40", "欧洲", "gz", "gzFCHI", "^FCHI", "100.FCHI"),
    ("gzSX5E", "欧洲斯托克50", "欧洲", "gz", "gzSX5E", "^STOXX50E", "100.SX5E"),
    ("gzFTSEMIB", "意大利富时MIB", "欧洲", "gz", "gzFTSEMIB", "FTSEMIB.MI", "100.MIB"),
    ("gzIBEX", "西班牙IBEX35", "欧洲", "gz", "gzIBEX", "^IBEX", "100.IBEX"),
    ("gzAEX", "荷兰AEX", "欧洲", "gz", "gzAEX", "^AEX", "100.AEX"),
    ("emSSMI", "瑞士SMI", "欧洲", "em", "100.SSMI", "^SSMI", "100.SSMI"),
    ("gzXU100", "土耳其BIST100", "欧洲", "gz", "gzXU100", "XU100.IS", None),
    ("gzRTS", "俄罗斯RTS", "欧洲", "gz", "gzRTS", None, "100.RTS"),
    ("gzN225", "日经225", "亚太", "gz", "gzN225", "^N225", "100.N225"),
    ("gzKS11", "韩国KOSPI", "亚太", "gz", "gzKS11", "^KS11", "100.KS11"),
    ("gzTWII", "台湾加权", "亚太", "gz", "gzTWII", "^TWII", "100.TWII"),
    ("hkHSI", "恒生指数", "亚太", "hk", "hkHSI", "^HSI", "100.HSI"),
    ("hkHSCEI", "恒生中国企业", "亚太", "hk", "hkHSCEI", "^HSCE", "100.HSCEI"),
    ("hkHSTECH", "恒生科技", "亚太", "hk", "hkHSTECH", None, None),
    ("emSENSEX", "印度SENSEX", "亚太", "em", "100.SENSEX", "^BSESN", "100.SENSEX"),
    ("emAS51", "澳洲标普200", "亚太", "em", "100.AS51", "^AXJO", "100.AS51"),
    ("emSTI", "新加坡海峡时报", "亚太", "em", "100.STI", "^STI", "100.STI"),
    ("gzJKSE", "印尼雅加达综合", "亚太", "gz", "gzJKSE", "^JKSE", "100.JKSE"),
    ("gzSET", "泰国SET", "亚太", "gz", "gzSET", None, "100.SET"),
    ("gzVNINDEX", "越南胡志明", "亚太", "gz", "gzVNINDEX", None, "100.VNINDEX"),
]
DAILY_KEEP = 1500
SEC_ZH2 = {"Finance": "金融", "Properties": "地产", "Utilities": "公用事业", "Commerce & Industry": "工商业", "Support services": "支持服务", "Distribution": "分销", "Logistics": "物流",
           "Mechanical Engineering": "机械工程", "Biotech": "生物科技", "E-Commerce": "电商", "Services": "服务", "Capital Goods": "资本品", "Power": "电力", "Heavy Industries": "重工业", "IT": "信息技术"}
YAHOO = "https://query2.finance.yahoo.com/v8/finance/chart/"


YB = {"n429": 0, "blocked": False}


def yahoo_chart(sym, **params):
    """Yahoo 限流（429）累计 3 次后，本轮不再访问 Yahoo，沿用缓存。"""
    if YB["blocked"]:
        return None
    try:
        r = S.get(YAHOO + requests.utils.quote(sym), params=params, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code == 429:
        YB["n429"] += 1
        if YB["n429"] >= 3:
            YB["blocked"] = True; log("yahoo: 被限流，本轮停止访问 Yahoo，沿用缓存")
        time.sleep(5)
        return None
    try:
        d = r.json() if r.status_code == 200 else None
    except ValueError:
        d = None
    try:
        res = d["chart"]["result"][0]
        return res["meta"], res.get("timestamp") or [], (res.get("indicators", {}).get("quote") or [{}])[0]
    except (TypeError, KeyError, IndexError):
        return None


def bars(meta, ts, q):
    off = meta.get("gmtoffset", 0); out = {}
    for i, t in enumerate(ts):
        c = (q.get("close") or [None] * len(ts))[i]
        if c is None:
            continue
        o = q["open"][i] if q["open"][i] is not None else c
        h = q["high"][i] if q["high"][i] is not None else max(o, c)
        l = q["low"][i] if q["low"][i] is not None else min(o, c)
        d = dt.datetime.fromtimestamp(t + off, dt.timezone.utc).strftime("%Y-%m-%d")
        out[d] = [d, r2(o), r2(c), r2(h), r2(l), int((q.get("volume") or [0] * len(ts))[i] or 0)]
    return [out[k] for k in sorted(out)]


def agg_month(rows):
    out = []
    for r in rows:
        if out and out[-1][0][:7] == r[0][:7]:
            b = out[-1]; b[0] = r[0]; b[2] = r[2]; b[3] = max(b[3], r[3]); b[4] = min(b[4], r[4]); b[5] += r[5]
        else:
            out.append(list(r[:6]))
    return out


def minutes(meta, ts, q, daily):
    """五日分钟线：按交易所当地时间分日，时间与价格（分）差分编码；prec 取前一交易日收盘。"""
    off = meta.get("gmtoffset", 0); days = {}
    for i, t in enumerate(ts):
        c = (q.get("close") or [None] * len(ts))[i]
        if c is None:
            continue
        lt = dt.datetime.fromtimestamp(t + off, dt.timezone.utc)
        days.setdefault(lt.strftime("%Y-%m-%d"), []).append((lt.hour * 60 + lt.minute, c, int((q.get("volume") or [0] * len(ts))[i] or 0)))
    closes = {b[0]: b[2] for b in daily}; dates = sorted(closes); out = []
    for d in sorted(days):
        prev = [x for x in dates if x < d]
        prec = closes[prev[-1]] if prev else days[d][0][1]
        tm, pc, vv, lt, lp = [], [], [], 0, 0
        for m, c, v in days[d]:
            cc = round(c * 100); tm.append(m - lt); pc.append(cc - lp); vv.append(v); lt, lp = m, cc
        out.append({"d": d, "prec": prec, "t": tm, "p": pc, "v": vv if any(vv) else None})
    return out


def tx_minutes(sym, kind):
    url = {"us": "https://web.ifzq.gtimg.cn/appstock/app/dayus/query", "hk": "https://web.ifzq.gtimg.cn/appstock/app/day/query"}[kind]
    d = jget(url, {"code": sym})
    try:
        days = d["data"][sym]["data"]
    except (TypeError, KeyError):
        return []
    out = []
    for x in sorted(days, key=lambda z: z["date"]):
        tm, pc, vv, lt, lp, pv = [], [], [], 0, 0, 0
        for s in x["data"]:
            f = s.split()
            if len(f) < 2:
                continue
            m = int(f[0][:2]) * 60 + int(f[0][2:]); cc = round(float(f[1]) * 100); cv = int(float(f[2])) if len(f) > 2 else 0
            tm.append(m - lt); pc.append(cc - lp); vv.append(max(0, cv - pv)); lt, lp, pv = m, cc, max(pv, cv)
        if tm:
            out.append({"d": f'{x["date"][:4]}-{x["date"][4:6]}-{x["date"][6:]}', "prec": float(x.get("prec") or 0), "t": tm, "p": pc, "v": vv if any(vv) else None})
    return out


def idx_quotes():
    q = tx_quotes([r[4] for r in INDICES if r[3] in ("us", "hk", "gz")], lanes=1); out = {}
    for sym, f in q.items():
        if sym.startswith("gz"):
            p, chg = num(f[3]), num(f[4])
            ts = bj_ms(f[2])
            out[sym] = {"p": p, "chg": chg, "pct": num(f[5]), "prev": r2(p - chg) if p is not None and chg is not None else None,
                        "ts": ts if ts and ts <= (time.time() + 600) * 1000 else None}   # 泰国、越南等的报价时间偶尔是未来时间，不可信就不用
        else:
            out[sym] = {"p": num(f[3]), "prev": num(f[4]), "o": num(f[5]), "chg": num(f[31]), "pct": num(f[32]), "h": num(f[33]), "l": num(f[34]),
                        "ts": et_ms(f[30]) if sym.startswith("us") else bj_ms(f[30])}
    return out


def refresh_indices(full=False):
    G = load("global.json", {}); old = {x["c"]: x for x in G.get("idx", [])}; out = []
    for code, name, region, src, live, ysym, em in INDICES:
        rec = old.get(code) or {"k": [], "m": [], "mm": []}
        rec.update({"c": code, "n": name, "r": region, "src": src, "sym": live, "em": em})
        try:
            if ysym:
                if full or len(rec["k"]) < 300:
                    x = yahoo_chart(ysym, period1=-2208988800, period2=int(time.time()), interval="1d") or yahoo_chart(ysym, period1=315532800, period2=int(time.time()), interval="1d")
                    if x and x[1]:
                        allk = bars(*x)
                        if len(allk) >= 30:
                            rec["k"], rec["m"], rec["tz"], rec["cur"] = allk[-DAILY_KEEP:], agg_month(allk), x[0].get("exchangeTimezoneName"), x[0].get("currency")
                    time.sleep(0.8)
                else:
                    x = yahoo_chart(ysym, range="1mo", interval="1d")
                    if x and x[1]:
                        recent = bars(*x)
                        rec["k"] = merge_bars(rec["k"], recent, keep=DAILY_KEEP)
                        if rec["m"]:                                  # 用最新日K重算最近两个月的月K
                            months = {r[0][:7] for r in recent}
                            rec["m"] = [m for m in rec["m"] if m[0][:7] not in months] + agg_month([r for r in rec["k"] if r[0][:7] in months])
                mn = yahoo_chart(ysym, range="5d", interval="1m")
                if mn and mn[1]:
                    rec["mm"] = minutes(*mn, rec["k"])
                time.sleep(0.5)
            elif src == "hk":                                          # Yahoo 没有恒生科技：用腾讯
                k = tx_daily(live, kind="hkfqkline", count=640, pages=3 if full or len(rec["k"]) < 300 else 1)
                k = [r[:6] for r in k]
                rec["k"] = merge_bars(rec["k"], k, keep=DAILY_KEEP); rec["m"] = agg_month(rec["k"]); rec["tz"] = "Asia/Hong_Kong"; rec["cur"] = "HKD"
            if not rec.get("mm") and src in ("us", "hk"):
                rec["mm"] = tx_minutes(live, src)
        except Exception as e:                                       # 单个指数失败不影响其他
            log("index", name, "failed:", type(e).__name__, e)
        out.append(rec)
    q = idx_quotes()
    for rec in out:
        if q.get(rec["sym"], {}).get("p"):
            rec["q"] = q[rec["sym"]]
    G["idx"] = out; G["asof"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    save("global.json", G)
    log("indices", sum(1 for r in out if len(r["k"]) >= 30), "with history,", sum(1 for r in out if r.get("mm")), "with minutes,", len(q), "quotes")
    return True


# ---------- 成分股 ----------
def refresh_lists():
    from lists import markets
    M = markets()
    if sum(len(v) for v in M.values()) < 800:
        log("lists: 名单抓取不完整，沿用缓存"); return False
    G = load("global.json", {}); st = G.get("st", {}); new = {}
    us = sorted({"us" + s["t"] for k in ("spx", "dow", "ndx") for s in M[k]}); hk = sorted({s["hk"] for s in M["hsi"]})
    names = tx_quotes(us + hk)
    for mk, lst in M.items():
        for s in lst:
            y = s["y"]
            if y in new:
                continue
            tx = ("us" + s["t"]) if mk in ("spx", "dow", "ndx") else s.get("hk")
            f = names.get(tx) if tx else None
            rec = dict(st.get(y, {}))                                   # 保留旧报价
            rec.update({"y": y, "t": s["t"], "sec": SEC_ZH2.get(s["sec"], s["sec"]), "tx": tx if f else None,
                        "n": f[1] if f else (rec.get("n") or s["en"]), "en": rec.get("en") or s["en"]})
            if f and tx.startswith("us"):
                rec["txk"] = "us" + f[2]
            new[y] = rec
    G["mk"] = {k: [s["y"] for s in v] for k, v in M.items()}; G["st"] = new
    save("global.json", G)
    log("lists", {k: len(v) for k, v in M.items()}, "tencent names", len(names))
    return True


def refresh_us_hk():
    G = load("global.json", {}); st = G.get("st", {})
    by = {s["tx"]: s for s in st.values() if s.get("tx")}
    q = tx_quotes(list(by))
    for sym, f in q.items():
        s = by[sym]; hk = sym.startswith("hk")
        if not num(f[3]):
            continue
        s.update({"n": f[1] or s.get("n"), "p": num(f[3]), "prev": num(f[4]), "o": num(f[5]), "chg": num(f[31]), "pct": num(f[32]), "h": num(f[33]), "l": num(f[34]),
                  "vol": num(f[36]), "amt": num(f[37]), "pe": num(f[39]), "mcap": (num(f[45]) or 0) * 1e8 or None, "h52": num(f[48]), "l52": num(f[49]),
                  "cur": (f[75] if hk and len(f) > 75 else f[35]) or ("HKD" if hk else "USD"), "ts": bj_ms(f[30]) if hk else et_ms(f[30]),
                  "tz": "Asia/Hong_Kong" if hk else "America/New_York", "ex": "港交所" if hk else ("纳斯达克" if f[2].endswith(".OQ") else "纽交所" if f[2].endswith(".N") else "美股")})
        if not hk:
            s["txk"] = "us" + f[2]
    save("global.json", G); log("us/hk quotes", len(q), "/", len(by))
    return True


def refresh_jp_kr():
    """日经225、KOSPI200：Naver 按市值翻页，补价格、涨跌、成交量、市值。"""
    G = load("global.json", {}); st = G.get("st", {}); H = {"User-Agent": UA}; got = {}
    for mk, url, key, tz, cur in [("n225", "https://api.stock.naver.com/stock/exchange/TOKYO/marketValue", "symbolCode", "Asia/Tokyo", "JPY"),
                                   ("kospi", "https://m.stock.naver.com/api/stocks/marketValue/KOSPI", "itemCode", "Asia/Seoul", "KRW")]:
        need = {y.split(".")[0]: y for y in G.get("mk", {}).get(mk, [])}; n = 0
        for page in range(1, 40):
            d = jget(url, {"page": page, "pageSize": 60}, headers=H)
            rows = (d or {}).get("stocks") or []
            for x in rows:
                y = need.get(x.get(key) or "")
                if not y:
                    continue
                raw = lambda k: num(str(x.get(k + "Raw") or x.get(k) or "").replace(",", ""))
                p, chg = raw("closePrice"), raw("compareToPreviousClosePrice")
                s = st[y]; n += 1
                s.update({"p": p, "chg": chg, "pct": raw("fluctuationsRatio"), "prev": r2(p - chg) if p is not None and chg is not None else s.get("prev"),
                          "o": raw("openPrice") or s.get("o"), "h": raw("highPrice") or s.get("h"), "l": raw("lowPrice") or s.get("l"),
                          "vol": raw("accumulatedTradingVolume") or s.get("vol"), "mcap": raw("marketValue") * (1 if mk == "n225" else 1) if x.get("marketValueRaw") else s.get("mcap"),
                          "cur": cur, "tz": tz})
                if mk == "kospi" and x.get("marketValueRaw"):
                    s["mcap"] = num(x["marketValueRaw"])
                t = x.get("localTradedAt") or (x.get("overMarketPriceInfo") or {}).get("localTradedAt")
                if t:
                    try: s["ts"] = int(dt.datetime.fromisoformat(t).timestamp() * 1000)
                    except ValueError: pass
            if n >= len(need) or not rows:
                break
            time.sleep(0.3)
        got[mk] = f"{n}/{len(need)}"
    save("global.json", G); log("naver", got)
    return True


def yahoo_fundamentals(syms):
    """Yahoo v7 quote 需要 cookie + crumb；被限流（429）时直接放弃，返回空。"""
    try:
        sess = requests.Session(); sess.headers["User-Agent"] = UA
        sess.get("https://fc.yahoo.com", timeout=20)
        c = sess.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=20)
        if c.status_code != 200 or not c.text:
            log("yahoo fundamentals: crumb", c.status_code, "- 跳过市值/市盈率"); return {}
        out = {}
        for i in range(0, len(syms), 50):
            r = sess.get("https://query2.finance.yahoo.com/v7/finance/quote", params={"symbols": ",".join(syms[i:i + 50]), "crumb": c.text}, timeout=30)
            if r.status_code != 200:
                break
            for q in r.json().get("quoteResponse", {}).get("result", []):
                out[q["symbol"]] = {"mcap": q.get("marketCap"), "pe": r2(q.get("trailingPE"))}
            time.sleep(2)
        log("yahoo fundamentals", len(out), "/", len(syms)); return out
    except (requests.RequestException, ValueError) as e:
        log("yahoo fundamentals failed:", type(e).__name__); return {}


def refresh_others():
    """富时100、DAX、CAC40、斯托克50、Nifty50：Yahoo chart 元数据，每秒约 1 次。"""
    G = load("global.json", {}); st = G.get("st", {}); mk = G.get("mk", {})
    ys = sorted({y for k in ("ftse", "dax", "cac", "sx5e", "nifty") for y in mk.get(k, [])}); n = 0
    fund = yahoo_fundamentals(ys)                                      # 市值、市盈率（Yahoo 不限流时才有）
    for y, f in fund.items():
        st[y].update({k: v for k, v in f.items() if v})
    for y in ys:
        x = yahoo_chart(y, range="1mo", interval="1d")
        if not x:
            time.sleep(1); continue
        m, ts, q = x; cl = [c for c in (q.get("close") or []) if c is not None]
        p = m.get("regularMarketPrice"); prev = cl[-2] if len(cl) >= 2 else m.get("chartPreviousClose")
        s = st[y]; n += 1
        s.update({"p": p, "prev": prev, "chg": round(p - prev, 4) if p and prev else None, "pct": round((p / prev - 1) * 100, 2) if p and prev else None,
                  "h": m.get("regularMarketDayHigh"), "l": m.get("regularMarketDayLow"), "vol": m.get("regularMarketVolume"), "h52": m.get("fiftyTwoWeekHigh"),
                  "l52": m.get("fiftyTwoWeekLow"), "cur": m.get("currency"), "tz": m.get("exchangeTimezoneName"), "ex": m.get("fullExchangeName"), "ts": (m.get("regularMarketTime") or 0) * 1000})
        if m.get("shortName"):
            s["en"] = m["shortName"]
            if not re.search(r"[぀-ヿ一-鿿]", s.get("n") or ""):
                s["n"] = m.get("longName") or s["n"]
        time.sleep(1.0)
    save("global.json", G); log("yahoo others", n, "/", len(ys))
    return n > 0

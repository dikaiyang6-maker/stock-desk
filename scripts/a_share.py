"""A 股：财报（东方财富数据中心，每天）、全市场行情（腾讯，每小时）、指数与题材股 K 线和分时（增量）。"""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from common import (a_sym, bj_now, get, jget, load, log, merge_bars, num, r2, save, tx_daily, tx_quotes)

MARKETS = ("上交所主板", "深交所主板", "深交所创业板", "上交所科创板", "北京证券交易所", "深交所风险警示板", "上交所风险警示板")
COLS = "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_MARKET,SECURITY_TYPE_CODE,BOARD_NAME,TOTAL_OPERATE_INCOME,YSTZ,PARENT_NETPROFIT,SJLTZ,WEIGHTAVG_ROE,XSMLL,MGJYXJJE,REPORTDATE"
THEMES = {
    "AI算力服务器": "601138 000977 000938 603019 000066 603296 688041",
    "光通信": "601869 600487 600522 002281 000988 600105 002384 300308 300502 300394 688498 920045",
    "AI PCB/覆铜板": "002916 002463 600183 001389 603186 002636 600601 603228 002938 001232 002913 603459 300476 688183",
    "AI芯片/存储": "603986 001309 603893 002049 603501 600460 603160 688256 688008 688525 300223 300661 920138",
    "半导体设备/制造": "002371 002156 600584 002185 002409 600206 603061 688981 688012 688072 688120 300604",
    "机器人": "002050 601689 002747 002008 002979 002472 000887 603025 300124 688017 920119 920156",
    "AI软件/视觉": "002415 002236 002230 601360 600845 002396 603236 688111 300033 300496",
    "固态电池/新能源": "002709 002074 002812 603659 002594 002340 600110 300750 300014 688005 920185 920809",
    "核聚变/超导": "600875 601727 603011 601985 688122",
    "商业航天/军工电子": "600879 601698 600118 002414 002179 002085 688568",
    "量子科技": "688027",
    "防御/高股息": "601398 601939 600036 601088 600900 601288",
}
TAG = {}
for _t, _cs in THEMES.items():
    for _c in _cs.split():
        TAG.setdefault(_c, []).append(_t)
IDX = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指", "sh000688": "科创50", "bj899050": "北证50", "sh000300": "沪深300"}
RPT = {"03-31": "Q1", "06-30": "H1", "09-30": "Q3", "12-31": "FY"}
SLOTS = [h * 100 + m for h in (9, 10, 11) for m in range(60) if 930 <= h * 100 + m <= 1130] + [h * 100 + m for h in (13, 14, 15) for m in range(60) if 1300 <= h * 100 + m <= 1500]


def board(c):
    if c.startswith(("688", "689")):
        return "star"
    if c.startswith(("300", "301", "302")):
        return "gem"
    if c.startswith(("92", "4", "8")):
        return "bj"
    return "sh" if c.startswith("6") else "sz"


# ---------- 财报 ----------
def _report_page(date, page, size=500):
    return jget("https://datacenter-web.eastmoney.com/api/data/v1/get", {"reportName": "RPT_LICO_FN_CPD", "columns": COLS, "filter": f"(REPORTDATE='{date}')",
                "pageNumber": page, "pageSize": size, "sortColumns": "SECURITY_CODE", "sortTypes": 1})


def _report(date):
    rows, page = [], 1
    while True:
        d = _report_page(date, page)
        res = (d or {}).get("result") or {}
        rows += res.get("data") or []
        if page >= (res.get("pages") or 0):
            break
        page += 1
    return rows


def quarter_ends(today, n=5):
    out, y, q = [], today.year, (today.month - 1) // 3
    for _ in range(n):
        if q == 0:
            y, q = y - 1, 4
        out.append(f"{y}-{['03-31', '06-30', '09-30', '12-31'][q - 1]}")
        q -= 1
    return out


def refresh_reports():
    """取最近两期：最新一期（正在披露，可能不全）+ 上一期已基本披露完（≥3000 家）的一期。"""
    cands = quarter_ends(bj_now().date())
    counts = {}
    for d in cands:
        res = ((_report_page(d, 1, 1) or {}).get("result") or {})
        counts[d] = res.get("count") or 0
    newest = next((d for d in cands if counts[d] >= 50), None)
    base = next((d for d in cands if counts[d] >= 3000), None)
    dates = sorted({d for d in (base, newest) if d})
    if not dates:
        log("reports: 东方财富数据中心没有返回数据，沿用缓存")
        return False
    data = {"dates": dates, "rows": {d: _report(d) for d in dates}}
    save("reports.json", data)
    log("reports", {d: len(v) for d, v in data["rows"].items()}, "counts", counts)
    return True


# ---------- 全市场行情 ----------
def refresh_market():
    rep = load("reports.json")
    if not rep:
        log("market: 没有财报缓存，跳过"); return False
    keep = lambda r: r["TRADE_MARKET"] in MARKETS and r["SECURITY_TYPE_CODE"] != "058001002"   # 去掉 B 股
    by_date = {d: {r["SECURITY_CODE"]: r for r in rep["rows"][d] if keep(r)} for d in rep["dates"]}
    order = sorted(rep["dates"], reverse=True)          # 新的一期优先
    codes = sorted(set().union(*by_date.values()))
    q = tx_quotes([a_sym(c) for c in codes])
    rows, ts = [], ""
    for c in codes:
        f = q.get(a_sym(c))
        d = next((d for d in order if c in by_date[d]), None)
        if not f or not d or len(f) < 49:
            continue
        price = num(f[3]) or 0
        if price <= 0:
            continue                                     # 停牌等无成交
        r = by_date[d][c]; ts = max(ts, f[30])
        lu, ld = num(f[47]), num(f[48])
        rows.append([c, f[1], board(c), r.get("BOARD_NAME") or "", price, num(f[32]) or 0.0, num(f[39]), num(f[46]), round(num(f[45]) or 0, 1),
                     RPT[d[5:]], round((r["TOTAL_OPERATE_INCOME"] or 0) / 1e8, 2), None if r["YSTZ"] is None else round(r["YSTZ"], 1),
                     round((r["PARENT_NETPROFIT"] or 0) / 1e8, 2), None if r["SJLTZ"] is None else round(r["SJLTZ"], 1),
                     None if r["WEIGHTAVG_ROE"] is None else round(r["WEIGHTAVG_ROE"], 2), None if r["XSMLL"] is None else round(r["XSMLL"], 1),
                     round((num(f[37]) or 0) / 1e4, 2), TAG.get(c, []), 1 if "ST" in f[1] else 0,
                     None if r.get("MGJYXJJE") is None else round(r["MGJYXJJE"], 2), num(f[4]), lu if lu and lu > 0 else None, ld if ld and ld > 0 else None,
                     num(f[33]), num(f[38]), num(f[44]), num(f[72]) if len(f) > 72 else None, num(f[49])])   # 最高、换手率%、流通市值亿、流通股本、量比
    if len(rows) < 1000:
        log("market: 行情只拿到", len(rows), "只，沿用缓存"); return False
    hist = load("hist.json", {})
    ik = {c: {"name": n, "k": [[r[0], r[2]] for r in (hist.get("idxk", {}).get(c, {}).get("k") or [])[-260:]]} for c, n in IDX.items()}
    asof = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}" if len(ts) >= 12 else bj_now().strftime("%Y-%m-%d %H:%M")
    cols = ["code", "name", "board", "ind", "price", "chg", "pe", "pb", "mcap", "rpt", "rev", "revY", "np", "npY", "roe", "gm", "amt", "tags", "st", "cfo", "prev", "lu", "ld", "hi", "tr", "fmc", "fsh", "qr"]
    save("a.json", {"asof": asof, "reports": rep["dates"], "cols": cols, "rows": rows, "themes": list(THEMES), "idx": ik})
    log("market rows", len(rows), "asof", asof)
    return True


# ---------- K 线与分时 ----------
def fiveday(sym, compact=False):
    d = jget("https://web.ifzq.gtimg.cn/appstock/app/day/query", {"code": sym})
    try:
        days = d["data"][sym]["data"]
    except (TypeError, KeyError):
        return []
    out = []
    for x in sorted(days, key=lambda z: z["date"]):
        pts = []
        for s in x["data"]:
            f = s.split()
            if len(f) >= 2 and int(f[0]) <= 1500:
                pts.append([int(f[0]), float(f[1]), float(f[2]) if len(f) > 2 else 0.0, float(f[3]) if len(f) > 3 else None])
        if not pts:
            continue
        day = f'{x["date"][:4]}-{x["date"][4:6]}-{x["date"][6:]}'
        if not compact:
            out.append({"d": day, "prec": float(x.get("prec") or 0), "p": [[p[0], p[1], int(p[2])] for p in pts]})
            continue
        # 个股：按 242 个分钟槽差分编码（价格、均价按分，量按手）
        by = {p[0]: p for p in pts}; last = None; p_, v_, a_ = [], [], []; lp = la = 0; cv = 0; pv = 0.0
        for hm in SLOTS:
            q = by.get(hm) or last or pts[0]; last = q
            price, cumv, cuma = q[1], q[2], q[3]
            pv += price * max(0, cumv - cv)
            avg = (cuma / (cumv * 100) if cuma else pv / cumv) if cumv > 0 else price
            pc, ac = round(price * 100), round(avg * 100)
            p_.append(pc - lp); a_.append(ac - la); v_.append(max(0, round(cumv - cv))); lp, la, cv = pc, ac, max(cv, cumv)
        out.append({"d": day, "prec": float(x.get("prec") or 0), "p": p_, "v": v_, "a": a_})
    return out


def refresh_hist(full_monthly=False):
    """指数：首次拉上市以来全部日K，之后每次补最近 40 根；题材股：保留近 500 根日K（除权时整段重拉）。"""
    hist = load("hist.json", {"idxk": {}, "idxm": {}, "sk": {}})
    extra = load("extra.json", {"skm": {}, "sm": {}})

    def idx_one(c):
        old = (hist["idxk"].get(c) or {}).get("k") or []
        new = tx_daily(c, count=2000, pages=10) if len(old) < 300 else tx_daily(c, count=40)
        return c, merge_bars(old, new), fiveday(c)

    with ThreadPoolExecutor(6) as ex:
        for c, k, m in ex.map(idx_one, IDX):
            if k:
                hist["idxk"][c] = {"name": IDX[c], "k": k}
            if m:
                hist["idxm"][c] = m

    def stock_one(c):
        sym = a_sym(c); old = hist["sk"].get(c) or []
        if len(old) < 100:
            k = tx_daily(sym, count=500)
        else:
            recent = tx_daily(sym, count=20)
            same = {r[0]: r for r in old}
            # 前复权价在除权后整体改变：重叠日收盘价差异超过 0.5% 就整段重拉
            drift = any(r[0] in same and same[r[0]][2] and abs(r[2] / same[r[0]][2] - 1) > 0.005 for r in recent[:-1])
            k = tx_daily(sym, count=500) if drift else merge_bars(old, recent, keep=500)
        return c, k, fiveday(sym, compact=True)

    with ThreadPoolExecutor(6) as ex:
        for c, k, m in ex.map(stock_one, TAG):
            if k:
                hist["sk"][c] = k
            if m:
                extra["sm"][c] = m
    if full_monthly or not extra.get("skm"):
        def month_one(c):
            d = jget("https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get", {"param": f"{a_sym(c)},month,,,640,qfq"})
            try:
                k = d["data"][a_sym(c)]
                k = k.get("qfqmonth") or k.get("month") or []
            except (TypeError, KeyError):
                return c, []
            return c, [[r[0], r2(r[1]), r2(r[2]), r2(r[3]), r2(r[4]), round(num(r[5]) or 0), round(num(r[8]) or 0) if len(r) > 8 else 0] for r in k]
        with ThreadPoolExecutor(6) as ex:
            for c, k in ex.map(month_one, TAG):
                if k:
                    extra["skm"][c] = k
    save("hist.json", hist); save("extra.json", extra)
    log("hist idx", {c: len(v["k"]) for c, v in hist["idxk"].items()}, "| stocks", len(hist["sk"]), "monthly", len(extra["skm"]), "minute", len(extra["sm"]))
    return True


# ---------- 资金面：两融、北向成交 ----------
def refresh_funds():
    out = {}
    d = jget("https://datacenter-web.eastmoney.com/api/data/v1/get", {"reportName": "RPTA_RZRQ_LSHJ", "columns": "ALL", "sortColumns": "DIM_DATE", "sortTypes": -1, "pageSize": 15})
    rows = ((d or {}).get("result") or {}).get("data") or []
    out["margin"] = [{"d": r["DIM_DATE"][:10], "rzye亿": r2((r.get("RZYE") or 0) / 1e8), "rz净买入亿": r2((r.get("RZJME") or 0) / 1e8), "rzmre亿": r2((r.get("RZMRE") or 0) / 1e8)} for r in rows]
    d = jget("https://datacenter-web.eastmoney.com/api/data/v1/get", {"reportName": "RPT_MUTUAL_DEAL_HISTORY", "columns": "ALL", "sortColumns": "TRADE_DATE", "sortTypes": -1, "pageSize": 30,
                                                                     "filter": '(MUTUAL_TYPE="005")'})      # 005 = 北向合计（2024-08 起不再披露净买入，只有成交额）
    rows = ((d or {}).get("result") or {}).get("data") or []
    out["north"] = [{"d": r["TRADE_DATE"][:10], "成交亿": r2((r.get("DEAL_AMT") or 0) / 100)} for r in rows if r.get("DEAL_AMT")]
    save("funds.json", out)
    log("funds margin", len(out["margin"]), "north", len(out["north"]))
    return True

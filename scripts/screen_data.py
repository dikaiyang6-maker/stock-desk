"""选股板块（D 型科技成长、F 型短线波段）用的数据。

    refresh_ext()   每天：研发投入、资产负债率、货币资金、商誉、TTM 营收、未来 90 天限售解禁、
                    近 180 天大股东和高管减持（东方财富数据中心）；主营产品构成每周一次（只取科技赛道的股票）
    refresh_tech()  每个交易日收盘后：全市场近 260 个交易日日K（腾讯，前复权）算均线、涨幅、成交额、换手、
                    涨停与连板，以及最近 10 个交易日的市场情绪

结果写在 data/screen.json，gen.py 原样放进网页的 D.sc。所有比例都是百分数，金额单位亿元。"""
import datetime as dt
import math
import re
from concurrent.futures import ThreadPoolExecutor

from common import a_sym, bj_now, close_date, done, due, jget, load, log, num, r2, save, tx_daily

EM = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# D 型的八个方向（文档第二部分 D）：东方财富行业 + 本站题材标签
TRACKS = {
    "半导体": ["半导体"],
    "AI算力": ["计算机设备", "元件", "IT服务Ⅱ"],
    "软件": ["软件开发"],
    "通信": ["通信设备", "通信服务", "光学光电子"],
    "机器人": ["自动化设备", "电机Ⅱ"],
    "创新药": ["化学制药", "生物制品"],
    "高端装备": ["专用设备", "航空装备Ⅱ", "航天装备Ⅱ", "航海装备Ⅱ", "军工电子Ⅱ", "轨交设备Ⅱ"],
    "新材料": ["金属新材料", "电子化学品Ⅱ", "非金属材料Ⅱ"],
}
THEME_TRACK = {"AI算力服务器": "AI算力", "光通信": "通信", "AI PCB/覆铜板": "AI算力", "AI芯片/存储": "半导体", "半导体设备/制造": "半导体",
               "机器人": "机器人", "AI软件/视觉": "软件", "核聚变/超导": "高端装备", "商业航天/军工电子": "高端装备", "量子科技": "通信"}
# 主营产品名称里出现这些词，算作该方向的“相关业务”（估算，用于题材真伪的初步判断）
TRACK_KW = {
    "半导体": "芯片|集成电路|半导体|晶圆|封装|封测|存储|MCU|传感器|功率器件|模拟|SoC|GPU|CPU|FPGA|光刻|刻蚀|薄膜|硅片|靶材|电子特气|分立器件|处理器",
    "AI算力": "服务器|算力|云|数据中心|AI|人工智能|GPU|PCB|印制电路|电路板|覆铜板|计算|存储|交换机|液冷|智算",
    "软件": "软件|系统|平台|信息化|云|SaaS|数据|解决方案|技术服务|AI|人工智能|网络安全|操作系统|数据库|智能",
    "通信": "光模块|光器件|光纤|光缆|通信|基站|射频|天线|交换|路由|光芯片|激光|卫星|网络设备|光电|光学|量子",
    "机器人": "机器人|减速器|伺服|电机|控制器|丝杠|传感器|自动化|关节|灵巧手|运动控制|驱动",
    "创新药": "药|制剂|生物|抗体|疫苗|创新|医药|CXO|研发服务|细胞|基因",
    "高端装备": "装备|设备|航空|航天|发动机|雷达|军工|导弹|卫星|船舶|仪器|仪表|机床|激光|轨道|电子|系统",
    "新材料": "材料|合金|稀土|碳纤维|复合|特种|膜|靶材|电子化学|高纯|陶瓷|石墨|钛|硅|气体",
}
SKIP_ITEM = re.compile(r"^(其他|其它|合计|总计|分部间抵销|内部抵销|抵销|补充)")
LIMIT = {"sh": 10, "sz": 10, "gem": 20, "star": 20, "bj": 30}   # 主板 ST 自 2026-07-06 起也是 ±10%


def em_all(report, flt, cols, sort="SECURITY_CODE", ps=500, lanes=4):
    """东方财富数据中心分页全取；任何一页失败就返回 None（沿用缓存，避免只拿到一部分数据）。"""
    base = {"reportName": report, "columns": cols, "filter": flt, "pageSize": ps, "source": "WEB", "client": "WEB",
            "sortColumns": sort, "sortTypes": ",".join(["1"] * len(sort.split(",")))}
    first = jget(EM, {**base, "pageNumber": 1})
    if not first:
        return None
    if not first.get("result"):
        return [] if first.get("code") == 9201 or "为空" in str(first.get("message")) else None   # 9201：查询结果为空
    res = first["result"]
    pages, rows = res.get("pages") or 0, list(res.get("data") or [])
    if pages > 1:
        with ThreadPoolExecutor(lanes) as ex:
            outs = list(ex.map(lambda n: jget(EM, {**base, "pageNumber": n}), range(2, pages + 1)))
        if any(not o or not o.get("result") for o in outs):
            return None
        for o in outs:
            rows += o["result"].get("data") or []
    return rows


def yi(v):
    v = num(v)
    return None if v is None else round(v / 1e8, 2)


def track_of(ind, tags):
    out = [t for t, inds in TRACKS.items() if ind in inds]
    out += [THEME_TRACK[g] for g in tags if g in THEME_TRACK and THEME_TRACK[g] not in out]
    return out


def _rows_by_code(rows, key="SECURITY_CODE"):
    out = {}
    for r in rows or []:
        out.setdefault(r[key], []).append(r)
    return out


def _shift_year(d, k=-1):
    return f"{int(d[:4]) + k}{d[4:]}"


# ---------- 东方财富：财务与股东行为 ----------
def refresh_ext():
    a, rep = load("a.json"), load("reports.json")
    if not a or not rep:
        log("screen ext: 没有行情或财报缓存，跳过"); return False
    I = {c: i for i, c in enumerate(a["cols"])}
    codes = {r[I["code"]] for r in a["rows"]}
    periods = sorted(rep["dates"], reverse=True)                      # 例如 ['2026-09-30', '2026-06-30']
    fy = f"{int(periods[-1][:4]) - (0 if periods[-1].endswith('12-31') else 1)}-12-31"   # 最近一个已披露完的年报
    old = load("screen.json", {}).get("ext", {})
    ext = {c: {} for c in codes}
    pin = lambda ps: "(" + ",".join(f"'{p}'" for p in ps) + ")"

    # 1. 研发投入：利润表（研发费用 / 营业总收入），取每只股票最新的一期
    rd = em_all("RPT_F10_FINANCE_GINCOME", f"(REPORT_DATE in {pin(periods + [fy])})(SECURITY_TYPE_CODE=\"058001001\")",
                "SECURITY_CODE,REPORT_DATE,RESEARCH_EXPENSE,TOTAL_OPERATE_INCOME", sort="SECURITY_CODE,REPORT_DATE")
    # 2. 资产负债表：资产负债率、货币资金、所有者权益
    bal = em_all("RPT_DMSK_FN_BALANCE", f"(REPORT_DATE in {pin(periods)})", "SECURITY_CODE,REPORT_DATE,DEBT_ASSET_RATIO,MONETARYFUNDS,TOTAL_EQUITY",
                 sort="SECURITY_CODE,REPORT_DATE")
    # 3. 商誉
    gw = em_all("RPT_GOODWILL_STOCKDETAILS", f"(REPORT_DATE in {pin(periods)})", "SECURITY_CODE,REPORT_DATE,GOODWILL", sort="SECURITY_CODE,REPORT_DATE")
    # 4. TTM 营收：最近年报 + 本期累计 − 上年同期累计
    prev = sorted({_shift_year(p) for p in periods if not p.endswith("12-31")})
    cpd = em_all("RPT_LICO_FN_CPD", f"(REPORTDATE in {pin([fy] + prev)})", "SECURITY_CODE,REPORTDATE,TOTAL_OPERATE_INCOME", sort="SECURITY_CODE,REPORTDATE")
    if None in (rd, bal, gw, cpd):
        log("screen ext: 东方财富财务数据不完整，沿用缓存"); return False

    def latest(rows_by, c, field, date_key="REPORT_DATE"):
        for r in sorted(rows_by.get(c, []), key=lambda r: r[date_key], reverse=True):
            if num(r.get(field)) is not None:
                return r
        return None
    rdb, balb, gwb, cpdb = _rows_by_code(rd), _rows_by_code(bal), _rows_by_code(gw), _rows_by_code(cpd)
    cur = {d: {} for d in periods}
    for d in periods:
        for r in rep["rows"][d]:
            cur[d][r["SECURITY_CODE"]] = num(r.get("TOTAL_OPERATE_INCOME"))
    for c in codes:
        e = ext[c]
        r = latest(rdb, c, "RESEARCH_EXPENSE")
        if r and num(r["TOTAL_OPERATE_INCOME"]):
            e["rd"] = round(num(r["RESEARCH_EXPENSE"]) / num(r["TOTAL_OPERATE_INCOME"]) * 100, 1)
            e["rdP"] = r["REPORT_DATE"][:10]
        r = latest(balb, c, "TOTAL_EQUITY")
        if r:
            e["debt"] = r2(num(r["DEBT_ASSET_RATIO"])); e["cash"] = yi(r["MONETARYFUNDS"]); e["eq"] = yi(r["TOTAL_EQUITY"])
            g = next((x for x in gwb.get(c, []) if x["REPORT_DATE"] == r["REPORT_DATE"]), None)
            e["gw"] = yi(g["GOODWILL"]) if g else 0.0
        fyv = next((num(x["TOTAL_OPERATE_INCOME"]) for x in cpdb.get(c, []) if x["REPORTDATE"][:10] == fy), None)
        p = next((d for d in periods if cur[d].get(c) is not None), None)
        if fyv is not None and p:
            if p.endswith("12-31"):
                e["revT"] = yi(cur[p][c])
            else:
                pv = next((num(x["TOTAL_OPERATE_INCOME"]) for x in cpdb.get(c, []) if x["REPORTDATE"][:10] == _shift_year(p)), None)
                if pv is not None:
                    e["revT"] = yi(fyv + cur[p][c] - pv)

    # 5. 未来 90 天限售解禁（占总股本比例）
    today = bj_now().date()
    lift = em_all("RPT_LIFT_STAGE", f"(FREE_DATE>='{today}')(FREE_DATE<='{today + dt.timedelta(days=90)}')", "SECURITY_CODE,FREE_DATE,TOTAL_RATIO,FREE_SHARES_TYPE",
                  sort="SECURITY_CODE,FREE_DATE")
    # 6. 近 180 天减持：持股 5% 以上股东（股东增减持）与董监高（二级市场买卖）
    since = today - dt.timedelta(days=180)
    hold = em_all("RPT_SHARE_HOLDER_INCREASE", f"(END_DATE>='{since}')(DIRECTION=\"减持\")", "SECURITY_CODE,END_DATE,HOLDER_NAME,AFTER_CHANGE_RATE",
                  sort="SECURITY_CODE,END_DATE")
    exe = em_all("RPT_EXECUTIVE_HOLD_DETAILS", f"(CHANGE_DATE>='{since}')(CHANGE_SHARES<0)(CHANGE_REASON=\"二级市场买卖\")", "SECURITY_CODE,CHANGE_DATE,PERSON_NAME,CHANGE_AMOUNT",
                 sort="SECURITY_CODE,CHANGE_DATE")
    if None in (lift, hold, exe):
        log("screen ext: 解禁或减持数据不完整，这几项沿用缓存")
    for c, rs in _rows_by_code(lift).items():
        if c in ext:
            ext[c]["lift"] = [rs[0]["FREE_DATE"][:10], round(sum(num(x["TOTAL_RATIO"]) or 0 for x in rs) * 100, 2), rs[0]["FREE_SHARES_TYPE"]]
    for c, rs in _rows_by_code(hold).items():
        if c in ext:
            ext[c]["hs"] = [len(rs), max(x["END_DATE"][:10] for x in rs), round(sum(num(x["AFTER_CHANGE_RATE"]) or 0 for x in rs), 2)]
    for c, rs in _rows_by_code(exe).items():
        if c in ext:
            ext[c]["es"] = [len(rs), max(x["CHANGE_DATE"][:10] for x in rs), round(-sum(num(x["CHANGE_AMOUNT"]) or 0 for x in rs) / 1e8, 2)]   # 次数、最后日期、合计金额（亿元）
    if lift is None or hold is None or exe is None:
        for c, e in ext.items():
            for k, src in (("lift", lift), ("hs", hold), ("es", exe)):
                if src is None and k in old.get(c, {}):
                    e[k] = old[c][k]

    # 7. 主营产品构成（科技赛道的股票，每周一次）：最新半年报/年报的按产品分类，没有就用按行业分类
    mo = load("screen.json", {}).get("mo", {})
    if due("screen_mainop", 24 * 7) or not mo:
        uni = sorted(c for c in codes if track_of(_ind(a, I, c), _tags(a, I, c)))
        got = _mainop(uni, [p for p in periods + [fy] if p.endswith(("06-30", "12-31"))])
        if got is not None:
            mo = got; done("screen_mainop")
            log("screen mainop", len(mo), "/", len(uni))
    out = load("screen.json", {})
    out.update({"ext": {c: e for c, e in ext.items() if e}, "extAsof": bj_now().strftime("%Y-%m-%d %H:%M"), "extFy": fy, "mo": mo,
                "tracks": TRACKS, "themeTrack": THEME_TRACK, "trackKw": TRACK_KW})
    save("screen.json", out)
    n = lambda k: sum(1 for e in ext.values() if k in e)
    log("screen ext", {k: n(k) for k in ("rd", "debt", "revT", "lift", "hs", "es")}, "fy", fy)
    return True


_AIDX = {}


def _index(a, I):
    key = id(a)
    if key not in _AIDX:
        _AIDX.clear(); _AIDX[key] = {r[I["code"]]: r for r in a["rows"]}
    return _AIDX[key]


def _ind(a, I, c):
    return _index(a, I)[c][I["ind"]]


def _tags(a, I, c):
    return _index(a, I)[c][I["tags"]] or []


def _mainop(codes, dates):
    """按 100 只一批取主营构成；每只股票取最新一期，优先按产品分类。"""
    def batch(cs):
        flt = f"(REPORT_DATE in ({','.join(repr(d) for d in dates)}))(MAINOP_TYPE in (\"1\",\"2\"))(SECURITY_CODE in ({','.join(chr(34) + c + chr(34) for c in cs)}))"
        return em_all("RPT_F10_FN_MAINOP", flt, "SECURITY_CODE,REPORT_DATE,MAINOP_TYPE,ITEM_NAME,MBI_RATIO,RANK", sort="SECURITY_CODE,REPORT_DATE,MAINOP_TYPE,RANK", lanes=2)
    parts = [codes[i:i + 100] for i in range(0, len(codes), 100)]
    with ThreadPoolExecutor(3) as ex:
        outs = list(ex.map(batch, parts))
    if any(o is None for o in outs):
        return None
    res = {}
    for c, rs in _rows_by_code([r for o in outs for r in o]).items():
        best = max(rs, key=lambda r: (r["REPORT_DATE"], r["MAINOP_TYPE"]))             # 最新一期；同一期里“2=按产品”优先于“1=按行业”
        items = [r for r in rs if r["REPORT_DATE"] == best["REPORT_DATE"] and r["MAINOP_TYPE"] == best["MAINOP_TYPE"]
                 and not SKIP_ITEM.match(r["ITEM_NAME"] or "") and (num(r["MBI_RATIO"]) or 0) > 0]
        items.sort(key=lambda r: -(num(r["MBI_RATIO"]) or 0))
        if items:
            res[c] = {"d": best["REPORT_DATE"][:10], "t": "产品" if best["MAINOP_TYPE"] == "2" else "行业",
                      "it": [[r["ITEM_NAME"][:24], round(num(r["MBI_RATIO"]) * 100, 1)] for r in items[:6]]}
    return res


# ---------- 腾讯日K：技术面与市场情绪 ----------
TECH_COLS = ["ma5", "ma10", "ma20", "ma60", "ma250", "ma5p", "ma10p", "ma60p", "r5", "r20", "r60", "r250", "amt5", "amt20", "tr5", "vr5", "h20", "lb", "zt5", "zb", "first", "n"]


def tech_due():
    """有某个交易日收盘后的完整行情、那天的日K统计还没算过（或上次用的行情不完整）时运行；从没算过时随时运行。"""
    sc = load("screen.json", {})
    if not sc.get("techAsof"):
        return True
    d = close_date()
    return bool(d) and (sc.get("techDate") != d or sc.get("techN", 0) < 5000)


def _lim_price(prev, lim, up=True):
    """交易所规则：前收盘价 ×（1 ± 涨跌幅）后四舍五入到分。"""
    return math.floor(prev * (1 + (lim if up else -lim) / 100) * 100 + 0.5 + 1e-7) / 100


def _is_zt(bar, prev, lim):
    return bool(prev) and bar[2] >= _lim_price(prev, lim) - 0.001


def _is_zb(bar, prev, lim):
    return bool(prev) and bar[3] >= _lim_price(prev, lim) - 0.001 and bar[2] < _lim_price(prev, lim) - 0.001   # 盘中触及涨停、收盘没封住


def _is_dt(bar, prev, lim):
    return bool(prev) and bar[2] <= _lim_price(prev, lim, False) + 0.001


def _stats(bars, board, fsh):
    """bars: [[日期,开,收,高,低,量(手),额(万元)]]，前复权。返回 TECH_COLS 对应的数值。"""
    n = len(bars)
    if n < 6:
        return None
    c = [b[2] for b in bars]
    ma = lambda k, off=0: round(sum(c[n - off - k:n - off]) / k, 3) if n - off >= k else None
    ret = lambda k: round((c[-1] / c[-1 - k] - 1) * 100, 2) if n > k and c[-1 - k] else None
    amt = lambda k: round(sum(b[6] for b in bars[-k:]) / k / 1e4, 2) if n >= k else None
    lim = LIMIT[board]
    zt = [i > 0 and i >= 5 if n < 260 else i > 0 for i in range(n)]       # 新股上市前 5 日不设涨跌幅
    zt = [ok and _is_zt(bars[i], c[i - 1], lim) for i, ok in enumerate(zt)]
    lb = 0
    for i in range(n - 1, 0, -1):
        if not zt[i]:
            break
        lb += 1
    v5 = sum(b[5] for b in bars[-5:]) / 5
    vprev = sum(b[5] for b in bars[-6:-1]) / 5 if n >= 6 else None
    zb = 1 if _is_zb(bars[-1], c[-2], lim) and (n >= 260 or n > 5) else 0
    return [ma(5), ma(10), ma(20), ma(60), ma(250), ma(5, 1), ma(10, 1), ma(60, 5), ret(5), ret(20), ret(60), ret(250), amt(5), amt(20),
            round(v5 * 100 / fsh * 100, 2) if fsh else None, round(bars[-1][5] / vprev, 2) if vprev else None,
            round(max(b[3] for b in bars[-21:-1]), 3) if n >= 21 else None, lb, sum(1 << k for k in range(5) if zt[n - 1 - k]), zb, bars[0][0], n], zt


def refresh_tech():
    a = load("a.json")
    if not a:
        return False
    I = {c: i for i, c in enumerate(a["cols"])}
    rows = a["rows"]
    if len(rows) < 5000:
        log("tech: 行情只有", len(rows), "只，不完整，沿用缓存"); return False
    if "fsh" not in I:
        log("tech: 行情缓存里还没有流通股本，先跑一次 market"); return False
    t0 = dt.datetime.now()
    with ThreadPoolExecutor(8) as ex:
        ks = list(ex.map(lambda r: tx_daily(a_sym(r[I["code"]]), count=260), rows))
    tech, by_day = {}, {}
    for r, bars in zip(rows, ks):
        res = _stats(bars, r[I["board"]], r[I["fsh"]]) if bars else None
        if not res:
            continue
        vals, zt = res
        tech[r[I["code"]]] = vals
        lim = LIMIT[r[I["board"]]]
        for i in range(max(1, len(bars) - 11), len(bars)):                 # 最近 11 个交易日逐日统计
            b, pc = bars[i], bars[i - 1][2]
            ch = (b[2] / pc - 1) * 100 if pc else 0
            d = by_day.setdefault(b[0], {"up": 0, "dn": 0, "zt": 0, "dt": 0, "zb": 0, "dn5": 0, "amt": 0.0, "ztc": [], "lb": 0})
            d["amt"] += b[6] / 1e4
            d["up" if ch > 0 else "dn" if ch < 0 else "flat"] = d.get("up" if ch > 0 else "dn" if ch < 0 else "flat", 0) + 1
            if ch <= -5:
                d["dn5"] += 1
            if zt[i]:
                d["zt"] += 1; d["ztc"].append(r[I["code"]])
            elif (len(bars) >= 260 or i >= 5) and _is_zb(b, pc, lim):
                d["zb"] += 1
            if (len(bars) >= 260 or i >= 5) and _is_dt(b, pc, lim):
                d["dt"] += 1
    ok = len(tech)
    if ok < len(rows) * 0.9:
        log("tech: 只拿到", ok, "/", len(rows), "只股票的K线，沿用缓存"); return False
    # 每日连板高度与昨日涨停股的次日表现
    days = sorted(by_day)[-11:]
    kmap = {r[I["code"]]: k for r, k in zip(rows, ks) if k}
    emo = []
    for j, d in enumerate(days):
        x = by_day[d]
        nxt = []
        if j > 0:
            for code in by_day[days[j - 1]]["ztc"]:
                k = kmap.get(code); i = next((i for i, b in enumerate(k) if b[0] == d), None) if k else None
                if i:
                    nxt.append((k[i][2] / k[i - 1][2] - 1) * 100)
        streak = {}
        for code in x["ztc"]:
            k = kmap[code]; i = next(i for i, b in enumerate(k) if b[0] == d); s = 0
            while i > 0 and _is_zt(k[i], k[i - 1][2], _board_lim(code)):
                s += 1; i -= 1
            streak[code] = s
        emo.append({"d": d, "up": x.get("up", 0), "dn": x.get("dn", 0), "zt": x["zt"], "dt": x["dt"], "zb": x["zb"], "dn5": x["dn5"], "amt": round(x["amt"], 0),
                    "lb": max(streak.values(), default=0), "nx": round(sum(nxt) / len(nxt), 2) if nxt else None})
    emo = emo[1:]                                                              # 第一天缺前一日数据，只用来算次日表现
    # 全市场成交额的 20 日均值（用最近 25 个交易日）
    amt_day = {}
    for k in ks:
        for b in (k or [])[-25:]:
            amt_day[b[0]] = amt_day.get(b[0], 0) + b[6] / 1e4
    ad = [amt_day[d] for d in sorted(amt_day)[-21:-1]]
    out = load("screen.json", {})
    out.update({"tech": tech, "techCols": TECH_COLS, "techAsof": bj_now().strftime("%Y-%m-%d %H:%M"), "techDate": days[-1] if days else None, "techN": len(rows),
                "emo": emo, "amt20": round(sum(ad) / len(ad), 0) if ad else None})
    save("screen.json", out)
    log("tech", ok, "/", len(rows), "stocks in", (dt.datetime.now() - t0).seconds, "s | last day", emo[-1] if emo else None)
    return True


_BOARD = {}


def _board_lim(code):
    if not _BOARD:
        a = load("a.json"); I = {c: i for i, c in enumerate(a["cols"])}
        _BOARD.update({r[I["code"]]: r[I["board"]] for r in a["rows"]})
    return LIMIT[_BOARD.get(code, "sh")]

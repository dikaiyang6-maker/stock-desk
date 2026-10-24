"""资金异动选股与监控（网页「资金监控」页）：每个交易日 A 股收盘后运行一次。

选股：先按当天的主力资金（新浪口径，主力 = 特大单）、盘口异动（东方财富 22 类）、龙虎榜和量价从全市场粗筛，
     再取候选股近 30 个交易日的资金流向细算连续性，分两类打分：强势异动（涨停或涨幅 ≥7%，看当天资金力度和异动）、
     资金吸筹（涨得不多但主力连续 ≥3 日流入），满 45 分的各取最多 4 只、同一行业最多 2 只（核心入选，每天最多 8 只）；
     监控池不足 30 只时，两类轮流从 35 分以上的补充入选到 30 只（同一行业当天最多 3 只）。监控池上限 50 只。
评分：同一套公式每天按最新数据重算（今日评分），页面显示每一项的得分。
详细信息：资金流入流出明细（特大单、大单、小单、散单）、主力控盘与持股（东方财富千股千评、十大流通股东、机构持仓、股东户数）、
     散户占比（小单和散单成交占比、十大流通股东以外的持股）、筹码分布（按日K和换手率估算）、龙虎榜买卖席位。
监控：池里每只股票每天更新主力净流入、涨跌、异动、龙虎榜、公告和新闻。
     出现“主力放弃”（连续 3 日流出且吐出入选以来流入的一半、放量大跌出货、跌破 20 日线且流出、龙虎榜机构净卖出）
     或其他因素（利空公告、较入选价跌超 12%、入选满 10 天资金热度消退、监控池已满）时进入观察期，
     再观察 5 个交易日（约一周）：期间主力回流（连续 2 日净流入且站上 10 日线）就恢复跟踪，否则移出。
DeepSeek：对新入选的股票做全面调查，对状态变化写说明（有 DEEPSEEK_API_KEY 时）。
节假日（当天没有 A 股收盘数据，或资金数据不是当天的）不运行，沿用上一个交易日的结果。
同一天重复运行时从当天开始前的状态重算，不会重复计观察天数。"""
import copy
import datetime as dt
import json
import os
from concurrent.futures import ThreadPoolExecutor

import requests

from common import a_sym, bj_now, get, jget, load, log, num, save, tx_daily

SINA = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow."
EMX, EMX_UT = "https://push2ex.eastmoney.com/", "7eea3edcaed734bea9cbfc24409ed989"
EM = "https://datacenter-web.eastmoney.com/api/data/v1/get"
MOVES = {8201: "火箭发射", 8202: "快速反弹", 8193: "大笔买入", 4: "封涨停板", 32: "打开跌停板", 64: "有大买盘", 8207: "竞价上涨", 8209: "高开5日线",
         8211: "向上缺口", 8213: "60日新高", 8215: "60日大幅上涨", 8204: "加速下跌", 8203: "高台跳水", 8194: "大笔卖出", 8: "封跌停板",
         16: "打开涨停板", 128: "有大卖盘", 8208: "竞价下跌", 8210: "低开5日线", 8212: "向下缺口", 8214: "60日新低", 8216: "60日大幅下跌"}
MOVE_W = {"火箭发射": 2, "快速反弹": 1, "封涨停板": 3, "打开跌停板": 1, "有大买盘": 1, "竞价上涨": .5, "高开5日线": .5, "向上缺口": 1, "60日新高": 2,
          "加速下跌": -2, "高台跳水": -2, "封跌停板": -3, "打开涨停板": -2, "有大卖盘": -1, "竞价下跌": -.5, "低开5日线": -.5, "向下缺口": -1, "60日新低": -2, "60日大幅下跌": -1}
BAD_ANN = ("减持", "立案", "调查", "处罚", "问询函", "监管函", "关注函", "警示", "风险提示", "退市", "冻结", "平仓", "预亏", "预减", "下修", "亏损", "终止", "违规", "诉讼", "仲裁", "被执行")
GOOD_ANN = ("中标", "重大合同", "签订", "回购", "增持", "预增", "预盈", "扭亏", "定点", "订单", "获批", "注册生效")
NEUTRAL_ANN = ("回复", "回函", "异常波动", "更正", "补充", "法律意见", "不减持", "终止减持", "提前终止", "实施完毕", "回购注销", "进展")   # 例行回复、更正、承诺不减持等不算利好利空
NEW_PER_DAY, POOL_MIN, POOL_MAX, WATCH_DAYS, STOP_LOSS = 8, 30, 50, 5, -12
CORE_MIN, FILL_MIN = 45, 35                             # 核心入选、补充入选的最低分
BIG_BOARD = ("gem", "star", "bj")
f2 = lambda v, d=2: None if v is None else round(v, d)


def clip(x):
    return max(0.0, min(1.0, x))


# ---------- 数据 ----------
def sina_market():
    """全市场今日资金：{代码: {m 主力净流入亿, mr 占成交额%, net 全部净流入亿, amt 成交额亿, cps 主力罗盘}}。"""
    rows = []
    for pg in range(1, 20):
        d = jget(SINA + "ssl_bkzj_ssggzj", {"page": pg, "num": 500, "sort": "r0_net", "asc": 0})
        if not isinstance(d, list):
            return None
        rows += d
        if len(d) < 500:
            break
    out = {}
    for r in rows:
        s = r.get("symbol") or ""
        if s[:2] in ("sh", "sz", "bj"):
            out[s[2:]] = {"m": (num(r.get("r0_net")) or 0) / 1e8, "mr": (num(r.get("r0_ratio")) or 0) * 100, "net": (num(r.get("netamount")) or 0) / 1e8,
                          "amt": (num(r.get("amount")) or 0) / 1e8, "cps": num(r.get("r0x_ratio"))}
    return out


def sina_hist(code, n=30):
    """个股每日资金：新到旧 [[日期, 收盘, 涨跌%, 换手%, 成交额亿, 全部净流入亿, 主力（特大单）净流入亿, 大单净流入亿, 小单净流入亿, 散单净流入亿,
    特大单成交占比%, 小单和散单成交占比%], ...]。"""
    d = jget(SINA + "ssl_qsfx_lscjfb", {"page": 1, "num": n, "sort": "opendate", "asc": 0, "daima": a_sym(code)}, tries=3)
    if not isinstance(d, list):
        return None
    out = []
    for r in d:
        v = {k: (num(r.get(k)) or 0) / 1e8 for k in ("r0", "r1", "r2", "r3", "netamount", "r0_net", "r1_net", "r2_net", "r3_net")}
        amt = v["r0"] + v["r1"] + v["r2"] + v["r3"]
        out.append([r["opendate"], num(r.get("trade")), f2((num(r.get("changeratio")) or 0) * 100), f2((num(r.get("turnover")) or 0) / 100), f2(amt, 3),
                    f2(v["netamount"], 3), f2(v["r0_net"], 3), f2(v["r1_net"], 3), f2(v["r2_net"], 3), f2(v["r3_net"], 3),
                    f2(v["r0"] / amt * 100, 1) if amt else None, f2((v["r2"] + v["r3"]) / amt * 100, 1) if amt else None])
    return out


def em_moves(date):
    """当天盘口异动：({代码: {类型名: 次数, "_buy": 大笔买入额亿, "_sell": 大笔卖出额亿, "_ev": [[时间, 类型名], ...]}}, {类型名: 全市场次数})；数据不是当天的返回 None。"""
    out, probe, tc = {}, None, {}
    for ty, name in MOVES.items():
        d = jget(EMX + "getAllStockChanges", {"type": ty, "pageindex": 0, "pagesize": 5000, "ut": EMX_UT, "dpt": "wzchanges"})
        tc[name] = ((d or {}).get("data") or {}).get("tc") or 0
        for x in ((d or {}).get("data") or {}).get("allstock") or []:
            o = out.setdefault(x["c"], {"_buy": 0.0, "_sell": 0.0, "_ev": []})
            o[name] = o.get(name, 0) + 1
            if ty in (8193, 8194):
                i = (x.get("i") or "").split(",")
                o["_buy" if ty == 8193 else "_sell"] += (num(i[3]) if len(i) > 3 else 0) or 0
            tm = f'{x["tm"] // 10000:02d}:{x["tm"] // 100 % 100:02d}'
            if MOVE_W.get(name) and len(o["_ev"]) < 40:
                o["_ev"].append([tm, name])
            probe = probe or (x["c"], x["m"])
    if not probe:
        return None
    d = jget(EMX + "getStockChanges", {"ut": EMX_UT, "date": date.replace("-", ""), "dpt": "wzchanges", "code": probe[0], "market": probe[1]})
    if str(((d or {}).get("data") or {}).get("d")) != date.replace("-", ""):
        log("flow: 盘口异动不是", date, "的数据，跳过异动"); return None
    for o in out.values():
        o["_buy"] = round(o["_buy"] / 1e8, 3); o["_sell"] = round(o["_sell"] / 1e8, 3); o["_ev"].sort()
    return out, tc


def em_all(report, flt, cols, sort="TRADE_DATE"):
    rows, page = [], 1
    while True:
        d = jget(EM, {"reportName": report, "columns": cols, "filter": flt, "pageSize": 500, "pageNumber": page, "sortColumns": sort, "sortTypes": -1})
        res = (d or {}).get("result") or {}
        if d is None:
            return None
        rows += res.get("data") or []
        if page >= (res.get("pages") or 1):
            return rows
        page += 1


LHB_COLS = "SECURITY_CODE,TRADE_DATE,EXPLAIN,EXPLANATION,BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,DEAL_NET_RATIO"


def lhb_rows(rows):
    """龙虎榜：{代码: [[日期, 上榜原因, 机构/营业部概况, 净买入亿], ...]}，同一天多条原因只留一条（净额相同）。"""
    out = {}
    for r in rows or []:
        x = out.setdefault(r["SECURITY_CODE"], [])
        d = r["TRADE_DATE"][:10]
        if any(e[0] == d for e in x):
            continue
        x.append([d, (r.get("EXPLANATION") or "").strip(), (r.get("EXPLAIN") or "").strip(), f2((num(r.get("BILLBOARD_NET_AMT")) or 0) / 1e8, 3)])
    return out


def anns(code):
    """最近公告 [[日期, 标题, 链接, 利好/利空/""], ...]。"""
    d = jget("https://np-anotice-stock.eastmoney.com/api/security/ann", {"sr": -1, "page_size": 10, "page_index": 1, "ann_type": "A", "client_source": "web",
                                                                         "stock_list": code, "f_node": 0, "s_node": 0}, tries=2)
    out = []
    for x in ((d or {}).get("data") or {}).get("list") or []:
        t = (x.get("title_ch") or x.get("title") or "").strip()
        tag = "" if any(k in t for k in NEUTRAL_ANN) and not any(k in t for k in ("立案", "处罚", "退市", "冻结")) else \
            "利空" if any(k in t for k in BAD_ANN) else "利好" if any(k in t for k in GOOD_ANN) else ""
        out.append([(x.get("notice_date") or "")[:10], t, f'https://data.eastmoney.com/notices/detail/{code}/{x.get("art_code")}.html', tag])
    return out if d else None


def news(code):
    """最近新闻 [[时间, 标题, 链接], ...]（东方财富个股资讯）。"""
    m = "1" if a_sym(code).startswith("sh") else "0"
    d = jget("https://np-listapi.eastmoney.com/comm/web/getListInfo", {"client": "web", "biz": "web_voice", "mTypeAndCode": f"{m}.{code}", "pageSize": 8, "type": 1, "req_trace": "flow"}, tries=2)
    lst = ((d or {}).get("data") or {}).get("list") or []
    return [[(x.get("Art_ShowTime") or "")[:16], (x.get("Art_Title") or "").strip(), x.get("Art_Url") or ""] for x in lst][:8] if d else None


def pmap(fn, items, lanes=4):
    with ThreadPoolExecutor(lanes) as ex:
        return dict(zip(items, ex.map(fn, items)))


# ---------- 指标 ----------
def metrics(h):
    """由 sina_hist（新到旧）算资金连续性和均线。"""
    m = [r[6] or 0 for r in h]; c = [r[1] for r in h if r[1]]
    ind = 0
    while ind < len(m) and m[ind] > 0:
        ind += 1
    outd = 0
    while outd < len(m) and m[outd] < 0:
        outd += 1
    ma = lambda p: sum(c[:p]) / p if len(c) >= p else None
    amt5 = sum(r[4] or 0 for r in h[:5])
    return {"m": m[0] if m else 0, "m3": sum(m[:3]), "m5": sum(m[:5]), "m10": sum(m[:10]), "in": ind, "out": outd, "ma5": ma(5), "ma10": ma(10), "ma20": ma(20),
            "c": c[0] if c else None, "mr5": sum(m[:5]) / amt5 * 100 if amt5 else 0}


def move_score(mv):
    if not mv:
        return 0.0
    s = sum(MOVE_W.get(k, 0) * min(v, 3) for k, v in mv.items() if not k.startswith("_"))
    return s + max(-3.0, min(3.0, (mv.get("_buy", 0) - mv.get("_sell", 0)) * 4))     # 大笔买卖净额，每 0.25 亿 1 分，最多 ±3


def move_text(mv, sign=0):
    """异动摘要；sign>0 只列偏多的，<0 只列偏空的。"""
    if not mv:
        return ""
    xs = [f"{k}×{v}" if v > 1 else k for k, v in sorted(mv.items(), key=lambda t: -abs(MOVE_W.get(t[0], 0))) if not k.startswith("_") and MOVE_W.get(k)
          and (sign == 0 or MOVE_W[k] * sign > 0)]
    nb, ns = mv.get("大笔买入", 0), mv.get("大笔卖出", 0)
    if (nb or ns) and sign >= 0:
        xs.append(f"大笔买入 {nb} 笔 / 卖出 {ns} 笔（净 {mv['_buy'] - mv['_sell']:+.2f} 亿）")
    return "、".join(xs)


def risk_flags(r, today):
    e = r["e"]; add = lambda d, n: (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat()
    out = []
    if e.get("lift") and e["lift"][0] <= add(today, 30):
        out.append(f'{e["lift"][0][5:]} 解禁 {e["lift"][1]}%')
    if any(x and x >= add(today, -30) for x in ((e.get("hs") or [None, None])[1], (e.get("es") or [None, None])[1])):
        out.append("近 30 天有股东或高管减持")
    if (r["npY"] is not None and r["npY"] <= -50) or (r["np"] is not None and r["np"] < 0 and not ((r["npY"] or 0) > 0)):
        out.append("业绩亏损或大幅下滑")
    return out


# ---------- 选股 ----------
def universe():
    a, sc = load("a.json", {}), load("screen.json", {})
    I = {c: i for i, c in enumerate(a.get("cols", []))}
    tech, tc, ext = sc.get("tech", {}), {c: i for i, c in enumerate(sc.get("techCols", []))}, sc.get("ext", {})
    U = {}
    for row in a.get("rows", []):
        r = {c: row[i] for c, i in I.items()}
        t = tech.get(r["code"])
        r["t"] = {k: t[i] for k, i in tc.items()} if t else {}
        r["e"] = ext.get(r["code"]) or {}
        U[r["code"]] = r
    return U, a.get("asof", "")


def score_parts(kind, m, mr, k, cap5, mv, lh_net, qr):
    """评分：返回 (总分, [[项目, 得分, 满分], ...])。强势异动看当天的资金力度和异动，资金吸筹看主力连续流入；龙虎榜净买入 +10、净卖出 -10。"""
    ms = clip((move_score(mv) + 3) / 9)
    if kind == "强势异动":
        parts = [("当日主力占成交额（20% 满分）", 25, clip(mr / 20)), ("当日主力净流入（3 亿满分）", 20, clip(m / 3)), ("连续流入天数（5 天满分）", 15, clip(k["in"] / 5)),
                 ("5 日主力占流通市值（3% 满分）", 15, clip(cap5 / 3)), ("盘口异动", 15, ms), ("量比（3 倍满分）", 5, clip(((qr or 1) - 1) / 2))]
    else:
        parts = [("连续流入天数（5 天满分）", 30, clip(k["in"] / 5)), ("5 日主力占流通市值（2% 满分）", 25, clip(cap5 / 2)), ("5 日主力占成交额（10% 满分）", 20, clip(k["mr5"] / 10)),
                 ("当日主力占成交额（15% 满分）", 15, clip(mr / 15)), ("盘口异动", 10, ms)]
    pts = [[n, round(w * x, 1), w] for n, w, x in parts]
    if lh_net:
        pts.append(["龙虎榜净买入" if lh_net > 0 else "龙虎榜净卖出", 10 if lh_net > 0 else -10, 10])
    return round(sum(x[1] for x in pts), 1), pts


def select(U, F, MV, LHB, today, skip, need):
    """今天新入选的股票：[{c 代码, s 分数, why 理由, h 资金历史, parts 评分构成, kind 类型, tier 核心/补充}]。"""
    pre = []
    for c, r in U.items():
        f = F.get(c)
        if not f or r["st"] or c in skip or not r["price"] or r["lu"] is None or r["amt"] < (1 if r["board"] == "bj" else 3):
            continue
        if f["m"] < 0.3 or f["mr"] < 5 or r["chg"] < -2 or (r["ld"] and r["price"] <= r["ld"] + 0.0005):
            continue
        if (r["t"].get("r20") or 0) > 50 or risk_flags(r, today):
            continue
        lh = (LHB.get(c) or [[None, "", "", 0]])[0]
        pre.append((f["mr"] + min(f["m"], 10) * 2 + move_score(MV.get(c)) * 2 + (3 if lh[3] > 0 else -3 if lh[3] < 0 else 0) + min(r["qr"] or 0, 5), c))
    pre.sort(reverse=True)
    hot = lambda c: U[c]["chg"] >= 7 or U[c]["price"] >= U[c]["lu"] - 0.0005
    cands = [c for _, c in pre if hot(c)][:60] + [c for _, c in pre if not hot(c)][:100]
    H = pmap(lambda c: sina_hist(c, 30), cands)
    out = []
    for c in cands:
        h, r, f = H.get(c), U[c], F[c]
        if not h or h[0][0] != today:
            continue
        k = metrics(h); mv = MV.get(c); lh = (LHB.get(c) or [[None, "", "", 0]])[0]
        if k["m5"] <= 0 or not k["ma20"] or k["c"] < k["ma20"]:      # 上市不满 20 个交易日（没有 20 日线）的不选
            continue
        cap5 = k["m5"] / r["fmc"] * 100 if r["fmc"] else 0
        kind = "强势异动" if hot(c) else "资金吸筹"
        if kind == "强势异动" and not (k["in"] >= 2 or (f["m"] >= 1 and f["mr"] >= 10)):
            continue
        if kind == "资金吸筹" and (k["in"] < 3 or r["chg"] < -1):
            continue
        s, parts = score_parts(kind, f["m"], f["mr"], k, cap5, mv, lh[3], r["qr"])
        if s < FILL_MIN:
            continue
        why = [f'主力净流入 {f["m"]:.2f} 亿，占成交额 {f["mr"]:.1f}%']
        why.append(f'连续 {k["in"]} 日主力净流入，5 日累计 {k["m5"]:.2f} 亿（占流通市值 {cap5:.2f}%）' if k["in"] >= 2 else f'5 日主力累计 {k["m5"]:+.2f} 亿（占流通市值 {cap5:.2f}%）')
        if move_text(mv, 1):
            why.append("异动：" + move_text(mv, 1))
        if lh[0]:
            why.append(f'龙虎榜：{lh[2] or lh[1]}，净买入 {lh[3]:+.2f} 亿')
        why.append(f'涨 {r["chg"]:+.2f}%，量比 {r["qr"] or 0:.1f}，换手 {r["tr"] or 0:.1f}%，收在 20 日线 {k["ma20"]:.2f} 上方')
        out.append({"c": c, "s": s, "why": why, "h": h, "parts": parts, "kind": kind})
    out.sort(key=lambda t: -t["s"])
    pick, n_ind = [], {}
    def take(t, tier):
        pick.append({**t, "tier": tier}); ind = U[t["c"]]["ind"]; n_ind[ind] = n_ind.get(ind, 0) + 1
    core = [t for t in out if t["s"] >= CORE_MIN]
    for half in (True, False):                          # 核心：满 45 分，两类各取 4 只，一类不够再用另一类补满 8 只；同一行业最多 2 只
        for want in ("强势异动", "资金吸筹"):
            for t in core:
                if len(pick) >= NEW_PER_DAY or (half and sum(1 for x in pick if x["kind"] == want) >= NEW_PER_DAY // 2):
                    break
                if t["kind"] == want and all(x["c"] != t["c"] for x in pick) and n_ind.get(U[t["c"]]["ind"], 0) < 2:
                    take(t, "核心")
    q = {k: [t for t in out if t["kind"] == k and all(x["c"] != t["c"] for x in pick)] for k in ("资金吸筹", "强势异动")}
    while len(pick) < need and (q["资金吸筹"] or q["强势异动"]):   # 补充：监控池不足 30 只时，两类轮流按分数补到 30 只（35 分以上）；同一行业当天最多 3 只
        for k in ("资金吸筹", "强势异动"):
            while q[k] and len(pick) < need:
                t = q[k].pop(0)
                if n_ind.get(U[t["c"]]["ind"], 0) < 3:
                    take(t, "补充"); break
    return sorted(pick, key=lambda t: (t["tier"] != "核心", -t["s"]))


# ---------- 监控 ----------
def entry_new(t, r, today):
    return {"code": t["c"], "name": r["name"], "ind": r["ind"], "board": r["board"], "in": today, "inP": r["price"], "score": t["s"], "kind": t["kind"], "tier": t["tier"],
            "scIn": t["parts"], "sc": [today, t["s"], t["parts"]], "why": t["why"], "st": "track", "watch": None,
            "f": list(reversed(t["h"]))[-40:], "log": [[today, "new", f'{"新入选" if t["tier"] == "核心" else "补充入选"}（{t["s"]:.0f} 分）：' + t["why"][0]]], "last": today}


def evaluate(p, k, r, mv, lh, ann, today):
    """返回进入观察期的原因 [(类别, 说明)]：主力放弃（流出、大跌、破位、龙虎榜、盘中）或其他因素（利空、止损、降温、ST）。"""
    why = []
    add = lambda cat, text: why.append((cat, text))
    f = p["f"]; since = [x[6] or 0 for x in f if x[0] >= p["in"]]
    cum = sum(since)
    if k["out"] >= 3 and (k["m3"] <= -0.5 * max(cum - k["m3"], 0) or cum <= 0):
        add("流出", f'主力放弃：连续 {k["out"]} 日主力净流出（3 日 {k["m3"]:.2f} 亿），入选以来累计 {cum:+.2f} 亿')
    big = 7 if p["board"] in BIG_BOARD else 5
    if f[-1][2] is not None and f[-1][2] <= -big and k["m"] < 0 and f[-1][4] and -k["m"] / f[-1][4] * 100 >= 10:
        add("大跌", f'主力放弃：放量大跌 {f[-1][2]:.1f}%，主力净流出 {-k["m"]:.2f} 亿（占成交额 {-k["m"] / f[-1][4] * 100:.0f}%）')
    if k["ma20"] and k["c"] < k["ma20"] and k["m"] < 0 and len(f) >= 2 and f[-2][1] and f[-2][1] < k["ma20"]:
        add("破位", f'主力放弃：连续 2 日收在 20 日线 {k["ma20"]:.2f} 下方，且主力净流出')
    if lh and lh[0] == today and lh[3] < 0 and "机构卖出" in (lh[2] or ""):
        add("龙虎榜", f'主力放弃：龙虎榜{lh[2]}，净卖出 {-lh[3]:.2f} 亿')
    if mv and (mv.get("高台跳水", 0) + mv.get("加速下跌", 0) >= 2 or mv.get("_sell", 0) - mv.get("_buy", 0) >= 0.5) and k["m"] < 0:
        add("盘中", "主力放弃：盘中" + move_text(mv, -1) + "，主力净流出")
    bad = [x for x in ann or [] if x[3] == "利空" and x[0] >= p["last"]]
    if bad:
        add("利空:" + bad[0][1][:40], f"利空公告：{bad[0][0][5:]} {bad[0][1][:40]}")
    ret = (k["c"] / p["inP"] - 1) * 100 if k["c"] and p["inP"] else 0
    if ret <= STOP_LOSS:
        add("止损", f"止损线：较入选价 {p['inP']:.2f} 下跌 {ret:.1f}%")
    if len(since) >= 10 and k["m5"] <= 0:
        add("降温", f'资金热度消退：入选 {len(since)} 个交易日，近 5 日主力累计 {k["m5"]:+.2f} 亿')
    if r and r["st"]:
        add("ST", "被实施风险警示（ST）")
    return why


def summary(p):
    since = [x for x in p["f"] if x[0] >= p["in"]]
    c = since[-1][1] if since else None
    return {"days": len(since), "ret": f2((c / p["inP"] - 1) * 100) if c and p["inP"] else None, "mcum": f2(sum(x[6] or 0 for x in since), 3)}


def update_pool(fl, U, MV, LHB30, A, N, today):
    """更新池里每只股票，返回今天的提醒。"""
    alerts = []
    say = lambda p, lv, text: (alerts.append({"lv": lv, "code": p["code"], "name": p["name"], "text": text}), p["log"].append([today, lv, text]), p.__setitem__("log", p["log"][-20:]))
    codes = [p["code"] for p in fl["pool"]]
    H = pmap(lambda c: sina_hist(c, 40), codes)
    keep, gone = [], []
    for p in fl["pool"]:
        c, h, r = p["code"], H.get(p["code"]), U.get(p["code"])
        if A.get(c) is not None:
            old = {x[0] for x in p.get("ann") or []}
            p["ann"] = A[c][:8]
            for x in A[c]:
                if x[0] not in old and x[0] >= p["last"] and x[3] == "利好":
                    say(p, "info", f"利好公告：{x[0][5:]} {x[1][:40]}"); break
        if N.get(c) is not None:
            p["news"] = N[c][:6]
        p["lhb"] = (LHB30.get(c) or [])[:6]
        if MV.get(c):
            p["mv"] = [[today, {k: v for k, v in MV[c].items() if k != "_ev"}, MV[c]["_ev"][-12:]]] + [x for x in p.get("mv") or [] if x[0] != today][:4]
        if not h or h[0][0] != today:                   # 停牌或数据没更新：不计观察天数
            if p["in"] != today:
                p["halt"] = today
            keep.append(p); continue
        p.pop("halt", None)
        by = {x[0]: x for x in p["f"]}
        for x in h:
            by[x[0]] = x
        p["f"] = [by[d] for d in sorted(by)][-40:]
        if r:
            p["name"] = r["name"]
        k = metrics(h); lh = (LHB30.get(c) or [None])[0]
        p["k"] = {kk: f2(v, 3) for kk, v in k.items()}
        if p["in"] == today:
            p["last"] = today; keep.append(p); continue
        if r:                                           # 今日评分：同一套公式按最新数据重算
            amt = h[0][4] or 0
            s_, parts = score_parts(p.get("kind") or "资金吸筹", k["m"], k["m"] / amt * 100 if amt else 0, k, k["m5"] / r["fmc"] * 100 if r["fmc"] else 0,
                                    MV.get(c), lh[3] if lh and lh[0] == today else 0, r["qr"])
            p["sc"] = [today, s_, parts]
        if lh and lh[0] == today:
            say(p, "good" if lh[3] > 0 else "bad", f"龙虎榜：{lh[2] or lh[1]}，净{'买入' if lh[3] > 0 else '卖出'} {abs(lh[3]):.2f} 亿")
        ev = evaluate(p, k, r, MV.get(c), lh, A.get(c), today)
        why = [t for _, t in ev]
        if p["st"] == "track":
            if why:
                p["st"] = "watch"; p["watch"] = {"since": today, "why": why, "cats": [c_ for c_, _ in ev], "n": 0}
                say(p, "warn", f"进入观察期（再观察 {WATCH_DAYS} 个交易日）：" + "；".join(why))
            elif k["in"] in (3, 5, 10):
                say(p, "good", f'连续 {k["in"]} 日主力净流入，5 日累计 {k["m5"]:.2f} 亿')
        else:
            w = p["watch"]; w["n"] += 1
            back = k["in"] >= 2 and k["ma10"] and k["c"] >= k["ma10"] and not any(c_.startswith("利空") for c_, _ in ev)
            if back:
                p["st"] = "track"; p["watch"] = None
                say(p, "good", f'主力回流，恢复跟踪：连续 {k["in"]} 日主力净流入（{k["m3"]:+.2f} 亿），收盘站上 10 日线 {k["ma10"]:.2f}')
            elif w["n"] >= WATCH_DAYS:
                p["st"] = "out"; p["outD"] = today
                s = summary(p)
                say(p, "out", f'观察满 {WATCH_DAYS} 个交易日主力没有回来，移出监控：入选以来 {s["ret"]:+.1f}%，主力累计 {s["mcum"]:+.2f} 亿')
            else:
                cats = w.setdefault("cats", [])
                new = [(c_, t) for c_, t in ev if c_ not in cats]          # 同一类原因只提醒一次
                cats += [c_ for c_, _ in new]; w["why"] += [t for _, t in new]
                if new:
                    say(p, "bad", f"观察第 {w['n']}/{WATCH_DAYS} 天，又出现：" + "；".join(t for _, t in new))
        p["last"] = today
        (gone if p["st"] == "out" else keep).append(p)
    fl["pool"] = keep
    for p in gone:
        fl["removed"].insert(0, {"code": p["code"], "name": p["name"], "ind": p.get("ind"), "in": p["in"], "out": today, "inP": p["inP"], "why": p["why"],
                                 "watch": (p.get("watch") or {}).get("why"), **summary(p), "log": p["log"][-8:], "ai": p.get("ai")})
    fl["removed"] = fl["removed"][:40]
    return alerts


# ---------- 详细信息：资金明细、主力控盘与持股、筹码分布、龙虎榜席位 ----------
def sina_now(code):
    """最近一个交易日的资金分类（新浪）：{r0 特大单, r1 大单, r2 小单, r3 散单: [流入亿, 流出亿, 成交额亿]}。"""
    d = jget(SINA + "ssi_ssfx_flzjtj", {"daima": a_sym(code)}, tries=2)
    if not isinstance(d, dict) or d.get("r0_in") is None:
        return None
    n = lambda k: f2((num(d.get(k)) or 0) / 1e8, 3)
    return {k: [n(k + "_in"), n(k + "_out"), n(k)] for k in ("r0", "r1", "r2", "r3")}


QG_COLS = "SECURITY_CODE,PRIME_COST,PRIME_COST_20DAYS,PRIME_COST_60DAYS,ORG_PARTICIPATE,PARTICIPATE_TYPE,RATIO,RATIO_3DAYS,RATIO_50DAYS,TOTALSCORE,RANK,FOCUS"
ORG_LEVEL = {"0": "未控盘（<10%）", "1": "轻度控盘（10%–25%）", "2": "中度控盘（25%–40%）", "3": "高度控盘（≥40%）"}


def qgqp(codes, today):
    """东方财富千股千评（交易日）：主力成本、机构参与度、主力买入占成交（超大单 + 大单）、综合得分。"""
    out = {}
    for i in range(0, len(codes), 50):
        q = ",".join(f'"{c}"' for c in codes[i:i + 50])
        for r in em_all("RPT_DMSK_TS_STOCKNEW", f"(TRADE_DATE='{today}')(SECURITY_CODE in ({q}))", QG_COLS) or []:
            pc = lambda k: f2((num(r.get(k)) or 0) * 100, 1)
            out[r["SECURITY_CODE"]] = {"d": today, "cost": f2(num(r.get("PRIME_COST"))), "cost20": f2(num(r.get("PRIME_COST_20DAYS"))), "cost60": f2(num(r.get("PRIME_COST_60DAYS"))),
                                       "org": pc("ORG_PARTICIPATE"), "lvl": ORG_LEVEL.get(str(r.get("PARTICIPATE_TYPE")), ""), "ratio": pc("RATIO"), "ratio3": pc("RATIO_3DAYS"),
                                       "ratio50": pc("RATIO_50DAYS"), "score": f2(num(r.get("TOTALSCORE")), 1), "rank": r.get("RANK"), "focus": f2(num(r.get("FOCUS")), 1)}
    return out


ORG_T = {"01": "基金", "02": "QFII", "03": "社保", "04": "券商", "05": "保险", "06": "信托", "07": "其他机构（含控股股东等法人）"}


def holders(code, today):
    """东方财富 F10 股东研究（季度）：十大流通股东、十大以外的流通股、机构持仓、股东户数、实际控制人。"""
    pre = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[a_sym(code)[:2]]
    d = jget("https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax", {"code": pre + code}, tries=2)
    if not isinstance(d, dict):
        return None
    top, lt, gd, jg = d.get("sdltgd") or [], (d.get("ltgf") or [{}])[0] or {}, d.get("gdrs") or [], d.get("jgcc") or []
    end = (top[0].get("END_DATE") or "")[:10] if top else ""
    dates = sorted({(x.get("REPORT_DATE") or "")[:10] for x in jg}, reverse=True)
    jd = end if end in dates else (dates[0] if dates else "")            # 机构持仓用和十大流通股东同一期（最新一期可能还没披露完）
    org = [x for x in jg if (x.get("REPORT_DATE") or "")[:10] == jd]
    tot = next((x for x in org if x.get("ORG_TYPE") == "00"), None)
    ctrl = (d.get("sjkzr") or [None])[0]
    return {"at": today, "end": end, "top": [[x.get("HOLDER_NAME"), x.get("HOLDER_TYPE"), f2(num(x.get("FREE_HOLDNUM_RATIO"))), x.get("HOLD_NUM_CHANGE")] for x in top[:10]],
            "topSum": f2(num(lt.get("HOLD_NUM_RATIO"))), "other": f2(num(lt.get("UNLIMITED_SHARES_RATIO"))), "limited": f2(num(lt.get("LIMITED_SHARES_RATIO"))),
            "org": {"d": jd, "n": tot.get("TOTAL_ORG_NUM") if tot else None, "pct": f2(num(tot.get("TOTAL_SHARES_RATIO"))) if tot else None,
                    "by": [[ORG_T.get(x.get("ORG_TYPE"), x.get("ORG_TYPE")), x.get("TOTAL_ORG_NUM"), f2(num(x.get("TOTAL_SHARES_RATIO")))] for x in org if x.get("ORG_TYPE") != "00"]},
            "hn": [[(x.get("END_DATE") or "")[:10], x.get("HOLDER_TOTAL_NUM"), f2(num(x.get("TOTAL_NUM_RATIO"))), x.get("AVG_FREE_SHARES"), f2((num(x.get("AVG_HOLD_AMT")) or 0) / 1e4, 1),
                    x.get("HOLD_FOCUS")] for x in gd[:5]],
            "ctrl": [ctrl.get("HOLDER_NAME"), f2(num(ctrl.get("HOLD_RATIO")))] if ctrl else None}


def chips(k, fsh, bins=80):
    """筹码分布：按每天的换手率把旧筹码衰减、把当天成交按三角形分布在最低价和最高价之间（峰值在均价），用近 250 个交易日估算。"""
    k = [r for r in k or [] if r[3] and r[4] and r[5]][-250:]
    if len(k) < 60 or not fsh:
        return None
    lo, hi = min(r[4] for r in k), max(r[3] for r in k)
    step = (hi - lo) / bins or 0.01
    dist = [0.0] * (bins + 1)
    for _, o, c, h, l, v, *_ in k:
        tr = min(1.0, v * 100 / fsh)
        dist = [x * (1 - tr) for x in dist]
        avg = (o + c + h + l) / 4; span = max(avg - l, h - avg) or step
        idx = range(int((l - lo) / step), int((h - lo) / step) + 1)
        w = [max(0.0, 1 - abs(lo + i * step - avg) / span) + 1e-6 for i in idx]
        sw = sum(w)
        for i, x in zip(idx, w):
            dist[i] += tr * x / sw
    tot = sum(dist) or 1; px = lambda i: lo + i * step; close = k[-1][2]
    def q(pct):
        acc = 0.0
        for i, x in enumerate(dist):
            acc += x
            if acc >= pct * tot:
                return px(i)
        return hi
    rng = lambda a, b: [round(q(a), 2), round(q(b), 2), round((q(b) - q(a)) / (q(b) + q(a)) * 100, 1)]
    mx = max(dist) or 1
    return {"d": k[-1][0], "lo": round(lo, 4), "step": round(step, 5), "v": [round(x / mx * 1000) for x in dist], "close": close,
            "profit": round(sum(x for i, x in enumerate(dist) if px(i) <= close) / tot * 100, 1), "avg": round(sum(px(i) * x for i, x in enumerate(dist)) / tot, 2),
            "r90": rng(.05, .95), "r70": rng(.15, .85), "peak": round(px(max(range(len(dist)), key=dist.__getitem__)), 2)}


def chip_text(ch, cost=None):
    pr, c, avg, (a, b, cc), pk = ch["profit"], ch["close"], ch["avg"], ch["r90"], ch["peak"]
    t = [f"获利盘 {pr:.0f}%、套牢盘 {100 - pr:.0f}%：" + ("几乎全部获利，短线获利回吐压力大" if pr >= 90 else "多数筹码获利，注意获利了结" if pr >= 70 else
          "获利和套牢筹码相当，多空分歧" if pr >= 30 else "多数筹码被套，上方抛压较重" if pr >= 10 else "几乎全部被套，反弹会遇到解套盘"),
         f"现价 {c:.2f}，{'高于' if c >= avg else '低于'}平均成本 {avg:.2f}（{(c / avg - 1) * 100:+.1f}%）",
         f"90% 筹码在 {a:.2f}–{b:.2f} 元，集中度 {cc:.1f}%（" + ("高度集中" if cc < 10 else "较集中" if cc < 20 else "较分散") + "，越小越集中）",
         f"筹码主峰 {pk:.2f} 元，" + (f"在现价下方 {(1 - pk / c) * 100:.1f}%，是主要支撑" if pk < c * 0.98 else f"在现价上方 {(pk / c - 1) * 100:.1f}%，是主要压力" if pk > c * 1.02 else "和现价接近，筹码在这里密集换手")]
    if cost:
        t.append(f"东方财富估算的主力成本 {cost:.2f} 元，现价{'高于' if c >= cost else '低于'}主力成本 {(c / cost - 1) * 100:+.1f}%")
    return t


SEAT_COLS = "SECURITY_CODE,TRADE_DATE,OPERATEDEPT_NAME,BUY,SELL,EXPLANATION"
seat_type = lambda nm: "机构" if "机构专用" in nm else "北向" if "股通" in nm else "营业部"


def seats(flt):
    """龙虎榜买卖席位：{(代码, 日期): {"buy": 买入前 5 [[席位, 买入亿, 卖出亿]], "sell": 卖出前 5}}。
    同一天有多个上榜原因时（当日统计和连续三日统计的金额不同）只取一个，优先当日统计，避免重复计算。"""
    rows = {side: em_all(rep, flt, SEAT_COLS) or [] for rep, side in (("RPT_BILLBOARD_DAILYDETAILSBUY", "buy"), ("RPT_BILLBOARD_DAILYDETAILSSELL", "sell"))}
    pick = {}
    for r in rows["buy"] + rows["sell"]:
        k, e = (r["SECURITY_CODE"], r["TRADE_DATE"][:10]), r.get("EXPLANATION") or ""
        if k not in pick or ("连续" in pick[k] and "连续" not in e):
            pick[k] = e
    out = {}
    for side in ("buy", "sell"):
        for r in rows[side]:
            if (r.get("EXPLANATION") or "") != pick[(r["SECURITY_CODE"], r["TRADE_DATE"][:10])]:
                continue
            o = out.setdefault((r["SECURITY_CODE"], r["TRADE_DATE"][:10]), {"buy": {}, "sell": {}})
            x = [(r.get("OPERATEDEPT_NAME") or "").strip(), f2((num(r.get("BUY")) or 0) / 1e8, 4), f2((num(r.get("SELL")) or 0) / 1e8, 4)]
            o[side][tuple(x)] = x
    return {k: {"buy": sorted(v["buy"].values(), key=lambda x: -x[1])[:5], "sell": sorted(v["sell"].values(), key=lambda x: -x[2])[:5]} for k, v in out.items()}


def seat_board(S, U, today):
    """今日龙虎榜：哪个席位买了哪只股票、多少钱。"""
    rows = []
    for (c, d), v in S.items():
        if d != today or c not in U:                    # 只统计 A 股（龙虎榜里还有可转债等）
            continue
        seen = set()
        for x in v["buy"] + v["sell"]:
            if tuple(x) not in seen:
                seen.add(tuple(x)); rows.append([x[0], seat_type(x[0]), c, (U.get(c) or {}).get("name", c), x[1], x[2], f2(x[1] - x[2], 4)])
    by, agg = {}, {}
    for x in rows:
        a = agg.setdefault(x[1], [0, 0.0, 0.0]); a[0] += 1; a[1] += x[4]; a[2] += x[5]
        if x[1] != "机构":                              # 机构专用是很多家机构共用的名称，不按席位合并
            b = by.setdefault(x[0], [x[0], x[1], 0.0, []]); b[2] += x[6]; b[3].append([x[2], x[3], x[6]])
    top = sorted(by.values(), key=lambda b: -b[2])
    return {"buy": sorted(rows, key=lambda x: -x[6])[:40], "sell": sorted(rows, key=lambda x: x[6])[:20], "n": len(rows), "stocks": len({x[2] for x in rows}),
            "agg": {k: [v[0], f2(v[1], 3), f2(v[2], 3)] for k, v in agg.items()},
            "multi": [[b[0], b[1], f2(b[2], 3), sorted(b[3], key=lambda x: -x[2])[:6]] for b in top if len(b[3]) >= 2][:15]}


def details(fl, U, today, since):
    """监控池每只股票的详细信息（每天收盘后更新；股东和持股是季度数据，每 7 天重新取一次）。"""
    pool = fl["pool"]; codes = [p["code"] for p in pool]
    week = (dt.date.fromisoformat(today) - dt.timedelta(days=7)).isoformat()
    FX = pmap(sina_now, codes)
    QG = qgqp(codes, today)
    HD = pmap(lambda c: holders(c, today), [p["code"] for p in pool if (p.get("hold") or {}).get("at", "") <= week], 3)
    K = pmap(lambda c: tx_daily(a_sym(c), count=250), codes)
    SP = {}
    for i in range(0, len(codes), 40):
        q = ",".join(f'"{c}"' for c in codes[i:i + 40])
        SP.update(seats(f"(SECURITY_CODE in ({q}))(TRADE_DATE>='{since}')"))
    for p in pool:
        c, r = p["code"], U.get(p["code"]) or {}
        if FX.get(c):
            p["fx"] = {"d": today, **FX[c]}
        if QG.get(c):
            p["qg"] = QG[c]
        if HD.get(c):
            p["hold"] = HD[c]
        ch = chips(K.get(c), r.get("fsh"))
        if ch:
            ch["text"] = chip_text(ch, (p.get("qg") or {}).get("cost"))
            p["chip"] = ch
        rs = {x[0]: x[1] for x in p.get("lhb") or []}
        p["seats"] = [[d, rs.get(d, ""), v["buy"], v["sell"]] for (cc, d), v in sorted(SP.items(), key=lambda t: t[0][1], reverse=True) if cc == c][:4]
    log("flow details | 资金明细", sum(1 for c in codes if FX.get(c)), "| 千股千评", len(QG), "| 股东（本次更新）", sum(1 for v in HD.values() if v),
        "| 筹码", sum(1 for p in pool if p.get("chip", {}).get("d") == today), "| 龙虎榜席位", sum(1 for p in pool if p["seats"]))


# ---------- 全市场概况 ----------
def market(U, F, MVN, LHB, today):
    rows = [(c, F[c]) for c in U if c in F and not U[c]["st"]]
    ind = {}
    for c, f in rows:
        if U[c]["ind"]:
            ind[U[c]["ind"]] = ind.get(U[c]["ind"], 0) + f["m"]
    rk = sorted(ind.items(), key=lambda t: t[1])
    top = lambda xs: [[c, U[c]["name"], f2(f["m"], 3), f2(f["mr"], 1), U[c]["chg"]] for c, f in xs]
    srt = sorted(rows, key=lambda t: t[1]["m"])
    return {"m": f2(sum(f["m"] for _, f in rows)), "inN": sum(1 for _, f in rows if f["m"] > 0), "outN": sum(1 for _, f in rows if f["m"] < 0),
            "top": top(srt[::-1][:30]), "bottom": top(srt[:30]), "indIn": [[k, f2(v)] for k, v in rk[::-1][:8]], "indOut": [[k, f2(v)] for k, v in rk[:8]],
            "moves": {k: v for k, v in (MVN or {}).items() if v}, "lhbN": sum(1 for x in LHB.values() if x and x[0][0] == today)}


# ---------- DeepSeek 全面调查 ----------
SYSTEM = """你是严谨的A股资金面研究员，为研究型网页写每日“资金异动股”调查报告。只能使用用户给出的JSON数据：新浪口径的主力资金流向（主力=特大单，单位亿元）和特大单、大单、小单、散单流入流出，东方财富盘口异动、龙虎榜和买卖席位、千股千评（主力成本、机构参与度），十大流通股东、机构持仓和股东户数（季度），按日K估算的筹码分布，行情与技术指标、最新财报、公告和新闻标题。不得编造数据、事件或来源；公告和新闻只能按标题字面意思引用并注明日期；数据里没有的写“待核实”。不给买卖指令、目标价或仓位建议。全部用简体中文。
只输出一个JSON对象，结构严格如下：
{"overview": "今天全市场主力资金与异动的一句话概括（不超过80字）",
 "stocks": {"六位代码": {"level": "积极|中性|谨慎", "conclusion": "一句话结论（不超过60字）", "funds": "资金面：主力流入的连续性和力度、占成交额比例、大单与散户、龙虎榜席位、主力控盘和持股结构（不超过150字）", "tech": "技术面：位置、均线、量价、筹码分布（获利盘、主峰、集中度）、涨幅是否透支（不超过120字）", "fundamental": "基本面：行业、业绩增速、估值（不超过100字）", "news": "消息面：公告和新闻要点（不超过100字，没有就写“近期没有重要公告”）", "risks": ["主要风险，2-3条"], "watch": ["接下来重点跟踪的信号，2-3条，例如主力是否继续流入、能否守住某均线"]}},
 "changes": {"六位代码": "这只股票状态变化的说明与需要注意的地方（不超过80字）"}}
stocks 必须覆盖“新入选”里的每一只，changes 必须覆盖“状态变化”里的每一只（没有就给空对象）。"""


def _stock_ctx(p, r, mv, extra):
    t, f = r.get("t") or {}, p["f"]
    return {"名称": r["name"], "行业": r["ind"], "题材": r["tags"], "板块": r["board"], "收盘": r["price"], "今日涨跌%": r["chg"], "换手%": r["tr"], "量比": r["qr"],
            "成交额亿": r["amt"], "流通市值亿": r["fmc"], "PE_TTM": r["pe"], "PB": r["pb"],
            "最新财报": {"期": r["rpt"], "营收亿": r["rev"], "营收同比%": r["revY"], "归母净利亿": r["np"], "净利同比%": r["npY"], "ROE%": r["roe"], "毛利率%": r["gm"]},
            "技术": {k: t.get(k) for k in ("ma5", "ma10", "ma20", "ma60", "r5", "r20", "r60", "h20", "lb")},
            "主力资金_近10日": [{"日期": x[0], "涨跌%": x[2], "主力净流入亿": x[6], "大单净流入亿": x[7], "成交额亿": x[4]} for x in f[-10:]],
            "入选理由": p["why"], "今日异动": {k: v for k, v in (mv or {}).items() if k != "_ev"}, "龙虎榜": p.get("lhb") or [],
            "公告": [x[:2] + ([x[3]] if x[3] else []) for x in p.get("ann") or []], "新闻": [x[:2] for x in p.get("news") or []], "风险提示": risk_flags(r, p["in"]),
            "资金明细_最近一日": {n: dict(zip(("流入亿", "流出亿", "成交额亿"), (p.get("fx") or {}).get(k) or [])) for k, n in (("r0", "特大单"), ("r1", "大单"), ("r2", "小单"), ("r3", "散单"))} if p.get("fx") else None,
            "小单和散单成交占比%_近5日": [x[11] for x in f[-5:] if len(x) > 11],
            "主力控盘_东方财富千股千评": p.get("qg"), "持股结构": {k: (p.get("hold") or {}).get(k) for k in ("end", "topSum", "other", "org", "hn", "ctrl")} if p.get("hold") else None,
            "筹码分布": {k: (p.get("chip") or {}).get(k) for k in ("profit", "avg", "r90", "r70", "peak", "text")} if p.get("chip") else None,
            "龙虎榜席位": p.get("seats") or [], "评分": p.get("sc", [None, None, None])[1:], **extra}


def investigate(fl, U, MV, alerts, today, mk):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        log("flow ai: 没有 DEEPSEEK_API_KEY，跳过"); return False
    by = {p["code"]: p for p in fl["pool"]} | {p["code"]: p for p in fl["removed"] if p.get("out") == today}
    new = {c: _stock_ctx(by[c], U[c], MV.get(c), {}) for c in fl["new"] if c in by and c in U}
    chg = {}
    for a in alerts:
        if a["lv"] in ("warn", "out") or (a["lv"] == "good" and "回流" in a["text"]):
            p = by.get(a["code"])
            if p and a["code"] not in new:
                chg[a["code"]] = {"名称": a["name"], "变化": a["text"], "入选日": p["in"], "入选理由": p["why"],
                                  "近10日主力": [[x[0], x[2], x[6]] for x in (p.get("f") or [])[-10:]], "公告": [x[:2] for x in (p.get("ann") or [])[:5]]}
    if not new and not chg:
        return False
    codes, ok = list(new), False
    for i in range(0, max(len(codes), 1), 10):          # 新入选可能有 30 只：每批 10 只，状态变化放在第一批
        part = {c: new[c] for c in codes[i:i + 10]}
        ok = investigate_batch(fl, by, part, chg if i == 0 else {}, today, mk, key) or ok
    return ok


def investigate_batch(fl, by, new, chg, today, mk, key):
    ctx = {"日期": today, "全市场": {"主力净流入合计亿": mk["m"], "主力净流入家数": mk["inN"], "主力净流出家数": mk["outN"], "行业主力流入前列": mk["indIn"], "行业主力流出前列": mk["indOut"],
                                "异动统计": mk["moves"]}, "新入选": new, "状态变化": chg}
    for model in [os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"), "deepseek-flash"]:
        try:
            r = requests.post("https://api.deepseek.com/chat/completions", timeout=480, headers={"Authorization": f"Bearer {key}"},
                              json={"model": model, "temperature": 0.3, "max_tokens": 32000 if "pro" in model else 8000, "response_format": {"type": "json_object"},
                                    "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)}]})
            r.raise_for_status()
            ch = r.json()["choices"][0]
            if ch.get("finish_reason") == "length":
                raise ValueError("输出达到长度上限被截断")
            o = json.loads(ch["message"]["content"])
            s = lambda x, n: str(x or "").strip()[:n]
            st = o.get("stocks") or {}
            got = 0
            for c in new:
                x = st.get(c)
                if isinstance(x, dict) and s(x.get("conclusion"), 90):
                    by[c]["ai"] = {"d": today, "model": model, "level": s(x.get("level"), 4), "conclusion": s(x.get("conclusion"), 90), **{k: s(x.get(k), 220) for k in ("funds", "tech", "fundamental", "news")},
                                   "risks": [s(v, 80) for v in (x.get("risks") or [])[:4] if s(v, 80)], "watch": [s(v, 80) for v in (x.get("watch") or [])[:4] if s(v, 80)]}
                    got += 1
            for c, v in (o.get("changes") or {}).items():
                if c in chg and s(v, 120):
                    by[c].setdefault("notes", []).insert(0, [today, s(v, 120)]); by[c]["notes"] = by[c]["notes"][:6]
            if new and got < len(new) / 2:
                raise ValueError(f"只写了 {got}/{len(new)} 只")
            if not fl.get("ai"):
                fl["ai"] = {"generated": bj_now().strftime("%Y-%m-%d %H:%M"), "model": model, "overview": s(o.get("overview"), 120)}
            log("flow ai ok", model, "| 新入选", got, "/", len(new), "| 状态变化", len(chg))
            return True
        except Exception as e:
            log("flow ai", model, "failed:", type(e).__name__, str(e)[:200])
    if len(new) > 3:                                    # 两个模型都失败（多半是输出太长被截断）：拆成两半再试
        ks = list(new); h = len(ks) // 2
        a = investigate_batch(fl, by, {c: new[c] for c in ks[:h]}, chg, today, mk, key)
        return investigate_batch(fl, by, {c: new[c] for c in ks[h:]}, {}, today, mk, key) or a
    return False


# ---------- 入口 ----------
def flow_due():
    t = bj_now()
    if t.weekday() >= 5 or t.hour * 60 + t.minute < 15 * 60 + 30:
        return False
    asof = load("a.json", {}).get("asof", "")
    if asof[:10] != t.strftime("%Y-%m-%d") or asof[11:] < "15:00":
        return False
    return load("flow.json", {}).get("date") != asof[:10]


def seed():
    """缓存丢了时，从已发布网页旁边的 flow.json 取回监控池，接着监控。"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return {}
    owner, name = repo.split("/", 1)
    r = get(f"https://{owner}.github.io/{name}/flow.json", tries=2)
    try:
        d = r.json() if r is not None else {}
    except ValueError:
        d = {}
    if d.get("pool") is not None:
        log("flow: 从已发布网页取回监控池", d.get("date"), len(d["pool"]), "只")
    return d if d.get("pool") is not None else {}


def refresh_flow():
    U, asof = universe()
    today = asof[:10]
    if len(U) < 4000 or asof[11:] < "15:00":
        log("flow: 没有当天收盘后的 A 股数据，跳过"); return False
    fl = load("flow.json") or seed() or {}
    base = fl.get("prev") if fl.get("date") == today and fl.get("prev") else {"pool": fl.get("pool", []), "removed": fl.get("removed", [])}   # 同一天重跑：从当天开始前的状态重算
    fl = {"prev": copy.deepcopy(base), **copy.deepcopy(base)}
    F = sina_market()
    chk = sina_hist("600519", 2)
    if not F or len(F) < 4000 or not chk or chk[0][0] != today:
        log("flow: 新浪资金数据不是", today, "的（", chk[0][0] if chk else None, "），跳过"); return False
    MV, MVN = em_moves(today) or ({}, {})
    lhb_today = em_all("RPT_DAILYBILLBOARD_DETAILSNEW", f"(TRADE_DATE='{today}')", LHB_COLS) or []
    LHB = lhb_rows(lhb_today)
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=45)).isoformat()

    def info(codes):                                    # 龙虎榜（近 45 天）、公告、新闻
        L = {}
        for i in range(0, len(codes), 40):
            q = ",".join(f'"{c}"' for c in codes[i:i + 40])
            L.update(lhb_rows(em_all("RPT_DAILYBILLBOARD_DETAILSNEW", f"(SECURITY_CODE in ({q}))(TRADE_DATE>='{since}')", LHB_COLS)))
        return L, pmap(anns, codes, 3), pmap(news, codes, 3)
    alerts = update_pool(fl, U, MV, *info([p["code"] for p in fl["pool"]]), today)           # 先更新池里已有的股票
    skip = {p["code"] for p in fl["pool"]} | {p["code"] for p in fl["removed"] if p.get("out") == today}
    picks = select(U, F, MV, LHB, today, skip, max(NEW_PER_DAY, POOL_MIN - len(fl["pool"])))   # 再选新股票，监控池不足 30 只时补到 30 只
    new = {"pool": [entry_new(t, U[t["c"]], today) for t in picks], "removed": []}
    alerts_new = [{"lv": "new", "code": t["c"], "name": U[t["c"]]["name"], "text": f'{"新入选" if t["tier"] == "核心" else "补充入选"}（{t["s"]:.0f} 分）：' + "；".join(t["why"][:3])} for t in picks]
    alerts = alerts_new + update_pool(new, U, MV, *info([t["c"] for t in picks]), today) + alerts
    fl["pool"] += new["pool"]
    fl["new"] = [t["c"] for t in picks]
    track = [p for p in fl["pool"] if p["st"] == "track" and p["in"] != today]
    for p in sorted(track, key=lambda p: (p.get("k") or {}).get("m5", 0))[:max(0, len(fl["pool"]) - POOL_MAX)]:   # 监控池超过 50 只：资金热度最弱的进入观察期
        p["st"] = "watch"; p["watch"] = {"since": today, "why": ["监控池已满（50 只），这只近 5 日主力资金最弱"], "cats": ["满"], "n": 0}
        text = "进入观察期：监控池已满，这只近 5 日主力资金最弱"; p["log"].append([today, "warn", text])
        alerts.append({"lv": "warn", "code": p["code"], "name": p["name"], "text": text})
    details(fl, U, today, since)
    mk = market(U, F, MVN, LHB, today)
    S_today = seats(f"(TRADE_DATE='{today}')")
    fl.update({"date": today, "asof": asof, "generated": bj_now().strftime("%Y-%m-%d %H:%M"), "alerts": alerts, "market": mk, "ai": None, "seats": seat_board(S_today, U, today),
               "src": {"flow": "新浪财经（主力 = 特大单）", "moves": "东方财富盘口异动" if MV else "（今天没取到）", "lhb": "东方财富龙虎榜", "ann": "东方财富公告", "news": "东方财富个股资讯",
                       "qg": "东方财富千股千评", "hold": "东方财富 F10 股东研究", "chip": "按腾讯日K和换手率估算"}})
    investigate(fl, U, MV, alerts, today, mk)
    save("flow.json", fl)
    log("flow", today, "| 新入选", len(picks), "核心", sum(1 for t in picks if t["tier"] == "核心"), [U[c]["name"] for c in fl["new"]], "| 监控池", len(fl["pool"]),
        {s: sum(1 for p in fl["pool"] if p["st"] == s) for s in ("track", "watch")}, "| 提醒", len(alerts), "| 移出", sum(1 for p in fl["removed"] if p["out"] == today))
    return True

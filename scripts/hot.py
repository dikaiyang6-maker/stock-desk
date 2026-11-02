"""热点（网页「热点」页）：用 Scrapling（见 scrape.py）抓同花顺和东方财富。每次运行刷新一次（交易时段是盘中数据），收盘后定稿；
网页打开时还会在浏览器里直接刷新（这几个接口允许本站跨域访问）。

同花顺：涨停池（涨停原因、几天几板、首次和最后封板时间、炸板次数、封单额、一字/T字/换手板、是否回封）、炸板池、跌停池、
       涨跌停家数和封板率（和上一个交易日对比）、涨停题材排行（题材的涨停家数、连板家数、最高板，个股的涨停原因详解）、
       人气热榜（1 小时、24 小时，概念和人气标签）、
       个股资金流向（全市场每只股票的流入、流出、净额。同花顺的页面要先运行它的 JS 生成 cookie，
       普通请求只能拿到第一页，所以用 Scrapling 的真实浏览器翻页，每个交易日收盘后一次）。
东方财富：人气榜（前 100 名、较前一日的排名变化）。"""
import datetime as dt
import os
import re
import time

import scrape
from common import bj_now, close_date, jget, load, log, num, save

THS = "https://data.10jqka.com.cn/dataapi/limit_up/"
FIELDS = "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004"
HOT = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
FUND = "https://data.10jqka.com.cn/funds/ggzjl/"
EMHOT = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
BJ = dt.timezone(dt.timedelta(hours=8))
f2 = lambda v, d=2: None if v is None else round(v, d)


def hms(ts):
    """同花顺的 Unix 时间戳（秒）→ 北京时间 HH:MM:SS。"""
    t = num(ts)
    return dt.datetime.fromtimestamp(t, BJ).strftime("%H:%M:%S") if t else None


def boards(x):
    """几天几板：high_days_value = 板数 << 16 | 天数；首板为 (1, 1)。"""
    v = x.get("high_days_value")
    if isinstance(v, int) and v > 0:
        return v >> 16, v & 0xFFFF
    return (1, 1) if x.get("high_days") == "首板" else (None, None)


def pool(kind, day):
    """同花顺涨停池 limit_up_pool / 炸板池 open_limit_pool / 跌停池 lower_limit_pool，翻页取全。
    返回 (列表, 接口里的统计)；接口出错、日期不对或当天还没开盘时返回 None。"""
    out, page, data = [], 1, {}
    while True:
        d = jget(THS + kind, {"page": page, "limit": 200, "field": FIELDS, "filter": "HS,GEM2STAR", "order_type": 0, "date": day.replace("-", ""),
                              "order_field": "330324" if kind == "limit_up_pool" else "199112"})
        if not d or d.get("status_code") != 0 or not d.get("data"):
            return None
        data = d["data"]
        if data.get("date") != day.replace("-", "") or (data.get("trade_status") or {}).get("id") == "no_open" and not data.get("info"):
            return None
        out += data.get("info") or []
        if page >= ((data.get("page") or {}).get("count") or 0):
            return out, data
        page += 1


def zt_row(x):
    b, n = boards(x)
    return {"c": x["code"], "n": x["name"], "why": x.get("reason_type") or "", "hd": x.get("high_days") or "", "b": b, "d": n,
            "t1": hms(x.get("first_limit_up_time")), "t2": hms(x.get("last_limit_up_time")), "zb": x.get("open_num") or 0,
            "tp": x.get("limit_up_type") or "", "fd": f2((num(x.get("order_amount")) or 0) / 1e8, 3), "tr": f2(num(x.get("turnover_rate"))),
            "mv": f2((num(x.get("currency_value")) or 0) / 1e8, 1), "chg": f2(num(x.get("change_rate"))), "p": num(x.get("latest")),
            "back": 1 if x.get("change_tag") == "LIMIT_BACK" and (x.get("open_num") or 0) > 0 else 0, "new": x.get("is_new") or 0}


def zb_row(x):
    return {"c": x["code"], "n": x["name"], "t1": hms(x.get("first_limit_up_time")), "t2": hms(x.get("last_limit_up_time")), "zb": x.get("open_num") or 0,
            "tr": f2(num(x.get("turnover_rate"))), "mv": f2((num(x.get("currency_value")) or 0) / 1e8, 1), "chg": f2(num(x.get("change_rate"))), "p": num(x.get("latest"))}


def dt_row(x):
    return {"c": x["code"], "n": x["name"], "tr": f2(num(x.get("turnover_rate"))), "mv": f2((num(x.get("currency_value")) or 0) / 1e8, 1),
            "chg": f2(num(x.get("change_rate"))), "p": num(x.get("latest"))}


def stat(data):
    """涨跌停家数：今天和上一个交易日的涨停、曾涨停、封板率、炸板、跌停、曾跌停。"""
    u, d = data.get("limit_up_count") or {}, data.get("limit_down_count") or {}
    one = lambda a, b: {"zt": a.get("num"), "ztEver": a.get("history_num"), "fbl": f2((num(a.get("rate")) or 0) * 100, 1), "zb": a.get("open_num"),
                        "dt": b.get("num"), "dtEver": b.get("history_num")}
    return {**one(u.get("today") or {}, d.get("today") or {}), "y": one(u.get("yesterday") or {}, d.get("yesterday") or {})}


def blocks(day):
    """同花顺涨停题材排行：[{c 代码, n 名称, chg 涨跌幅, zt 涨停家数, lb 连板家数, hi 最高板, days 持续天数, s [[代码, 名称, 几天几板, 涨跌幅]]}]，
    以及题材里每只股票的涨停原因详解 {代码: 文字}（同花顺用 AI 汇总公开信息生成，去掉了末尾的免责声明，网页上另外注明）。"""
    d = jget(THS + "block_top", {"filter": "HS,GEM2STAR", "date": day.replace("-", "")})
    if not d or d.get("status_code") != 0 or d.get("data") is None:
        return None
    out, info = [], {}
    for b in d["data"]:
        ss = []
        for s in b.get("stock_list") or []:
            ss.append([s["code"], s["name"], s.get("high") or "", f2(num(s.get("change_rate")))])
            txt = re.sub(r"\s*（免责声明[:：].*?）\s*$", "", (s.get("reason_info") or "").strip(), flags=re.S)
            if txt:
                info[s["code"]] = txt
        out.append({"c": b.get("code"), "n": b.get("name"), "chg": f2(num(b.get("change"))), "zt": b.get("limit_up_num"), "lb": b.get("continuous_plate_num"),
                    "hi": b.get("high") or "", "days": b.get("days"), "s": ss})
    return out, info


def ths_hot(kind):
    """同花顺人气热榜（kind：hour 1 小时、day 24 小时）前 100 名。"""
    d = jget(HOT, {"stock_type": "a", "type": kind, "list_type": "normal"})
    xs = ((d or {}).get("data") or {}).get("stock_list")
    if not xs:
        return None
    return [{"c": x["code"], "n": x["name"], "rk": x.get("order"), "rkc": x.get("hot_rank_chg"), "chg": f2(num(x.get("rise_and_fall"))), "heat": num(x.get("rate")),
             "tags": (x.get("tag") or {}).get("concept_tag") or [], "pop": (x.get("tag") or {}).get("popularity_tag") or ""} for x in xs]


def em_hot():
    """东方财富人气榜前 100 名：[[代码, 排名, 较前一日排名变化（正数为上升）]]。"""
    d = scrape.post_json(EMHOT, {"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38", "marketType": "", "pageNo": 1, "pageSize": 100})
    xs = (d or {}).get("data")
    if not xs:
        return None
    return [[x["sc"][2:], x.get("rk"), x.get("hisRc")] for x in xs if len(x.get("sc") or "") == 8]


def yi(s):
    """同花顺表格里的金额（“909.43万”“1.35亿”“-8310.08万”）→ 亿元。"""
    m = re.match(r"\s*(-?[\d.]+)\s*(万|亿)?", s or "")
    if not m:
        return None
    v = float(m.group(1))
    return round(v / 1e4 if m.group(2) == "万" else v if m.group(2) == "亿" else v / 1e8, 4)


def fund_rows(page):
    rows = {}
    for tr in page.css("table tbody tr"):
        td = [x.get_all_text(strip=True) for x in tr.css("td")]
        if len(td) >= 10 and re.fullmatch(r"\d{6}", td[1]):
            rows[td[1]] = [num(td[3]), yi(td[6]), yi(td[7]), yi(td[8]), yi(td[9])]     # 最新价、流入、流出、净额、成交额（亿元）
    return rows


def ths_fund(prices):
    """同花顺个股资金流向（全市场，按涨跌幅排序翻页）：{代码: [流入, 流出, 净额, 成交额]}（亿元）。
    用 Scrapling 的 StealthySession 打开真实浏览器：先进资金流向页让同花顺的 JS 生成 cookie，再在同一个浏览器里翻页。"""
    b = scrape.browser()
    if b is None:
        log("hot: 没有可用的浏览器（Chrome / Chromium），跳过同花顺资金流向"); return None
    rows, pages = {}, 0
    try:
        with b as s:
            home = s.fetch(FUND, wait_selector="table.m-table tbody tr", network_idle=True)
            rows.update(fund_rows(home))
            info = home.css(".m-page .page_info")
            m = re.search(r"\d+\s*/\s*(\d+)", info[0].get_all_text() if info else "")
            pages = int(m.group(1)) if m else 120
            miss = []
            for p in range(2, min(pages, 150) + 1):
                got = fund_rows(s.fetch(f"{FUND}field/zdf/order/desc/page/{p}/ajax/1/free/1/"))
                if not got and not m:                           # 没拿到总页数时，翻到空页为止
                    break
                rows.update(got)
                if not got:
                    miss.append(p)
            for p in miss:                                      # 没拿到的页最后再试一次（偶尔超时或被同花顺暂时拦下）
                time.sleep(1)
                rows.update(fund_rows(s.fetch(f"{FUND}field/zdf/order/desc/page/{p}/ajax/1/free/1/")))
    except Exception as e:                                      # 浏览器启动失败、超时等
        log("hot: 同花顺资金流向抓取出错", type(e).__name__, str(e)[:200])
    xs = [(c, v[0], prices.get(c)) for c, v in rows.items() if prices.get(c) and v[0]]
    ok = sum(1 for _, a, b in xs if abs(a - b) <= max(0.011, b * 0.01))     # 允许收盘集合竞价造成的几分钱差别
    log("hot: 同花顺资金流向", len(rows), "只 /", pages, "页，最新价和本站收盘价一致", ok, "/", len(xs))
    if len(rows) < 4000 or ok < 0.9 * len(xs):                  # 不完整或者还不是这一天的收盘数据
        return None
    return {c: v[1:] for c, v in rows.items()}


def refresh_hot():
    a = load("a.json", {})
    day = (a.get("asof") or "")[:10]
    if len(a.get("rows") or []) < 5000 or not day:
        log("hot: 没有完整的 A 股行情，跳过"); return False
    now = bj_now().strftime("%Y-%m-%d %H:%M")
    final = now >= day + " 15:05"                               # 收盘后（或之后的日子）抓的就是这一天的最终数据
    old = load("hot.json", {})
    same = lambda k: (old.get(k) or {}).get("d") == day       # 这次没抓到时，只沿用同一天的旧数据
    h = {"date": day, "generated": now, "src": "同花顺、东方财富（Scrapling 抓取）"}

    zt, zb, dn = pool("limit_up_pool", day), pool("open_limit_pool", day), pool("lower_limit_pool", day)
    if zt:
        h["pool"] = {"d": day, "t": now[11:], "final": final, "stat": stat(zt[1]), "zt": [zt_row(x) for x in zt[0]],
                     "zb": [zb_row(x) for x in (zb or ([], {}))[0]], "dt": [dt_row(x) for x in (dn or ([], {}))[0]]}
    elif same("pool"):
        h["pool"] = old["pool"]
    bl = blocks(day)
    if bl:
        h["blocks"] = {"d": day, "t": now[11:], "list": bl[0], "info": bl[1]}
    elif same("blocks"):
        h["blocks"] = old["blocks"]
    hot = {k: ths_hot(k) for k in ("hour", "day")}
    h["hot"] = {"t": now, "hour": hot["hour"], "day": hot["day"]} if hot["hour"] else old.get("hot")
    em = em_hot()
    h["emHot"] = {"t": now, "list": em} if em else old.get("emHot")

    h["fund"] = old.get("fund") if same("fund") else None    # 同花顺资金流向：收盘后的完整数据，每个交易日一次
    if close_date() == day and not same("fund") and os.environ.get("HOT_BROWSER") != "0":
        I = {c: i for i, c in enumerate(a["cols"])}
        rows = ths_fund({r[I["code"]]: r[I["price"]] for r in a["rows"]})
        if rows:
            h["fund"] = {"d": day, "t": bj_now().strftime("%H:%M"), "n": len(rows), "rows": rows}
    if not h.get("pool") and not h.get("hot") and not h.get("emHot"):
        log("hot: 同花顺、东方财富都没取到，沿用上次"); return False
    save("hot.json", h)
    p = h.get("pool") or {}
    log("hot", day, "| 涨停", len(p.get("zt") or []), "炸板", len(p.get("zb") or []), "跌停", len(p.get("dt") or []), "| 题材", len((h.get("blocks") or {}).get("list") or []),
        "| 同花顺热榜", len((h.get("hot") or {}).get("hour") or []), "| 东方财富人气榜", len((h.get("emHot") or {}).get("list") or []),
        "| 同花顺资金流向", (h.get("fund") or {}).get("n"), "| 定稿" if p.get("final") else "| 盘中")
    return True

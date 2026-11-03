"""资讯页的数据。全部来自公开接口（不需要登录，不碰付费数据），东方财富、同花顺、Nasdaq 走 Scrapling（模拟 Chrome）：
- 7×24 快讯：东方财富（用它自己的栏目区分国内、海外）、同花顺、新浪财经，合并去重，标出要闻和相关 A 股；
- 财报：A 股最新业绩、业绩预告、未来三周预约披露（东方财富数据中心），美股财报日历和已公布的 EPS（Nasdaq）；
- 论坛：东方财富股吧（上证指数吧 + 今日龙头、人气股各自的股吧），只取帖子标题、阅读数、评论数和时间，不取用户信息；
- 龙头：按公开公式从同花顺涨停池和人气榜算（leaders），网页里用同一个公式实时算。
旧的 items 字段（快讯标题）留给 DeepSeek 研判用。"""
import datetime as dt
import json
import re
import time
from zoneinfo import ZoneInfo

from common import BJ, UA, bj_now, get, jget, load, log, save

EM_FAST = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
# 东方财富 7×24 栏目：102 全部，101 焦点；海外栏目：全球股市、国际、美国、欧洲、英国、日本、加拿大、澳洲、其他国家、美联储、欧洲央行、英国央行、日本央行、加拿大央行、澳洲联储
EM_GL = ("105", "106", "111", "112", "113", "114", "115", "116", "117", "119", "120", "121", "122", "123", "124")
THS_FAST = "https://news.10jqka.com.cn/tapp/news/push/stock/"
SINA_ROLL = "https://feed.mix.sina.com.cn/api/roll/get"
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
NQ = "https://api.nasdaq.com/api/calendar/earnings"
GUBA = "https://guba.eastmoney.com/list,{}.html"
INDEX_BAR = ("zssh000001", "上证指数吧")
# 标题里有这些词的快讯算海外（东方财富的快讯先看它在不在海外栏目里）。网页里的 NW_GL 是同一份
GL_KW = ("美国", "美联储", "美股", "纳指", "纳斯达克", "道指", "道琼斯", "标普", "美债", "美元", "欧洲", "欧元", "欧盟", "英国", "英镑", "日本", "日元",
         "日经", "韩国", "德国", "法国", "俄罗斯", "乌克兰", "以色列", "伊朗", "中东", "特朗普", "白宫", "欧佩克", "OPEC", "布伦特", "加拿大", "澳大利亚",
         "澳洲", "印度", "越南", "墨西哥", "巴西", "沙特", "土耳其", "中概", "港股", "恒指", "恒生", "外盘", "海外", "国际油价", "国际金价", "COMEX", "IMF",
         "华尔街", "纽约", "伦敦", "东京", "首尔", "莫斯科", "基辅", "加沙", "哈马斯", "胡塞", "泽连斯基", "普京", "马克龙", "鲍威尔", "贝森特", "欧央行", "日央行",
         "朝鲜", "叙利亚", "黎巴嫩", "也门", "阿联酋", "卡塔尔", "伊拉克", "阿富汗", "巴基斯坦", "斯坦", "阿曼", "新加坡", "马来西亚", "泰国", "印尼", "菲律宾",
         "新西兰", "南非", "埃及", "阿根廷", "智利", "委内瑞拉", "意大利", "西班牙", "荷兰", "瑞士", "瑞典", "挪威", "丹麦", "波兰", "希腊", "比利时",
         "英伟达", "特斯拉", "微软", "谷歌", "亚马逊", "Meta", "奈飞", "台积电", "三星", "英特尔", "高通", "博通", "甲骨文", "OpenAI", "AMD", "苹果公司",
         "超微电脑", "葡萄牙", "亚太经合", "APEC", "盘前涨", "盘前跌", "盘后涨", "盘后跌", "联合国", "北约", "世卫", "世贸", "七国集团", "二十国集团", "G7", "G20")
CN_ADR = {"BABA": "阿里巴巴", "JD": "京东", "PDD": "拼多多", "BIDU": "百度", "NTES": "网易", "TCOM": "携程", "NIO": "蔚来", "XPEV": "小鹏汽车", "LI": "理想汽车",
          "BILI": "哔哩哔哩", "TME": "腾讯音乐", "BEKE": "贝壳", "YUMC": "百胜中国", "ZTO": "中通快递", "HTHT": "华住", "FUTU": "富途", "TAL": "好未来",
          "EDU": "新东方", "VIPS": "唯品会", "QFIN": "奇富科技", "MNSO": "名创优品", "IQ": "爱奇艺", "WB": "微博", "ATHM": "汽车之家", "BZ": "BOSS直聘",
          "YMM": "满帮", "TIGR": "老虎证券", "GDS": "万国数据", "KC": "金山云", "HSAI": "禾赛", "ZK": "极氪", "LEGN": "传奇生物", "ONC": "百济神州",
          "ZLAB": "再鼎医药", "DQ": "大全新能源", "JKS": "晶科能源", "CAN": "嘉楠科技", "EH": "亿航智能", "WRD": "文远知行", "PONY": "小马智行", "LU": "陆金所",
          "TSM": "台积电"}
PERIOD = {"03-31": "一季报", "06-30": "中报", "09-30": "三季报", "12-31": "年报"}


def _ts(x):
    try:
        return dt.datetime.fromtimestamp(int(x), BJ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""


def _clean(s, n):
    s = re.sub(r"\s+", " ", (s or "").replace("　", " ")).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _key(title):
    return re.sub(r"[\W_]+", "", title)[:12]


def _norm(s):
    return re.sub(r"[\W_]+", "", s or "")


def is_gl(title):
    return any(k in title for k in GL_KW)


def universe():
    a = load("a.json", {})
    try:
        i = a["cols"].index("code")
        return {r[i] for r in a["rows"]}
    except (KeyError, ValueError, TypeError):
        return None


# ---------- 7×24 快讯 ----------
def em_list(col, n):
    d = jget(EM_FAST, {"client": "web", "biz": "web_724", "fastColumn": col, "sortEnd": "", "pageSize": n, "req_trace": "sd"}, headers={"User-Agent": UA}, tries=2)
    return ((d or {}).get("data") or {}).get("fastNewsList") if d else None


def em_codes(sl, U):
    """东方财富 stockList（"1.600519"、"0.300750"、"116.00700"、"90.BK1036"）里的 A 股代码；1.000001 这类指数不算。"""
    out = []
    for s in sl or []:
        m, _, c = str(s).partition(".")
        if not re.fullmatch(r"\d{6}", c) or (m == "1" and not c.startswith("6")) or (m == "0" and (c[0] not in "0348" or c.startswith("399"))) or m not in ("0", "1"):
            continue
        if (U is None or c in U) and c not in out:
            out.append(c)
    return out


def flash(U):
    """快讯：[id, 时间, cn|gl, 要闻 0/1, 来源, 标题, 摘要, 链接, [相关 A 股]]，新到旧。"""
    main, imp = em_list("102", 200), em_list("101", 60)
    gl_ids, extra = set(), []
    for col in EM_GL:
        xs = em_list(col, 40) or []
        gl_ids.update(x.get("code") for x in xs)
        extra += xs
    imp_ids = {x.get("code") for x in imp or []}
    out, seen = [], set()
    for x in (main or []) + (imp or []) + extra:
        i = x.get("code")
        if not i or i in seen:
            continue
        seen.add(i)
        title = _clean(x.get("title"), 90)
        summ = _clean(re.sub(r"^【[^】]*】", "", x.get("summary") or ""), 1000)        # 快讯全文（网页里点开看）
        if not title:
            title, summ = _clean(summ, 60), summ
        if not title:
            continue
        out.append([f"em{i}", (x.get("showTime") or "")[:19], "gl" if i in gl_ids or is_gl(title) else "cn",
                    1 if i in imp_ids or x.get("titleColor") == 3 else 0, "东方财富", title, summ,
                    f"https://finance.eastmoney.com/a/{i}.html", em_codes(x.get("stockList"), U)])
    em_n = len(out)
    d = jget(THS_FAST, {"page": 1, "tag": "", "track": "website", "pagesize": 100}, headers={"User-Agent": UA}, tries=2)
    for y in ((d or {}).get("data") or {}).get("list") or []:
        title = _clean(y.get("title"), 90)
        if not title:
            continue
        cs = [s.get("stockCode") for s in y.get("stock") or [] if s.get("stockMarket") in ("17", "33", "151") and re.fullmatch(r"\d{6}", s.get("stockCode") or "")]
        abroad = any(s.get("stockMarket") in ("169", "177", "185") for s in y.get("stock") or [])      # 美股、港股
        out.append([f"ths{y.get('id')}", _ts(y.get("ctime")), "gl" if abroad or is_gl(title) else "cn", 1 if str(y.get("import")) == "3" or str(y.get("color")) == "2" else 0,
                    "同花顺", title, _clean(y.get("digest"), 1000), y.get("url") or "", [c for c in cs if U is None or c in U]])
    ths_n = len(out) - em_n
    d = jget(SINA_ROLL, {"pageid": 153, "lid": 2516, "k": "", "num": 50, "page": 1})
    for x in ((d or {}).get("result") or {}).get("data") or []:
        title = _clean(x.get("title"), 90)
        if title:
            out.append([f"sina{x.get('docid') or x.get('url')}", _ts(x.get("ctime")), "gl" if "环球" in (x.get("media_name") or "") or is_gl(title) else "cn", 0,
                        "新浪财经" + (" · " + x["media_name"] if x.get("media_name") else ""), title, _clean(x.get("intro"), 300), x.get("url") or "", []])
    out = [x for x in out if x[1]]
    for x in out:                       # 摘要只是把标题重复一遍（标点不同）就不要
        if x[6] and _norm(x[6]).startswith(_norm(x[5])[:20]) and len(_norm(x[6])) < len(_norm(x[5])) + 8:
            x[6] = ""
    out.sort(key=lambda x: x[1], reverse=True)
    # 同一条消息几家都发：标题前 12 个字（去掉标点）相同、时间相差 2 小时以内的合并，来源写在一起
    merged, by = [], {}
    for x in out:
        k = _key(x[5]); j = by.get(k)
        if j is not None and abs(dt.datetime.fromisoformat(merged[j][1]) - dt.datetime.fromisoformat(x[1])) < dt.timedelta(hours=2):
            m = merged[j]
            s0 = x[4].split(" · ")[0]
            if s0 not in m[4]:
                have = set(m[4].split(" · ")) | {s0}
                m[4] = " · ".join(s for s in ("东方财富", "同花顺", "新浪财经") if s in have)
            m[2] = "gl" if "gl" in (m[2], x[2]) else "cn"
            m[3] = max(m[3], x[3]); m[8] = m[8] + [c for c in x[8] if c not in m[8]]
            if not m[6] and x[6]:
                m[6] = x[6]
            continue
        by[k] = len(merged); merged.append(x)
    log("news flash", "东方财富", em_n, "同花顺", ths_n, "合并后", len(merged))
    return merged if em_n or ths_n else None


# ---------- 财报 ----------
def dc(name, cols, flt, sort, st, ps):
    p = {"reportName": name, "columns": cols, "filter": flt, "sortColumns": sort, "sortTypes": st, "pageNumber": 1, "pageSize": ps, "source": "WEB", "client": "WEB"}
    d = jget(DC, p, headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"}, tries=3)
    if d is None:
        return None
    return (d.get("result") or {}).get("data") or []


def period(d):
    d = (d or "")[:10]
    return f"{d[:4]}{PERIOD.get(d[5:], '')}" if d else ""


def yi(v, n=2):
    return round(v / 1e8, n) if isinstance(v, (int, float)) else None


def r2(v, n=1):
    return round(v, n) if isinstance(v, (int, float)) else None


def a_earnings(U):
    today = bj_now().date()
    ok = lambda c: U is None or c in U
    res = dc("RPT_LICO_FN_CPD", "SECURITY_CODE,SECURITY_NAME_ABBR,NOTICE_DATE,REPORTDATE,TOTAL_OPERATE_INCOME,YSTZ,PARENT_NETPROFIT,SJLTZ,WEIGHTAVG_ROE,BASIC_EPS",
             f"(SECURITY_TYPE_CODE=\"058001001\")(NOTICE_DATE>='{today - dt.timedelta(days=20)}')", "NOTICE_DATE,SECURITY_CODE", "-1,1", 500)
    pre = dc("RPT_PUBLIC_OP_NEWPREDICT", "SECURITY_CODE,SECURITY_NAME_ABBR,NOTICE_DATE,REPORT_DATE,PREDICT_FINANCE_CODE,PREDICT_FINANCE,PREDICT_TYPE,ADD_AMP_LOWER,ADD_AMP_UPPER,PREDICT_AMT_LOWER,PREDICT_AMT_UPPER,CHANGE_REASON_EXPLAIN",
             f"(SECURITY_TYPE_CODE=\"058001001\")(NOTICE_DATE>='{today - dt.timedelta(days=45)}')", "NOTICE_DATE,SECURITY_CODE", "-1,1", 800)
    cal = dc("RPT_PUBLIC_BS_APPOIN", "SECURITY_CODE,SECURITY_NAME_ABBR,REPORT_DATE,APPOINT_PUBLISH_DATE",
             f"(APPOINT_PUBLISH_DATE>='{today}')(APPOINT_PUBLISH_DATE<='{today + dt.timedelta(days=21)}')", "APPOINT_PUBLISH_DATE,SECURITY_CODE", "1,1", 500)
    out = {}
    if res is not None:     # [代码, 名称, 公告日, 报告期, 营收(亿), 营收同比%, 归母净利(亿), 净利同比%, ROE%, EPS]
        out["res"] = [[x["SECURITY_CODE"], x.get("SECURITY_NAME_ABBR"), (x.get("NOTICE_DATE") or "")[:10], period(x.get("REPORTDATE")), yi(x.get("TOTAL_OPERATE_INCOME")),
                       r2(x.get("YSTZ")), yi(x.get("PARENT_NETPROFIT"), 3), r2(x.get("SJLTZ")), r2(x.get("WEIGHTAVG_ROE"), 2), r2(x.get("BASIC_EPS"), 3)]
                      for x in res if ok(x.get("SECURITY_CODE"))][:250]
    if pre is not None:     # [代码, 名称, 公告日, 报告期, 类型, 变动下限%, 上限%, 净利下限(亿), 上限(亿), 原因]，每家每期只留一条（优先归母净利润）
        best = {}
        for x in pre:
            c = x.get("SECURITY_CODE"); k = (c, (x.get("REPORT_DATE") or "")[:10])
            if not ok(c):
                continue
            fin = x.get("PREDICT_FINANCE") or ""
            rank = 0 if x.get("PREDICT_FINANCE_CODE") == "004" else 1 if "净利润" in fin and "扣除" not in fin else 2
            if k not in best or rank < best[k][0]:
                best[k] = (rank, x)
        rows = sorted((x for _, x in best.values()), key=lambda x: ((x.get("NOTICE_DATE") or ""), x.get("SECURITY_CODE")), reverse=True)
        out["pre"] = [[x["SECURITY_CODE"], x.get("SECURITY_NAME_ABBR"), (x.get("NOTICE_DATE") or "")[:10], period(x.get("REPORT_DATE")), x.get("PREDICT_TYPE") or "",
                       r2(x.get("ADD_AMP_LOWER")), r2(x.get("ADD_AMP_UPPER")), yi(x.get("PREDICT_AMT_LOWER"), 3), yi(x.get("PREDICT_AMT_UPPER"), 3),
                       _clean(x.get("CHANGE_REASON_EXPLAIN"), 60)] for x in rows][:150]
    if cal is not None:     # {日期: [[代码, 名称, 报告期], ...]}
        days = {}
        for x in cal:
            if ok(x.get("SECURITY_CODE")):
                days.setdefault((x.get("APPOINT_PUBLISH_DATE") or "")[:10], []).append([x["SECURITY_CODE"], x.get("SECURITY_NAME_ABBR"), period(x.get("REPORT_DATE"))])
        out["cal"] = days
    return out


def money(s):
    """Nasdaq 的 "$1,234.5"、"($0.12)"、"N/A" → 数字。"""
    s = (s or "").strip()
    v = re.sub(r"[^\d.]", "", s)
    try:
        return -float(v) if s.startswith("(") else float(v)
    except ValueError:
        return None


def us_earnings():
    """美股财报日历：上一个美股交易日（带实际 EPS 和超预期幅度）和今天起 4 个交易日；每天按市值取前 25 家，加上中概股。日期是美东日期。"""
    et = dt.datetime.now(ZoneInfo("America/New_York")).date()
    prev = et - dt.timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= dt.timedelta(days=1)
    days, d = [prev], et
    while len(days) < 5:
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    H = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
    out = []
    for d in days:
        j = jget(NQ, {"date": d.isoformat()}, headers=H, tries=2)
        if j is None:
            continue
        rows = ((j.get("data") or {}).get("rows")) or []
        for x in rows:
            x["_m"] = money(x.get("marketCap")) or 0
        rows.sort(key=lambda x: -x["_m"])
        keep = rows[:25] + [x for x in rows[25:] if x.get("symbol") in CN_ADR]
        tm = {"time-pre-market": "盘前", "time-after-hours": "盘后"}
        out.append({"d": d.isoformat(), "n": len(rows), "rows": [[x.get("symbol"), _clean(x.get("name"), 40), CN_ADR.get(x.get("symbol"), ""), tm.get(x.get("time"), ""),
                                                                   round(x["_m"] / 1e8, 1) if x["_m"] else None, money(x.get("epsForecast")), money(x.get("eps")),
                                                                   money(x.get("surprise")), x.get("noOfEsts") if (x.get("noOfEsts") or "").isdigit() else "",
                                                                   x.get("fiscalQuarterEnding") or ""] for x in keep]})
    return out or None


# ---------- 龙头（公式和网页里的 nwLeaders 一样） ----------
def leaders(hot, cap=16):
    """龙头：① 空间龙头 = 涨停池里连板数最高的（≥2 板）；② 题材龙头 = 同花顺涨停题材前 8 名里，每个题材连板最高、同板数先封板、再比封单额的那只；
    ③ 人气龙头 = 同花顺 1 小时热榜前 5 + 东方财富人气榜前 5。返回 [[代码, [标签...]], ...]。"""
    hot = hot or {}
    zt = {x["c"]: x for x in (hot.get("pool") or {}).get("zt") or []}
    key = lambda c: (-(zt[c].get("b") or 1), zt[c].get("t1") or "99:99:99", -(zt[c].get("fd") or 0))
    tags = {}
    add = lambda c, t: tags.setdefault(c, []).append(t)
    lb = sorted((c for c, x in zt.items() if (x.get("b") or 1) >= 2), key=key)
    if lb:
        top = zt[lb[0]].get("b")
        for c in [c for c in lb if zt[c].get("b") == top][:3]:
            add(c, f"空间龙头 {top}连板")
    for b in ((hot.get("blocks") or {}).get("list") or [])[:8]:
        cs = sorted((s[0] for s in b.get("s") or [] if s[0] in zt), key=key)
        if cs:
            add(cs[0], f"{b.get('n')}龙头")
    for x in ((hot.get("hot") or {}).get("hour") or [])[:5]:
        add(x["c"], f"同花顺热榜第{x.get('rk')}")
    for x in ((hot.get("emHot") or {}).get("list") or [])[:5]:
        add(x[0], f"东方财富人气第{x[1]}")
    return [[c, t] for c, t in tags.items()][:cap]


# ---------- 股吧 ----------
def guba_bar(bar):
    """一个股吧的帖子：[帖子 id, 标题, 发帖时间, 阅读, 评论]，取最新 12 帖和评论最多的 12 帖；置顶和别的吧推过来的帖子不要。"""
    d = None
    for k in range(3):
        r = get(GUBA.format(bar), headers={"User-Agent": UA, "Referer": "https://guba.eastmoney.com/"}, tries=2, timeout=25)
        m = r is not None and re.search(r"var\s+article_list\s*=\s*", r.text)
        if m:
            try:
                d = json.JSONDecoder().raw_decode(r.text, m.end())[0]
                break
            except ValueError:
                pass
        time.sleep(4 * (k + 1))
    if not isinstance(d, dict):
        return None
    xs = [x for x in d.get("re") or [] if x.get("stockbar_code") == bar and not x.get("post_top_status") and (x.get("post_title") or "").strip()
          and not re.fullmatch(r"\$[^$]*\$", (x.get("post_title") or "").strip())]
    rows = [[str(x.get("post_id")), _clean(x.get("post_title"), 70), (x.get("post_publish_time") or "")[:16], x.get("post_click_count") or 0, x.get("post_comment_count") or 0]
            for x in xs]
    new = sorted(rows, key=lambda x: x[2], reverse=True)[:12]
    hot = [x for x in sorted(rows, key=lambda x: (x[4], x[3]), reverse=True) if x not in new][:12]
    return {"n": next((x.get("stockbar_name") for x in xs if x.get("stockbar_name")), ""), "cnt": d.get("count"), "p": new + hot,
            "t": bj_now().strftime("%Y-%m-%d %H:%M")}


def guba(lead, old):
    """上证指数吧 + 龙头各自的股吧。偶尔有一两个吧被临时拦住，就用上一轮抓到的（一天以内），每个吧带自己的抓取时间。"""
    bars, ok, day = {}, 0, (bj_now() - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    for bar in [INDEX_BAR[0]] + [c for c, _ in lead]:
        g = guba_bar(bar)
        if g:
            bars[bar] = g; ok += 1
        elif (old.get(bar) or {}).get("t", "") >= day:
            bars[bar] = old[bar]
    log("news guba", ok, "/", 1 + len(lead), "（沿用上一轮", len(bars) - ok, "个）")
    return bars if ok else None


def refresh_news():
    old, U = load("news.json", {}), universe()
    t = bj_now().strftime("%Y-%m-%d %H:%M")
    out = {"t": t}
    fl = flash(U)
    if fl:
        out["flash"] = [x for x in fl if x[1] >= (bj_now() - dt.timedelta(days=2)).strftime("%Y-%m-%d")][:200]
        out["items"] = [{"t": x[1][:16], "src": x[4], "title": x[5], "sum": x[6], "url": x[7]} for x in fl[:120]]
    else:
        log("news: 没拿到快讯，沿用缓存")
        out["flash"], out["items"] = old.get("flash") or [], old.get("items") or []
    ea = a_earnings(U)
    us = us_earnings()
    oe = old.get("earn") or {}
    out["earn"] = {"t": t if ea else oe.get("t"), **{k: ea.get(k, oe.get(k)) for k in ("res", "pre", "cal")}, "us": us if us else oe.get("us"),
                   "usT": t if us else oe.get("usT")}
    lead = leaders(load("hot.json", {}))
    out["lead"] = lead
    gb = guba(lead, (old.get("guba") or {}).get("bars") or {})
    out["guba"] = {"t": t, "bars": gb} if gb else old.get("guba")
    if not out["flash"] and not old:
        return False
    save("news.json", out)
    log("news", len(out["flash"]), "flash | res", len(out["earn"].get("res") or []), "pre", len(out["earn"].get("pre") or []),
        "us days", len(out["earn"].get("us") or []), "| guba", len((out.get("guba") or {}).get("bars") or {}))
    return True


if __name__ == "__main__":
    refresh_news()

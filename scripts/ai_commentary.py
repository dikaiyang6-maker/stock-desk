"""每天用 DeepSeek 根据最新数据写六维研判、今日市场解读和精选个股点评。没有 DEEPSEEK_API_KEY 时跳过，网页沿用上一版或原始文字。"""
import json
import os
import re

import requests

from common import ROOT, bj_now, load, log, save

DIMS = ["宏观环境", "政策面", "资金面", "技术面", "板块与题材", "市场情绪"]


def sma(a, p):
    return sum(a[-p:]) / p if len(a) >= p else None


def rsi(c, p=14):
    if len(c) <= p:
        return None
    ag = sum(max(c[i] - c[i - 1], 0) for i in range(1, p + 1)) / p; al = sum(max(c[i - 1] - c[i], 0) for i in range(1, p + 1)) / p
    for i in range(p + 1, len(c)):
        d = c[i] - c[i - 1]; ag = (ag * (p - 1) + max(d, 0)) / p; al = (al * (p - 1) + max(-d, 0)) / p
    return 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 1)


def macd(c):
    def ema(a, p):
        k, e, out = 2 / (p + 1), a[0], []
        for x in a:
            e = x * k + e * (1 - k); out.append(e)
        return out
    e12, e26 = ema(c, 12), ema(c, 26); dif = [a - b for a, b in zip(e12, e26)]; dea = ema(dif, 9)
    return round(dif[-1], 2), round(dea[-1], 2)


def boll_pb(c):
    if len(c) < 20:
        return None
    w = c[-20:]; m = sum(w) / 20; sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
    return None if sd == 0 else round((c[-1] - (m - 2 * sd)) / (4 * sd) * 100)


def pct(a, b):
    return None if not a or not b else round((a / b - 1) * 100, 2)


def context():
    a, hist, funds, G, news = load("a.json", {}), load("hist.json", {}), load("funds.json", {}), load("global.json", {}), load("news.json", {})
    cols = a.get("cols", []); R = [dict(zip(cols, r)) for r in a.get("rows", [])]
    ctx = {"数据时间": a.get("asof"), "财报期": a.get("reports"), "字段说明": "精选个股 rpt：Q1=一季报，H1=半年报，Q3=三季报（前三季累计），FY=年报；rev 营收亿元，revY 营收同比%，np 归母净利亿元，npY 净利同比%，roe 加权ROE%（未年化），gm 毛利率%；pe 市盈率TTM。指数成交额单位亿元。"}
    # 指数
    idx = {}
    for code, v in (hist.get("idxk") or {}).items():
        k = v["k"]; c = [r[2] for r in k]; amt = [r[6] or 0 for r in k]
        if len(c) < 60:
            continue
        dif, dea = macd(c[-300:])
        idx[v["name"]] = {"收盘": c[-1], "当日pct": pct(c[-1], c[-2]), "近5日pct": pct(c[-1], c[-6]), "近20日pct": pct(c[-1], c[-21]), "近60日pct": pct(c[-1], c[-61]),
                          "MA20": round(sma(c, 20), 1), "MA60": round(sma(c, 60), 1), "RSI14": rsi(c[-300:]), "MACD": {"DIF": dif, "DEA": dea}, "布林%B": boll_pb(c),
                          "一年高点": max(r[3] for r in k[-250:]), "成交额亿": round(amt[-1] / 1e4), "成交额20日均亿": round(sum(amt[-20:]) / 20 / 1e4), "成交额60日均亿": round(sum(amt[-60:]) / 60 / 1e4)}
    ctx["A股指数"] = idx
    # 宽度、涨跌停、行业、题材
    if R:
        lu = sum(1 for r in R if r["lu"] and r["price"] >= r["lu"] - 0.0005)      # 没有涨跌停价的是不设涨跌幅限制的新股，不计入
        ld = sum(1 for r in R if r["ld"] and r["price"] <= r["ld"] + 0.0005)
        ctx["市场宽度"] = {"上涨": sum(1 for r in R if r["chg"] > 0), "下跌": sum(1 for r in R if r["chg"] < 0), "涨停": lu, "跌停": ld, "总数": len(R),
                         "中位数涨跌pct": sorted(r["chg"] for r in R)[len(R) // 2], "两市成交额亿": round(sum(r["amt"] for r in R))}
        ind = {}
        for r in R:
            if r["ind"] and r["mcap"]:
                x = ind.setdefault(r["ind"], [0.0, 0.0, 0]); x[0] += r["chg"] * r["mcap"]; x[1] += r["mcap"]; x[2] += 1
        rank = sorted(((k, round(v[0] / v[1], 2)) for k, v in ind.items() if v[2] >= 5), key=lambda t: t[1])
        ctx["行业涨跌_市值加权"] = {"领跌": rank[:8], "领涨": rank[-8:][::-1]}
        th = {}
        for r in R:
            for t in r["tags"] or []:
                x = th.setdefault(t, [0.0, 0.0]); x[0] += r["chg"] * r["mcap"]; x[1] += r["mcap"]
        ctx["题材今日_市值加权pct"] = {k: round(v[0] / v[1], 2) for k, v in th.items() if v[1]}
    ctx["两融"] = (funds.get("margin") or [])[:10]
    ctx["北向成交额"] = (funds.get("north") or [])[:10]
    # 全球
    glob = {}
    for ix in G.get("idx", []):
        q = ix.get("q") or {}
        if q.get("p"):
            glob[ix["n"]] = {"最新": q["p"], "涨跌pct": q.get("pct")}
    ctx["全球指数"] = glob
    ctx["快讯标题"] = [f'{i["t"]} [{i["src"]}] {i["title"]}' for i in (news.get("items") or [])[:70]]
    # 精选个股
    tpl = (ROOT / "template.html").read_text(encoding="utf-8")
    block = tpl[tpl.index("const PICKS = ["):tpl.index("];", tpl.index("const PICKS = ["))]
    codes = re.findall(r'\["(\d{6})",', block); by = {r["code"]: r for r in R}
    ctx["精选个股"] = {c: {k: by[c].get(k) for k in ("name", "ind", "price", "chg", "pe", "rpt", "rev", "revY", "np", "npY", "roe", "gm", "tags")} for c in codes if c in by}
    return ctx


SYSTEM = """你是严谨的A股策略研究员，为一个研究型网页撰写每日研判。只能使用用户给出的JSON数据（指数、宽度、行业、题材、两融、北向成交、全球指数、快讯标题、精选个股），不得编造数据、事件、政策或来源；快讯标题只能按标题字面意思引用并注明时间与来源；数据里没有的内容写进 missing，不要猜。
规则：每个维度分别列多方逻辑和空方逻辑，不给单边结论；关键判断注明依据（数据+时间）；结合T+1和涨跌停制度（主板±10%含ST，创业板/科创板±20%，北交所±30%）；短期（1-2周）与中期（1-3个月）展望用“情景+触发条件”表达；不给具体买卖指令、目标价或仓位建议。全部用简体中文。
只输出一个JSON对象，结构严格如下：
{"title": "A 股六维研判 · <日期>", "summary": "一句话总结（不超过60字）",
 "dims": [{"name": "宏观环境|政策面|资金面|技术面|板块与题材|市场情绪", "facts": [{"item": "指标", "value": "数值与含义", "src": "数据来源 · 时间"}], "bull": ["多方逻辑"], "bear": ["空方逻辑"]}],
 "rules": ["结合T+1和涨跌停的解读，2-3条"],
 "outlook": {"short": [{"case": "情景", "trigger": "触发条件"}], "mid": [{"case": "情景", "trigger": "触发条件"}]},
 "drivers": [{"kind": "外部|内部|结构", "title": "今日市场的主要驱动（10字以内）", "text": "说明，不超过90字，注明依据"}],
 "picks": {"六位代码": "这只股票基于最新数据的一句话点评，不超过50字，不含买卖建议"},
 "missing": ["还需要哪些数据才能判断更准"]}
dims 必须正好 6 个且按上面顺序；每个维度 facts 2-4 条、bull 和 bear 各 2-3 条；drivers 正好 3 条；picks 覆盖给出的每只精选个股。"""


def call(model, ctx, key):
    r = requests.post("https://api.deepseek.com/chat/completions", timeout=480, headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "temperature": 0.3, "max_tokens": 32000, "response_format": {"type": "json_object"},
                            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)}]})
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def clean(o, codes):
    s = lambda x, n: str(x or "").strip()[:n]
    lst = lambda xs, n, k: [s(x, n) for x in (xs or [])[:k] if s(x, n)]
    dims = []
    for name in DIMS:
        d = next((x for x in o.get("dims", []) if x.get("name") == name), None) or {}
        dims.append({"name": name, "facts": [{"item": s(f.get("item"), 30), "value": s(f.get("value"), 160), "src": s(f.get("src"), 60)} for f in (d.get("facts") or [])[:5] if isinstance(f, dict)],
                     "bull": lst(d.get("bull"), 200, 4), "bear": lst(d.get("bear"), 200, 4)})
    out = {"title": s(o.get("title"), 60), "summary": s(o.get("summary"), 120), "dims": dims, "rules": lst(o.get("rules"), 220, 4),
           "outlook": {k: [{"case": s(x.get("case"), 160), "trigger": s(x.get("trigger"), 160)} for x in ((o.get("outlook") or {}).get(k) or [])[:4] if isinstance(x, dict)] for k in ("short", "mid")},
           "drivers": [{"kind": s(x.get("kind"), 6), "title": s(x.get("title"), 24), "text": s(x.get("text"), 220)} for x in (o.get("drivers") or [])[:3] if isinstance(x, dict)],
           "picks": {c: s(v, 90) for c, v in (o.get("picks") or {}).items() if c in codes and s(v, 90)}, "missing": lst(o.get("missing"), 120, 6)}
    if sum(len(d["bull"]) + len(d["bear"]) for d in dims) < 12 or len(out["drivers"]) < 2:
        raise ValueError("DeepSeek 输出不完整")
    return out


def refresh_ai():
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        log("ai: 没有 DEEPSEEK_API_KEY，跳过（网页沿用上一版研判）"); return False
    ctx = context()
    if not ctx.get("A股指数"):
        log("ai: 缺少指数数据，跳过"); return False
    for model in [os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"), "deepseek-flash"]:
        try:
            out = clean(call(model, ctx, key), set(ctx.get("精选个股", {})))
            out.update({"generated": bj_now().strftime("%Y-%m-%d %H:%M"), "model": model, "asof": ctx.get("数据时间"), "news_n": len(ctx["快讯标题"])})
            save("ai.json", out)
            log("ai ok", model, "| dims", [len(d["bull"]) + len(d["bear"]) for d in out["dims"]], "| picks", len(out["picks"]))
            return True
        except Exception as e:
            log("ai", model, "failed:", type(e).__name__, str(e)[:200])
    return False

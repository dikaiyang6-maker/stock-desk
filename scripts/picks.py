"""精选名单：每个交易日 A 股收盘后，按原来的五档从前沿科技股里重新挑选。

前沿科技股 = 选股页 D 型的八个方向（东方财富行业）+ 本站题材标签（不含防御/高股息），剔除 ST 和日均成交额不足 3 亿元的。
每档的条件写在 TIERS 里，同一只股票只进一档；理由由数据生成，DeepSeek 每日研判再给每只写一句点评。
节假日（当天没有 A 股收盘数据）不更新，沿用上一个交易日的名单。"""
from common import bj_now, load, log, save
from screen_data import THEME_TRACK, TRACKS

ANN = {"Q1": 4, "H1": 2, "Q3": 4 / 3, "FY": 1}
RPT_NAME = {"Q1": "一季报", "H1": "半年报", "Q3": "前三季", "FY": "年报"}
MAIN = ("sh", "sz")


def _rows():
    a, sc = load("a.json"), load("screen.json", {})
    I = {c: i for i, c in enumerate(a["cols"])}
    tech, tc = sc.get("tech", {}), {c: i for i, c in enumerate(sc.get("techCols", []))}
    ext = sc.get("ext", {})
    out = []
    for row in a["rows"]:
        r = {c: row[i] for c, i in I.items()}
        tracks = [t for t, inds in TRACKS.items() if r["ind"] in inds] + [THEME_TRACK[g] for g in r["tags"] or [] if g in THEME_TRACK]
        themed = [g for g in r["tags"] or [] if g != "防御/高股息"]
        if not tracks and not themed or r["st"]:
            continue
        t = tech.get(r["code"])
        r["amt20"] = t[tc["amt20"]] if t and "amt20" in tc else None
        r["r60"] = t[tc["r60"]] if t and "r60" in tc else None
        if (r["amt20"] or 0) < 3:
            continue
        r["roeA"] = None if r["roe"] is None else r["roe"] * ANN.get(r["rpt"], 1)
        r["rd"] = (ext.get(r["code"]) or {}).get("rd")
        r["track"] = "、".join(dict.fromkeys(tracks + themed))
        out.append(r)
    return out, a["asof"]


def _why(r):
    p = [f'{RPT_NAME.get(r["rpt"], "")}净利 {r["np"]:.1f} 亿（{r["npY"]:+.0f}%）' if r["npY"] is not None else f'{RPT_NAME.get(r["rpt"], "")}净利 {r["np"]:.1f} 亿']
    if r["revY"] is not None:
        p.append(f'营收 {r["revY"]:+.0f}%')
    if r["pe"] and r["pe"] > 0:
        p.append(f'PE {r["pe"]:.0f} 倍')
    if r["roeA"] is not None:
        p.append(f'年化 ROE {r["roeA"]:.0f}%')
    if r["rd"] is not None and r["rd"] >= 8:
        p.append(f'研发投入占营收 {r["rd"]:.0f}%')
    if r["r60"] is not None and abs(r["r60"]) >= 20:
        p.append(f'近 60 日 {r["r60"]:+.0f}%')
    return f'{r["track"]}：' + "，".join(p)


g = lambda v, d=0: d if v is None else v
TIERS = [
    ("业绩已兑现的前沿龙头", "高增长已经体现在报表上，估值相对合理", 6,
     lambda r: r["board"] in MAIN and r["np"] > 0 and g(r["npY"]) >= 30 and g(r["revY"]) >= 15 and 0 < g(r["pe"]) <= 45 and g(r["roeA"]) >= 10 and g(r["cfo"]) > 0 and r["mcap"] >= 300,
     lambda r: min(g(r["npY"]), 200) * 0.4 + g(r["roeA"]) * 1.5 + (45 - r["pe"]) * 0.5 + min(r["mcap"], 5000) / 500),
    ("高增长、高弹性", "增速惊人，但估值或周期性也高，大跌里波动更大", 7,
     lambda r: r["board"] in MAIN and r["np"] >= 1 and (g(r["npY"]) >= 80 or g(r["revY"]) >= 60) and r["mcap"] >= 150,   # 净利至少 1 亿，避免低基数造成的虚高增速
     lambda r: min(g(r["npY"]), 500) + min(g(r["revY"]), 300) * 0.5),
    ("低估值，前沿业务尚在早期", "估值便宜，前沿技术还不是利润主力", 6,
     lambda r: r["board"] in MAIN and r["np"] >= 1 and 0 < g(r["pe"]) <= 20 and g(r["npY"]) > 0 and r["mcap"] >= 100,
     lambda r: (20 - r["pe"]) * 2 + min(g(r["npY"]), 100) * 0.3 + g(r["roeA"])),
    ("创业板 · 科创板 · 北交所 前沿龙头", "主板以外的硬科技核心公司，涨跌幅 ±20%（北交所 ±30%），波动更大", 10,
     lambda r: r["board"] not in MAIN and ((r["np"] > 0 and 0 < g(r["pe"]) <= 300) or (r["np"] <= 0 and g(r["rd"]) >= 15)) and g(r["revY"]) >= 10 and r["mcap"] >= (50 if r["board"] == "bj" else 300),
     lambda r: min(r["mcap"], 8000) / 100 + min(g(r["npY"]), 200) * 0.2 + min(g(r["rd"]), 40) * 0.5),
    ("技术前沿，业绩还没跟上", "题材强、报表弱，适合放进观察名单", 4,
     lambda r: (r["np"] <= 0 or g(r["npY"]) < 0) and g(r["r60"]) >= 20 and r["mcap"] >= 100,
     lambda r: g(r["r60"])),
]


def picks_due():
    """交易日 15:30 之后、当天有 A 股收盘数据、今天还没生成过时运行。"""
    t = bj_now()
    if t.weekday() >= 5 or t.hour * 60 + t.minute < 15 * 60 + 30:
        return False
    asof = load("a.json", {}).get("asof", "")
    if asof[:10] != t.strftime("%Y-%m-%d") or asof[11:] < "15:00":
        return False
    return load("picks.json", {}).get("date") != asof[:10]


def refresh_picks():
    rows, asof = _rows()
    if len(rows) < 200:
        log("picks: 前沿科技股只有", len(rows), "只，数据不完整，沿用上次名单"); return False
    used, tiers = set(), []
    for name, note, n, ok, score in TIERS:
        cand = sorted((r for r in rows if r["code"] not in used and ok(r)), key=score, reverse=True)[:n]
        used.update(r["code"] for r in cand)
        tiers.append({"tier": name, "note": note, "items": [[r["code"], _why(r)] for r in cand]})
    save("picks.json", {"date": asof[:10], "asof": asof, "generated": bj_now().strftime("%Y-%m-%d %H:%M"), "tiers": tiers})
    log("picks", asof, {t["tier"][:6]: len(t["items"]) for t in tiers})
    return True

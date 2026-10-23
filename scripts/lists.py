"""成分股名单（每周）：标普500/道指30（FinanceDataReader + 维基百科）、纳指100（Nasdaq API）、恒生、日经225、富时100、DAX、CAC40、斯托克50、Nifty50、KOSPI200（维基百科）。"""
import io, re
import pandas as pd
import FinanceDataReader as fdr
from common import get

def wiki_tables(page, lang="en"):
    h = get(f"https://{lang}.wikipedia.org/wiki/{page}")
    return pd.read_html(io.StringIO(h.text)) if h else []

def col(t, *keys):
    for c in t.columns:
        s = " ".join(map(str, c)) if isinstance(c, tuple) else str(c)
        if any(k.lower() in s.lower() for k in keys): return c
    return None

def norm(s): return re.sub(r"[^a-z0-9]", "", re.sub(r"\b(inc|corp|corporation|company|co|the|plc|ltd|group|holdings?)\b", "", str(s).lower()))

SECTOR_ZH = {"information technology": "信息技术", "technology": "科技", "health care": "医疗保健", "healthcare": "医疗保健", "financials": "金融", "financial services": "金融",
    "consumer discretionary": "可选消费", "consumer cyclical": "可选消费", "communication services": "通信服务", "telecommunications": "电信", "industrials": "工业",
    "consumer staples": "日常消费", "consumer defensive": "日常消费", "energy": "能源", "utilities": "公用事业", "real estate": "房地产", "materials": "原材料", "basic materials": "原材料"}
def sector_zh(s):
    s = str(s or "").strip()
    if not s or s == "nan": return ""
    low = s.lower()
    for k, v in SECTOR_ZH.items():
        if low == k or low.startswith(k): return v
    for kw, v in [("bank", "银行"), ("insur", "保险"), ("pharma", "医药"), ("semiconductor", "半导体"), ("software", "软件"), ("auto", "汽车"), ("oil", "石油天然气"), ("gas", "石油天然气"),
                  ("mining", "采矿"), ("metal", "金属"), ("chemical", "化工"), ("retail", "零售"), ("food", "食品"), ("beverage", "饮料"), ("tobacco", "烟草"), ("media", "传媒"), ("telecom", "电信"),
                  ("real estate", "房地产"), ("construction", "建筑"), ("aerospace", "航空航天"), ("defen", "国防"), ("transport", "交通运输"), ("travel", "旅游休闲"), ("leisure", "旅游休闲"), ("utilit", "公用事业"),
                  ("electric", "电气"), ("industrial", "工业"), ("financ", "金融"), ("invest", "投资"), ("health", "医疗保健"), ("medical", "医疗保健"), ("consumer", "消费"), ("personal", "个人用品"),
                  ("household", "家居用品"), ("technolog", "科技"), ("electronic", "电子"), ("material", "原材料"), ("energy", "能源"), ("textile", "纺织服装"), ("apparel", "纺织服装"), ("luxury", "奢侈品")]:
        if kw in low: return v
    return s
JP_SECTOR = None

def markets():
    M = {}
    spx = fdr.StockListing("S&P500")
    FIX = {"BRKB": "BRK.B", "BFB": "BF.B"}                       # FinanceDataReader 去掉了类别股代码里的点
    M["spx"] = [{"t": FIX.get(r.Symbol, r.Symbol), "y": FIX.get(r.Symbol, r.Symbol).replace(".", "-"), "en": r.Name, "sec": sector_zh(r.Sector)} for r in spx.itertuples()]
    bynorm = {norm(r["en"]): r for r in M["spx"]}
    # 道琼斯30：维基百科只列公司名，按标普500成分映射代码
    h = get("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average").text
    i = h.find('id="Components"'); seg = h[i:i + 60000] if i > 0 else h
    titles = re.findall(r'<li><a rel="mw:WikiLink" href="[^"]+" title="([^"]+)">', seg)
    dow = []
    for t in titles:
        k = norm(t)
        hit = bynorm.get(k) or next((v for kk, v in bynorm.items() if kk.startswith(k) or k.startswith(kk) and len(kk) > 3), None)
        if hit and hit not in dow: dow.append(hit)
        if len(dow) == 30: break
    M["dow"] = dow
    nd = get("https://api.nasdaq.com/api/quote/list-type/nasdaq100", headers={"Accept": "application/json"}).json()["data"]["data"]["rows"]
    M["ndx"] = [{"t": r["symbol"], "y": r["symbol"].replace(".", "-"), "en": re.sub(r"\s+(Common Stock|Class [A-C].*|Ordinary Shares.*|American Depositary.*|ADS.*)$", "", r["companyName"]), "sec": ""} for r in nd]
    for r in M["ndx"]:
        hit = next((x for x in M["spx"] if x["t"] == r["t"]), None)
        if hit: r["sec"] = hit["sec"]
    # 恒生指数
    t = next(t for t in wiki_tables("Hang_Seng_Index") if col(t, "Ticker") is not None and len(t) > 50)
    M["hsi"] = []
    for _, r in t.iterrows():
        m = re.search(r"\d+", str(r[col(t, "Ticker")]))
        if m: n = int(m.group()); M["hsi"].append({"t": f"{n:05d}", "y": f"{n:04d}.HK", "hk": f"hk{n:05d}", "en": str(r[col(t, "Name")]), "sec": sector_zh(r[col(t, "Sub-index")]) if col(t, "Sub-index") is not None else ""})
    # 日经225（日文维基按行业分表）
    M["n225"] = []
    for t in wiki_tables("%E6%97%A5%E7%B5%8C%E5%B9%B3%E5%9D%87%E6%A0%AA%E4%BE%A1", "ja"):
        cc, nc = col(t, "証券コード"), col(t, "銘柄")
        if cc is None or nc is None: continue
        for _, r in t.iterrows():
            m = re.search(r"\d{4}", str(r[cc]))
            if m and not any(x["t"] == m.group() for x in M["n225"]): M["n225"].append({"t": m.group(), "y": m.group() + ".T", "en": str(r[nc]), "sec": ""})
    # 富时100 / DAX / CAC40 / 斯托克50 / Nifty50 / KOSPI200
    def from_wiki(page, tick_keys, name_keys, sec_keys, suffix, minrows):
        t = next(t for t in wiki_tables(page) if col(t, *tick_keys) is not None and col(t, *name_keys) is not None and len(t) >= minrows)
        out = []
        for _, r in t.iterrows():
            tk = str(r[col(t, *tick_keys)]).strip().split()[0]
            if not tk or tk == "nan": continue
            y = suffix(tk, r, t)
            out.append({"t": tk, "y": y, "en": str(r[col(t, *name_keys)]), "sec": sector_zh(r[col(t, *sec_keys)]) if col(t, *sec_keys) is not None else ""})
        return out
    M["ftse"] = from_wiki("FTSE_100_Index", ["Ticker"], ["Company"], ["sector", "industry"], lambda tk, r, t: tk.rstrip(".").replace(".", "-") + ".L", 90)
    M["dax"] = from_wiki("DAX", ["Ticker"], ["Company"], ["Sector"], lambda tk, r, t: tk if "." in tk else tk + ".DE", 35)
    M["cac"] = from_wiki("CAC_40", ["Ticker"], ["Company"], ["Sector"], lambda tk, r, t: tk if "." in tk else tk + ".PA", 35)
    EX = {"xetra": ".DE", "frankfurt": ".DE", "paris": ".PA", "amsterdam": ".AS", "milan": ".MI", "madrid": ".MC", "brussels": ".BR", "helsinki": ".HE", "dublin": ".IR", "lisbon": ".LS", "vienna": ".VI"}
    def sx(tk, r, t):
        if "." in tk: return tk
        ml = str(r[col(t, "Main listing")]).lower()
        return tk + next((v for k, v in EX.items() if k in ml), "")
    M["sx5e"] = from_wiki("Euro_Stoxx_50", ["Ticker"], ["Name"], ["Sector"], sx, 45)
    M["nifty"] = from_wiki("NIFTY_50", ["Symbol"], ["Company"], ["Sector"], lambda tk, r, t: tk + ".NS", 45)
    M["kospi"] = from_wiki("KOSPI_200", ["Symbol"], ["Company"], ["Sector"], lambda tk, r, t: re.sub(r"\D", "", tk).zfill(6) + ".KS", 150)
    return M


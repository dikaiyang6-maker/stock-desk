"""把 data/ 里的最新数据合进 template.html，生成 site/index.html（网页版，浏览器里打开会自动切到腾讯实时行情）。"""
import json

from pypinyin import Style, lazy_pinyin

import screen_data
from common import DATA, ROOT, SITE, bj_now, load, log

SEC_ZH2 = {"Finance": "金融", "Properties": "地产", "Utilities": "公用事业", "Commerce & Industry": "工商业"}


PREFIX = ("*ST", "ST", "N", "C", "U", "W", "XD", "XR", "DR")


def _ini(name):
    letters = lazy_pinyin(name, style=Style.FIRST_LETTER, errors=lambda x: [c.lower() for c in x if c.isascii() and c.isalnum()])
    return "".join(letters).lower()


def pinyin_initials(a, G):
    """A 股和全球个股名称的拼音首字母；带 ST、N 等前缀的另存一份去掉前缀的，空格分隔。"""
    out = {"a": {}, "g": {}}
    I = {c: i for i, c in enumerate(a["cols"])}
    for r in a["rows"]:
        name = r[I["name"]]; v = {_ini(name)}
        for p in PREFIX:
            if name.startswith(p) and len(name) > len(p):
                v.add(_ini(name[len(p):].lstrip("-")))
        out["a"][r[I["code"]]] = " ".join(x for x in v if x)
    for y, s in (G.get("st") or {}).items():
        n = s.get("n") or ""
        if any("\u4e00" <= ch <= "\u9fff" for ch in n):          # 只给有汉字的名称（美股、港股中文简称等）
            out["g"][y] = _ini(n)
    return out


def build():
    a, hist, extra, G = load("a.json"), load("hist.json", {}), load("extra.json", {}), load("global.json", {})
    if not a or not hist.get("idxk"):
        raise SystemExit("缺少 A 股数据，无法生成网页")
    data = dict(a)
    data["built"] = bj_now().strftime("%Y-%m-%d %H:%M")          # 网页生成时间（北京时间），网页据此提示自动更新是否停了
    data["idxk"] = hist["idxk"]
    data["idxm"] = {c: [{"d": x["d"], "prec": x["prec"], "p": [p[:3] for p in x["p"]]} for x in v] for c, v in hist.get("idxm", {}).items()}
    data["sk"] = hist.get("sk", {})
    data["skm"] = extra.get("skm", {})
    data["sm"] = extra.get("sm", {})
    if G.get("idx"):
        idx = []
        for ix in G["idx"]:
            ix = dict(ix)
            if len(ix.get("k") or []) < 30:
                ix["k"], ix["m"] = [], []
            idx.append(ix)
        rnd = lambda v: round(v, 4) if isinstance(v, float) else v
        st = {k: {kk: rnd(vv) for kk, vv in s.items() if vv not in (None, "")} for k, s in (G.get("st") or {}).items()}
        for s in st.values():
            s["sec"] = SEC_ZH2.get(s.get("sec"), s.get("sec"))
        data["g"] = {"asof": G.get("asof"), "idx": idx, "mk": G.get("mk", {}), "st": st}
    ai = load("ai.json")
    if ai:
        data["ai"] = ai
    pk = load("picks.json", {})
    if pk.get("tiers"):
        data["pk"] = pk                                           # 每个交易日收盘后按规则更新的精选名单
    fl = load("flow.json", {})
    if fl.get("pool") is not None:
        data["fl"] = {k: v for k, v in fl.items() if k != "prev"}  # 资金监控：每日资金异动选股、监控池、观察期
    data["py"] = pinyin_initials(a, G)                            # 拼音首字母，搜索用（如 拓普集团 → tpjt）
    sc = load("screen.json", {})
    if sc.get("ext") or sc.get("tech"):                       # 选股板块：按列压成数组，减小网页体积
        ext_cols = ["rd", "rdP", "debt", "cash", "eq", "gw", "revT", "lift", "hs", "es"]
        data["sc"] = {"extCols": ext_cols, "ext": {c: [e.get(k) for k in ext_cols] for c, e in sc.get("ext", {}).items()},
                      **{k: sc.get(k) for k in ("techCols", "tech", "mo", "emo", "amt20", "extAsof", "extFy", "techAsof", "techDate")},
                      "tracks": screen_data.TRACKS, "themeTrack": screen_data.THEME_TRACK, "trackKw": screen_data.TRACK_KW}   # 赛道配置直接取代码里的，不依赖当天抓取
    ta = json.loads((ROOT / "static" / "ta.json").read_text(encoding="utf-8")) if (ROOT / "static" / "ta.json").exists() else {}
    t = (ROOT / "template.html").read_text(encoding="utf-8")
    t = t.replace("/*__DATA__*/", json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
    t = t.replace("/*__TA__*/", json.dumps(ta, ensure_ascii=False).replace("</", "<\\/"))
    head = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<meta name="robots" content="noindex"><link rel="icon" href="data:,">'
            '<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0}img{max-width:100%}</style></head><body>')
    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(head + t + "</body></html>", encoding="utf-8")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    if fl.get("pool") is not None:                                # 完整的监控状态，数据缓存丢了时 flow.py 从这里取回
        (SITE / "flow.json").write_text(json.dumps(fl, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("site", (SITE / "index.html").stat().st_size // 1024, "KB | asof", a.get("asof"), "| ai", ai.get("generated") if ai else None,
        "| global idx", len(data.get("g", {}).get("idx", [])), "stocks", len(data.get("g", {}).get("st", {})))


if __name__ == "__main__":
    build()

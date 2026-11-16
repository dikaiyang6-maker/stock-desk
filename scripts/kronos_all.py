"""全部个股和指数的 AI K 线预测（开源模型 Kronos-small），在单独的工作流 .github/workflows/kronos.yml 里运行。

覆盖：沪深京全部 A 股、A 股主要指数、全球指数、本站的全球个股（美股、港股、日股、韩股、欧股、印度）。
两批：asia（A 股、港日韩和亚太指数，北京时间 16:00 以后）和 west（美股、欧股、印度和欧美指数，美股收盘后）。
每只预测之后 10 个交易日：最近最多 256 个交易日的日K（开高低收、成交量、成交额），贪心解码（每次取概率最大的结果，
同样的数据得到同样的预测，算力只要抽样平均的几分之一）。

准确率：
  1. 预测记录：每只每天的 5 日、10 日预测涨跌存下来，真实行情出来后统计方向准确率和收盘价误差，并和“价格不变”基准比；
  2. 回看检验：每次抽 10% 的标的，只用 10 个交易日以前的数据预测最近 10 天，当天就能看到这个模型大概准不准。
模型输出只是概率性的推演，准确率照实显示在网页上，不构成投资建议。

用法（工作流里依次运行）：
  python scripts/kronos_all.py plan [--group auto|asia|west|all] [--force]   → kall_plan/plan.json；写 GITHUB_OUTPUT：due、matrix
  python scripts/kronos_all.py shard --i K --n N                              → kall_part/part_K.json（需要 torch 和 Kronos 代码）
  python scripts/kronos_all.py merge                                          → kall/kr.json（网页用）、kall/log.json、kall/state.json
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA, KALL, PLAN, PART = ROOT / "data", ROOT / "kall", ROOT / "kall_plan", ROOT / "kall_part"
BJ = dt.timezone(dt.timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
PRED, MAXL, BUCKETS, CHECK_EVERY, KEEP_LOG, PER_SHARD = 10, 256, (256, 128, 64), 10, 30, 380
sys.path.insert(0, os.path.dirname(__file__))
from kronos_fc import MODEL, MODEL_REV, TOKENIZER, TOKENIZER_REV   # noqa: E402  模型和权重版本和量化页那 8 个标的一致

# 全球个股按交易所分市场；asia 批在北京时间下午跑，west 批在美股收盘后跑
MKT = {"纽交所": "美股", "纳斯达克": "美股", "美股": "美股", "港交所": "港股", "Tokyo": "日股", "KSE": "韩股", "NSE": "印度"}
ASIA_MKT = {"A股", "A股指数", "港股", "日股", "韩股"}
# A 股指数：腾讯代码 → 名称（和网页里能打开的指数一致，再加中证500、中证1000、上证50）
A_IDX = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指", "sh000688": "科创50", "bj899050": "北证50",
         "sh000300": "沪深300", "sh000905": "中证500", "sh000852": "中证1000", "sh000016": "上证50"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def jload(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def jsave(p, obj):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(p)


def gh_output(**kv):
    f = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{k}={v}" for k, v in kv.items()]
    if f:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))


# ---------------- plan：要不要跑、跑哪些、分几片 ----------------
def a_sym(code):
    if code.startswith(("92", "4", "8")):
        return "bj" + code
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def universe(groups):
    """[[类别, 代码, 数据源, 取数代码, 名称, 市场], ...]；类别 a=A股 ai=A股指数 gi=全球指数 gs=全球个股。"""
    items = []
    if "asia" in groups:
        a = jload(DATA / "a.json", {}) or {}
        I = {c: i for i, c in enumerate(a.get("cols", []))}
        for r in a.get("rows", []):
            c = r[I["code"]]
            items.append(["a", c, "tx", a_sym(c), r[I["name"]], "A股"])
        items += [["ai", c, "tx", c, n, "A股指数"] for c, n in A_IDX.items()]
    G = jload(DATA / "global.json", {}) or {}
    from global_mkt import INDICES                                            # 指数代码表（只用常量）
    for code, name, region, src, live, ysym, _em in INDICES:
        west = region != "亚太" or code == "emSENSEX"                           # 印度收盘晚，放在 west 批
        if ("west" if west else "asia") not in groups:
            continue
        if ysym:
            items.append(["gi", code, "yh", ysym, name, "全球指数"])
        elif src == "hk":
            items.append(["gi", code, "txhk", live, name, "全球指数"])
    for y, s in (G.get("st") or {}).items():
        mkt = MKT.get(s.get("ex"), "欧股")
        if ("asia" if mkt in ASIA_MKT else "west") not in groups:
            continue
        if mkt == "美股" and s.get("txk"):
            items.append(["gs", y, "txus", s["txk"], s.get("n") or s.get("en") or y, mkt])
        elif mkt == "港股" and s.get("tx"):
            items.append(["gs", y, "txhk", s["tx"], s.get("n") or y, mkt])
        else:
            items.append(["gs", y, "yh", y, s.get("n") or s.get("en") or y, mkt])
    return items


def plan(group, force):
    st = jload(KALL / "state.json", {}) or {}
    bj, utc = dt.datetime.now(BJ), dt.datetime.now(dt.timezone.utc)
    want = {"auto": ("asia", "west"), "all": ("asia", "west")}.get(group, (group,))
    groups = []
    if "asia" in want and (force or (bj.weekday() < 5 and (bj.hour, bj.minute) >= (16, 0) and (st.get("asia") or {}).get("date") != bj.strftime("%Y-%m-%d"))):
        groups.append("asia")
    if "west" in want and (force or (utc.weekday() < 5 and (utc.hour, utc.minute) >= (21, 10) and (st.get("west") or {}).get("date") != utc.strftime("%Y-%m-%d"))):
        groups.append("west")
    if not groups:
        log("plan: 现在没有要跑的批次", {"group": group, "state": st})
        gh_output(due="false", matrix="[]", n="0", groups="")
        return
    items = universe(groups)
    n = max(1, min(20, math.ceil(len(items) / PER_SHARD)))
    jsave(PLAN / "plan.json", {"groups": groups, "dates": {"asia": bj.strftime("%Y-%m-%d"), "west": utc.strftime("%Y-%m-%d")},
                                "made": utc.strftime("%Y-%m-%dT%H:%MZ"), "n": n, "items": items})
    log("plan:", groups, len(items), "个标的，分", n, "片", {m: sum(1 for x in items if x[5] == m) for m in sorted({x[5] for x in items})})
    gh_output(due="true", matrix=json.dumps(list(range(n))), n=str(n), groups=",".join(groups))


# ---------------- shard：取日K、跑模型 ----------------
SES = requests.Session(); SES.headers["User-Agent"] = UA


def _get(url, params=None, tries=3, timeout=20):
    for k in range(tries):
        try:
            r = SES.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (404, 400):
                return None
            if r.status_code == 429:
                time.sleep(4 + 4 * k)
        except requests.RequestException:
            pass
        time.sleep(1 + k)
    return None


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def bars_tx(api, sym, count=MAXL + 30):
    """腾讯日K（前复权）→ [[日期, 开, 高, 低, 收, 量, 额], ...]。api：newfqkline（A股、A股指数）、hkfqkline、usfqkline。"""
    r = _get(f"https://web.ifzq.gtimg.cn/appstock/app/{api}/get", {"param": f"{sym},day,,,{count},qfq"})
    try:
        x = r.json()["data"][sym]
        k = x.get("qfqday") or x.get("day") or []
    except (AttributeError, ValueError, TypeError, KeyError):
        return None
    out = []
    for b in k:
        o, c, h, l = _f(b[1]), _f(b[2]), _f(b[3]), _f(b[4])
        if None in (o, c, h, l) or c <= 0:
            continue
        v = _f(b[5]) or 0.0
        a = _f(b[8]) if len(b) > 8 and not isinstance(b[8], dict) else None
        out.append([b[0], o, h, l, c, v, a if a is not None else v * c])
    return out


def bars_yh(sym):
    r = _get("https://query2.finance.yahoo.com/v8/finance/chart/" + requests.utils.quote(sym), {"range": "2y", "interval": "1d"}, timeout=30)
    try:
        res = r.json()["chart"]["result"][0]
        ts, q, off = res.get("timestamp") or [], res["indicators"]["quote"][0], res["meta"].get("gmtoffset", 0)
    except (AttributeError, ValueError, TypeError, KeyError, IndexError):
        return None
    out = {}
    for i, t in enumerate(ts):
        c = _f((q.get("close") or [None] * len(ts))[i])
        if not c or c <= 0:
            continue
        o = _f(q["open"][i]) or c; h = _f(q["high"][i]) or max(o, c); l = _f(q["low"][i]) or min(o, c)
        v = _f((q.get("volume") or [0] * len(ts))[i]) or 0.0
        d = dt.datetime.fromtimestamp(t + off, dt.timezone.utc).strftime("%Y-%m-%d")
        out[d] = [d, o, h, l, c, v, v * c]
    rows = [out[k] for k in sorted(out)]
    return rows[-(MAXL + 30):]


def fetch(item):
    kind, key, src, fsym = item[:4]
    try:
        if src == "tx":
            return bars_tx("newfqkline", fsym)
        if src == "txhk":
            return bars_tx("hkfqkline", fsym)
        if src == "txus":
            return bars_tx("usfqkline", fsym)
        time.sleep(0.4)                                                        # Yahoo 慢一点，避免限流
        return bars_yh(fsym)
    except Exception as e:                                                    # 单只失败不影响其他
        log("fetch", key, type(e).__name__, str(e)[:80])
        return None


def shard(i, n):
    import numpy as np
    import pandas as pd
    import torch
    from model import Kronos, KronosPredictor, KronosTokenizer
    torch.manual_seed(0); torch.set_num_threads(max(1, os.cpu_count() or 1))
    P = jload(PLAN / "plan.json")
    items = [(gi, it) for gi, it in enumerate(P["items"]) if gi % n == i]
    t0 = time.time()
    fast = [x for x in items if x[1][2] != "yh"]; slow = [x for x in items if x[1][2] == "yh"]
    with ThreadPoolExecutor(8) as ex:
        got = dict(zip([gi for gi, _ in fast], ex.map(lambda x: fetch(x[1]), fast)))
    with ThreadPoolExecutor(2) as ex:
        got.update(zip([gi for gi, _ in slow], ex.map(lambda x: fetch(x[1]), slow)))
    log(f"shard {i}/{n}: {len(items)} 个标的，取到 {sum(1 for v in got.values() if v)} 个日K，用时 {time.time() - t0:.0f}s")
    tok = KronosTokenizer.from_pretrained(TOKENIZER, revision=TOKENIZER_REV)
    model = Kronos.from_pretrained(MODEL, revision=MODEL_REV)
    pred = KronosPredictor(model, tok, device="cpu", max_context=512)
    cols = ["open", "high", "low", "close", "volume", "amount"]

    def frame(b):
        df = pd.DataFrame(b, columns=["date"] + cols)
        ts = pd.Series(pd.to_datetime(df["date"]))
        fut = pd.Series(pd.bdate_range(ts.iloc[-1] + pd.Timedelta(days=1), periods=PRED))
        return df[cols].astype(float).reset_index(drop=True), ts.reset_index(drop=True), fut

    jobs, res, fail, short = [], {}, [], []                                   # jobs：(桶长度, 标的序号, 用途, 日K)
    for gi, it in items:
        b = got.get(gi)
        if not b:
            fail.append(it[1]); continue
        L = next((x for x in BUCKETS if len(b) >= x), None)
        if not L:
            short.append(it[1]); continue
        jobs.append((L, gi, "fc", b[-L:]))
        if gi % CHECK_EVERY == 0 and len(b) >= L + PRED:                      # 回看检验（抽样 10%）
            jobs.append((L, gi, "chk", b[-(L + PRED):-PRED]))
    out = {}
    for L in BUCKETS:
        js = [j for j in jobs if j[0] == L]
        for s in range(0, len(js), 32):
            chunk = js[s:s + 32]
            fr = [frame(j[3]) for j in chunk]
            try:
                ps = pred.predict_batch([x for x, _, _ in fr], [t for _, t, _ in fr], [f for _, _, f in fr], pred_len=PRED, T=1.0, top_k=1, top_p=1.0, sample_count=1, verbose=False)
            except Exception as e:
                log("predict", L, type(e).__name__, str(e)[:120]); fail += [P["items"][j[1]][1] for j in chunk if j[2] == "fc"]; continue
            for j, p in zip(chunk, ps):
                out[(j[1], j[2])] = p
        log(f"  长度 {L}：{len(js)} 次预测，累计 {time.time() - t0:.0f}s")
    rnd = lambda v: round(float(v), 2)
    for gi, it in items:
        p = out.get((gi, "fc")); b = got.get(gi)
        if p is None or not b:
            continue
        c0 = b[-1][4]
        r = {"k": it[0], "key": it[1], "m": it[5], "d": b[-1][0], "c": round(c0, 4), "L": next(x for x in BUCKETS if len(b) >= x),
             "p": [rnd((v / c0 - 1) * 100) for v in p["close"]], "lo": rnd((p["low"].min() / c0 - 1) * 100), "hi": rnd((p["high"].max() / c0 - 1) * 100),
             "rc": [[x[0], round(x[4], 4)] for x in b[-45:]]}                      # 最近 45 天收盘：merge 用来核对以前的预测
        if it[0] in ("gi", "gs"):
            r["h"] = [rnd((x[4] / c0 - 1) * 100) for x in b[-21:-1]]           # 海外标的网页上可能没有 K 线：带最近 20 天走势画图
        q = out.get((gi, "chk"))
        if q is not None:
            base = b[-PRED - 1][4]
            r["chk"] = [rnd((q["close"].iloc[4] / base - 1) * 100), rnd((b[-PRED + 4][4] / base - 1) * 100), rnd((q["close"].iloc[-1] / base - 1) * 100), rnd((c0 / base - 1) * 100)]
        res[f"{it[0]}:{it[1]}"] = r                                            # 类别:代码，四类之间不会撞
    jsave(PART / f"part_{i}.json", {"i": i, "n": n, "res": res, "fail": fail, "short": short, "secs": round(time.time() - t0)})
    log(f"shard {i}/{n}: 预测 {len(res)} 个，失败 {len(fail)}，上市太短 {len(short)}，用时 {time.time() - t0:.0f}s")


# ---------------- merge：合并、核对准确率、写网页文件 ----------------
SEC = {"a": "a", "ai": "ai", "gi": "gi", "gs": "gs"}


def _stats(rows):
    """rows：[(预测%, 实际%)] → [次数, 方向对%, 收盘价平均误差%, “价格不变”基准误差%, 实际上涨占比%]。"""
    n = len(rows)
    if not n:
        return None
    hit = sum(1 for p, r in rows if (p > 0) == (r > 0) and p != 0) / n * 100
    mae = sum(abs((1 + p / 100) / (1 + r / 100) - 1) for p, r in rows) / n * 100
    base = sum(abs(1 / (1 + r / 100) - 1) for _, r in rows) / n * 100
    up = sum(1 for _, r in rows if r > 0) / n * 100
    return [n, round(hit, 1), round(mae, 2), round(base, 2), round(up, 1)]


def merge():
    P = jload(PLAN / "plan.json", {}) or {}
    kr = jload(KALL / "kr.json", {}) or {}
    lg = jload(KALL / "log.json", {}) or {}
    st = jload(KALL / "state.json", {}) or {}
    parts = [jload(p) for p in sorted(PART.glob("part_*.json"))]
    parts = [p for p in parts if p]
    res = {k: v for p in parts for k, v in p["res"].items()}
    if not res:
        log("merge: 没有任何结果，保留上一次"); sys.exit(1)
    fail = [k for p in parts for k in p["fail"]]; short = [k for p in parts for k in p["short"]]
    for s in SEC.values():
        kr.setdefault(s, {})
    real = {}                                                                 # 每个标的最近 45 天的收盘
    for key, r in res.items():
        kr[SEC[r["k"]]][r["key"]] = [r["d"], r["c"], r["p"], r["lo"], r["hi"], 0] + ([r["h"]] if "h" in r else [])
        e = lg.setdefault(key, [])
        e[:] = [x for x in e if x[0] != r["d"]] + [[r["d"], r["p"][4], r["p"][9]]]   # 同一个基准日只留一条（节假日重复运行不重复计数）
        del e[:-KEEP_LOG]
        real[key] = r["rc"]
    # 预测记录核对：每个标的、每个市场
    by_m = {}
    for key, r in res.items():
        rc = real[key]; pos = {d: i for i, (d, _) in enumerate(rc)}
        rows5, rows10 = [], []
        for d, p5, p10 in lg.get(key, []):
            i = pos.get(d)
            if i is None:
                continue
            if i + 5 < len(rc):
                rows5.append((p5, (rc[i + 5][1] / rc[i][1] - 1) * 100))
            if i + 10 < len(rc):
                rows10.append((p10, (rc[i + 10][1] / rc[i][1] - 1) * 100))
        s5, s10 = _stats(rows5), _stats(rows10)
        if s5 or s10:
            kr[SEC[r["k"]]][r["key"]][5] = [s5, s10]
        m = by_m.setdefault(r["m"], {"5": [], "10": [], "c5": [], "c10": []})
        m["5"] += rows5; m["10"] += rows10
        if r.get("chk"):
            m["c5"].append((r["chk"][0], r["chk"][1])); m["c10"].append((r["chk"][2], r["chk"][3]))
    acc = kr.setdefault("acc", {}); chk = kr.setdefault("chk", {}); cnt = kr.setdefault("cnt", {})
    for mkt in by_m:
        cnt[mkt] = sum(1 for r in res.values() if r["m"] == mkt)            # 这次每个市场预测了多少只
    for mkt, m in by_m.items():
        acc[mkt] = {"h5": _stats(m["5"]), "h10": _stats(m["10"])}
        if m["c10"]:
            chk[mkt] = {"h5": _stats(m["c5"]), "h10": _stats(m["c10"]), "t": P.get("made")}
    now_bj = dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M")
    gs = P.get("groups", [])
    for g in gs:
        st[g] = {"date": P["dates"][g], "t": now_bj}
        kr.setdefault("runs", {})[g] = {"t": now_bj, "n": len(res), "fail": len(fail), "short": len(short), "with": gs}
    kr.update({"built": now_bj, "model": "Kronos-small", "pred": PRED, "maxL": MAXL, "decode": "greedy"})
    jsave(KALL / "kr.json", kr); jsave(KALL / "log.json", lg); jsave(KALL / "state.json", st)
    log("merge:", len(res), "个预测，失败", len(fail), "，上市太短", len(short), "| 预测记录核对",
        {k: (v["h5"] or [0])[0] for k, v in acc.items()}, "| 回看检验", {k: v["h10"][:3] for k, v in chk.items() if v.get("h10")})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["plan", "shard", "merge"])
    ap.add_argument("--group", default="auto")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--i", type=int, default=0)
    ap.add_argument("--n", type=int, default=1)
    a = ap.parse_args()
    if a.cmd == "plan":
        plan(a.group, a.force or os.environ.get("FORCE_KALL") == "1")
    elif a.cmd == "shard":
        shard(a.i, a.n)
    else:
        merge()

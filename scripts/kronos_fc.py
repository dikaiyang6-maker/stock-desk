"""AI K 线预测：开源时序大模型 Kronos（github.com/shiyu-coder/Kronos，模型 NeoQuasar/Kronos-small），结果写进 data/kronos.json。

每个交易日收盘后（工作流里单独的步骤，见 update.yml）：
  1. 读入每个标的最近 400 个交易日的日K（腾讯，前复权），抽样 3 条路径取平均，预测之后 10 个交易日；
  2. 把每天的预测记下来，等真实行情出来后自动核对：5 日、10 日后的收盘误差和涨跌方向对不对，累积成准确率；
  3. 每周做一次“回看检验”：只用 10 个交易日以前的数据预测最近 10 天，和真实走势对比（第一次运行也会做）。
模型输出只是概率推演，误差可能很大，网页上照实显示准确率，不构成投资建议。

用法：python scripts/kronos_fc.py due | run    （run 需要 PYTHONPATH 里能找到 Kronos 仓库的 model 包）"""
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from common import ROOT, bj_now, close_date, load, log, save   # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
TARGETS = [("sh000001", "上证指数"), ("sh000300", "沪深300"), ("sz399006", "创业板指"), ("sh000688", "科创50"),
           ("sh600519", "贵州茅台"), ("sz300750", "宁德时代"), ("sh688981", "中芯国际"), ("sh601318", "中国平安")]
LOOKBACK, PRED, SAMPLES, KEEP_LOG = 400, 10, 3, 80
# 模型权重（safetensors 格式）固定在审查过的版本；Kronos 代码在工作流里固定在提交 67b630e
MODEL, MODEL_REV = "NeoQuasar/Kronos-small", "901c26c1332695a2a8f243eb2f37243a37bea320"
TOKENIZER, TOKENIZER_REV = "NeoQuasar/Kronos-Tokenizer-base", "0e0117387f39004a9016484a186a908917e22426"


def due():
    """有新的收盘数据、这一天还没预测时为 True；同一天最多试 2 次。"""
    d = close_date()
    if not d:
        return False
    k = load("kronos.json", {})
    if os.environ.get("FORCE_KRONOS") == "1":
        return True
    if k.get("date") == d:
        return False
    tries = k.get("try") if (k.get("try") or {}).get("date") == d else {"date": d, "n": 0}
    if tries["n"] >= 2:
        return False
    tries["n"] += 1
    k["try"] = tries
    save("kronos.json", k)
    return True


def _daily(sym, days=700):
    import gp
    end = bj_now().date()
    df = gp.daily(sym, end - dt.timedelta(days=days), end, "qfq").reset_index(drop=True)
    d = close_date()
    return df[df["date"] <= d].reset_index(drop=True) if d else df


def _future_days(last, n):
    """之后 n 个交易日：跳过周末（节假日不知道，按工作日排）。"""
    import pandas as pd
    return list(pd.bdate_range(pd.Timestamp(last) + pd.Timedelta(days=1), periods=n))


def run():
    import numpy as np
    import pandas as pd
    import torch
    from model import Kronos, KronosPredictor, KronosTokenizer
    torch.manual_seed(42)
    np.random.seed(42)
    torch.set_num_threads(max(1, os.cpu_count() or 1))
    d = close_date() or (load("a.json", {}).get("asof") or "")[:10]
    old = load("kronos.json", {})
    t0 = time.time()
    tok = KronosTokenizer.from_pretrained(TOKENIZER, revision=TOKENIZER_REV)
    model = Kronos.from_pretrained(MODEL, revision=MODEL_REV)
    pred = KronosPredictor(model, tok, device="cpu", max_context=512)

    def forecast(hist, ts):
        x = hist[["open", "high", "low", "close", "volume", "amount"]].astype(float).reset_index(drop=True)
        return pred.predict(df=x, x_timestamp=pd.Series(pd.to_datetime(hist["date"])).reset_index(drop=True),
                            y_timestamp=pd.Series(ts), pred_len=len(ts), T=1.0, top_p=0.9, sample_count=SAMPLES, verbose=False)

    week_check = not old.get("checkDate") or (dt.date.fromisoformat(d) - dt.date.fromisoformat(old["checkDate"])).days >= 7
    items, fails, closes = [], [], {}
    for sym, name in TARGETS:
        try:
            df = _daily(sym)
            if len(df) < LOOKBACK + PRED:
                fails.append(sym); continue
            closes[sym] = dict(zip(df["date"], df["close"].astype(float)))
            fut = _future_days(df["date"].iloc[-1], PRED)
            p = forecast(df.iloc[-LOOKBACK:], fut)
            c0 = float(df["close"].iloc[-1])
            it = {"sym": sym, "name": name, "base": round(c0, 2), "baseD": df["date"].iloc[-1],
                  "hist": [[r["date"], round(float(r["close"]), 2)] for r in df.iloc[-60:].to_dict("records")],
                  "fc": [[str(t.date()), round(float(c), 2), round(float(lo), 2), round(float(hi), 2)] for t, c, lo, hi in zip(fut, p["close"], p["low"], p["high"])]}
            it["chg"] = round((it["fc"][-1][1] / c0 - 1) * 100, 2)
            if week_check:                                                   # 回看检验：用 10 个交易日以前的数据预测最近 10 天
                hist, real = df.iloc[-(LOOKBACK + PRED):-PRED], df.iloc[-PRED:]
                q = forecast(hist, list(pd.to_datetime(real["date"])))
                rc, pc, start = real["close"].astype(float).values, q["close"].astype(float).values, float(hist["close"].iloc[-1])
                it["check"] = {"from": real["date"].iloc[0], "to": real["date"].iloc[-1], "start": round(start, 2),
                               "real": [round(x, 2) for x in rc], "pred": [round(x, 2) for x in pc],
                               "mae": round(float(np.mean(np.abs(pc / rc - 1)) * 100), 2), "end": round(float((pc[-1] / rc[-1] - 1) * 100), 2),
                               "dir": bool(np.sign(pc[-1] - start) == np.sign(rc[-1] - start))}
            else:
                it["check"] = next((x.get("check") for x in old.get("items", []) if x["sym"] == sym), None)
            items.append(it)
        except Exception as e:
            log("kronos:", sym, type(e).__name__, str(e)[:120]); fails.append(sym)
    # 预测记录：每天每个标的一条，真实行情出来后核对 5 日、10 日
    book = [x for x in old.get("log", []) if x["d"] != d] + [{"d": d, "sym": it["sym"], "base": it["base"], "fc": [f[1] for f in it["fc"]]} for it in items]
    book = book[-KEEP_LOG * len(TARGETS):]
    acc = {}
    for h in (5, 10):
        hits = errs = n = 0
        per = {}
        for x in book:
            c = closes.get(x["sym"])
            if not c:
                continue
            days = sorted(k for k in c if k > x["d"])
            if len(days) < h:
                continue
            real = c[days[h - 1]]
            p = x["fc"][h - 1]
            ok = (p - x["base"]) * (real - x["base"]) > 0
            e = abs(p / real - 1) * 100
            n += 1; hits += ok; errs += e
            s = per.setdefault(x["sym"], [0, 0, 0.0]); s[0] += 1; s[1] += ok; s[2] += e
        acc[f"h{h}"] = {"n": n, "dir": round(hits / n * 100, 1) if n else None, "mae": round(errs / n, 2) if n else None,
                        "per": {k: [v[0], round(v[1] / v[0] * 100, 1), round(v[2] / v[0], 2)] for k, v in per.items()}}
    out = {"date": d, "built": bj_now().strftime("%Y-%m-%d %H:%M"), "model": "Kronos-small", "pred": PRED, "samples": SAMPLES, "lookback": LOOKBACK,
           "items": items, "fail": fails, "log": book, "acc": acc, "checkDate": d if week_check else old.get("checkDate"), "secs": round(time.time() - t0)}
    save("kronos.json", out)
    log("kronos", d, "|", len(items), "个标的，用时", out["secs"], "s | 失败", fails, "| 累计核对", {k: (v["n"], v["dir"], v["mae"]) for k, v in acc.items()})
    return bool(items)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "due":
        print("true" if due() else "false")
    elif cmd == "run":
        run()
    else:
        raise SystemExit(__doc__)

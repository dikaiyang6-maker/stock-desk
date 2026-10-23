"""每次运行（GitHub Actions 交易时段每小时一次）：按各项任务的周期决定刷新什么，任何一项失败都沿用缓存继续，最后生成网页。

    行情、指数与题材股 K 线、分时、全球指数、美股港股日韩成分股   每次（约 1 小时）
    快讯标题                                                       每次
    欧洲、印度成分股（Yahoo，较慢）                                 每 6 小时
    财报、两融与北向成交                                           每 20 小时
    DeepSeek 研判（A 股收盘后，北京时间 15:30 之后）                每个交易日一次
    指数完整历史、题材股月K、成分股名单                             每 7 天

环境变量：FORCE=1 全部重跑；ONLY=任务名,任务名 只跑指定任务；DEEPSEEK_API_KEY 可选。"""
import gzip
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(__file__))
from common import DATA, ROOT, bj_now, done, due, load, log, save   # noqa: E402
import a_share, global_mkt, news, ai_commentary, gen   # noqa: E402


def seed():
    """缓存为空（第一次运行或缓存过期）时，用仓库里的全球指数历史种子，避免首次运行依赖 Yahoo。"""
    if (DATA / "global.json").exists() or not (ROOT / "seed" / "global_idx.json.gz").exists():
        return
    s = json.loads(gzip.open(ROOT / "seed" / "global_idx.json.gz", "rt", encoding="utf-8").read())
    save("global.json", {"asof": s["asof"], "idx": s["idx"], "mk": s.get("mk", {}), "st": s.get("st", {})})
    log("seed: 已载入全球指数历史种子", s["asof"])


def ai_due():
    t = bj_now()
    if t.weekday() >= 5 or t.hour * 60 + t.minute < 15 * 60 + 30:     # 交易日 15:30 之后
        return False
    last = load("ai.json", {}).get("generated", "")
    return not last.startswith(t.strftime("%Y-%m-%d"))


TASKS = [  # 名称, 周期（小时）, 函数
    ("reports", 20, a_share.refresh_reports),
    ("funds", 20, a_share.refresh_funds),
    ("hist", 0.9, lambda: a_share.refresh_hist(full_monthly=due("hist_monthly", 24 * 7))),
    ("market", 0.9, a_share.refresh_market),
    ("lists", 24 * 7, global_mkt.refresh_lists),
    ("indices", 0.9, lambda: global_mkt.refresh_indices(full=due("indices_full", 24 * 7))),
    ("us_hk", 0.9, global_mkt.refresh_us_hk),
    ("jp_kr", 0.9, global_mkt.refresh_jp_kr),
    ("others", 6, global_mkt.refresh_others),
    ("news", 0.9, news.refresh_news),
]


def main():
    t0 = time.time(); seed(); only = set(filter(None, os.environ.get("ONLY", "").split(",")))
    results = {}
    for name, hours, fn in TASKS:
        if only and name not in only:
            continue
        if not only and not due(name, hours):
            results[name] = "skip"; continue
        try:
            ok = fn()
            if ok is not False:
                done(name)
                if name == "hist" and due("hist_monthly", 24 * 7): done("hist_monthly")
                if name == "indices" and due("indices_full", 24 * 7): done("indices_full")
            results[name] = "ok" if ok is not False else "kept-cache"
        except Exception as e:
            traceback.print_exc(); results[name] = f"failed: {type(e).__name__}"
    if (not only or "ai" in only) and (ai_due() or os.environ.get("FORCE") == "1" or "ai" in only):
        try:
            results["ai"] = "ok" if ai_commentary.refresh_ai() else "kept-previous"
        except Exception as e:
            traceback.print_exc(); results["ai"] = f"failed: {type(e).__name__}"
    gen.build()
    log("done in", round(time.time() - t0), "s |", json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()

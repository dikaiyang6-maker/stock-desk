"""在 daily_stock_analysis（单独的虚拟环境）里跑大盘复盘，用本站的全市场行情补上市场统计。

daily_stock_analysis 的涨跌家数、涨跌停家数和两市成交额来自东方财富 push2 接口，在云端机房经常取不到，
复盘里就会写“数据缺失”。这里先算好本站 data/a.json 里同一天的统计（和网页「市场宽度」同一口径），
把它的 get_market_stats 换成优先用这份数据，其余（指数、板块排行、模型调用、报告格式）都照它原来的逻辑。

用法（在 daily_stock_analysis 目录里，用它的 Python）：
    python <本仓库>/scripts/dsa_run.py <本仓库>/data/a.json --market-review --no-notify --force-run"""
import json
import logging
import os
import sys


def site_stats(path):
    a = json.load(open(path, encoding="utf-8"))
    I = {c: i for i, c in enumerate(a["cols"])}
    up = down = flat = lu = ld = 0
    amt = 0.0
    for r in a["rows"]:
        chg, price = r[I["chg"]], r[I["price"]]
        if chg is None or not price:
            flat += 1
            continue
        up += chg > 0
        down += chg < 0
        flat += chg == 0
        if r[I["lu"]] is not None and price >= r[I["lu"]] - 0.0005:
            lu += 1
        elif r[I["ld"]] is not None and price <= r[I["ld"]] + 0.0005:
            ld += 1
        amt += r[I["amt"]] or 0                                            # a.json 的成交额单位是亿元
    return {"up_count": up, "down_count": down, "flat_count": flat, "limit_up_count": lu, "limit_down_count": ld,
            "total_amount": round(amt, 2)}, a.get("asof")


def main():
    stats, asof = site_stats(sys.argv[1])
    sys.argv = ["main.py"] + sys.argv[2:]
    sys.path.insert(0, os.getcwd())
    from data_provider import base

    orig = base.DataFetcherManager.get_market_stats

    def get_market_stats(self, *, purpose="unspecified"):
        logging.getLogger("dsa_run").info("[MarketStats] 用 stock-desk 的全市场行情（%s）：%s", asof, stats)
        return dict(stats) if stats.get("up_count") or stats.get("down_count") else orig(self, purpose=purpose)

    base.DataFetcherManager.get_market_stats = get_market_stats
    import main as dsa_main
    return dsa_main.main()


if __name__ == "__main__":
    sys.exit(main())

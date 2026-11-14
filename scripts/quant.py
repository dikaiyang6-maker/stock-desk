"""市场页的「外部变量」和「AI 大盘复盘」，结果写进 data/quant.json。

  外部变量    OpenBB：美债 2 年 / 10 年 / 30 年收益率（美联储 H.15，federal_reserve）、10Y−2Y 利差，
              标普 500、纳指 100、费城半导体、VIX（雅虎财经，yfinance），各取最近 60 个交易日。
              美国收盘后更新，北京时间早上能取到，每 4 小时检查一次（build.py 的 quant_ext 任务）。
  AI 大盘复盘 开源项目 daily_stock_analysis（github.com/ZhuLinsen/daily_stock_analysis）的大盘复盘，模型用 DeepSeek。
              它锁定旧版 akshare（< 1.20），和本仓库冲突，所以工作流把它单独装在 dsa/.venv 里，每个交易日收盘后跑一次。
              这里只负责：判断今天要不要跑（due）、把它写出的 Markdown 拆成导语 / 盘面总览 / 其余部分（import）。

命令行：python scripts/quant.py ext | due | import 报告.md"""
import datetime as dt
import glob
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(__file__))
from common import bj_now, close_date, load, log, save   # noqa: E402

RATES = [("y2", "2 年期美债", "year_2"), ("y10", "10 年期美债", "year_10"), ("y30", "30 年期美债", "year_30")]
IDX = [("SPX", "标普 500", "^GSPC"), ("NDX", "纳指 100", "^NDX"), ("SOX", "费城半导体", "^SOX"), ("VIX", "VIX 波动率", "^VIX")]
DAYS = 60


def _series(df, col, k=1.0, nd=2):
    s = df[col].dropna()
    return [[str(d)[:10], round(float(v) * k, nd)] for d, v in s.items()][-DAYS:]


def refresh_ext():
    import warnings
    warnings.filterwarnings("ignore")
    from openbb import obb
    start = (bj_now().date() - dt.timedelta(days=130)).isoformat()
    q = load("quant.json", {})
    rates, idx, errs = [], [], []
    try:
        tr = obb.fixedincome.government.treasury_rates(provider="federal_reserve", start_date=start).to_df()
        for i, n, col in RATES:                                              # 美联储给的是小数（0.0488），换成百分比
            k = _series(tr, col, 100)
            if len(k) >= 20:
                rates.append({"id": i, "n": n, "k": k})
    except Exception as e:
        traceback.print_exc(); errs.append(f"美债：{type(e).__name__}")
    for i, n, sym in IDX:
        try:
            df = obb.index.price.historical(symbol=sym, provider="yfinance", start_date=start).to_df()
            k = _series(df, "close")
            if len(k) >= 20:
                idx.append({"id": i, "n": n, "k": k})
        except Exception as e:
            traceback.print_exc(); errs.append(f"{n}：{type(e).__name__}")
    if not rates and not idx:
        log("quant ext: OpenBB 没取到数据，沿用缓存", errs); return False
    by = {r["id"]: dict(r["k"]) for r in rates}
    spread = None
    if "y10" in by and "y2" in by:
        days = sorted(set(by["y10"]) & set(by["y2"]))[-DAYS:]
        spread = {"id": "s102", "n": "10Y−2Y 利差", "k": [[d, round(by["y10"][d] - by["y2"][d], 2)] for d in days]}
    old_ids = {x["id"] for x in q.get("rates", []) + q.get("idx", [])}
    new_ids = {x["id"] for x in rates + idx}
    if old_ids - new_ids:                                                    # 某一项这次没取到：沿用上一次的那一项
        rates += [x for x in q.get("rates", []) if x["id"] not in new_ids]
        idx += [x for x in q.get("idx", []) if x["id"] not in new_ids]
        idx.sort(key=lambda x: [i for i, *_ in IDX].index(x["id"]) if x["id"] in [i for i, *_ in IDX] else 9)
    q.update({"extT": bj_now().strftime("%Y-%m-%d %H:%M"), "rates": rates, "spread": spread or q.get("spread"), "idx": idx, "extErr": errs})
    save("quant.json", q)
    log("quant ext:", {x["id"]: x["k"][-1] for x in rates + idx}, "| errors", errs)
    return True


def review_due(count=False):
    """有新的收盘数据（交易日收盘后，或第二天开盘前补跑）、这一天的复盘还没生成时为 True。
    同一天最多试 3 次（count=True 时记一次），免得它的数据源出问题时每小时都白跑几分钟。"""
    d = close_date()
    if not d or not os.environ.get("DEEPSEEK_API_KEY"):
        return False
    q = load("quant.json", {})
    if os.environ.get("FORCE_REVIEW") == "1":                               # 手动运行工作流时可以要求重新生成
        return True
    if (q.get("review") or {}).get("date") == d:
        return False
    tries = q.get("reviewTry") if (q.get("reviewTry") or {}).get("date") == d else {"date": d, "n": 0}
    if tries["n"] >= 3:
        return False
    if count:
        tries["n"] += 1
        q["reviewTry"] = tries
        save("quant.json", q)
    return True


def _section(md, start_pat, end_pat=None):
    m = re.search(start_pat, md, re.M)
    if not m:
        return ""
    rest = md[m.start():]
    if end_pat:
        e = re.search(end_pat, rest[1:], re.M)
        if e:
            rest = rest[:e.start() + 1]
    return rest.strip()


def import_review(path):
    """把 daily_stock_analysis 的 market_review_YYYYMMDD.md 拆成导语、盘面总览和其余部分。"""
    md = open(path, encoding="utf-8").read()
    md = re.sub(r"^\[dsa-[^\]]*\]: #.*$", "", md, flags=re.M)                  # 它给程序看的标记行
    md = md.replace("🔴 ", "").replace("🟢 ", "").replace("⚪ ", "")           # 表格里的颜色符号（网页按红涨绿跌自己上色）
    lead = ""
    m = re.search(r"^>\s*(.+)$", md, re.M)
    if m:
        lead = m.group(1).strip()
    head = _section(md, r"^###\s*一、", r"^###\s*二、")
    body = _section(md, r"^###\s*二、")
    if not head:                                                            # 格式变了：整篇放进正文
        head, body = "", re.sub(r"^#{1,2} .*$", "", md, flags=re.M).strip()
    body = re.sub(r"\n*建议仅供参考，不构成投资建议。?\s*$", "", body).strip()
    if len(head) + len(body) < 200:
        raise SystemExit(f"复盘内容太短，可能生成失败：{path}")
    d = close_date() or (load("a.json", {}).get("asof") or "")[:10]
    q = load("quant.json", {})
    q["review"] = {"date": d, "built": bj_now().strftime("%Y-%m-%d %H:%M"), "lead": lead, "head": head, "body": body,
                   "model": os.environ.get("DSA_MODEL_LABEL", "DeepSeek"), "src": "daily_stock_analysis"}
    save("quant.json", q)
    log("quant review:", d, "| lead", lead[:60], "| head", len(head), "body", len(body))
    return True


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "ext":
        refresh_ext()
    elif cmd == "due":
        print("true" if review_due(count=True) else "false")
    elif cmd == "import":
        files = sorted(glob.glob(sys.argv[2])) if len(sys.argv) > 2 else []
        if not files:
            raise SystemExit("没有找到复盘报告文件")
        import_review(files[-1])
    else:
        raise SystemExit(__doc__)

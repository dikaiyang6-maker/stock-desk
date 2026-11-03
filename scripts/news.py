"""财经快讯标题（东方财富 7×24、新浪财经滚动），供 DeepSeek 研判引用。"""
import datetime as dt

from common import BJ, UA, jget, log, save


def refresh_news():
    items = []
    d = jget("https://np-weblist.eastmoney.com/comm/web/getFastNewsList", {"client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": "", "pageSize": 80, "req_trace": "1"},
             headers={"User-Agent": UA})
    for x in ((d or {}).get("data") or {}).get("fastNewsList") or []:
        items.append({"t": x.get("showTime", "")[:16], "src": "东方财富快讯", "title": x.get("title") or (x.get("summary") or "")[:60], "sum": (x.get("summary") or "")[:160]})
    d = jget("https://feed.mix.sina.com.cn/api/roll/get", {"pageid": 153, "lid": 2516, "k": "", "num": 50, "page": 1})
    for x in ((d or {}).get("result") or {}).get("data") or []:
        try:
            t = dt.datetime.fromtimestamp(int(x["ctime"]), BJ).strftime("%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            t = ""
        items.append({"t": t, "src": "新浪财经 · " + (x.get("media_name") or ""), "title": x.get("title", ""), "sum": (x.get("intro") or "")[:160], "url": x.get("url")})
    items = [i for i in items if i["title"]]
    items.sort(key=lambda i: i["t"], reverse=True)
    if not items:
        log("news: 没拿到快讯，沿用缓存"); return False
    save("news.json", {"items": items[:120]})
    log("news", len(items))
    return True

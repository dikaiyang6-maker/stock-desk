# A股与全球市场选股台

沪深京 A 股全市场（5500+ 只）和 29 个世界指数、1275 只海外成分股的研究型网页。页面由 GitHub Actions 自动更新并发布到 GitHub Pages；在浏览器里打开时会自动切换为腾讯实时行情（全球指数每 5 秒、打开的个股每 5 秒刷新）。

**仅供研究参考，不构成投资建议。市场有风险，决策需独立判断。**

## 更新频率

| 内容 | 频率 | 数据源 |
|---|---|---|
| A 股全市场行情、涨跌停价、市盈率、市值 | 周一至周五每小时 | 腾讯行情 |
| A 股指数（上市以来）与题材股 K 线、五日分时 | 每小时（增量） | 腾讯行情 |
| 世界指数日K、五日分钟线、报价 | 每小时（增量），完整历史每周 | Yahoo Finance、腾讯行情 |
| 美股、港股成分股 | 每小时 | 腾讯行情（中文简称） |
| 日经225、KOSPI200 成分股 | 每小时 | Naver |
| 富时100、DAX、CAC40、斯托克50、Nifty50 成分股 | 每 6 小时 | Yahoo Finance |
| 财报（自动选择最新披露期）、两融、北向成交 | 每天 | 东方财富数据中心 |
| 六维研判、今日市场解读、精选个股点评 | 每个交易日 A 股收盘后（北京 15:30 之后的第一轮） | DeepSeek，依据本页数据和东方财富、新浪财经快讯标题 |
| 成分股名单 | 每周 | 维基百科、Nasdaq、FinanceDataReader |

GitHub 的定时任务是“尽力而为”，高峰期可能晚几分钟到几十分钟，偶尔会跳过一轮。

## 第一次配置（约 3 分钟）

1. **开启 Pages**：Settings → Pages → Build and deployment → Source 选 **GitHub Actions**。
2. **添加 DeepSeek Key（可选）**：Settings → Secrets and variables → Actions → New repository secret，Name 填 `DEEPSEEK_API_KEY`，Value 填你的 key。不填也能运行，研判部分会显示原有的人工文字。
3. **手动跑第一次**：Actions → 更新数据并发布网页 → Run workflow（`force` 填 `1` 表示全部重新抓取）。第一次约 8 分钟，之后每小时一轮约 1～2 分钟。
4. 打开 `https://<你的用户名>.github.io/stock-desk/`，在手机上可用 Safari / Chrome 的「添加到主屏幕」。

## 本地运行

```bash
pip install -r requirements.txt
export DEEPSEEK_API_KEY=...        # 可选
python scripts/build.py            # 生成 site/index.html
FORCE=1 python scripts/build.py    # 全部重新抓取
ONLY=market,news python scripts/build.py   # 只跑指定任务
```

## 目录

- `scripts/build.py`：排程入口，按各任务周期决定刷新什么，任何一项失败都沿用缓存
- `scripts/a_share.py`、`global_mkt.py`、`lists.py`、`news.py`、`ai_commentary.py`、`gen.py`：各数据模块与网页生成
- `template.html`：网页模板（图表引擎、指标、分时、全球市场、AI 助手）
- `seed/`：全球指数历史种子，缓存为空且 Yahoo 访问不到时使用
- `data/`、`site/`：运行时生成，不进 git（数据通过 Actions 缓存在两次运行之间保留）

## 已知限制

- 印度、澳洲、新加坡、瑞士指数的实时报价和部分海外指数的实时分时依赖东方财富，在部分网络下访问不到时用快照加 5 秒报价。
- 日本、欧洲、印度、韩国个股没有浏览器可直接访问的免费实时接口，网页里显示最近一次更新的快照。
- 俄罗斯、泰国、越南指数没有免费历史数据，只有报价。
- AI 研判由模型自动生成，只引用页面提供的数据和快讯标题，可能有误；请以原始公告和数据为准。

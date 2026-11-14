---
name: a-share-gp
description: 用 gp 命令查 A 股数据和做回测——实时行情和五档（easyquotation）、日K（akshare 腾讯/新浪，或 mootdx 通达信）、涨停/炸板/跌停池和龙虎榜（akshare 东方财富）、问财自然语言选股（pywencai）、stock-api（Node）行情和搜索、操盘线 B/S 等策略回测（akquant），以及直接调用任意 akshare 函数。Use when the user asks for A股行情、K线、五档、涨停池、龙虎榜、大宗交易、解禁、机构调研、问财选股、回测，or mentions gp, akshare, easyquotation, mootdx, pywencai, stock-api or akquant.
---

# gp：A 股数据命令行

`gp` 把六个开源库包成一组中文命令：akshare、easyquotation、mootdx、pywencai、stock-api、akquant。
源码在 stock-desk 仓库的 `tools/gp.py`（https://github.com/dikaiyang6-maker/stock-desk ）。

## 0. 先确认装好了

```bash
gp doctor        # 六个库的版本、Node.js、各数据源能否连通
```

找不到 `gp` 时安装（Python 3.11+；有 Node.js 18+ 时顺带装 stock-api）：

```bash
curl -fsSL https://raw.githubusercontent.com/dikaiyang6-maker/stock-desk/main/tools/install.sh | bash
```

云端会话的环境设置脚本已经把这些库装进系统 Python，并把 `gp` 放在 `/usr/local/bin`；如果没有，就运行上面这条。

## 1. 常用命令（加 `--json` 得到 JSON，便于解析）

| 要做什么 | 命令 |
|---|---|
| 实时行情（新浪，失败换腾讯） | `gp q 600519 300750 sh000001`，加 `--book` 看五档（手 / 金额） |
| 日K（默认前复权，akshare 腾讯→新浪） | `gp k 600519 -n 60`；`--fq none` 不复权；`--src tdx` 走通达信 |
| 涨停池 / 炸板 / 跌停 / 强势股 | `gp zt`、`gp zt 20261113 --kind zb`（zb 炸板、dt 跌停、qs 强势） |
| 龙虎榜（按净买额排序） | `gp lhb`、`gp lhb 20261113` |
| 问财选股 | `gp wc "今日涨停，非ST，连板天数从大到小"` |
| stock-api 行情 / K线 / 搜索 | `gp js 600519 510300`、`gp js 510300 --k 20`、`gp js 宁德` |
| 回测（akquant） | `gp bt 600519`（操盘线 B/S，默认近两年、100 万）；`--strategy ma` 均线 5/20；`--strategy hold` 买入持有；`--trades` 逐笔；`--start 20240101 --cash 500000` |
| 任意 akshare 函数 | `gp ak --search 解禁` 找函数；`gp ak stock_dzjy_mrmx symbol=A股 start_date=20261113 end_date=20261113` |
| 通达信（mootdx） | `gp tdx 600519`、`gp tdx 600519 --ticks`（分笔）、`--minute`（分时） |

代码写 `600519`、`sh600519`、`600519.SH` 都行，也可以写名称或拼音首字母（`茅台`、`gzmt`）。指数要带前缀：`sh000001` 上证指数、`sz399001` 深证成指、`sz399006` 创业板指（不带前缀的 `000001` 是平安银行）。

回测口径：信号在当天收盘后产生，下一根 K 线开盘成交（akquant 的 next-open 撮合），A 股 T+1、低佣金费率、100 股整手（科创板最少 200 股）。操盘线公式和网站“操盘”页一致：操盘线 = EMA(收盘, 13)；B = 收盘上穿操盘线且 MACD 的 DIF > DEA；S = 收盘下破操盘线且 DIF < DEA。

## 2. 在 Python 里直接用

```python
import akshare as ak, easyquotation
ak.stock_zh_a_hist_tx(symbol="sh600519", start_date="20250101", end_date="20261113", adjust="qfq")   # 日K，volume 单位是股，amount 是元
ak.stock_zh_a_daily_sina(symbol="sh600519", start_date="20250101", end_date="20261113", adjust="qfq")
ak.stock_zt_pool_em(date="20261113"); ak.stock_lhb_detail_em(start_date="20261113", end_date="20261113")
easyquotation.use("sina").real(["sh600519", "sh000001"], prefix=True)   # 新浪：turnover 是成交量（股，指数是手），volume 是成交额（元）
```

akshare 改名很频繁：1.20 以后 `stock_zh_a_daily` → `stock_zh_a_daily_sina`，`stock_zh_a_hist` → `stock_zh_a_hist_em`。找不到函数时用 `gp ak --search 关键词`。

## 3. 限制，照实告诉用户

- **mootdx（通达信）**：要直连通达信服务器 TCP 7709。Claude 云端会话不允许直连 TCP，`gp tdx` 会报连不上——不要想办法绕过，告诉用户在自己电脑上运行。
- **pywencai（问财）**：需要用户自己登录 www.iwencai.com 后的 cookie，放在环境变量 `WENCAI_COOKIE`。不要让用户把 cookie 贴到聊天里，也不要替用户登录；`gp wc` 不带 cookie 时会打印设置方法。
- **东方财富 push2 行情接口**偶尔对云端 IP 限流（502 / 连接被重置）。日K优先用腾讯 / 新浪（`gp k` 已经这样做），涨停池、龙虎榜走的是东方财富数据中心，一般不受影响。
- 新浪接口偶尔对云端 IP 重置连接，`gp q` 会自动换腾讯，`gp k` 会自动换新浪 / 腾讯。

## 4. 汇报

用中文；股价、指数保留两位小数（ETF、可转债三位）；成交额写成“x.xx 亿 / x.xx 万”。只做公开数据的整理和分析，不替用户下买卖结论，结尾加：以上内容仅为基于公开信息的分析参考，不构成投资建议，市场有风险，决策需独立判断。

# RITCx Algorithmic ETF Arbitrage Trading Bot

## 1. 项目概述

本项目是 Rotman International Trading Competition (RITC)x **Algorithmic ETF Arbitrage Case** 的 Python 自动化交易系统。证券与外汇交易全部通过 RIT DMA REST API 执行；ETF Converter（申购/赎回）无法走 API，由终端看板提示操作员在 RIT 客户端手动点击。

* **单轮时长**：300 tick（约 5 分钟），模拟 1 个月交易。
* **执行分工**：
  * 程序自动：Tender Offer 评估/接单、RITC 拆单平仓、BULL/BEAR 篮子锁仓、统计套利、USD 敞口对冲、仓位风控。
  * 人工仅做：看板出现 `[ACTION]` / `【新增 #N】` 时，在 `Assets -> Converters` 点蓝色 **Use**。无提示时不要点 Converter（每次固定消耗 $1,500 USD）。

启动入口：`python main.py`。依赖仅为 Python 3.8+ 与 `requests`。连接参数、阈值全部集中在 `config.py`。

---

## 2. 市场参数与硬性规则

基础货币为 **CAD**，USD 采用直接标价法（1 USD 兑换多少 CAD）。

| Ticker | 类型 | 计价货币 | 初始价 | Market 手续费 | Limit 返佣 | 单笔最大下单量 | 仓位权重 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `CAD` | Currency | CAD | n/a | n/a | n/a | 2,500,000 | 0 |
| `USD` | Currency | CAD | n/a | n/a | n/a | 2,500,000 | 0 |
| `BULL` | Stock | CAD | $10 | $0.02/股 | $0.01/股 | 10,000 | 1 |
| `BEAR` | Stock | CAD | $15 | $0.02/股 | $0.01/股 | 10,000 | 1 |
| `RITC` | ETF | USD | $25 | $0.02/股 | $0.01/股 | 10,000 | 2 |

### 2.1 定价、限额与 Converter

* **均衡定价**：`P_RITC,USD × USD/CAD = P_BULL,CAD + P_BEAR,CAD`
* **仓位限制（超限拒单）**：
  * `Gross = |BULL| + |BEAR| + 2 × |RITC|`，硬上限 `MAX_GROSS_LIMIT = 300,000`
  * `Net = BULL + BEAR + 2 × RITC`，多头 `200,000` / 空头 `-200,000`
* **ETF Converters（仅人工）**：
  * Creation：10,000 BULL + 10,000 BEAR + $1,500 USD → 10,000 RITC
  * Redemption：10,000 RITC + $1,500 USD → 10,000 BULL + 10,000 BEAR
  * 转换约 2 tick，固定成本 **$0.15 USD/股**（$1,500 / 10,000）
* **期末清算**：非零仓位按最后成交价或理论价平仓。统计套利在最后 15 tick 停止主动平仓，持有至结算。

---

## 3. 如何运行（操作指导）

### 3.1 赛前检查

1. 在 `config.py` 填写 `API_BASE_URL`、`USERNAME`、`PASSWORD`（DMA Basic 认证）。
2. 安装依赖：`pip install requests`（或将 `AUTO_INSTALL_DEPS = True`）。
3. 操作员打开 RIT 客户端，预先停在 `Assets -> Converters`，准备点蓝色 Use。
4. 确认终端能发声（macOS 用 `Glass.aiff`；其他系统响铃 `\a`）。`PLAY_SOUND_ALERT = False` 可静音。

### 3.2 启动与退出

```text
python main.py
```

* Case 未开始（`STOPPED` / 连不上）时，主循环会每秒轮询并在同一行显示等待状态，不会乱下单。
* 进入 `ACTIVE` 后按 `LOOP_INTERVAL`（当前 **0.30s**）跑一轮；频率故意偏低，减轻 HTTP 429。
* **Ctrl+C / SIGTERM**：停止循环 → 撤销全部 OPEN 挂单 → 关闭 HTTP Session。

详细流水写入 `ritcx_arb.log`；终端只刷看板。

### 3.3 终端看板怎么看

每轮清屏刷新，重点字段：

* **Gross / Net 进度条**：接近 100% 时统计套利会被软上限拦住；Tender 仍可能接单，此时优先听 Converter 警报释放额度。
* **Positions**：BULL / BEAR / RITC / USD。
* **Arb**：可执行价差（CAD）、手续费 hurdle、最近动作（`WAIT` / `OPEN_*` / `PROFIT_UNWIND` / `SOFT_CAP_HOLD` / `ENDGAME_HOLD` / `THIN_BOOK`）。
* **FX**：净 USD 敞口、USD 现金、底仓 `$4,000`、最近对冲动作。
* **Tenders**：当前 API 返回的活跃要约列表（未接单也会显示）。
* **Converter 四态**：
  * 红底 `【新增 #N】`：立刻按说明点 Use（可建议最多 2 次）。
  * 黄字 `锁定挂起`：同一任务粘性驻留约 15 秒，等待仓位变化，不要当新任务连点。
  * 绿字 `已兑现`：仓位已抵消，冷却 2 tick，无需重复点击。
  * 灰字 IDLE：明确写明未触发原因（手数不够、USD 不足、冷却中等）。

### 3.4 操作员 Converter 动作

看板会写清填什么、点几次：

* **ETF-Redemption**：填 RITC=10000、USD=1500，点蓝色 Use（程序建议次数，单次警报最多 2 手）。
* **ETF-Creation**：填 BULL=10000、BEAR=10000、USD=1500，同样点 Use。
* 账户可转手数大于建议次数时，看板会提示「分批执行」；兑现后再看是否弹出 `【新增 #N+1】`。
* USD 现金不足 $1,500 时程序**不会响铃**，会提示等待 FX 模块补足 USD。不要在警报外手动 Convert。

### 3.5 轮次间常用调参（`config.py`）

| 目的 | 参数 | 当前默认 |
| :--- | :--- | :--- |
| 减轻 429 | `LOOP_INTERVAL` / `REQUEST_TIMEOUT` / `MAX_429_RETRIES` | 0.30s / 0.5s / 5 |
| 套利更保守 | `ARB_GROSS_SOFT_CAP_RATIO` / `ARB_OPEN_THRESHOLD_CAD` | 0.35 / 0.12 CAD |
| Tender 更挑单 | `TENDER_MIN_UNIT_EDGE_USD` | 0.08 USD |
| 平仓更快 | `TENDER_SLICE_QTY` / `TENDER_ACCEL_AFTER_SEC` / `TENDER_DRIFT_STOP_USD` | 4500 / 10s / $0.06 |
| 大单双轨锁仓 | `TENDER_BASKET_TRIGGER_REM_QTY` 等 | 剩余 > 30,000 才启动 |
| Converter 更敏感 | `CONVERTER_MIN_BATCH_USES` / `CONVERTER_GROSS_TRIGGER` | 1 手 / 65% Gross |
| USD 底仓 / 少换汇 | `FX_USD_CASH_FLOOR` / `FX_NET_EXPOSURE_THRESHOLD` | 4,000 / 80,000 |

---

## 4. 主循环与优先级

`main.py` 每轮顺序固定（Tender 优先占用额度）：

1. **Tender**：轮询要约 → 估值接单 → 入账确认 → 双轨平仓。
2. **统计套利**：walk_book 价差；软上限与尾盘冻结。
3. **FX 对冲**：按 `FX_HEDGE_INTERVAL`（2.5s）节流；卖 USD 时强制留底仓。
4. **Converter 检测**：新警报才响铃并加 2-tick 冷却。
5. **刷新看板**。

---

## 5. 模块逻辑（与代码一致）

### 5.1 `api_client.py` — DMA REST

* 复用 `requests.Session()`，Basic Auth，单次超时 `REQUEST_TIMEOUT`。
* HTTP 429：读 `Retry-After` 或 body `wait`，最多重试 `MAX_429_RETRIES` 次。
* Tender 接单：`POST /v1/tenders/{id}?price=...`（price 必须为正有限数，保留 4 位小数）。
* 撤单 404 视为已成交，DEBUG 忽略。LIMIT 下单把 `price` 放在 query params。

### 5.2 `orderbook.py` — 盘口与风控

* `walk_book(ticker, side, qty)`：逐档吃单 VWAP / 滑点；深度不够则 `sufficient_depth=False`。套利与 Converter 滑点判断都走这里，**不用买一卖一估大单**。
* 同一 ticker 订单簿缓存约 80ms，避免一轮循环内重复拉 book。
* `check_limits`：模拟成交后 Gross/Net，供套利、Tender 拆单、篮子锁仓使用。

### 5.3 `strategy_tender.py` — Tender 决策与双轨平仓

**接单（不再用两条 walk_book 退出路径比价）**：

* 非法/非正 `price`：跳过，不接单。
* `unit_edge_usd`：BUY 要约 = `RITC mid − tender_px`；SELL 要约 = `tender_px − RITC mid`。
* 模拟接单后 Gross/Net 不超硬限额，且 `unit_edge_usd >= TENDER_MIN_UNIT_EDGE_USD (0.08)` 才 `ACCEPT`。未达阈值只记日志，不调用 decline。

**入账状态机**：`settled=False` 直到 RITC 仓位相对接单前跳变超过要约数量的 50%，或等待 `SETTLEMENT_WAIT_SEC = 2.5s`。避免模拟器入账延迟导致任务提前 DONE。

**有效净敞口**：`remaining_qty` = 当前同向 RITC 仓位 − 本任务已建立且仍持有的反向 BULL/BEAR 篮子。人工点 Converter 后两边同时抵消，也会把剩余量归零。

**轨道 1 — RITC 被动拆单**（入账前后都可挂，加速吃单仅在 settled 后）：

* 每次约 **4,500** 股 LIMIT 挂 Best Ask（平多）或 Best Bid（平空），同时最多 **2** 笔，赚 $0.01 返佣。
* REQUOTE：挂单存活 ≥ **1.5s**，或存活 ≥ **1.0s** 且价格偏离盘口超过 1 tick（$0.01）。
* 加速转市价（每次最多 5,000 股）：入账后价格向不利方向漂移 > **$0.06**，或接单超过 **10s** 仍未平完，或剩余 tick ≤ **25**。

**轨道 2 — BULL/BEAR 同步篮子**（仅 settled 且剩余净敞口 > **30,000**）：

* 每批市价 5,000 BULL + 5,000 BEAR（两批满一手 Converter），累计上限 30,000，间隔 ≥ 0.6s。
* 合成隐含 USD 价相对 `tender_px` 至少再赚 **$0.05** 才下篮子。
* 目的：快速压低 Net，并把仓位摆成 Converter 可点的形态，而不是一次砸 10,000 股 RITC。

### 5.4 `strategy_arb.py` — 统计套利

* 用 `ARB_ORDER_QTY = 2,000` 的 walk_book VWAP 算 CAD 可执行价差：
  * 卖 ETF：`edge_short_etf = RITC_bid_vwap × FX_bid − (BULL_ask_vwap + BEAR_ask_vwap)`
  * 买 ETF：`edge_long_etf = (BULL_bid_vwap + BEAR_bid_vwap) − RITC_ask_vwap × FX_ask`
* 手续费 hurdle：`0.02 × FX + 0.04` CAD。开仓阈值 = `max(ARB_OPEN_THRESHOLD_CAD=0.12, hurdle+0.03)`。
* 任意一腿深度不足 → `THIN_BOOK`，不开仓。
* **开仓**：三腿同时市价；Gross 软上限 **35%**（约 105,000），给 Tender 留额度。
* **平仓**：必须反向价差 ≥ `hurdle + ARB_CLOSE_PROFIT_CAD(0.02)`，且距上次成交 ≥ **2s**。不满足则持有等收敛或期末理论价。
* 最后 **15 tick**：`ENDGAME_HOLD`，不再主动开平。

### 5.5 `fx_hedger.py` — USD 敞口

* `Net USD = USD 现金 + Position_RITC × RITC mid`（无 mid 时按 25）。
* 每 **2.5s** 最多对冲一次；`|净敞口| > 80,000` 才动手，最小手数 2,000。
* **卖 USD 必须留下 `FX_USD_CASH_FLOOR = 4,000`**，保证 Converter 至少够两次 $1,500。
* 模块内有尾盘 15 tick 冻结，但当前 `main.py` 调用为 `fx.step(snapshot)` **未传入 tick**，因此实盘尾盘 FX 冻结未启用；套利尾盘冻结是生效的。

### 5.6 `converter_monitor.py` — 人机协同

持仓形态：

* Redemption：`RITC ≥ 10,000` 且 `BULL ≤ -10,000` 且 `BEAR ≤ -10,000`
* Creation：`RITC ≤ -10,000` 且 `BULL ≥ 10,000` 且 `BEAR ≥ 10,000`

当前配置下 `CONVERTER_MIN_BATCH_USES = 1`，满一手即可视为高价值触发；另可因 Gross ≥ **65%** 或直接平 RITC 滑点 > **$0.15/股**（或深度不足）触发。建议次数 = `min(可转手数, USD 能付手数, 2)`。

兑现判定：相对警报基准仓位，三腿同向变化均 ≥ 8,000 股。之后 2-tick 冷却；若仍有可转手数会立刻生成下一编号任务。

---

## 6. 代码目录

```text
ritcx_etf_arb/
├── config.py             # API、费率、软硬限额、Tender/套利/FX/Converter 全部阈值
├── api_client.py         # DMA REST（Session、Basic Auth、429、Tender price query）
├── orderbook.py          # walk_book VWAP、80ms book 缓存、Gross/Net 校验
├── strategy_arb.py       # 统计套利（walk_book 价差、软上限 35%、尾盘 15 tick）
├── strategy_tender.py    # Tender 估值、入账确认、RITC 拆单 + 篮子双轨平仓
├── fx_hedger.py          # 净 USD 对冲（现金底仓、80k 阈值、2.5s 节流）
├── converter_monitor.py  # 看板、编号任务、15s 粘性、兑现确认、声音警报
├── main.py               # 主循环、STOPPED 等待、Ctrl+C 撤单退出
└── ritcx_arb.log         # 运行流水（启动后生成）
```

---

## 7. 实现约定

1. Python 3.8+，第三方依赖仅 `requests`。
2. 所有 REST 走同一 Session，带超时与 429 退避，避免单次抖动打死主循环。
3. 比赛轮次只改 `config.py`，不要在策略文件里写死阈值。
4. 终端保持看板；决策、接单、拆单、对冲、Converter 任务写入 `ritcx_arb.log` 复盘。

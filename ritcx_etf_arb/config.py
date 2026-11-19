"""
全局配置与依赖检查模块
所有可调阈值与比赛参数集中在此，方便轮次间快速调整。
"""

import importlib
import subprocess
import sys


def ensure_dependencies(auto_install: bool = False) -> None:
    """检查运行环境与核心依赖（仅需 Python 3.8+ 与 requests）。"""
    if sys.version_info < (3, 8):
        raise RuntimeError(f"需要 Python 3.8 或更高版本，当前版本: {sys.version}")

    try:
        importlib.import_module("requests")
    except ImportError:
        if auto_install:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
        else:
            raise ImportError(
                "缺少依赖库 'requests'。请执行: pip install requests "
                "或将 config.AUTO_INSTALL_DEPS 设为 True。"
            )


AUTO_INSTALL_DEPS = False

# ==================== RIT DMA REST API 连接配置 ====================
API_BASE_URL = "http://flserver.rotman.utoronto.ca:16500/v1"
USERNAME = "btby"
PASSWORD = "balance"
REQUEST_TIMEOUT = 0.5         # 单次请求超时时间（秒），防止网络抖动阻塞主循环
MAX_429_RETRIES = 5           # 触发 429 限流时的最大重试次数
DEFAULT_429_WAIT = 0.5        # 响应头未提供 Retry-After 时的默认等待秒数

# ==================== 主循环与日志配置 ====================
LOOP_INTERVAL = 0.30          # 降低轮询频率，减少 429 限流
STOPPED_POLL_INTERVAL = 1.0
LOG_FILE = "ritcx_arb.log"
FX_HEDGE_INTERVAL = 2.5       # 放缓 FX 对冲频率，减少双向打脸

# ==================== 标的代码与市场参数 ====================
CAD = "CAD"
USD = "USD"
BULL = "BULL"
BEAR = "BEAR"
RITC = "RITC"
ALL_TICKERS = (BULL, BEAR, RITC, USD, CAD)
EQUITY_TICKERS = (BULL, BEAR, RITC)

POSITION_WEIGHTS = {
    BULL: 1,
    BEAR: 1,
    RITC: 2,
    USD: 0,
    CAD: 0,
}

MAX_ORDER_SIZE = {
    BULL: 10_000,
    BEAR: 10_000,
    RITC: 10_000,
    USD: 2_500_000,
    CAD: 2_500_000,
}

MARKET_FEE_PER_SHARE = 0.02
LIMIT_REBATE_PER_SHARE = 0.01

# ==================== 仓位风控硬/软上限 ====================
MAX_GROSS_LIMIT = 300_000
MAX_LONG_NET = 200_000
MAX_SHORT_NET = -200_000

# 统计套利 Gross 软上限比例（35%，为 Tender Offer 预留充足空间）
ARB_GROSS_SOFT_CAP_RATIO = 0.35

# ==================== 统计套利 (Statistical Arbitrage) 参数 ====================
ARB_OPEN_THRESHOLD_CAD = 0.12
ARB_CLOSE_PROFIT_CAD = 0.02
ARB_ORDER_QTY = 2_000
ARB_MIN_HOLD_SECONDS = 2.0
ARB_ENDGAME_FREEZE_TICKS = 15
TOTAL_CASE_TICKS = 300

# ==================== FX 对冲参数 ====================
FX_USD_CASH_FLOOR = 4_000.0       # 强制保留 USD 现金底仓（默认 $4,000）
FX_CONVERTER_USD_REQ = 1_500.0    # 单次 Converter 所需 USD
FX_NET_EXPOSURE_THRESHOLD = 80_000.0  # 放宽净 USD 敞口阈值，避免频繁换汇损耗
FX_HEDGE_MIN_QTY = 2_000

# ==================== Tender Offer 参数 ====================
TENDER_MIN_UNIT_EDGE_USD = 0.08        # 提高接单门槛至 $0.08 USD，过滤低毛利鸡肋单
TENDER_SLICE_QTY = 4_500               # RITC 被动限价拆单数量（4,000~5,000 股，赚 $0.01 返佣）
TENDER_MARKET_SLICE_QTY = 5_000        # 触发止损/超时加速时的市价单数量（5,000 股）
TENDER_MAX_OPEN_ORDERS = 2             # 盘口同时维护的最大活跃限价单数
TENDER_REQUOTE_SEC = 1.5               # 挂单超时重挂时间（1.5s）
TENDER_REQUOTE_MIN_LIVE_SEC = 1.0      # 挂单最短存活时间（减少 1 tick 抖动导致的无效撤单）
TENDER_ACCEL_AFTER_SEC = 10.0          # 接单超过 10 秒仍未平完则转市价单快速收尾
TENDER_DRIFT_STOP_USD = 0.06           # 不利方向价格跑偏止损阈值（$0.06 USD）
TENDER_TICK_SIZE_USD = 0.01            # 最小变动价位（1 tick）
TENDER_URGENT_REMAINING_TICKS = 25     # 尾盘最后 25 ticks 强制加速

# 双轨篮子同步对冲参数（RITC + BULL/BEAR）
TENDER_BASKET_TRIGGER_REM_QTY = 30_000 # 剩余未平仓 > 30,000 股时启动同步篮子锁仓
TENDER_BASKET_HEDGE_BATCH_QTY = 5_000  # 每次同步对冲 5,000 股 BULL + 5,000 股 BEAR（2 批即满 1 手 Converter）
TENDER_BASKET_HEDGE_MAX_QTY = 30_000   # 单笔 Tender 累计篮子对冲上限
TENDER_BASKET_MIN_EDGE_USD = 0.05      # 篮子合成隐含 USD 价格相对 tender_px 需满足的最低毛利
TENDER_BASKET_INTERVAL_SEC = 0.6       # 篮子对冲批次间隔（秒）

# ==================== Converter 监控与警报参数 ====================
CONVERTER_LOT_SIZE = 10_000
CONVERTER_MIN_BATCH_USES = 1           # 对冲满 10,000 股且 Gross >= 65% 时即触发报警
CONVERTER_GROSS_TRIGGER = 0.65         # Gross 占用率 >= 65% 时响铃提示释放 40,000 Gross 额度
CONVERTER_SLIPPAGE_TRIGGER_USD = 0.15
CONVERTER_COOLDOWN_TICKS = 2
PLAY_SOUND_ALERT = True
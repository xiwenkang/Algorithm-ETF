"""
RITCx Algorithmic ETF Arbitrage 主程序入口
- 检查运行依赖与初始化日志系统
- 支持比赛处于 STOPPED 状态时安全等待开始
- 支持 Ctrl+C 优雅退出并清理挂单与 HTTP Session
"""

import logging
import os
import signal
import sys
import time
from typing import Optional

import config

# 在导入业务模块前优先执行依赖检查
config.ensure_dependencies(auto_install=config.AUTO_INSTALL_DEPS)

from api_client import RitApiClient
from converter_monitor import ConverterMonitor
from fx_hedger import FxHedger
from orderbook import OrderBookEngine
from strategy_arb import StatArbStrategy
from strategy_tender import TenderStrategy

shutdown_requested = False


def _handle_shutdown_signal(signum: int, _frame: Optional[object]) -> None:
    """捕获中断信号，标记优雅退出。"""
    global shutdown_requested
    logging.getLogger("ritcx.main").info("收到退出信号 (%s)，准备优雅停止...", signum)
    shutdown_requested = True


def setup_logging() -> None:
    """配置详细流水写入本地 .log 文件，终端保持清爽看板。"""
    log_dir = os.path.dirname(os.path.abspath(config.LOG_FILE))
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    root_logger = logging.getLogger("ritcx")
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    file_handler = logging.FileHandler(config.LOG_FILE, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    root_logger.propagate = False


def wait_for_active_case(api: RitApiClient) -> Optional[int]:
    """当 Case 处于 STOPPED 或未启动状态时安全等待，返回首个 ACTIVE tick。"""
    logger = logging.getLogger("ritcx.main")
    while not shutdown_requested:
        tick, status = api.get_tick_status()
        if status == "ACTIVE" and tick is not None:
            logger.info("检测到比赛进入 ACTIVE 状态，当前 Tick=%d", tick)
            return tick

        sys.stdout.write(
            f"\r等待比赛开始... 当前状态: {status or 'UNREACHABLE'} | Tick: {tick if tick is not None else '-'}   "
        )
        sys.stdout.flush()
        time.sleep(config.STOPPED_POLL_INTERVAL)

    return None


def main() -> None:
    setup_logging()
    logger = logging.getLogger("ritcx.main")

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    api = RitApiClient(
        base_url=config.API_BASE_URL,
        username=getattr(config, "USERNAME", ""),
        password=getattr(config, "PASSWORD", ""),
        timeout=config.REQUEST_TIMEOUT,
    )
    ob = OrderBookEngine(api)
    arb = StatArbStrategy(api, ob)
    tender = TenderStrategy(api, ob)
    fx = FxHedger(api, ob)
    monitor = ConverterMonitor(ob)

    logger.info("RITCx Algorithmic ETF Arbitrage 系统已启动")

    try:
        while not shutdown_requested:
            tick, status = api.get_tick_status()

            if status != "ACTIVE":
                tick = wait_for_active_case(api)
                if shutdown_requested or tick is None:
                    break
                status = "ACTIVE"

            snapshot = ob.get_portfolio_snapshot()

            # 1. Tender Offer 评估、接单、2,000 股被动拆单平仓 / 跑偏或尾盘转市价或篮子锁仓
            tender.step(snapshot, tick=tick)

            # 2. 统计套利（最后 15 秒冻结主动平仓；遵守 Gross 软上限）
            arb_st = arb.step(tick, snapshot)

            # 3. 净 USD 敞口定期对冲（强制保留 USD 现金底仓）
            fx_st = fx.step(snapshot)

           # 4. Converter 信号检测与 2-Tick 冷却锁警报
            conv_sig = monitor.detect_signal(tick, snapshot)
            if conv_sig.phase == "NEW_ALERT":
                monitor.trigger_alert(tick, conv_sig)

            # 5. 终端实时刷新看板
            arb_summary = (
                f"ShortETF={arb_st.edge_short_etf:+.4f}CAD | LongETF={arb_st.edge_long_etf:+.4f}CAD | "
                f"Hurdle={arb_st.fee_hurdle_cad:.4f}CAD | {arb_st.last_action}"
            )
            fx_summary = (
                f"NetUSD={fx_st.net_usd_exposure:+,.0f} | USDCash={fx_st.usd_cash:,.0f} "
                f"(Floor={config.FX_USD_CASH_FLOOR:,.0f}) | {fx_st.last_hedge_action}"
            )
            monitor.render_dashboard(
                tick=tick,
                status=status,
                snapshot=snapshot,
                arb_info=arb_summary,
                fx_info=fx_summary,
                tenders_info=tender.active_tenders_summary,
                converter_sig=conv_sig,
            )

            time.sleep(config.LOOP_INTERVAL)

    except Exception as exc:
        logger.exception("主循环发生未预期异常: %s", exc)
        sys.stderr.write(f"\n主循环异常退出: {exc}\n")
    finally:
        logger.info("正在清理未成交挂单并关闭连接...")
        try:
            open_orders = api.get_open_orders()
            for order in open_orders:
                oid = order.get("order_id")
                if oid is not None:
                    api.cancel_order(int(oid))
        except Exception as cleanup_exc:
            logger.warning("清理挂单时出现异常: %s", cleanup_exc)
        api.close()
        logger.info("系统已安全退出")


if __name__ == "__main__":
    main()
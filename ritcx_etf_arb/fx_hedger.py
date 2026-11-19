"""
FX 汇率敞口自动对冲模块 (USD/CAD)
- 净 USD 敞口 = USD 现金 + RITC 持仓 * RITC 中间价
- 强制保留 USD 现金底仓（默认 $4,000 USD）
- 尾盘（最后 15 秒）停止主动市价对冲 USD，避免临近结算吃滑点
"""

from dataclasses import dataclass
import logging
import time
from typing import Optional

from api_client import RitApiClient
import config
from orderbook import OrderBookEngine, PortfolioSnapshot

logger = logging.getLogger("ritcx.fx")


@dataclass
class FxStatus:
    """FX 对冲状态快照。"""

    usd_cash: float = 0.0
    net_usd_exposure: float = 0.0
    hedgeable_usd: float = 0.0
    last_hedge_action: str = "IDLE"


class FxHedger:
    """USD/CAD 敞口对冲器。"""

    def __init__(self, api: RitApiClient, ob: OrderBookEngine) -> None:
        self.api = api
        self.ob = ob
        self.last_run_ts: float = 0.0
        self.status = FxStatus()

    @staticmethod
    def _is_endgame(tick: Optional[int]) -> bool:
        if tick is None:
            return False
        return (config.TOTAL_CASE_TICKS - int(tick)) <= config.ARB_ENDGAME_FREEZE_TICKS

    def step(self, snapshot: PortfolioSnapshot, tick: Optional[int] = None) -> FxStatus:
        """按设定周期计算净 USD 敞口并执行对冲。"""
        now = time.monotonic()
        if now - self.last_run_ts < config.FX_HEDGE_INTERVAL:
            return self.status
        self.last_run_ts = now

        _, _, ritc_mid = self.ob.get_top_of_book(config.RITC)
        ritc_px = ritc_mid if ritc_mid is not None else 25.0

        usd_cash = float(snapshot.cash_by_ticker.get(config.USD, 0.0))
        ritc_pos = int(snapshot.positions.get(config.RITC, 0))

        net_usd_exposure = usd_cash + ritc_pos * ritc_px
        self.status.usd_cash = usd_cash
        self.status.net_usd_exposure = net_usd_exposure

        if self._is_endgame(tick):
            self.status.hedgeable_usd = 0.0
            self.status.last_hedge_action = "ENDGAME_HOLD"
            return self.status

        if abs(net_usd_exposure) <= config.FX_NET_EXPOSURE_THRESHOLD:
            self.status.hedgeable_usd = 0.0
            self.status.last_hedge_action = "WITHIN_BAND"
            return self.status

        if net_usd_exposure > config.FX_NET_EXPOSURE_THRESHOLD:
            max_sellable_cash = max(0.0, usd_cash - config.FX_USD_CASH_FLOOR)
            target_sell_usd = net_usd_exposure - config.FX_NET_EXPOSURE_THRESHOLD
            hedge_qty = int(min(target_sell_usd, max_sellable_cash, config.MAX_ORDER_SIZE[config.USD]))

            self.status.hedgeable_usd = hedge_qty
            if hedge_qty >= config.FX_HEDGE_MIN_QTY:
                if self.api.place_order(config.USD, "SELL", hedge_qty, order_type="MARKET"):
                    logger.info(
                        "FX 自动对冲：卖出 %d USD（净敞口 %.2f USD，保留现金底仓 %.2f USD）",
                        hedge_qty,
                        net_usd_exposure,
                        config.FX_USD_CASH_FLOOR,
                    )
                    self.status.last_hedge_action = f"SELL_USD({hedge_qty})"
                    return self.status
            else:
                self.status.last_hedge_action = "CASH_FLOOR_PROTECTED"
                return self.status

        elif net_usd_exposure < -config.FX_NET_EXPOSURE_THRESHOLD:
            target_buy_usd = abs(net_usd_exposure) - config.FX_NET_EXPOSURE_THRESHOLD
            hedge_qty = int(min(target_buy_usd, config.MAX_ORDER_SIZE[config.USD]))

            self.status.hedgeable_usd = hedge_qty
            if hedge_qty >= config.FX_HEDGE_MIN_QTY:
                if self.api.place_order(config.USD, "BUY", hedge_qty, order_type="MARKET"):
                    logger.info(
                        "FX 自动对冲：买入 %d USD（净敞口 %.2f USD）",
                        hedge_qty,
                        net_usd_exposure,
                    )
                    self.status.last_hedge_action = f"BUY_USD({hedge_qty})"
                    return self.status

        self.status.last_hedge_action = "HOLD"
        return self.status
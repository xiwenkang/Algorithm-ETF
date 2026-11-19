"""
ETF 与成分股统计套利模块 (Statistical Arbitrage)
- 使用 walk_book 计算下单数量 qty 的真实深度可执行价差（避免仅看买一/卖一被假深度误导）
- 严格区分开仓与平仓：平仓必须反向价差也覆盖手续费，且至少间隔 ARB_MIN_HOLD_SECONDS
- 遵守 Gross 软上限（默认 30%），为 Tender Offer 预留充足额度
- 比赛最后 15 秒停止主动平仓
"""

from dataclasses import dataclass
import logging
import time
from typing import Dict, Optional

from api_client import RitApiClient
import config
from orderbook import OrderBookEngine, PortfolioSnapshot

logger = logging.getLogger("ritcx.arb")


@dataclass
class ArbStatus:
    """统计套利当前状态快照。"""

    edge_short_etf: float = 0.0
    edge_long_etf: float = 0.0
    fee_hurdle_cad: float = 0.0
    soft_cap_blocked: bool = False
    endgame_frozen: bool = False
    last_action: str = "IDLE"


class StatArbStrategy:
    """ETF 与 BULL/BEAR 篮子统计套利策略。"""

    def __init__(self, api: RitApiClient, ob: OrderBookEngine) -> None:
        self.api = api
        self.ob = ob
        self.status = ArbStatus()
        self.last_trade_ts: float = 0.0

    @staticmethod
    def is_endgame(tick: Optional[int]) -> bool:
        """判断是否进入比赛最后 15 秒。"""
        if tick is None:
            return False
        remaining = config.TOTAL_CASE_TICKS - int(tick)
        return remaining <= config.ARB_ENDGAME_FREEZE_TICKS

    @staticmethod
    def compute_fee_hurdle(fx_ref: float) -> float:
        """三腿市价手续费（CAD）：0.02 USD * FX + 0.04 CAD。"""
        fx = fx_ref if fx_ref and fx_ref > 0 else 1.35
        return config.MARKET_FEE_PER_SHARE * fx + 2.0 * config.MARKET_FEE_PER_SHARE

    def step(self, tick: Optional[int], snapshot: PortfolioSnapshot) -> ArbStatus:
        """执行单步统计套利信号检测与下单。"""
        qty = min(config.ARB_ORDER_QTY, config.MAX_ORDER_SIZE[config.RITC])

        # 使用 walk_book 计算真实吃单 VWAP，而非仅看买一/卖一
        w_bull_bid = self.ob.walk_book(config.BULL, "SELL", qty)
        w_bull_ask = self.ob.walk_book(config.BULL, "BUY", qty)
        w_bear_bid = self.ob.walk_book(config.BEAR, "SELL", qty)
        w_bear_ask = self.ob.walk_book(config.BEAR, "BUY", qty)
        w_ritc_bid = self.ob.walk_book(config.RITC, "SELL", qty)
        w_ritc_ask = self.ob.walk_book(config.RITC, "BUY", qty)
        fx_bid, fx_ask, fx_mid = self.ob.get_top_of_book(config.USD)

        if (
            not all(
                w.sufficient_depth and w.vwap > 0
                for w in (w_bull_bid, w_bull_ask, w_bear_bid, w_bear_ask, w_ritc_bid, w_ritc_ask)
            )
            or fx_bid is None
            or fx_ask is None
        ):
            self.status.last_action = "THIN_BOOK"
            return self.status

        # 正向套利：卖 RITC（吃 bids），买 BULL + BEAR（吃 asks）
        edge_short_etf = (w_ritc_bid.vwap * fx_bid) - (w_bull_ask.vwap + w_bear_ask.vwap)
        # 反向套利：买 RITC（吃 asks），卖 BULL + BEAR（吃 bids）
        edge_long_etf = (w_bull_bid.vwap + w_bear_bid.vwap) - (w_ritc_ask.vwap * fx_ask)

        fee_hurdle = self.compute_fee_hurdle(fx_mid or fx_bid)
        open_threshold = max(config.ARB_OPEN_THRESHOLD_CAD, fee_hurdle + 0.03)
        close_threshold = fee_hurdle + config.ARB_CLOSE_PROFIT_CAD

        soft_cap = int(config.MAX_GROSS_LIMIT * config.ARB_GROSS_SOFT_CAP_RATIO)
        endgame = self.is_endgame(tick)

        self.status.edge_short_etf = edge_short_etf
        self.status.edge_long_etf = edge_long_etf
        self.status.fee_hurdle_cad = fee_hurdle
        self.status.endgame_frozen = endgame
        self.status.soft_cap_blocked = snapshot.gross >= soft_cap

        if endgame:
            self.status.last_action = "ENDGAME_HOLD"
            return self.status

        now = time.monotonic()
        if now - self.last_trade_ts < config.ARB_MIN_HOLD_SECONDS:
            self.status.last_action = "COOLDOWN"
            return self.status

        # 只有当反向价差也足以覆盖三腿手续费并额外盈利时才主动平仓（否则持有等收敛或期末按理论价结算）
        if self._try_profitable_unwind(snapshot, edge_short_etf, edge_long_etf, close_threshold):
            self.last_trade_ts = now
            return self.status

        # 开仓前检查软上限（给 Tender Offer 预留空间）
        if snapshot.gross >= soft_cap:
            self.status.last_action = "SOFT_CAP_HOLD"
            return self.status

        if edge_short_etf >= open_threshold:
            if self._execute_arb_legs(snapshot, "SELL", "BUY", qty, soft_cap):
                self.last_trade_ts = now
                self.status.last_action = f"OPEN_SHORT_ETF({edge_short_etf:+.4f})"
                return self.status

        elif edge_long_etf >= open_threshold:
            if self._execute_arb_legs(snapshot, "BUY", "SELL", qty, soft_cap):
                self.last_trade_ts = now
                self.status.last_action = f"OPEN_LONG_ETF({edge_long_etf:+.4f})"
                return self.status

        self.status.last_action = "WAIT"
        return self.status

    def _try_profitable_unwind(
        self,
        snapshot: PortfolioSnapshot,
        edge_short_etf: float,
        edge_long_etf: float,
        close_threshold: float,
    ) -> bool:
        """只有反向价差也高于 close_threshold（覆盖平仓手续费并盈利）时才平仓。"""
        ritc_pos = snapshot.positions.get(config.RITC, 0)
        bull_pos = snapshot.positions.get(config.BULL, 0)
        bear_pos = snapshot.positions.get(config.BEAR, 0)

        # 此前做空 RITC + 做多篮子 -> 现在反向价差 edge_long_etf >= close_threshold 时平仓并再赚一笔
        if ritc_pos < 0 and bull_pos > 0 and bear_pos > 0 and edge_long_etf >= close_threshold:
            qty = min(abs(ritc_pos), bull_pos, bear_pos, config.ARB_ORDER_QTY)
            if qty > 0 and self._execute_arb_legs(snapshot, "BUY", "SELL", qty, config.MAX_GROSS_LIMIT):
                logger.info("统计套利获利平仓（平空 RITC / 平多篮子）qty=%d edge=%.4f", qty, edge_long_etf)
                self.status.last_action = f"PROFIT_UNWIND({qty})"
                return True

        # 此前做多 RITC + 做空篮子 -> 现在正向价差 edge_short_etf >= close_threshold 时平仓并再赚一笔
        if ritc_pos > 0 and bull_pos < 0 and bear_pos < 0 and edge_short_etf >= close_threshold:
            qty = min(ritc_pos, abs(bull_pos), abs(bear_pos), config.ARB_ORDER_QTY)
            if qty > 0 and self._execute_arb_legs(snapshot, "SELL", "BUY", qty, config.MAX_GROSS_LIMIT):
                logger.info("统计套利获利平仓（平多 RITC / 平空篮子）qty=%d edge=%.4f", qty, edge_short_etf)
                self.status.last_action = f"PROFIT_UNWIND({qty})"
                return True

        return False

    def _execute_arb_legs(
        self,
        snapshot: PortfolioSnapshot,
        etf_action: str,
        basket_action: str,
        qty: int,
        gross_cap: int,
    ) -> bool:
        """三腿风控校验与同步下单。"""
        sim_pos: Dict[str, int] = dict(snapshot.positions)
        etf_delta = -qty if etf_action == "SELL" else qty
        basket_delta = qty if basket_action == "BUY" else -qty

        legs = [
            (config.RITC, etf_action, etf_delta),
            (config.BULL, basket_action, basket_delta),
            (config.BEAR, basket_action, basket_delta),
        ]

        for ticker, _, delta in legs:
            ok, reason = self.ob.check_limits(sim_pos, ticker, delta, gross_cap_override=gross_cap)
            if not ok:
                logger.debug("套利腿风控未通过 [%s delta=%d]: %s", ticker, delta, reason)
                return False
            sim_pos[ticker] = sim_pos.get(ticker, 0) + delta

        ok_ritc = self.api.place_order(config.RITC, etf_action, qty, order_type="MARKET")
        ok_bull = self.api.place_order(config.BULL, basket_action, qty, order_type="MARKET")
        ok_bear = self.api.place_order(config.BEAR, basket_action, qty, order_type="MARKET")

        if ok_ritc and ok_bull and ok_bear:
            logger.info("统计套利三腿下单 | RITC %s %d | BULL/BEAR %s %d", etf_action, qty, basket_action, qty)
            return True
        return False
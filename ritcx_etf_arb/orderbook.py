"""
盘口深度与仓位风控引擎
- walk_book(ticker, side, qty): 逐档遍历订单簿计算真实加权均价（VWAP）与滑点
- 仓位权重：BULL=1, BEAR=1, RITC=2, USD=0, CAD=0
- check_limits(ticker, qty_delta): 每次下单或接 Tender 前的强制风控校验
"""

from dataclasses import dataclass
import logging
from typing import Dict, List, Optional, Tuple

from api_client import RitApiClient
import config

logger = logging.getLogger("ritcx.orderbook")


@dataclass
class BookWalkResult:
    """订单簿深度遍历结果。"""

    ticker: str
    side: str               # "BUY" (吃 asks) 或 "SELL" (吃 bids)
    requested_qty: int
    filled_qty: int
    vwap: float             # 成交加权平均价
    best_price: float       # 最优档位价格（买盘取 Best Ask，卖盘取 Best Bid）
    worst_price: float      # 吃穿到的最差档位价格
    slippage_per_share: float  # 相对最优档位的每股滑点（正数表示不利滑点）
    total_notional: float   # 成交总金额
    sufficient_depth: bool  # 订单簿深度是否足以吃下全部 requested_qty


@dataclass
class PortfolioSnapshot:
    """账户持仓、现金与仓位风控快照。"""

    positions: Dict[str, int]   # 各标的持仓数量
    cash_by_ticker: Dict[str, float]  # 各标的可用/账面余额（如 USD 现金）
    gross: int                  # |BULL| + |BEAR| + 2*|RITC|
    net: int                    # BULL + BEAR + 2*RITC
    gross_ratio: float          # gross / MAX_GROSS_LIMIT
    net_long_ratio: float       # max(0, net) / MAX_LONG_NET
    net_short_ratio: float      # max(0, -net) / abs(MAX_SHORT_NET)


class OrderBookEngine:
    """订单簿与实时风控引擎。"""

    def __init__(self, api_client: RitApiClient) -> None:
        self.api = api_client
        self._book_cache: Dict[str, Tuple[float, Dict[str, List[Dict[str, float]]]]] = {}
        self._cache_ttl = 0.08  # 极短缓存（80ms），同一轮循环内复用避免重复请求

    def get_book(self, ticker: str, force_refresh: bool = False) -> Dict[str, List[Dict[str, float]]]:
        """获取并规范化排序后的订单簿（bids 降序，asks 升序）。"""
        now = time_monotonic()
        if not force_refresh and ticker in self._book_cache:
            ts, cached = self._book_cache[ticker]
            if now - ts <= self._cache_ttl:
                return cached

        raw = self.api.get_order_book(ticker)
        if not raw:
            return {"bids": [], "asks": []}

        bids: List[Dict[str, float]] = []
        for level in raw.get("bids", []) or []:
            qty = float(level.get("quantity", 0.0)) - float(level.get("quantity_filled", 0.0))
            price = float(level.get("price", 0.0))
            if qty > 0 and price > 0:
                bids.append({"price": price, "quantity": qty})

        asks: List[Dict[str, float]] = []
        for level in raw.get("asks", []) or []:
            qty = float(level.get("quantity", 0.0)) - float(level.get("quantity_filled", 0.0))
            price = float(level.get("price", 0.0))
            if qty > 0 and price > 0:
                asks.append({"price": price, "quantity": qty})

        bids.sort(key=lambda x: x["price"], reverse=True)
        asks.sort(key=lambda x: x["price"])

        book = {"bids": bids, "asks": asks}
        self._book_cache[ticker] = (now, book)
        return book

    def get_top_of_book(self, ticker: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """返回 (best_bid, best_ask, mid_price)。"""
        book = self.get_book(ticker)
        best_bid = book["bids"][0]["price"] if book["bids"] else None
        best_ask = book["asks"][0]["price"] if book["asks"] else None
        mid = None
        if best_bid is not None and best_ask is not None:
            mid = 0.5 * (best_bid + best_ask)
        elif best_bid is not None:
            mid = best_bid
        elif best_ask is not None:
            mid = best_ask
        return best_bid, best_ask, mid

    def walk_book(self, ticker: str, side: str, qty: int) -> BookWalkResult:
        """
        逐档遍历订单簿计算吃下 qty 单位时的真实 VWAP 与滑点。
        - side == "BUY": 从低到高吃 asks
        - side == "SELL": 从高到低吃 bids
        """
        side_u = side.upper()
        req_qty = max(0, int(qty))
        book = self.get_book(ticker)
        levels = book["asks"] if side_u == "BUY" else book["bids"]

        if not levels or req_qty == 0:
            return BookWalkResult(
                ticker=ticker,
                side=side_u,
                requested_qty=req_qty,
                filled_qty=0,
                vwap=0.0,
                best_price=0.0,
                worst_price=0.0,
                slippage_per_share=0.0,
                total_notional=0.0,
                sufficient_depth=(req_qty == 0),
            )

        best_price = float(levels[0]["price"])
        remaining = float(req_qty)
        filled = 0.0
        notional = 0.0
        worst_price = best_price

        for lvl in levels:
            if remaining <= 1e-9:
                break
            lvl_price = float(lvl["price"])
            lvl_qty = float(lvl["quantity"])
            take_qty = min(remaining, lvl_qty)
            if take_qty > 0:
                notional += take_qty * lvl_price
                filled += take_qty
                remaining -= take_qty
                worst_price = lvl_price

        filled_int = int(round(filled))
        vwap = (notional / filled) if filled > 0 else 0.0
        if filled > 0:
            if side_u == "BUY":
                slippage = max(0.0, vwap - best_price)
            else:
                slippage = max(0.0, best_price - vwap)
        else:
            slippage = 0.0

        sufficient = remaining <= 1e-6

        return BookWalkResult(
            ticker=ticker,
            side=side_u,
            requested_qty=req_qty,
            filled_qty=filled_int,
            vwap=vwap,
            best_price=best_price,
            worst_price=worst_price,
            slippage_per_share=slippage,
            total_notional=notional,
            sufficient_depth=sufficient,
        )

    def get_portfolio_snapshot(self) -> PortfolioSnapshot:
        """拉取账户持仓与余额，计算 Gross / Net 敞口及占用比例。"""
        secs = self.api.get_securities()
        positions: Dict[str, int] = {t: 0 for t in config.ALL_TICKERS}
        cash_map: Dict[str, float] = {t: 0.0 for t in config.ALL_TICKERS}

        for item in secs:
            t = item.get("ticker")
            if t in config.ALL_TICKERS:
                positions[t] = int(item.get("position", 0))
                # RIT /securities 字段中 position 即为该标的净持仓（USD 标的的 position 即为 USD 余额/仓位）
                cash_map[t] = float(item.get("position", 0.0))

        gross, net = self.compute_gross_net(positions)
        gross_ratio = gross / config.MAX_GROSS_LIMIT if config.MAX_GROSS_LIMIT > 0 else 0.0
        net_long_ratio = max(0, net) / config.MAX_LONG_NET if config.MAX_LONG_NET > 0 else 0.0
        net_short_ratio = max(0, -net) / abs(config.MAX_SHORT_NET) if config.MAX_SHORT_NET != 0 else 0.0

        return PortfolioSnapshot(
            positions=positions,
            cash_by_ticker=cash_map,
            gross=gross,
            net=net,
            gross_ratio=gross_ratio,
            net_long_ratio=net_long_ratio,
            net_short_ratio=net_short_ratio,
        )

    @staticmethod
    def compute_gross_net(positions: Dict[str, int]) -> Tuple[int, int]:
        """
        严格按规则计算 Gross 与 Net：
        Gross = |BULL| + |BEAR| + 2 * |RITC|
        Net   = BULL + BEAR + 2 * RITC
        """
        bull = int(positions.get(config.BULL, 0))
        bear = int(positions.get(config.BEAR, 0))
        ritc = int(positions.get(config.RITC, 0))

        gross = abs(bull) + abs(bear) + 2 * abs(ritc)
        net = bull + bear + 2 * ritc
        return gross, net

    def check_limits(
        self,
        positions: Dict[str, int],
        ticker: str,
        qty_delta: int,
        gross_cap_override: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        下单或接受 Tender 前的风控校验。
        qty_delta: 对该 ticker 仓位的变动量（买入为正，卖出为负）。
        """
        if ticker not in config.POSITION_WEIGHTS:
            return False, f"未知标的: {ticker}"

        max_ord = config.MAX_ORDER_SIZE.get(ticker)
        if max_ord is not None and abs(int(qty_delta)) > max_ord:
            return False, f"{ticker} 单笔变动 {abs(int(qty_delta))} 超过单笔上限 {max_ord}"

        # FX 标的权重为 0，不占用股票/ETF Gross 与 Net 限额
        if config.POSITION_WEIGHTS[ticker] == 0:
            return True, "OK"

        new_pos = dict(positions)
        new_pos[ticker] = int(new_pos.get(ticker, 0)) + int(qty_delta)
        new_gross, new_net = self.compute_gross_net(new_pos)

        gross_limit = gross_cap_override if gross_cap_override is not None else config.MAX_GROSS_LIMIT
        if new_gross > gross_limit:
            return False, f"Gross 超限 ({new_gross} > {gross_limit})"

        if new_net > config.MAX_LONG_NET:
            return False, f"Net 多头超限 ({new_net} > {config.MAX_LONG_NET})"

        if new_net < config.MAX_SHORT_NET:
            return False, f"Net 空头超限 ({new_net} < {config.MAX_SHORT_NET})"

        return True, "OK"


def time_monotonic() -> float:
    import time

    return time.monotonic()
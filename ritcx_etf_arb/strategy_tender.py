"""
Private Tender Offer 决策与双轨加速平仓引擎（RITC 拆单 + BULL/BEAR 同步篮子锁仓）
1. 使用 RITC 当前 bid/ask/mid 计算单股毛利 unit_edge_usd，>= TENDER_MIN_UNIT_EDGE_USD (0.08) 且风控通过才 ACCEPT
2. 保留入账确认状态机（settled=False -> settled=True），防止模拟器入账延迟导致提前 DONE
3. 双轨加速：
   - 轨道 1：RITC 4,500 股被动限价拆单挂 Best Ask/Bid，存活 >=1.5s 或偏离 >1 tick 才 REQUOTE
   - 轨道 2：remaining_qty > 30,000 且 BULL/BEAR 合成隐含价优于 tender_px ± $0.05 时，同步下 5,000 股反向篮子对冲（上限 30,000 股）
4. 价格不利漂移 > $0.06 或耗时 > 10s 时，切换 5,000 股市价单快速收尾
"""

from dataclasses import dataclass, field
import logging
import math
import time
from typing import Any, Dict, List, Optional

from api_client import RitApiClient
import config
from orderbook import OrderBookEngine, PortfolioSnapshot

logger = logging.getLogger("ritcx.tender")

SETTLEMENT_WAIT_SEC = 2.5


@dataclass
class TenderEvaluation:
    tender_id: int
    action: str
    qty: int
    tender_px: float
    ritc_bid: float
    ritc_ask: float
    ritc_mid: float
    unit_edge_usd: float
    limits_ok: bool
    limit_reason: str
    should_accept: bool


@dataclass
class OpenSliceOrder:
    order_id: int
    qty: int
    price: float
    placed_at: float


@dataclass
class ActiveTenderTask:
    tender_id: int
    action: str               # "BUY" (RITC +qty) 或 "SELL" (RITC -qty)
    tender_qty: int
    tender_px: float
    entry_ritc_mid: float
    unwind_side: str          # 接单 BUY -> "SELL"；接单 SELL -> "BUY"
    pre_ritc_pos: int
    expected_delta: int
    remaining_qty: int
    prev_effective_exposure: int
    settled: bool = False
    basket_hedged_qty: int = 0
    last_basket_ts: float = 0.0
    open_slices: List[OpenSliceOrder] = field(default_factory=list)
    last_action_ts: float = 0.0
    created_at: float = field(default_factory=time.monotonic)


class TenderStrategy:
    """Tender Offer 评估、接单与双轨加速平仓管理器。"""

    def __init__(self, api: RitApiClient, ob: OrderBookEngine) -> None:
        self.api = api
        self.ob = ob
        self.seen_tender_ids: set[int] = set()
        self.active_tasks: List[ActiveTenderTask] = []
        self.active_tenders_summary: List[str] = []

    def step(self, snapshot: PortfolioSnapshot, tick: Optional[int] = None) -> None:
        self._poll_and_evaluate_tenders(snapshot, tick=tick)
        self._manage_tasks(snapshot, tick=tick)

    @staticmethod
    def _parse_valid_price(raw_price: Any) -> Optional[float]:
        if raw_price is None:
            return None
        try:
            px = float(raw_price)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(px) or px <= 0:
            return None
        return px

    def _poll_and_evaluate_tenders(self, snapshot: PortfolioSnapshot, tick: Optional[int] = None) -> None:
        tenders = self.api.get_tenders()
        summaries: List[str] = []

        for t in tenders:
            tid = int(t.get("tender_id", -1))
            if tid < 0:
                continue
            qty = int(t.get("quantity", 0))
            valid_px = self._parse_valid_price(t.get("price"))
            display_px = valid_px if valid_px is not None else 0.0
            action = str(t.get("action", "BUY")).upper()
            summaries.append(f"#{tid} {action} {qty}@{display_px:.4f}")

            if tid in self.seen_tender_ids:
                continue
            self.seen_tender_ids.add(tid)

            if valid_px is None:
                logger.warning("Tender #%d 价格非法 (%r)，跳过评估", tid, t.get("price"))
                continue

            eval_res = self.evaluate_tender(
                tender_id=tid,
                action=action,
                qty=qty,
                tender_px=valid_px,
                snapshot=snapshot,
            )

            decision_str = "ACCEPT" if eval_res.should_accept else "REJECT"
            logger.info(
                "Tender #%d | action=%s qty=%d tender_px=%.4f | RITC bid=%.4f ask=%.4f mid=%.4f | unit_edge=%.4f USD | 风控=%s(%s) | 决策=%s",
                eval_res.tender_id,
                eval_res.action,
                eval_res.qty,
                eval_res.tender_px,
                eval_res.ritc_bid,
                eval_res.ritc_ask,
                eval_res.ritc_mid,
                eval_res.unit_edge_usd,
                eval_res.limits_ok,
                eval_res.limit_reason,
                decision_str,
            )

            if eval_res.should_accept:
                if self.api.accept_tender(tid, price=valid_px):
                    unwind_side = "SELL" if action == "BUY" else "BUY"
                    entry_mid = eval_res.ritc_mid if eval_res.ritc_mid > 0 else valid_px
                    expected_delta = qty if action == "BUY" else -qty
                    pre_pos = int(snapshot.positions.get(config.RITC, 0))

                    task = ActiveTenderTask(
                        tender_id=tid,
                        action=action,
                        tender_qty=qty,
                        tender_px=valid_px,
                        entry_ritc_mid=entry_mid,
                        unwind_side=unwind_side,
                        pre_ritc_pos=pre_pos,
                        expected_delta=expected_delta,
                        remaining_qty=qty,
                        prev_effective_exposure=qty,
                        settled=False,
                        created_at=time.monotonic(),
                    )
                    self.active_tasks.append(task)
                    logger.info(
                        "Tender #%d 已接单 | 等待入账 (settled=False) | unwind=%s qty=%d pre_pos=%d entry_mid=%.4f (tick=%s)",
                        tid,
                        unwind_side,
                        qty,
                        pre_pos,
                        entry_mid,
                        tick,
                    )

        self.active_tenders_summary = summaries

    def evaluate_tender(
        self,
        tender_id: int,
        action: str,
        qty: int,
        tender_px: float,
        snapshot: PortfolioSnapshot,
    ) -> TenderEvaluation:
        best_bid, best_ask, mid = self.ob.get_top_of_book(config.RITC)
        bid_val = float(best_bid) if best_bid is not None else 0.0
        ask_val = float(best_ask) if best_ask is not None else 0.0

        if mid is not None and mid > 0:
            mid_val = float(mid)
        elif bid_val > 0 and ask_val > 0:
            mid_val = 0.5 * (bid_val + ask_val)
        else:
            mid_val = bid_val or ask_val or tender_px

        action_u = action.upper()
        if action_u == "BUY":
            unit_edge_usd = mid_val - tender_px
            ritc_delta = qty
        else:
            unit_edge_usd = tender_px - mid_val
            ritc_delta = -qty

        sim_pos = dict(snapshot.positions)
        sim_pos[config.RITC] = sim_pos.get(config.RITC, 0) + ritc_delta
        sim_gross, sim_net = self.ob.compute_gross_net(sim_pos)

        limits_ok = True
        limit_reason = "OK"
        if sim_gross > config.MAX_GROSS_LIMIT:
            limits_ok = False
            limit_reason = f"Gross 超限 ({sim_gross} > {config.MAX_GROSS_LIMIT})"
        elif sim_net > config.MAX_LONG_NET:
            limits_ok = False
            limit_reason = f"Net 多头超限 ({sim_net} > {config.MAX_LONG_NET})"
        elif sim_net < config.MAX_SHORT_NET:
            limits_ok = False
            limit_reason = f"Net 空头超限 ({sim_net} < {config.MAX_SHORT_NET})"

        min_edge = float(getattr(config, "TENDER_MIN_UNIT_EDGE_USD", 0.08))
        should_accept = limits_ok and (qty > 0) and (unit_edge_usd >= min_edge)

        return TenderEvaluation(
            tender_id=tender_id,
            action=action_u,
            qty=qty,
            tender_px=tender_px,
            ritc_bid=bid_val,
            ritc_ask=ask_val,
            ritc_mid=mid_val,
            unit_edge_usd=unit_edge_usd,
            limits_ok=limits_ok,
            limit_reason=limit_reason,
            should_accept=should_accept,
        )

    def _cancel_all_slices(self, task: ActiveTenderTask) -> None:
        for s in task.open_slices:
            self.api.cancel_order(s.order_id)
        task.open_slices.clear()

    def _compute_effective_exposure(self, task: ActiveTenderTask, snapshot: PortfolioSnapshot) -> int:
        """
        计算当前未对冲的真实 RITC 净敞口：
        扣除由本任务建立且当前仍实际持有的反向 BULL/BEAR 篮子数量，避免过度平仓，
        同时兼容人工点击 Converter 后 RITC 与篮子同步抵消的情况。
        """
        ritc_pos = int(snapshot.positions.get(config.RITC, 0))
        bull_pos = int(snapshot.positions.get(config.BULL, 0))
        bear_pos = int(snapshot.positions.get(config.BEAR, 0))

        if task.unwind_side == "SELL":
            raw_exp = max(0, ritc_pos)
            actual_opp_basket = min(max(0, -bull_pos), max(0, -bear_pos))
        else:
            raw_exp = max(0, -ritc_pos)
            actual_opp_basket = min(max(0, bull_pos), max(0, bear_pos))

        effective_basket_hedged = min(task.basket_hedged_qty, actual_opp_basket)
        return max(0, raw_exp - effective_basket_hedged)

    def _check_and_update_settlement(
        self,
        task: ActiveTenderTask,
        snapshot: PortfolioSnapshot,
        cur_ritc_pos: int,
        elapsed: float,
    ) -> None:
        if task.settled:
            return

        jump = cur_ritc_pos - task.pre_ritc_pos
        threshold = 0.5 * task.tender_qty
        jumped = (
            (task.expected_delta > 0 and jump >= threshold)
            or (task.expected_delta < 0 and jump <= -threshold)
        )

        if jumped or elapsed >= SETTLEMENT_WAIT_SEC:
            task.settled = True
            eff_exp = self._compute_effective_exposure(task, snapshot)
            task.remaining_qty = eff_exp
            task.prev_effective_exposure = eff_exp
            logger.info(
                "Tender #%d 入账确认完成 (settled=True) | pre=%d -> cur=%d (jump=%+d) | 待平净敞口=%d | 耗时=%.2fs",
                task.tender_id,
                task.pre_ritc_pos,
                cur_ritc_pos,
                jump,
                eff_exp,
                elapsed,
            )

    def _try_sync_basket_hedge(self, task: ActiveTenderTask, snapshot: PortfolioSnapshot, now: float) -> bool:
        """
        轨道 2（同步篮子锁仓）：
        - 条件：remaining_qty > TENDER_BASKET_TRIGGER_REM_QTY (30,000) 且未达 TENDER_BASKET_HEDGE_MAX_QTY (30,000)
        - 盈利校验：BULL/BEAR 盘口合成隐含 USD 价格优于 tender_px ± TENDER_BASKET_MIN_EDGE_USD (0.05)
        - 成功后立刻释放 Net Limit，并从 remaining_qty 中扣减已锁仓数量
        """
        trigger_rem = int(getattr(config, "TENDER_BASKET_TRIGGER_REM_QTY", 30_000))
        max_hedge = int(getattr(config, "TENDER_BASKET_HEDGE_MAX_QTY", 30_000))
        batch_size = int(getattr(config, "TENDER_BASKET_HEDGE_BATCH_QTY", 5_000))
        min_edge = float(getattr(config, "TENDER_BASKET_MIN_EDGE_USD", 0.05))
        interval_sec = float(getattr(config, "TENDER_BASKET_INTERVAL_SEC", 0.6))

        if task.remaining_qty <= trigger_rem:
            return False
        if task.basket_hedged_qty >= max_hedge:
            return False
        if (now - task.last_basket_ts) < interval_sec:
            return False

        hedge_qty = min(
            batch_size,
            max_hedge - task.basket_hedged_qty,
            task.remaining_qty,
            config.MAX_ORDER_SIZE[config.BULL],
            config.MAX_ORDER_SIZE[config.BEAR],
        )
        if hedge_qty <= 0:
            return False

        bull_bid, bull_ask, _ = self.ob.get_top_of_book(config.BULL)
        bear_bid, bear_ask, _ = self.ob.get_top_of_book(config.BEAR)
        fx_bid, fx_ask, _ = self.ob.get_top_of_book(config.USD)
        if None in (bull_bid, bull_ask, bear_bid, bear_ask, fx_bid, fx_ask):
            return False

        if task.unwind_side == "SELL":
            # 接单 BUY RITC -> 卖出 BULL + BEAR 锁仓
            basket_side = "SELL"
            implied_usd = (float(bull_bid) + float(bear_bid)) / float(fx_ask)
            basket_edge_usd = implied_usd - task.tender_px
        else:
            # 接单 SELL RITC -> 买入 BULL + BEAR 锁仓
            basket_side = "BUY"
            implied_usd = (float(bull_ask) + float(bear_ask)) / float(fx_bid)
            basket_edge_usd = task.tender_px - implied_usd

        if basket_edge_usd < min_edge:
            return False

        delta = -hedge_qty if basket_side == "SELL" else hedge_qty
        sim_pos: Dict[str, int] = dict(snapshot.positions)
        ok_bull, _ = self.ob.check_limits(sim_pos, config.BULL, delta)
        if not ok_bull:
            return False
        sim_pos[config.BULL] = sim_pos.get(config.BULL, 0) + delta

        ok_bear, _ = self.ob.check_limits(sim_pos, config.BEAR, delta)
        if not ok_bear:
            return False

        ok_b1 = self.api.place_order(config.BULL, basket_side, hedge_qty, order_type="MARKET")
        ok_b2 = self.api.place_order(config.BEAR, basket_side, hedge_qty, order_type="MARKET")
        if ok_b1 and ok_b2:
            task.basket_hedged_qty += hedge_qty
            task.remaining_qty = max(0, task.remaining_qty - hedge_qty)
            task.last_basket_ts = now
            logger.info(
                "Tender #%d 同步篮子锁仓 | %s BULL/BEAR %d (累计=%d) | 隐含=%.4f USD (edge=%+.4f) | 剩余净敞口=%d",
                task.tender_id,
                basket_side,
                hedge_qty,
                task.basket_hedged_qty,
                implied_usd,
                basket_edge_usd,
                task.remaining_qty,
            )
            return True

        return False

    def _manage_tasks(self, snapshot: PortfolioSnapshot, tick: Optional[int] = None) -> None:
        if not self.active_tasks:
            return

        now = time.monotonic()
        slice_target = int(min(10_000, max(4_000, getattr(config, "TENDER_SLICE_QTY", 4_500))))
        mkt_slice_target = int(min(10_000, getattr(config, "TENDER_MARKET_SLICE_QTY", 5_000)))
        max_open_orders = int(max(1, min(2, getattr(config, "TENDER_MAX_OPEN_ORDERS", 2))))
        requote_sec = float(getattr(config, "TENDER_REQUOTE_SEC", 1.5))
        min_live_sec = float(getattr(config, "TENDER_REQUOTE_MIN_LIVE_SEC", 1.0))
        accel_after_sec = float(getattr(config, "TENDER_ACCEL_AFTER_SEC", 10.0))
        drift_threshold = float(getattr(config, "TENDER_DRIFT_STOP_USD", 0.06))
        tick_size = float(getattr(config, "TENDER_TICK_SIZE_USD", 0.01))
        urgent_ticks = int(getattr(config, "TENDER_URGENT_REMAINING_TICKS", 25))

        remaining_ticks = (config.TOTAL_CASE_TICKS - int(tick)) if tick is not None else None
        cur_ritc_pos = int(snapshot.positions.get(config.RITC, 0))

        remaining_tasks: List[ActiveTenderTask] = []

        for task in self.active_tasks:
            elapsed = now - task.created_at
            self._check_and_update_settlement(task, snapshot, cur_ritc_pos, elapsed)

            if task.settled:
                eff_exposure = self._compute_effective_exposure(task, snapshot)
                filled_delta = max(0, task.prev_effective_exposure - eff_exposure)
                task.remaining_qty = eff_exposure
                task.prev_effective_exposure = eff_exposure

                if filled_delta > 0:
                    logger.info(
                        "Tender #%d 平仓进度 | 剩余=%d | RITC=%d (篮子已锁=%d) | 操作=FILL qty=%d",
                        task.tender_id,
                        task.remaining_qty,
                        cur_ritc_pos,
                        task.basket_hedged_qty,
                        filled_delta,
                    )
                    if task.open_slices and task.remaining_qty > 0:
                        self._cancel_all_slices(task)

                if eff_exposure <= 0:
                    self._cancel_all_slices(task)
                    logger.info(
                        "Tender #%d 平仓任务已完成 | 耗时=%.2fs | RITC=%d | 篮子锁仓=%d",
                        task.tender_id,
                        elapsed,
                        cur_ritc_pos,
                        task.basket_hedged_qty,
                    )
                    continue

                # 轨道 2：大仓位时同步用 BULL/BEAR 篮子锁仓，快速归零 Net Limit
                self._try_sync_basket_hedge(task, snapshot, now)
                if task.remaining_qty <= 0:
                    self._cancel_all_slices(task)
                    logger.info(
                        "Tender #%d 平仓任务已完成（双轨锁仓完毕）| 耗时=%.2fs | 篮子锁仓=%d",
                        task.tender_id,
                        elapsed,
                        task.basket_hedged_qty,
                    )
                    continue

            best_bid, best_ask, mid_usd = self.ob.get_top_of_book(config.RITC)
            if best_bid is None or best_ask is None:
                remaining_tasks.append(task)
                continue

            cur_mid = float(mid_usd) if mid_usd is not None else 0.5 * (best_bid + best_ask)
            passive_px = float(best_ask) if task.unwind_side == "SELL" else float(best_bid)

            # 优化 REQUOTE：仅当挂单超时 (>=1.5s) 或存活 >=1.0s 且盘口偏离超过 1 tick (>0.01) 时撤单重挂
            stale_or_drifted = [
                s for s in task.open_slices
                if (now - s.placed_at) >= requote_sec
                or ((now - s.placed_at) >= min_live_sec and abs(s.price - passive_px) > (tick_size + 1e-6))
            ]
            if stale_or_drifted:
                for s in stale_or_drifted:
                    self.api.cancel_order(s.order_id)
                    task.open_slices.remove(s)
                    logger.info(
                        "Tender #%d 平仓进度 | 剩余=%d | RITC=%d | 操作=REQUOTE qty=%d px=%.4f",
                        task.tender_id,
                        task.remaining_qty,
                        cur_ritc_pos,
                        s.qty,
                        passive_px,
                    )
                task.last_action_ts = now

            # 仅在入账确认后，价格不利漂移 > $0.06 或耗时 > 10s（或尾盘）时，切换 5,000 股市价单快速收尾
            if task.settled:
                if task.unwind_side == "SELL":
                    adverse_move = task.entry_ritc_mid - cur_mid
                else:
                    adverse_move = cur_mid - task.entry_ritc_mid

                price_adverse = adverse_move > drift_threshold
                time_accel = (elapsed >= accel_after_sec and task.remaining_qty > 0) or (
                    remaining_ticks is not None and remaining_ticks <= urgent_ticks
                )

                if (price_adverse or time_accel) and (now - task.last_action_ts >= 0.4):
                    self._cancel_all_slices(task)
                    accel_qty = min(
                        task.remaining_qty,
                        mkt_slice_target,
                        config.MAX_ORDER_SIZE[config.RITC],
                    )
                    delta = accel_qty if task.unwind_side == "BUY" else -accel_qty
                    ok, _ = self.ob.check_limits(snapshot.positions, config.RITC, delta)
                    if ok and self.api.place_order(config.RITC, task.unwind_side, accel_qty, order_type="MARKET"):
                        task.last_action_ts = now
                        logger.warning(
                            "Tender #%d 平仓进度 | 剩余=%d | RITC=%d | 操作=MARKET qty=%d px=%.4f (drift=%.3f, t=%.1fs)",
                            task.tender_id,
                            task.remaining_qty,
                            cur_ritc_pos,
                            accel_qty,
                            cur_mid,
                            adverse_move,
                            elapsed,
                        )
                    remaining_tasks.append(task)
                    continue

            # 轨道 1：常规被动限价拆单（每次 4,500 股，最多维护 2 笔挂单赚返佣）
            committed_qty = sum(s.qty for s in task.open_slices)
            avail_to_post = task.remaining_qty - committed_qty

            if (
                len(task.open_slices) < max_open_orders
                and avail_to_post > 0
                and passive_px > 0
                and (now - task.last_action_ts >= 0.25)
            ):
                post_qty = min(
                    avail_to_post,
                    slice_target,
                    config.MAX_ORDER_SIZE[config.RITC],
                )
                delta = post_qty if task.unwind_side == "BUY" else -post_qty
                ok, _ = self.ob.check_limits(snapshot.positions, config.RITC, delta)
                if ok:
                    resp = self.api.place_order(
                        config.RITC,
                        task.unwind_side,
                        post_qty,
                        order_type="LIMIT",
                        price=round(passive_px, 4),
                    )
                    if resp and isinstance(resp, dict) and "order_id" in resp:
                        task.open_slices.append(
                            OpenSliceOrder(
                                order_id=int(resp["order_id"]),
                                qty=post_qty,
                                price=round(passive_px, 4),
                                placed_at=now,
                            )
                        )
                        task.last_action_ts = now
                        logger.info(
                            "Tender #%d 平仓进度 | 剩余=%d | RITC=%d | 操作=LIMIT qty=%d px=%.4f",
                            task.tender_id,
                            task.remaining_qty,
                            cur_ritc_pos,
                            post_qty,
                            round(passive_px, 4),
                        )

            remaining_tasks.append(task)

        self.active_tasks = remaining_tasks
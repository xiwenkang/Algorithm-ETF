"""
人机协同 Converter 监控与终端实时看板模块
- 分层触发：仅在 >= 2 手（20,000 股）或 Gross >= 60% 或直接平仓滑点 > $0.15/股时报警
- 粘性驻留 15 秒 + 任务编号（#1, #2...）+ 仓位兑现对比，明确区分“待操作/挂起”、“已兑现”与“新增”
"""

from dataclasses import dataclass
import logging
import math
import os
import platform
import sys
import time
from typing import List, Optional

import config
from orderbook import OrderBookEngine, PortfolioSnapshot

logger = logging.getLogger("ritcx.monitor")

STICKY_SECONDS = 15.0
MAX_SUGGESTED_USES_PER_ALERT = 2
MIN_FULFILLED_DELTA = 8_000


@dataclass
class ConverterSignal:
    """Converter 看板状态。"""

    task_id: int = 0
    phase: str = "IDLE"                # "NEW_ALERT" | "ACTIVE_HOLD" | "FULFILLED_CHECK" | "IDLE"
    signal_type: Optional[str] = None  # "ETF-Redemption" 或 "ETF-Creation"
    uses: int = 0
    total_shares: int = 0
    required_usd: float = 0.0
    headline: str = ""
    detail: str = ""
    reason: str = ""


class ConverterMonitor:
    """带任务编号与兑现确认的 Converter 看板。"""

    def __init__(self, ob: OrderBookEngine) -> None:
        self.ob = ob
        self.cooldown_until_tick: int = -1
        self.task_counter: int = 0

        self.active_task_id: int = 0
        self.active_type: Optional[str] = None
        self.active_uses: int = 0
        self.active_since_ts: float = 0.0
        self.baseline_pos: Optional[tuple] = None

    def _compute_raw_opportunity(self, snapshot: PortfolioSnapshot):
        """计算当前账户持仓下可执行的 Converter 类型与最大整数手数。"""
        lot = config.CONVERTER_LOT_SIZE
        bull = snapshot.positions.get(config.BULL, 0)
        bear = snapshot.positions.get(config.BEAR, 0)
        ritc = snapshot.positions.get(config.RITC, 0)
        usd_cash = float(snapshot.cash_by_ticker.get(config.USD, 0.0))

        # 多 RITC (>=10000) + 空 BULL/BEAR (<=-10000) -> ETF-Redemption
        if ritc >= lot and bull <= -lot and bear <= -lot:
            max_uses = min(ritc, abs(bull), abs(bear)) // lot
            return "ETF-Redemption", max_uses, usd_cash, "SELL"

        # 空 RITC (<=-10000) + 多 BULL/BEAR (>=10000) -> ETF-Creation
        if ritc <= -lot and bull >= lot and bear >= lot:
            max_uses = min(abs(ritc), bull, bear) // lot
            return "ETF-Creation", max_uses, usd_cash, "BUY"

        return None, 0, usd_cash, "SELL"

    def _is_fulfilled(self, snapshot: PortfolioSnapshot) -> bool:
        """对比触发基准仓位与当前仓位，判断是否已点击 Converter 生效。"""
        if self.baseline_pos is None or self.active_type is None:
            return False
        base_bull, base_bear, base_ritc = self.baseline_pos
        cur_bull = snapshot.positions.get(config.BULL, 0)
        cur_bear = snapshot.positions.get(config.BEAR, 0)
        cur_ritc = snapshot.positions.get(config.RITC, 0)

        if self.active_type == "ETF-Redemption":
            return (
                (base_ritc - cur_ritc) >= MIN_FULFILLED_DELTA
                and (cur_bull - base_bull) >= MIN_FULFILLED_DELTA
                and (cur_bear - base_bear) >= MIN_FULFILLED_DELTA
            )
        else:
            return (
                (cur_ritc - base_ritc) >= MIN_FULFILLED_DELTA
                and (base_bull - cur_bull) >= MIN_FULFILLED_DELTA
                and (base_bear - cur_bear) >= MIN_FULFILLED_DELTA
            )

    def detect_signal(self, tick: Optional[int], snapshot: PortfolioSnapshot) -> ConverterSignal:
        """评估 Converter 状态（分层触发 + 粘性驻留 + 兑现确认）。"""
        now = time.monotonic()
        lot = config.CONVERTER_LOT_SIZE
        bull = snapshot.positions.get(config.BULL, 0)
        bear = snapshot.positions.get(config.BEAR, 0)
        ritc = snapshot.positions.get(config.RITC, 0)
        cur_tuple = (bull, bear, ritc)

        conv_type, max_uses, usd_cash, unwind_side = self._compute_raw_opportunity(snapshot)
        max_affordable = max(0, int(math.floor(usd_cash / config.FX_CONVERTER_USD_REQ)))

        # 1. 若当前有驻留任务，先检查是否已兑现
        if self.active_type is not None:
            if self._is_fulfilled(snapshot):
                done_id = self.active_task_id
                done_type = self.active_type
                self.active_type = None
                self.active_uses = 0
                self.baseline_pos = None
                if tick is not None:
                    self.cooldown_until_tick = max(self.cooldown_until_tick, int(tick) + config.CONVERTER_COOLDOWN_TICKS)

                if conv_type and max_uses >= config.CONVERTER_MIN_BATCH_USES and max_affordable > 0:
                    self.task_counter += 1
                    new_id = self.task_counter
                    suggested = min(max_uses, max_affordable, MAX_SUGGESTED_USES_PER_ALERT)
                    self.active_task_id = new_id
                    self.active_type = conv_type
                    self.active_uses = suggested
                    self.active_since_ts = now
                    self.baseline_pos = cur_tuple
                    return ConverterSignal(
                        task_id=new_id,
                        phase="NEW_ALERT",
                        signal_type=conv_type,
                        uses=suggested,
                        total_shares=suggested * lot,
                        required_usd=suggested * config.FX_CONVERTER_USD_REQ,
                        headline=f"[✓ #{done_id} {done_type} 已兑现] -> 【新增 #{new_id}】请再操作 {conv_type} ({suggested} 次)",
                        detail=self._build_action_detail(conv_type, suggested, usd_cash, max_uses),
                        reason=f"#{done_id} 已生效；仍有 {max_uses} 手可转，新增建议 {suggested} 次",
                    )

                return ConverterSignal(
                    task_id=done_id,
                    phase="FULFILLED_CHECK",
                    signal_type=None,
                    headline=f"[✓ #{done_id} {done_type} 仓位已抵消/兑现完成] 无需重复点击",
                    detail="已检测到对应持仓变化，当前无新增 Converter 转换任务。",
                    reason=f"任务 #{done_id} 已完成（2-Tick 冷却中）",
                )

            if (now - self.active_since_ts) < STICKY_SECONDS:
                return ConverterSignal(
                    task_id=self.active_task_id,
                    phase="ACTIVE_HOLD",
                    signal_type=None,
                    uses=self.active_uses,
                    total_shares=self.active_uses * lot,
                    required_usd=self.active_uses * config.FX_CONVERTER_USD_REQ,
                    headline=f"[锁定挂起 #{self.active_task_id} - 等待仓位变化] 点击 {self.active_type} -> [Use] {self.active_uses} 次",
                    detail=self._build_action_detail(self.active_type, self.active_uses, usd_cash, max_uses or self.active_uses),
                    reason=f"任务 #{self.active_task_id} 驻留中（剩余 {max(0.0, STICKY_SECONDS - (now - self.active_since_ts)):.1f}s）",
                )

            self.active_type = None
            self.baseline_pos = None

        # 2. 分层评估新机会：小仓位统计套利默认不走 Converter，节省 $1,500/次 转换费
        if conv_type is None or max_uses <= 0:
            return ConverterSignal(
                phase="IDLE",
                reason="当前无满 10,000 股双向篮子可转（小仓位套利建议直接持有或平仓，省转换费）",
            )

        gross_high = snapshot.gross_ratio >= config.CONVERTER_GROSS_TRIGGER
        batch_large = max_uses >= config.CONVERTER_MIN_BATCH_USES
        walk_ritc = self.ob.walk_book(config.RITC, unwind_side, min(max_uses * lot, config.MAX_ORDER_SIZE[config.RITC]))
        slippage_high = (not walk_ritc.sufficient_depth) or (
            walk_ritc.slippage_per_share > config.CONVERTER_SLIPPAGE_TRIGGER_USD
        )

        if not (batch_large or gross_high or slippage_high):
            return ConverterSignal(
                phase="IDLE",
                reason=f"有 {max_uses} 手可转，但未达高价值触发线（Gross {snapshot.gross_ratio:.1%} < {config.CONVERTER_GROSS_TRIGGER:.0%}，且 < {config.CONVERTER_MIN_BATCH_USES} 手）",
            )

        if max_affordable <= 0:
            return ConverterSignal(
                phase="IDLE",
                reason=f"满足 {conv_type} ({max_uses} 手)，但 USD 现金 (${usd_cash:,.0f}) 不足 $1,500，等待 FX 补足 USD",
            )

        if tick is not None and int(tick) <= self.cooldown_until_tick:
            return ConverterSignal(
                phase="IDLE",
                reason=f"2-Tick 冷却锁中（至 Tick {self.cooldown_until_tick}）",
            )

        suggested_uses = min(max_uses, max_affordable, MAX_SUGGESTED_USES_PER_ALERT)
        self.task_counter += 1
        tid = self.task_counter
        self.active_task_id = tid
        self.active_type = conv_type
        self.active_uses = suggested_uses
        self.active_since_ts = now
        self.baseline_pos = cur_tuple

        return ConverterSignal(
            task_id=tid,
            phase="NEW_ALERT",
            signal_type=conv_type,
            uses=suggested_uses,
            total_shares=suggested_uses * lot,
            required_usd=suggested_uses * config.FX_CONVERTER_USD_REQ,
            headline=f"【新增 #{tid}】立刻点击 {conv_type} -> 蓝色 [Use] 共 {suggested_uses} 次",
            detail=self._build_action_detail(conv_type, suggested_uses, usd_cash, max_uses),
            reason=f"高价值/高占用触发（{max_uses} 手可用，Gross={snapshot.gross_ratio:.1%}）",
        )

    @staticmethod
    def _build_action_detail(conv_type: str, uses: int, usd_cash: float, total_possible_uses: int) -> str:
        shares = uses * config.CONVERTER_LOT_SIZE
        need_usd = uses * config.FX_CONVERTER_USD_REQ
        if conv_type == "ETF-Redemption":
            base = f"填 RITC=10000, USD=1500 -> 点蓝色 Use {uses} 次（平 {shares:,} 股，需 ${need_usd:,.0f} USD；当前 USD=${usd_cash:,.0f}）"
        else:
            base = f"填 BULL=10000, BEAR=10000, USD=1500 -> 点蓝色 Use {uses} 次（平 {shares:,} 股，需 ${need_usd:,.0f} USD；当前 USD=${usd_cash:,.0f}）"
        if total_possible_uses > uses:
            base += f" [账户共 {total_possible_uses} 手可转，建议分批执行]"
        return base

    def trigger_alert(self, tick: Optional[int], sig: ConverterSignal) -> None:
        if sig.phase != "NEW_ALERT" or not sig.signal_type:
            return
        if tick is not None:
            self.cooldown_until_tick = max(self.cooldown_until_tick, int(tick) + config.CONVERTER_COOLDOWN_TICKS)

        logger.warning("CONVERTER 新任务 #%d | Tick=%s | %s | %s", sig.task_id, tick, sig.headline, sig.detail)

        if config.PLAY_SOUND_ALERT:
            if platform.system() == "Darwin":
                os.system("afplay /System/Library/Sounds/Glass.aiff &")
            else:
                sys.stdout.write("\a")
                sys.stdout.flush()

    @staticmethod
    def _render_bar(ratio: float, width: int = 20) -> str:
        clamped = max(0.0, min(1.0, ratio))
        filled = int(round(clamped * width))
        return "[" + "#" * filled + "-" * (width - filled) + f"] {ratio * 100:5.1f}%"

    def render_dashboard(
        self,
        tick: Optional[int],
        status: Optional[str],
        snapshot: PortfolioSnapshot,
        arb_info: str,
        fx_info: str,
        tenders_info: List[str],
        converter_sig: ConverterSignal,
    ) -> None:
        gross_bar = self._render_bar(snapshot.gross_ratio)
        net_ratio = (
            snapshot.net / config.MAX_LONG_NET
            if snapshot.net >= 0
            else snapshot.net / abs(config.MAX_SHORT_NET)
        )
        net_bar = self._render_bar(abs(net_ratio))

        pos = snapshot.positions
        lines: List[str] = [
            "\033[2J\033[H" + "=" * 88,
            f" RITCx ALGORITHMIC ETF ARBITRAGE MONITOR | Tick: {str(tick):>4} | Status: {str(status):<8}",
            "=" * 88,
            f" Gross : {gross_bar}  ({snapshot.gross:>7,} / {config.MAX_GROSS_LIMIT:,})",
            f" Net   : {net_bar}  ({snapshot.net:>+7,} | L:{config.MAX_LONG_NET:,} / S:{config.MAX_SHORT_NET:,})",
            "-" * 88,
            f" Positions | BULL: {pos.get(config.BULL, 0):>+8,} | BEAR: {pos.get(config.BEAR, 0):>+8,} | RITC: {pos.get(config.RITC, 0):>+8,} | USD: {pos.get(config.USD, 0):>+9,}",
            f" Arb       | {arb_info}",
            f" FX        | {fx_info}",
            f" Tenders   | {', '.join(tenders_info) if tenders_info else 'None'}",
            "-" * 88,
        ]

        if converter_sig.phase == "NEW_ALERT":
            lines.append(f"\033[1;31;43m >>> {converter_sig.headline} <<< \033[0m")
            lines.append(f"\033[1;33m     {converter_sig.detail} \033[0m")
        elif converter_sig.phase == "ACTIVE_HOLD":
            lines.append(f"\033[1;33m >>> {converter_sig.headline} <<< \033[0m")
            lines.append(f"     {converter_sig.detail} ({converter_sig.reason})")
        elif converter_sig.phase == "FULFILLED_CHECK":
            lines.append(f"\033[1;32m >>> {converter_sig.headline} <<< \033[0m")
            lines.append(f"     {converter_sig.detail}")
        else:
            lines.append(f" Converter | {converter_sig.reason}")

        lines.append("=" * 88)
        lines.append(" 提示: 仅高占用/大篮子需转换时提示；无提示时不要手动点 Converter（每次 $1,500 USD 成本）。")

        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
"""
RIT DMA REST API 客户端封装模块
- 全局复用 requests.Session()
- 支持 DMA Basic 认证（USERNAME / PASSWORD）
- 严格设置超时与异常捕获，防止单次网络抖动导致主循环崩溃
- 自动处理 HTTP 429 限流退避
"""

import base64
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

import config

logger = logging.getLogger("ritcx.api")


class RitApiClient:
    """RIT DMA REST API 封装类。"""

    def __init__(
        self,
        base_url: str = config.API_BASE_URL,
        username: str = getattr(config, "USERNAME", ""),
        password: str = getattr(config, "PASSWORD", ""),
        timeout: float = config.REQUEST_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Basic {token}",
                "Accept": "application/json",
            }
        )

    def close(self) -> None:
        """关闭底层 HTTP Session。"""
        try:
            self.session.close()
        except Exception as exc:
            logger.warning("关闭 HTTP Session 时出现异常: %s", exc)

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        log_detail_on_error: bool = False,
        ignore_404: bool = False,
    ) -> Optional[Any]:
        """底层统一请求方法，内置超时保护、异常处理与 429 退避。"""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        retries = 0

        while True:
            try:
                resp = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    data=data,
                    timeout=self.timeout,
                )
            except requests.Timeout:
                logger.warning("API 请求超时 [%s %s] (timeout=%.2fs)", method, endpoint, self.timeout)
                return None
            except requests.RequestException as exc:
                logger.warning("API 网络异常 [%s %s]: %s", method, endpoint, exc)
                return None

            if resp.status_code == 429:
                retries += 1
                if retries > config.MAX_429_RETRIES:
                    logger.error(
                        "API 触发 429 限流且达到最大重试次数 (%d) [%s]",
                        config.MAX_429_RETRIES,
                        endpoint,
                    )
                    return None
                wait_sec = self._parse_retry_after(resp)
                logger.warning(
                    "API 触发 429 限流 [%s]，第 %d/%d 次等待 %.2fs",
                    endpoint,
                    retries,
                    config.MAX_429_RETRIES,
                    wait_sec,
                )
                time.sleep(wait_sec)
                continue

            if resp.status_code == 401:
                logger.error("API 认证失败 (HTTP 401) [%s %s]，请检查 USERNAME 和 PASSWORD", method, endpoint)
                return None

            if resp.status_code == 404 and ignore_404:
                logger.debug("撤单目标已成交或不存在 (HTTP 404) [%s %s]", method, endpoint)
                return {}

            if not resp.ok:
                if log_detail_on_error:
                    logger.error(
                        "API 请求失败 [%s %s] HTTP %d | full_url=%s | params=%s | body=%s",
                        method,
                        endpoint,
                        resp.status_code,
                        resp.url,
                        params,
                        resp.text,
                    )
                else:
                    logger.error(
                        "API 请求失败 [%s %s] HTTP %d: %s",
                        method,
                        endpoint,
                        resp.status_code,
                        resp.text[:300],
                    )
                return None

            if not resp.text or not resp.text.strip():
                return {}

            try:
                return resp.json()
            except ValueError as exc:
                logger.error("API 响应 JSON 解析失败 [%s %s] url=%s: %s", method, endpoint, resp.url, exc)
                return None

    @staticmethod
    def _parse_retry_after(resp: requests.Response) -> float:
        """解析 429 响应的等待时间。"""
        header_val = resp.headers.get("Retry-After")
        if header_val:
            try:
                return max(0.05, float(header_val))
            except ValueError:
                pass
        try:
            body = resp.json()
            if isinstance(body, dict) and "wait" in body:
                return max(0.05, float(body["wait"]))
        except Exception:
            pass
        return config.DEFAULT_429_WAIT

    # ==================== 常用业务接口封装 ====================

    def get_case(self) -> Optional[Dict[str, Any]]:
        """获取当前 Case 状态（包含 tick、status 等）。"""
        res = self._request("GET", "case")
        return res if isinstance(res, dict) else None

    def get_tick_status(self) -> Tuple[Optional[int], Optional[str]]:
        """返回 (tick, status)，失败时返回 (None, None)。"""
        case_data = self.get_case()
        if not case_data:
            return None, None
        tick = case_data.get("tick")
        status = case_data.get("status")
        return (int(tick) if tick is not None else None), (str(status) if status is not None else None)

    def get_securities(self, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取证券与货币列表及持仓/现金信息。"""
        params = {"ticker": ticker} if ticker else None
        res = self._request("GET", "securities", params=params)
        if isinstance(res, list):
            return res
        if isinstance(res, dict) and ticker:
            return [res]
        return []

    def get_order_book(self, ticker: str, limit: int = 20) -> Optional[Dict[str, Any]]:
        """获取指定标的的订单簿深度（bids / asks）。"""
        res = self._request("GET", "securities/book", params={"ticker": ticker, "limit": limit})
        return res if isinstance(res, dict) else None

    def get_tenders(self) -> List[Dict[str, Any]]:
        """获取当前活跃的 Tender Offers 列表。"""
        res = self._request("GET", "tenders")
        return res if isinstance(res, list) else []

    def accept_tender(self, tender_id: int, price: Optional[float] = None) -> bool:
        """接受 Tender Offer：将合法数值 price 放入 URL Query 参数 params 中调用 POST /v1/tenders/{tender_id}。"""
        if price is None:
            logger.error("接受 Tender #%s 失败：缺少合法 price 参数（当前为 None）", tender_id)
            return False

        try:
            px_val = float(price)
        except (TypeError, ValueError):
            logger.error("接受 Tender #%s 失败：price 非法数值 (%r)", tender_id, price)
            return False

        if not math.isfinite(px_val) or px_val <= 0:
            logger.error("接受 Tender #%s 失败：price 必须为正有限数 (%r)", tender_id, price)
            return False

        query_params: Dict[str, Any] = {"price": round(px_val, 4)}
        res = self._request("POST", f"tenders/{tender_id}", params=query_params, log_detail_on_error=True)
        if res is not None:
            logger.info("已接受 Tender Offer #%s | params=%s", tender_id, query_params)
            return True

        logger.warning("接受 Tender Offer #%s 失败 | params=%s", tender_id, query_params)
        return False

    def decline_tender(self, tender_id: int) -> bool:
        """拒绝 Tender Offer。"""
        res = self._request("DELETE", f"tenders/{tender_id}")
        if res is not None:
            logger.info("已拒绝 Tender Offer #%s", tender_id)
            return True
        return False

    def place_order(
        self,
        ticker: str,
        action: str,
        quantity: int,
        order_type: str = "MARKET",
        price: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """下单接口（支持 MARKET / LIMIT）。"""
        qty = int(quantity)
        if qty <= 0:
            logger.warning("忽略无效下单数量: ticker=%s action=%s qty=%d", ticker, action, qty)
            return None

        params: Dict[str, Any] = {
            "ticker": ticker,
            "action": action.upper(),
            "quantity": qty,
            "type": order_type.upper(),
        }
        if order_type.upper() == "LIMIT":
            if price is None:
                logger.error("LIMIT 订单缺少 price 参数: ticker=%s", ticker)
                return None
            params["price"] = round(float(price), 4)

        res = self._request("POST", "orders", params=params)
        if res is not None:
            logger.info(
                "下单成功 | ticker=%s action=%s type=%s qty=%d price=%s order_id=%s",
                ticker,
                action.upper(),
                order_type.upper(),
                qty,
                params.get("price", "MKT"),
                res.get("order_id") if isinstance(res, dict) else "N/A",
            )
        return res if isinstance(res, dict) else ({} if res is not None else None)

    def cancel_order(self, order_id: int) -> bool:
        """按 order_id 撤销挂单；若返回 404 说明挂单已完全成交，按 DEBUG 忽略。"""
        res = self._request("DELETE", f"orders/{order_id}", ignore_404=True)
        if res is not None:
            logger.debug("撤单请求已处理 | order_id=%s", order_id)
            return True
        return False

    def get_open_orders(self, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取当前未成交挂单（OPEN）。"""
        params = {"status": "OPEN"}
        if ticker:
            params["ticker"] = ticker
        res = self._request("GET", "orders", params=params)
        return res if isinstance(res, list) else []

    def get_limits(self) -> List[Dict[str, Any]]:
        """获取账户风控限额信息（/limits）。"""
        res = self._request("GET", "limits")
        return res if isinstance(res, list) else []
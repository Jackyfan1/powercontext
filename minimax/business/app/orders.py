"""订单查询服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

VALID_STATUSES = frozenset({"paid", "pending", "cancelled"})


@dataclass(frozen=True)
class Order:
    order_id: str
    status: str
    amount: float
    created_at: str


def list_orders(
    orders: Iterable[Order],
    *,
    status: Optional[str] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> Sequence[Order]:
    """按状态与金额区间筛选订单，按创建时间倒序返回。"""
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"unknown status: {status}")

    result = list(orders)
    if status is not None:
        result = [order for order in result if order.status == status]
    if min_amount is not None:
        result = [order for order in result if order.amount >= min_amount]
    if max_amount is not None:
        result = [order for order in result if order.amount <= max_amount]

    result.sort(key=lambda order: order.created_at, reverse=True)
    if offset:
        result = result[offset:]
    if limit is not None:
        result = result[:limit]
    return result

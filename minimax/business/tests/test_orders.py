"""订单查询服务测试：target 为目标功能测试，regression 为原有行为回归测试。"""

from __future__ import annotations

import pytest

from app.orders import Order, list_orders


@pytest.fixture()
def orders() -> list[Order]:
    return [
        Order("A-1001", "paid", 100.0, "2026-01-01T10:00:00"),
        Order("A-1002", "paid", 150.0, "2026-01-02T10:00:00"),
        Order("A-1003", "pending", 200.0, "2026-01-03T10:00:00"),
        Order("A-1004", "paid", 260.0, "2026-01-04T10:00:00"),
        Order("A-1005", "cancelled", 80.0, "2026-01-05T10:00:00"),
    ]


@pytest.mark.target
def test_max_amount_filters_upper_bound(orders: list[Order]) -> None:
    """目标功能：支持 max_amount 上限筛选（闭区间）。"""
    result = list_orders(orders, min_amount=100.0, max_amount=200.0)
    assert [order.order_id for order in result] == ["A-1003", "A-1002", "A-1001"]


@pytest.mark.target
def test_min_amount_includes_boundary(orders: list[Order]) -> None:
    """目标功能：min_amount 边界为闭区间，等于下限的订单必须保留。"""
    result = list_orders(orders, status="paid", min_amount=150.0)
    assert [order.order_id for order in result] == ["A-1004", "A-1002"]


@pytest.mark.regression
def test_status_filter_only_paid(orders: list[Order]) -> None:
    result = list_orders(orders, status="paid")
    assert {order.status for order in result} == {"paid"}
    assert len(result) == 3


@pytest.mark.regression
def test_sorted_by_created_at_desc(orders: list[Order]) -> None:
    result = list_orders(orders)
    created = [order.created_at for order in result]
    assert created == sorted(created, reverse=True)


@pytest.mark.regression
def test_pagination_limit_offset(orders: list[Order]) -> None:
    result = list_orders(orders, limit=2, offset=1)
    assert [order.order_id for order in result] == ["A-1004", "A-1003"]


@pytest.mark.regression
def test_unknown_status_raises(orders: list[Order]) -> None:
    with pytest.raises(ValueError):
        list_orders(orders, status="refunded")


@pytest.mark.regression
def test_without_filters_returns_all(orders: list[Order]) -> None:
    assert len(list_orders(orders)) == len(orders)

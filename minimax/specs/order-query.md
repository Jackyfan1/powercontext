# 订单查询服务规格

## S1 筛选维度

订单查询必须支持 `status`、`min_amount`、`max_amount` 三个筛选条件，三者可组合使用。

## S2 金额边界

金额筛选为闭区间：`amount >= min_amount` 且 `amount <= max_amount`，等于边界的订单必须保留。

## S3 非法状态

`status` 取值必须是 `paid` / `pending` / `cancelled` 之一，其他取值必须抛出 `ValueError`。

## S4 排序与分页

结果按 `created_at` 倒序排序；先应用 `offset` 再应用 `limit`。

## S5 兼容性

不传任何筛选条件时必须返回全部订单，既有调用方的行为不得改变。

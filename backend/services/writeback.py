"""飞书写回队列（接口桩：订单来源模块会替换本文件的实现，签名不变）。

采购结果变化的地方（插件回报成功、人工核实已下单）调用 enqueue_for_order；
它判断该任务是否有飞书写回目标、连接是否启用写回、结果是否为模拟，再决定入队。
"""

from __future__ import annotations


def enqueue_for_order(conn, workspace_id: int, order_id: int) -> int | None:
    """输入：工作区、任务 id → 输出：新入队的写回 id；不需要写回时返回 None。"""
    return None

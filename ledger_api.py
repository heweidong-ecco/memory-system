# 创建 Ledger 查询模块
#!/usr/bin/env python3
"""Ledger 不可变账本——查询与回溯接口"""

import json
from typing import Optional, List, Dict, Any
import psycopg2
from psycopg2.extras import RealDictCursor

# ==================== 配置 ====================
PG_CONN = {
    "dbname": "memory_system",
    "user": "memory_user",
    "password": "memory_pass_2026",
    "host": "localhost",
    "port": 5432,
}


# ==================== 数据库连接 ====================
# 更新使用连接池 连接。
from db_pool import get_cursor
'''def _get_conn():
    """创建数据库连接"""
    return psycopg2.connect(**PG_CONN, cursor_factory=RealDictCursor)
'''


def _to_dict(value):
    """将 JSONB 字段统一转为 dict（psycopg2 有时返回 str）"""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


# ==================== 核心查询接口 ====================

def get_entity_history(
    user_id: str,          # 新增参数
    entity_id: str,
    limit: int = 20,
    ascending: bool = False,
) -> List[Dict[str, Any]]:
    """
    获取某个实体的完整事件历史，按时间排序。

    参数:
    - user_id: 用户 ID
    - entity_id: 实体 ID，如 user_123
    - limit: 返回条数，默认 20
    - ascending: 是否按时间正序（默认 False，即倒序——最新的在前）

    返回: 事件列表，每项包含所有 Ledger 字段
    """
    order = "ASC" if ascending else "DESC"
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT ledger_id, event_type, entity_id, event_data,
                   old_value, new_value, timestamp, source_agent,
                       session_id, policy_version
                FROM ledger
                WHERE user_id = %s AND entity_id = %s
                ORDER BY timestamp {order}
                LIMIT %s
                """,
                (user_id, entity_id, limit),
            )
        rows = cur.fetchall()

    for row in rows:
        row["event_data"] = _to_dict(row.get("event_data"))
        row["old_value"] = _to_dict(row.get("old_value"))
        row["new_value"] = _to_dict(row.get("new_value"))
        row["timestamp"] = str(row.get("timestamp"))
    return rows


def get_events_by_type(
    user_id: str,
    entity_id: str,
    event_type: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    按事件类型过滤某个实体的历史记录。

    参数:
    - user_id: 用户 ID
    - entity_id: 实体 ID
    - event_type: state_change / tool_call / skill_call / user_input / rag_retrieval
    - limit: 返回条数

    返回: 指定类型的事件列表
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT ledger_id, event_type, entity_id, event_data,
                   old_value, new_value, timestamp, source_agent,
                       session_id, policy_version
                FROM ledger
                WHERE user_id = %s AND entity_id = %s AND event_type = %s
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (user_id, entity_id, event_type, limit),
            )
        rows = cur.fetchall()

    for row in rows:
        row["event_data"] = _to_dict(row.get("event_data"))
        row["old_value"] = _to_dict(row.get("old_value"))
        row["new_value"] = _to_dict(row.get("new_value"))
        row["timestamp"] = str(row.get("timestamp"))
    return rows


def get_tool_calls(user_id: str, entity_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """获取某个实体的所有工具调用记录"""
    return get_events_by_type(user_id, entity_id, "tool_call", limit)


def get_skill_calls(user_id: str, entity_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """获取某个实体的所有技能调用记录"""
    return get_events_by_type(user_id, entity_id, "skill_call", limit)


def get_state_changes(user_id: str, entity_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """获取某个实体的所有状态变更记录"""
    return get_events_by_type(user_id, entity_id, "state_change", limit)


def get_events_by_session(user_id: str, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    获取某个会话的全部事件（按时间正序）。

    用于完整还原一次会话中 Agent 做了什么。
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT ledger_id, event_type, entity_id, event_data,
                   old_value, new_value, timestamp, source_agent,
                   session_id, policy_version
            FROM ledger
            WHERE user_id = %s AND session_id = %s
            ORDER BY timestamp ASC
            LIMIT %s
                """,
                (user_id, session_id, limit),
            )
        rows = cur.fetchall()

    for row in rows:
        row["event_data"] = _to_dict(row.get("event_data"))
        row["old_value"] = _to_dict(row.get("old_value"))
        row["new_value"] = _to_dict(row.get("new_value"))
        row["timestamp"] = str(row.get("timestamp"))
    return rows


def get_current_state(user_id: str, entity_id: str, field: str) -> Optional[Dict[str, Any]]:
    """
    获取某个实体的某个字段的“当前状态”。

    实现方式：查询该字段最近一次 state_change 的 new_value。
    这是 Ledger 回溯的核心价值——从时间序列中取最新值，保证 100% 正确。
    """
    with get_cursor() as cur:
            cur.execute(
                """
                SELECT new_value
                FROM ledger
                WHERE user_id = %s
                  AND entity_id = %s
                  AND event_type = 'state_change'
                  AND event_data->>'field' = %s
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (user_id, entity_id, field),
            )
            row = cur.fetchone()

    if not row:
        return None
    return _to_dict(row.get("new_value"))

# 新增 按时间范围查询事件
"""实现一个 get_events_by_time_range(entity_id, start_time, end_time) 接口，
支持按时间范围查询事件。
用杭州 → 成都 → 北京的事件链测试，只查询中间某段时间的记录。
"""
def get_events_by_time_range(
    user_id: str,
    entity_id: str,
    start_time: str,
    end_time: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    按时间范围查询某个实体的事件。

    参数:
    - user_id: 用户 ID
    - entity_id: 实体 ID
    - start_time: 起始时间（ISO 格式，如 '2026-08-13 09:00:00+00:00'）
    - end_time: 结束时间（ISO 格式）
    - limit: 返回条数

    返回: 时间范围内的所有事件，按时间正序
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT ledger_id, event_type, entity_id, event_data,
                   old_value, new_value, timestamp, source_agent,
                   session_id, policy_version
            FROM ledger
            WHERE user_id = %s
              AND entity_id = %s
              AND timestamp >= %s
              AND timestamp <= %s
                ORDER BY timestamp ASC
                LIMIT %s
                """,
                (user_id, entity_id, start_time, end_time, limit),
            )
        rows = cur.fetchall()

    for row in rows:
        row["event_data"] = _to_dict(row.get("event_data"))
        row["old_value"] = _to_dict(row.get("old_value"))
        row["new_value"] = _to_dict(row.get("new_value"))
        row["timestamp"] = str(row.get("timestamp"))
    return rows


# 在 main() 中添加测试
def test_time_range(user_id: str = "user_default"):
    """测试时间范围查询——只查询杭州→成都→北京链中间某段时间"""
    print("\n[选做测试] 时间范围查询")
    events = get_events_by_time_range(
        user_id,
        "user_123",
        start_time="2026-08-13 07:00:00+00:00",
        end_time="2026-08-13 08:00:00+00:00",
    )
    for event in events:
        print(f"  {event['timestamp']} | {event['event_type']}")

# ==================== 测试 ====================

def main():
    """测试 Ledger 查询接口"""
    print("=" * 60)
    print("  Ledger 不可变账本查询接口测试")
    print("=" * 60)

    user_id = "user_default"
    entity_id = "user_123"

    # 测试 1：按实体回溯完整历史
    print("\n[测试1] 按实体回溯完整历史（倒序）")
    history = get_entity_history(user_id, entity_id, limit=10)
    for event in history:
        print(f"  {event['timestamp']} | {event['event_type']}")

    # 测试 2：杭州 → 成都 → 北京 的位置回溯
    print("\n[测试2] 状态回溯：当前位置")
    print(f"  当前状态: {get_current_state(user_id, entity_id, 'location')}")

    # 测试 3：工具调用记录
    print("\n[测试3] 工具调用记录")
    tool_calls = get_tool_calls(user_id, entity_id)
    for call in tool_calls:
        data = call.get("event_data", {})
        print(f"  {call['timestamp']} | {data.get('tool_name')} | {data.get('status')}")
        print(f"    输入: {data.get('input', '')[:50]}")
        print(f"    输出: {data.get('output', '')[:50]}")

    # 测试 4：技能调用记录
    print("\n[测试4] 技能调用记录")
    skill_calls = get_skill_calls(user_id, entity_id)
    for call in skill_calls:
        data = call.get("event_data", {})
        print(f"  {call['timestamp']} | {data.get('skill_name')} | {data.get('status')}")

    # 测试 5：按会话回溯
    print("\n[测试5] 按会话回溯")
    session_events = get_events_by_session(user_id, "session_20260923_001")
    for event in session_events:
        print(f"  {event['timestamp']} | {event['event_type']} | {event['source_agent']}")

    print("\n✅ Ledger 查询接口验证完成")

    print("开始执行测试...")
    # 在这里调用你的测试函数
    test_time_range()
    # 如果还有其他测试或主逻辑，也在这里调用
    # test_other_function()


if __name__ == "__main__":
    main()
# 创建统一调用入口 
#!/usr/bin/env python3
"""统一调用入口——对外只暴露两个接口：record_event 和 unified_search"""

from typing import Dict, Any, List, Optional

from event_bus import EventBus
from retrieval_orchestrator import unified_search


def record_event(
    user_id: str,
    event_type: str,
    entity_id: str,
    event_data: Dict[str, Any],
    old_value: Optional[Dict[str, Any]] = None,
    new_value: Optional[Dict[str, Any]] = None,
    source_agent: Optional[str] = None,
    session_id: Optional[str] = None,
    policy_version: Optional[str] = None,
) -> int:
    """
    统一的写入入口。所有事件都必须经过此函数。
    user_id 会被写入 Ledger 的 user_id 字段，实现多租户隔离。
    """
    bus = EventBus()
    # 注意：event_bus.record_event 需要增加 user_id 参数
    # 这是第 16 天改造的一部分
    ledger_id = bus.record_event(
        user_id=user_id,
        event_type=event_type,
        entity_id=entity_id,
        event_data=event_data,
        old_value=old_value,
        new_value=new_value,
        source_agent=source_agent,
        session_id=session_id,
        policy_version=policy_version,
    )
    bus.close()
    return ledger_id


def query_memory(
    user_id: str,
    query: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    统一的读取入口。所有查询都必须经过此函数。
    user_id 会被传递到每一层，实现多租户隔离。
    """
    # entity_id 与 user_id 同为当前用户（本系统以用户档案为检索实体）
    return unified_search(entity_id=user_id, query=query, session_id=session_id, user_id=user_id)
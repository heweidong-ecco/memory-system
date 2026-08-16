# 创建事件流总线模块
'''
所有的工具调用、Skills 调用、状态变更，都只需要调用这一个函数。函数内部自动完成：
写入 Redis 热缓存（快，供高频读取）
写入 PostgreSQL ledger 表（持久，供回溯审计）
更新 summary 表中的相关字段（后续第 8 天再做）
一句话：事件流总线是“所有事件进系统的唯一入口”。
'''
#!/usr/bin/env python3
"""事件流总线——统一的事件入口，自动分发到 Redis 和 PostgreSQL"""

#!/usr/bin/env python3
"""事件流总线——统一的事件入口，自动分发到 Redis 和 PostgreSQL"""

import json
from datetime import datetime
from typing import Optional, Dict, Any

import redis
import psycopg2
from psycopg2.extras import Json

from cache_manager import (
    cache_set, tool_key, skill_key, kv_key, sliding_key,
    TTL_KV, TTL_SUMMARY,
    redis_client,
)

# ==================== 配置 ====================
REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_DB = 0

PG_HOST = "localhost"
PG_PORT = 5432
PG_DB = "memory_system"
PG_USER = "memory_user"
PG_PASSWORD = "memory_pass_2026"


# ==================== 事件流总线 ====================
class EventBus:
    """统一的事件流总线，所有工具调用、Skills调用、状态变更都从这里进入"""

    def __init__(self):
        self.redis_client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True
        )
        self.pg_conn = psycopg2.connect(
            host=PG_HOST,
            port=PG_PORT,
            dbname=PG_DB,
            user=PG_USER,
            password=PG_PASSWORD,
        )
        self.pg_conn.autocommit = True
        self.cursor = self.pg_conn.cursor()

    def record_event(
        self,
        event_type: str,
        entity_id: str,
        event_data: Dict[str, Any],
        old_value: Optional[Dict[str, Any]] = None,
        new_value: Optional[Dict[str, Any]] = None,
        source_agent: Optional[str] = None,
        session_id: Optional[str] = None,
        policy_version: Optional[str] = None,
        user_id: str = "user_default",
    ) -> int:
        """
        统一的事件记录入口。
        所有工具调用、Skills调用、状态变更都通过这里进入系统。

        参数:
        - event_type: 事件类型 (tool_call / skill_call / state_change)
        - entity_id: 实体 ID (如 user_123, session_456)
        - event_data: 事件完整数据
        - old_value: 状态变更前的值 (仅 state_change 使用)
        - new_value: 状态变更后的值 (仅 state_change 使用)
        - source_agent: 来源 Agent 名称
        - session_id: 会话 ID，用于关联同一次会话的所有事件
        - user_id: 用户 ID，用于多用户隔离，默认 user_default

        返回:
        - ledger_id: 新插入的 Ledger 记录 ID
        """
        # 1. 写入 PostgreSQL Ledger (持久化账本)
        ledger_id = self._append_to_ledger(
            event_type=event_type,
            entity_id=entity_id,
            event_data=event_data,
            old_value=old_value,
            new_value=new_value,
            source_agent=source_agent,
            session_id=session_id,
            policy_version=policy_version,
            user_id=user_id,
        )

        # 2. 更新 Redis 热缓存 (按事件类型分发)
        self._update_redis_cache(
            event_type=event_type,
            entity_id=entity_id,
            event_data=event_data,
            new_value=new_value,
            user_id=user_id,
        )

        # 3. 对成功的 tool_call/skill_call，写入候选向量库
        if event_type in ("tool_call", "skill_call"):
            status = event_data.get("status", "")
            if status == "success":
                output_text = event_data.get("output", "")
                query = self._get_query_from_session(user_id, session_id)
                if query and output_text:
                    from skill_candidate_store import add_candidate_vector
                    add_candidate_vector(
                        entity_id=entity_id,
                        session_id=session_id,
                        ledger_id=ledger_id,
                        query=query,
                        success_text=output_text,
                        user_id=user_id,
                    )

        return ledger_id

    def _append_to_ledger(
        self,
        event_type: str,
        entity_id: str,
        event_data: Dict[str, Any],
        old_value: Optional[Dict[str, Any]],
        new_value: Optional[Dict[str, Any]],
        source_agent: Optional[str],
        session_id: Optional[str],
        policy_version: Optional[str],
        user_id: str = "user_default",
    ) -> int:
        """写入 PostgreSQL ledger 表（Append-Only）"""
        sql = """
        INSERT INTO ledger (
            user_id, event_type, entity_id, event_data, old_value, new_value,
            source_agent, session_id, policy_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING ledger_id
        """
        self.cursor.execute(
            sql,
            (
                user_id,
                event_type,
                entity_id,
                Json(event_data),
                Json(old_value) if old_value else None,
                Json(new_value) if new_value else None,
                source_agent,
                session_id,
                policy_version,
            ),
        )
        ledger_id = self.cursor.fetchone()[0]
        return ledger_id
    # ==================== 更新 Redis 热缓存 ====================
    # 统一 Redis 键格式 ,使用统一的键命名
    '''
    def _update_redis_cache(
        self,
        event_type: str,
        entity_id: str,
        event_data: Dict[str, Any],
        new_value: Optional[Dict[str, Any]] = None,
    ):
        """
        根据事件类型更新 Redis 热缓存。new_value 作为独立参数传入，修复未定义问题。
        更新 Redis 缓存——使用统一键命名
        """
        if event_type == "tool_call":
            # 缓存工具调用的最后结果摘要
            tool_name = event_data.get("tool_name", "unknown")
            key = f"tool:{entity_id}:{tool_name}:last_result"
            self.redis_client.set(
                key,
                json.dumps(event_data, ensure_ascii=False),
                ex=3600,  # 1 小时 TTL
            )

        elif event_type == "skill_call":
            # 缓存 Skills 调用的最后结果摘要
            skill_name = event_data.get("skill_name", "unknown")
            key = f"skill:{entity_id}:{skill_name}:last_result"
            self.redis_client.set(
                key,
                json.dumps(event_data, ensure_ascii=False),
                ex=3600,  # 1 小时 TTL
            )

        elif event_type == "state_change":
            # 状态变更：更新用户档案的热缓存（位置、偏好等）
            field = event_data.get("field", "unknown")
            if new_value:
                key = f"user_profile:{entity_id}:{field}"
                self.redis_client.set(
                    key,
                    json.dumps(new_value, ensure_ascii=False),
                    ex=86400,  # 24 小时 TTL
                )
    '''
    def _update_redis_cache(self, event_type, entity_id, event_data, new_value=None, user_id="user_default"):
        """更新 Redis 缓存——使用统一键命名（含 user_id 隔离）"""
        if event_type == "tool_call":
            tool_name = event_data.get("tool_name", "unknown")
            key = tool_key(user_id, entity_id, tool_name)
            cache_set(key, event_data, TTL_SUMMARY)  # 工具摘要 1 小时

        elif event_type == "skill_call":
            skill_name = event_data.get("skill_name", "unknown")
            key = skill_key(user_id, entity_id, skill_name)
            cache_set(key, event_data, TTL_SUMMARY)  # 技能摘要 1 小时

        elif event_type == "state_change":
            field = event_data.get("field", "unknown")
            if new_value:
                key = kv_key(user_id, entity_id, field)
                cache_set(key, new_value, TTL_KV)  # 状态 60 秒

        elif event_type == "user_input":
            # 滑动窗口：最近输入列表
            key = sliding_key(user_id, entity_id)
            redis_client.lpush(key, json.dumps(event_data, ensure_ascii=False))
            redis_client.ltrim(key, 0, 9)  # 只保留最近 10 条
            redis_client.expire(key, TTL_SUMMARY)  # 1 小时过期
    
    def _get_query_from_session(self, user_id: str, session_id: str) -> Optional[str]:
        """
        从 Ledger 中查询同一 session_id 下的 user_input 事件，提取原始 query。

        参数:
        - user_id: 用户 ID，用于多用户隔离
        - session_id: 会话 ID

        返回: query 文本，如果不存在则返回 None
        """
        if not session_id:
            return None

        sql = """
        SELECT event_data
        FROM ledger
        WHERE user_id = %s AND session_id = %s AND event_type = 'user_input'
        ORDER BY timestamp ASC
        LIMIT 1
        """
        self.cursor.execute(sql, (user_id, session_id))
        row = self.cursor.fetchone()

        if not row:
            return None

        event_data = row[0] if isinstance(row, tuple) else row.get("event_data")
        if isinstance(event_data, str):
            try:
                event_data = json.loads(event_data)
            except json.JSONDecodeError:
                return None

        return event_data.get("text", "")
    
    def close(self):
        """关闭连接"""
        self.cursor.close()
        self.pg_conn.close()
        self.redis_client.close()


# ==================== 测试 ====================
def main():
    bus = EventBus()

    # 模拟同一次会话中的事件序列
    session_id = "session_20260923_001"
    print(f"会话 ID: {session_id}\n")

    # 测试 1: 状态变更（杭州 → 成都）
    print("测试 1: 状态变更")
    ledger_id_1 = bus.record_event(
        event_type="state_change",
        entity_id="user_123",
        event_data={"field": "location"},
        old_value={"city": "杭州"},
        new_value={"city": "成都"},
        source_agent="travel_agent",
        session_id=session_id,
        user_id="user_default",
    )
    print(f"  Ledger ID: {ledger_id_1}")

    # 测试 2: 工具调用
    print("\n测试 2: 工具调用")
    ledger_id_2 = bus.record_event(
        event_type="tool_call",
        entity_id="user_123",
        event_data={
            "tool_name": "web_search",
            "input": "成都到北京的航班",
            "output": "3 个航班结果",
            "status": "success",
        },
        source_agent="travel_agent",
        session_id=session_id,
        user_id="user_default",
    )
    print(f"  Ledger ID: {ledger_id_2}")

    # 测试 3: Skills 调用
    print("\n测试 3: Skills 调用")
    ledger_id_3 = bus.record_event(
        event_type="skill_call",
        entity_id="user_123",
        event_data={
            "skill_name": "flight_booking",
            "trigger": "用户要求订机票",
            "output": "预订成功",
            "status": "success",
        },
        source_agent="travel_agent",
        session_id=session_id,
        user_id="user_default",
    )
    print(f"  Ledger ID: {ledger_id_3}")

    # 验证 Redis 缓存
    print("\n验证 Redis 缓存:")
    checks = [
        ("tool:user_default:user_123:web_search", "工具调用结果"),
        ("skill:user_default:user_123:flight_booking", "Skills 调用结果"),
        ("kv:user_default:user_123:location", "状态变更缓存"),
    ]
    for key, desc in checks:
        val = bus.redis_client.get(key)
        status = "✅" if val else "❌"
        print(f"  {status} {desc}: {key}")
        if val:
            print(f"      值: {val[:100]}")

    bus.close()
    print("\n✅ 事件流总线验证完成")


if __name__ == "__main__":
    main()
# 创建 KV 读取层模块
# 在 memory-system/ 目录下创建 kv_api.py
#!/usr/bin/env python3
"""KV 精准读取层——user_profile 表的确定性读写接口"""

import json
from typing import Optional, Dict, Any
import psycopg2
from psycopg2.extras import RealDictCursor

from db_pool import get_conn, get_cursor
from cache_manager import (
    cache_get, cache_set, cache_delete,
    kv_key, TTL_KV,
)

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
    """JSONB 字段统一转为 dict"""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


# ==================== 核心接口 ====================
# 加入 Cache-Aside 读取
# KV 精准读取层提供对 user_profile 表的确定性读写接口，支持 Cache-Aside 模式，确保数据一致性和高性能访问。
"""
关键设计：
读取走 Cache-Aside：先 Redis → 未命中 PG → 回写 Redis
写入时同步更新 Redis 缓存
缓存键统一用 kv:{entity_id}:{field}
"""
def get_profile_value(user_id: str, key: str) -> Optional[Dict[str, Any]]:
    """
    KV 精准读取——Cache-Aside 模式 + 用户隔离 ：
    1. 先查 Redis 缓存
    2. 未命中查 PostgreSQL
    精准读取一个 KV 值。
    参数: user_id 例 'user_123', key 例 'user_123:location'
    返回: 值（dict）或 None
    3. 命中 PG 后回写 Redis
    """
    # 拆分 key 为 entity_id 和 field
    # key 格式：user_123:location  → entity_id=user_123, field=location
    parts = key.split(":", 1)
    if len(parts) != 2:
        return None
    entity_id, field = parts
    cache_key = kv_key(user_id, entity_id, field)

    # 第 1 步：查 Redis
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    # 第 2 步：查 PostgreSQL
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE user_profile
            SET access_count = access_count + 1,
                last_accessed = NOW()
            WHERE user_id = %s AND key = %s
            RETURNING value
            """,
            (user_id, key),
        )
        row = cur.fetchone()

    if not row:
        return None

    value = _to_dict(row["value"])

    # 第 3 步：回写 Redis
    cache_set(cache_key, value, TTL_KV)

    return value

def set_profile_value(
    key: str,
    value: Dict[str, Any],
    entity_type: str = "user",
    ttl_seconds: Optional[int] = None,
    user_id: str = "user_default",
) -> int:
    """
    写入或更新一个 KV 值（upsert），同步更新 Redis 缓存，返回新版本号

    参数:
    - key: 唯一键
    - value: 要存储的值（dict）
    - entity_type: 实体类型 user/session/system
    - ttl_seconds: 可选，过期时间（秒）
    - user_id: 用户 ID，用于多用户隔离，默认 user_default

    返回: 新版本号 version
    """
    expires_at = None
    if ttl_seconds is not None:
        with get_cursor() as cur:
            cur.execute("SELECT (NOW() + (%s || ' seconds')::interval) AS expires_at", (ttl_seconds,))
            row = cur.fetchone()
            expires_at = row["expires_at"]

    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_profile (user_id, key, value, entity_type, version, updated_at, expires_at)
            VALUES (%s, %s, %s, %s, 1, NOW(), %s)
            ON CONFLICT (user_id, key)
            DO UPDATE SET
                value = EXCLUDED.value,
                entity_type = EXCLUDED.entity_type,
                version = user_profile.version + 1,
                updated_at = NOW(),
                expires_at = EXCLUDED.expires_at
            RETURNING version
            """,
            (user_id, key, json.dumps(value, ensure_ascii=False), entity_type, expires_at),
        )
        row = cur.fetchone()
        version = row["version"]
    # get_cursor 会自动 commit

    # 更新 Redis 缓存
    # 写入后同步更新 Redis
    parts = key.split(":", 1)
    if len(parts) == 2:
        entity_id, field = parts
        cache_key = kv_key(user_id, entity_id, field)
        cache_set(cache_key, value, ttl_seconds if ttl_seconds else TTL_KV)

    return version

def delete_profile_value(key: str, user_id: str = "user_default") -> bool:
    """删除一个 KV 值。返回是否删除成功（按用户隔离）。"""
    with get_cursor() as cur:
        cur.execute("DELETE FROM user_profile WHERE user_id = %s AND key = %s", (user_id, key))
        return cur.rowcount > 0


def get_profile_meta(key: str, user_id: str = "user_default") -> Optional[Dict[str, Any]]:
    """获取 KV 的完整元数据（版本、访问次数、过期时间等）"""
    with get_cursor() as cur:
            cur.execute(
                """
                SELECT key, value, entity_type, version, updated_at,
                       expires_at, access_count, last_accessed
                FROM user_profile
                WHERE user_id = %s AND key = %s
                """,
                (user_id, key),
            )
            row = cur.fetchone()

    if not row:
        return None

    result = dict(row)
    result["value"] = _to_dict(result.get("value"))
    result["updated_at"] = str(result.get("updated_at"))
    result["expires_at"] = str(result.get("expires_at")) if result.get("expires_at") else None
    result["last_accessed"] = str(result.get("last_accessed")) if result.get("last_accessed") else None
    return result


# ==================== 工具/Skills 摘要写入 ====================

def set_tool_summary(entity_id: str, tool_name: str, summary: Dict[str, Any], user_id: str = "user_default") -> int:
    """
    将工具调用结果摘要写入 KV 层。
    键格式: {entity_id}:tool_summary:{tool_name}
    """
    key = f"{entity_id}:tool_summary:{tool_name}"
    return set_profile_value(key, summary, entity_type="tool", user_id=user_id)


def set_skill_summary(entity_id: str, skill_name: str, summary: Dict[str, Any], user_id: str = "user_default") -> int:
    """
    将 Skills 调用结果摘要写入 KV 层。
    键格式: {entity_id}:skill_summary:{skill_name}
    """
    key = f"{entity_id}:skill_summary:{skill_name}"
    return set_profile_value(key, summary, entity_type="skill", user_id=user_id)


def get_tool_summary(entity_id: str, tool_name: str, user_id: str = "user_default") -> Optional[Dict[str, Any]]:
    """读取工具调用结果摘要"""
    key = f"{entity_id}:tool_summary:{tool_name}"
    return get_profile_value(user_id, key)


def get_skill_summary(entity_id: str, skill_name: str, user_id: str = "user_default") -> Optional[Dict[str, Any]]:
    """读取 Skills 调用结果摘要"""
    key = f"{entity_id}:skill_summary:{skill_name}"
    return get_profile_value(user_id, key)


# ==================== 测试 ====================

def main():
    print("=" * 60)
    print("  KV 精准读取层测试")
    print("=" * 60)

    # 测试 1：KV 基础读写
    print("\n[测试1] KV 基础读写")
    set_profile_value(
        key="user_123:location",
        value={"city": "北京", "updated_source": "manual"},
        entity_type="user",
    )
    location = get_profile_value("user_default", "user_123:location")
    print(f"  读取 location: {location}")

    # 测试 2：多次写入后 version 递增
    print("\n[测试2] 乐观锁版本递增")
    v1 = set_profile_value("user_123:location", {"city": "成都"})
    v2 = set_profile_value("user_123:location", {"city": "北京"})
    meta = get_profile_meta("user_123:location", user_id="user_default")
    print(f"  第一次写入 version: {v1}")
    print(f"  第二次写入 version: {v2}")
    print(f"  当前 version: {meta['version']}")

    # 测试 3：工具摘要写入和读取
    print("\n[测试3] 工具调用结果摘要")
    set_tool_summary(
        "user_123",
        "web_search",
        {
            "last_input": "成都到北京的航班",
            "last_output": "3 个航班结果",
            "last_status": "success",
            "last_called_at": "2026-09-26T10:00:00Z",
        },
    )
    tool_summary = get_tool_summary("user_123", "web_search")
    print(f"  工具摘要: {tool_summary}")

    # 测试 4：Skills 摘要写入和读取
    print("\n[测试4] Skills 调用结果摘要")
    set_skill_summary(
        "user_123",
        "flight_booking",
        {
            "last_input": "订机票",
            "last_output": "预订成功",
            "last_status": "success",
            "last_called_at": "2026-09-26T10:01:00Z",
        },
    )
    skill_summary = get_skill_summary("user_123", "flight_booking")
    print(f"  技能摘要: {skill_summary}")

    # 测试 5：删除
    print("\n[测试5] 删除 KV")
    delete_profile_value("user_123:location", user_id="user_default")
    deleted = get_profile_value("user_default", "user_123:location")
    print(f"  删除后读取: {deleted}")

    print("\n✅ KV 精准读取层验证完成")


if __name__ == "__main__":
    main()
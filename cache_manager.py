# 创建统一缓存管理器
#!/usr/bin/env python3
"""统一缓存管理器——分层 TTL + 键命名规范"""

import json
from typing import Optional, Any
import redis

# Redis 连接
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# ==================== 键命名规范 ====================
# 统一格式：{prefix}:{user_id}:{entity_id}:{sub_key}
# 加入 user_id 防止不同用户操作同一 entity_id 时缓存串数据
# 例如：
#   KV 层: kv:user_default:user_123:location
#   工具摘要: tool:user_default:user_123:web_search
#   技能摘要: skill:user_default:user_123:flight_booking
#   滑动窗口: sliding:user_default:user_123:recent_inputs

def kv_key(user_id: str, entity_id: str, field: str) -> str:
    return f"kv:{user_id}:{entity_id}:{field}"

def tool_key(user_id: str, entity_id: str, tool_name: str) -> str:
    return f"tool:{user_id}:{entity_id}:{tool_name}"

def skill_key(user_id: str, entity_id: str, skill_name: str) -> str:
    return f"skill:{user_id}:{entity_id}:{skill_name}"

def sliding_key(user_id: str, entity_id: str) -> str:
    return f"sliding:{user_id}:{entity_id}:recent_inputs"


# ==================== 分层 TTL ====================
TTL_KV = 60                 # KV 层：60 秒（可能频繁变）
TTL_SUMMARY = 3600          # Summary 层：1 小时
TTL_RAG = 300               # RAG 层：5 分钟
TTL_SKILLS = 86400 * 365    # Skills 层：永久（1 年近似）


# ==================== 缓存 API ====================

def cache_set(key: str, value: Any, ttl: int = TTL_KV) -> None:
    """写入缓存，带 TTL"""
    redis_client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)

def cache_get(key: str) -> Optional[Any]:
    """读取缓存，未命中返回 None"""
    raw = redis_client.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw

def cache_delete(key: str) -> None:
    """删除缓存"""
    redis_client.delete(key)

def cache_flush_prefix(prefix: str) -> None:
    """批量删除某个前缀的所有缓存"""
    for key in redis_client.scan_iter(f"{prefix}:*"):
        redis_client.delete(key)
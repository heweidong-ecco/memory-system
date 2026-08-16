# 创建任务匹配模块
#!/usr/bin/env python3
"""程序性记忆复用——新任务到来时，自动匹配已有 Skill"""

import json
import os
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

from skill_candidate_store import _get_embedding, _cosine_similarity


# ==================== 配置 ====================
PG_CONN = {
    "dbname": "memory_system",
    "user": "memory_user",
    "password": "memory_pass_2026",
    "host": "localhost",
    "port": 5432,
}

QUERY_MATCH_THRESHOLD = 0.70  # 复用时的 query 匹配阈值


def _get_conn():
    return psycopg2.connect(**PG_CONN, cursor_factory=RealDictCursor)


# ==================== 核心：任务匹配 ====================

def match_skill_to_task(entity_id: str, query: str) -> Optional[Dict[str, Any]]:
    """
    判断一个新 query 是否匹配已有的固化 Skill。

    逻辑：
    1. 查询 skill_snapshots 中该实体的所有快照
    2. 对新 query 做 embedding
    3. 与每个快照的 sample_queries 做相似度比较
    4. 取最高相似度，如果超过阈值则返回匹配的 Skill

    返回: 匹配的 Skill 信息，或 None（无匹配）
    """
    # 1. 获取所有快照
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, skill_name, task_description,
                       sample_queries, sample_success_texts, sample_count
                FROM skill_snapshots
                WHERE entity_id = %s
                ORDER BY updated_at DESC
                """,
                (entity_id,),
            )
            snapshots = cur.fetchall()

    if not snapshots:
        return None

    # 2. 对新 query 做 embedding
    query_embedding = _get_embedding(query)
    if not query_embedding:
        return None

    # 3. 计算与每个快照的相似度
    best_match = None
    best_similarity = 0.0

    for snapshot in snapshots:
        sample_queries = snapshot["sample_queries"] or []
        if not sample_queries:
            continue

        # 对快照中的每个样本 query 计算相似度，取最大值
        max_sim = 0.0
        for sample_query in sample_queries:
            sample_embedding = _get_embedding(sample_query)
            sim = _cosine_similarity(query_embedding, sample_embedding)
            if sim > max_sim:
                max_sim = sim

        if max_sim > best_similarity:
            best_similarity = max_sim
            best_match = {
                "snapshot_id": snapshot["id"],
                "skill_name": snapshot["skill_name"],
                "task_description": snapshot["task_description"],
                "similarity": round(max_sim, 4),
                "sample_count": snapshot["sample_count"],
            }

    # 4. 判断是否超过阈值
    if best_match and best_similarity >= QUERY_MATCH_THRESHOLD:
        return best_match

    return None


def should_skip_inference(match_result: Optional[Dict[str, Any]]) -> bool:
    """判断是否应该跳过推理，直接加载匹配的 Skill"""
    return match_result is not None and match_result["similarity"] >= QUERY_MATCH_THRESHOLD


# ==================== 测试 ====================

def main():
    print("=" * 60)
    print("  程序性记忆复用测试")
    print("=" * 60)

    entity_id = "user_123"

    # 测试 1：匹配已有 Skill
    print("\n[测试1] 匹配已有 Skill")
    test_queries = [
        "帮我订一张去上海的机票",       # 应该匹配 flight-booking
        "我想飞广州",                   # 应该匹配 flight-booking
        "帮我写一段Python代码",         # 不应该匹配
    ]

    for q in test_queries:
        match = match_skill_to_task(entity_id, q)
        if match and should_skip_inference(match):
            print(f"  ✅ '{q}' → 匹配 Skill: {match['skill_name']} (相似度 {match['similarity']})")
        else:
            print(f"  ❌ '{q}' → 无匹配，需要重新推理")

    print("\n✅ 程序性记忆复用验证完成")


if __name__ == "__main__":
    main()
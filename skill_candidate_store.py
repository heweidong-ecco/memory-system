# 这是程序性记忆专用向量库。它独立于记忆系统的向量语义层。
#!/usr/bin/env python3
"""技能候选库（skill_candidate_store）——程序性记忆专用向量层

设计原则：
1. 独立于记忆系统的向量语义层，物理隔离
2. 只存"成功的 query + success 内容"的 embedding
3. 每条记录标注对应的 ledger_id，可回溯完整事件链
4. 用双阈值判断"是否同类任务"：先看结果相似度（0.75），再看意图相似度（0.50）
"""

import json
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone

import psycopg2
import requests
from psycopg2.extras import RealDictCursor


# ==================== 配置 ====================
PG_CONN = {
    "dbname": "memory_system",
    "user": "memory_user",
    "password": "memory_pass_2026",
    "host": "localhost",
    "port": 5432,
}

OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "bge-m3"

# 双阈值
SUCCESS_SIMILARITY_THRESHOLD = 0.85   # success 内容相似度阈值（主筛）
QUERY_SIMILARITY_THRESHOLD = 0.65     # query 相似度阈值（二次确认）

# 扫描间隔（秒）
SCAN_COLD_START_INTERVAL = 86400      # 冷启动期：1 天
SCAN_STABLE_INTERVAL = 604800         # 稳定期：1 周
SHADOW_SAMPLE_SIZE = 100              # 影子模式采样数
SHADOW_SAMPLE_DAYS = 3                # 影子模式采样天数
SHADOW_STABLE_DAYS = 7                # 新工具稳定天数后删除旧工具
SNAPSHOT_MAX_SAMPLES = 5              # 评估快照最多保留的成功案例数


# ==================== 数据库连接 ====================
# 更新使用连接池 连接。
from db_pool import get_cursor
'''def _get_conn():
    """创建数据库连接"""
    return psycopg2.connect(**PG_CONN, cursor_factory=RealDictCursor)
'''


def _get_embedding(text: str) -> List[float]:
    """调用 Ollama 将文本转为向量"""
    if not text or not text.strip():
        return [0.0] * 768  # 返回零向量占位
    response = requests.post(
        OLLAMA_EMBED_URL,
        json={"model": EMBED_MODEL, "input": text},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["embeddings"][0]


def _cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """计算两个向量的余弦相似度"""
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = sum(a * a for a in vec1) ** 0.5
    norm2 = sum(b * b for b in vec2) ** 0.5
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


# ==================== 候选向量写入 ====================

def add_candidate_vector(
    entity_id: str,
    session_id: str,
    ledger_id: int,
    query: str,
    success_text: str,
    user_id: str = "user_default",
) -> int:
    """
    把一条"成功的 query + success 内容"写入候选库。

    参数:
    - entity_id: 实体 ID
    - session_id: 会话 ID
    - ledger_id: 对应的 Ledger 记录 ID（用于回溯完整事件链）
    - query: 用户原始意图
    - success_text: 任务成功后的结果文本
    - user_id: 用户 ID，用于多用户隔离

    返回: 新记录的 ID
    """
    query_embedding = _get_embedding(query)
    success_embedding = _get_embedding(success_text)

    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO skill_candidate_vectors
                (user_id, entity_id, session_id, ledger_id, query, success_text,
                    query_embedding, success_embedding, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s::vector, NOW())
            RETURNING id
            """,
            (
                user_id,
                entity_id,
                session_id,
                ledger_id,
                query,
                success_text,
                query_embedding,
                success_embedding,
            ),
        )
        record_id = cur.fetchone()["id"]
    return record_id


# ==================== 候选向量检索 ====================

def find_similar_success_candidates(
    entity_id: str,
    success_text: str,
    limit: int = 10,
    user_id: str = "user_default",
) -> List[Dict[str, Any]]:
    """
    找到 success 内容与给定文本相似度 > 0.75 的候选记录（按用户隔离）。

    返回: 相似候选列表，按相似度降序
    """
    success_embedding = _get_embedding(success_text)

    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, entity_id, session_id, ledger_id, query, success_text,
                success_embedding <=> %s::vector AS cosine_distance
            FROM skill_candidate_vectors
            WHERE user_id = %s AND entity_id = %s
            ORDER BY cosine_distance
            LIMIT %s
            """,
            (success_embedding, user_id, entity_id, limit),
        )
        rows = cur.fetchall()

    results = []
    for row in rows:
        cosine_distance = float(row["cosine_distance"])
        similarity = 1.0 - cosine_distance
        if similarity >= SUCCESS_SIMILARITY_THRESHOLD:
            results.append({
                "id": row["id"],
                "session_id": row["session_id"],
                "ledger_id": row["ledger_id"],
                "query": row["query"],
                "success_text": row["success_text"],
                "similarity": round(similarity, 4),
            })

    results.sort(key=lambda x: -x["similarity"])
    return results


def find_similar_queries(
    entity_id: str,
    query: str,
    exclude_ids: List[int] = None,
    limit: int = 10,
    user_id: str = "user_default",
) -> List[Dict[str, Any]]:
    """
    找到 query 与给定文本相似度 > 0.50 的候选记录（按用户隔离）。

    返回: 相似 query 候选列表，按相似度降序
    """
    query_embedding = _get_embedding(query)
    exclude_ids = exclude_ids or []

    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, entity_id, session_id, ledger_id, query, success_text,
                query_embedding <=> %s::vector AS cosine_distance
                FROM skill_candidate_vectors
                WHERE user_id = %s AND entity_id = %s AND id != ALL(%s)
                ORDER BY cosine_distance
                LIMIT %s
                """,
                (query_embedding, user_id, entity_id, exclude_ids, limit),
            )
        rows = cur.fetchall()

    results = []
    for row in rows:
        cosine_distance = float(row["cosine_distance"])
        similarity = 1.0 - cosine_distance
        if similarity >= QUERY_SIMILARITY_THRESHOLD:
            results.append({
                "id": row["id"],
                "session_id": row["session_id"],
                "ledger_id": row["ledger_id"],
                "query": row["query"],
                "success_text": row["success_text"],
                "similarity": round(similarity, 4),
            })

    results.sort(key=lambda x: -x["similarity"])
    return results


# ==================== 双阈值联动判断 ====================

def find_task_pattern_candidates(
    entity_id: str,
    query: str,
    success_text: str,
    user_id: str = "user_default",
) -> Tuple[List[Dict[str, Any]], str]:
    """
    完整的程序性记忆触发判断（按用户隔离）。

    流程：
    1. 先用 success 内容找相似候选（阈值 0.75）
    2. 在筛出的候选中，再用 query 找相似（阈值 0.50）
    3. 两者都命中的候选，就是"同类任务"的完整模式

    返回: (候选列表, 触发原因)
    """
    # 第 1 步：success 内容筛选
    success_candidates = find_similar_success_candidates(entity_id, success_text, user_id=user_id)

    if not success_candidates:
        return [], "success内容无相似候选"

    # 第 2 步：query 二次确认
    exclude_ids = [c["id"] for c in success_candidates]
    query_candidates = find_similar_queries(entity_id, query, exclude_ids=None, user_id=user_id)

    # 取交集：在两个集合中都出现的候选
    query_ids = {c["id"] for c in query_candidates}
    matched = [c for c in success_candidates if c["id"] in query_ids]

    if not matched:
        return [], f"success相似 {len(success_candidates)} 条，但 query 无匹配"

    return matched, f"success相似且query相似，共 {len(matched)} 条"

    return "no"


# ==================== 测试 ====================

def main():
    print("=" * 60)
    print("  技能候选库（skill_candidate_store）测试")
    print("=" * 60)

    entity_id = "user_123"
    user_id = "user_default"

    # 测试 1：写入候选向量
    print("\n[测试1] 写入候选向量")
    # 模拟三条成功订票的候选
    candidates = [
        ("帮我订一张去北京的机票", "预订成功，订单号 BK001，航班 CA1234"),
        ("我想飞北京，帮我订票", "预订成功，订单号 BK002，航班 CA1234"),
        ("订一张去北京的机票", "预订成功，订单号 BK003，航班 CA1234"),
    ]
    for i, (q, s) in enumerate(candidates):
        record_id = add_candidate_vector(
            entity_id, f"session_trigger_{i}", 1000 + i, q, s, user_id=user_id
        )
        print(f"  写入 {record_id}: '{q[:20]}...' → '{s[:20]}...'")

    # 测试 2：相似 success 检索
    print("\n[测试2] success 内容相似检索")
    similar_success = find_similar_success_candidates(
        entity_id, "预订成功，订单号 BK999，航班 CA1234", user_id=user_id
    )
    print(f"  找到 {len(similar_success)} 条相似 success")
    for c in similar_success:
        print(f"    相似度 {c['similarity']} | {c['success_text'][:40]}")

    # 测试 3：双阈值联动
    print("\n[测试3] 双阈值联动判断")
    matched, reason = find_task_pattern_candidates(
        entity_id,
        "帮我订一张去北京的机票",
        "预订成功，订单号 BK999，航班 CA1234",
        user_id=user_id,
    )
    print(f"  触发判断: {reason}")
    for c in matched:
        print(f"    匹配: '{c['query']}' → '{c['success_text'][:40]}'")

    print("\n✅ 技能候选库验证完成")


if __name__ == "__main__":
    main()
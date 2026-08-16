# 创建 RAG 外部知识层模块
#!/usr/bin/env python3
"""RAG 外部知识层——独立管理 + 版本快照 + 条件性调用"""

import json
import os
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

from vector_semantic import (
    _get_embedding,
    _tokenize,
    rrf_fusion,
    vector_search,
    bm25_search,
)
from policy_api import should_call_rag, check_rag_reference
from ledger_api import get_entity_history

load_dotenv()

PG_CONN = {
    "dbname": "memory_system",
    "user": "memory_user",
    "password": "memory_pass_2026",
    "host": "localhost",
    "port": 5432,
}

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")


# ==================== 数据库连接 ====================
# 更新使用连接池 连接。
from db_pool import get_cursor
'''def _get_conn():
    """创建数据库连接"""
    return psycopg2.connect(**PG_CONN, cursor_factory=RealDictCursor)
'''

def _to_dict(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


# ==================== 外部知识写入 ====================
'''
def add_external_knowledge(
    source: str,
    title: str,
    content: str,
    version: str,
    metadata: Optional[Dict] = None,
) -> int:
    """
    添加外部知识，并自动将同 source+title 的旧记录标记为 deprecated
    向外部知识库添加一条知识。
    参数:
    - source: 来源（如 "product-docs"）
    - title: 文档标题
    - content: 知识内容
    - version: 版本号
    - metadata: 其他元数据

    返回: knowledge_id
    """
    embedding = _get_embedding(content)

    with get_cursor() as conn:
        with conn.cursor() as cur:
            # 1. 将同 source + title 的旧记录标记为 deprecated
            cur.execute(
                """
                UPDATE rag_knowledge_base
                SET status = 'deprecated'
                WHERE source = %s AND title = %s AND status = 'active'
                """,
                (source, title),
            )

            # 2. 插入新记录
            cur.execute(
                """
                INSERT INTO rag_knowledge_base
                    (source, title, content, content_embedding, version, metadata)
                VALUES (%s, %s, %s, %s::vector, %s, %s)
                RETURNING knowledge_id
                """,
                (
                    source,
                    title,
                    content,
                    embedding,
                    version,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            knowledge_id = cur.fetchone()["knowledge_id"]
    return knowledge_id
'''
def add_external_knowledge(source, title, content, version, metadata=None, tenant_id="public"):
    """
    添加外部知识，并自动将同 source+title 的旧记录标记为 deprecated
    向外部知识库添加一条知识。
    参数:
    - source: 来源（如 "product-docs"）
    - title: 文档标题
    - content: 知识内容
    - version: 版本号
    - metadata: 其他元数据
    - tenant_id: 租户 ID，用于多租户隔离，默认 public

    返回: knowledge_id
    """
    embedding = _get_embedding(content)

    with get_cursor() as cur:
        # 1. 将同 source + title 的旧记录标记为 deprecated（仅限当前租户）
        cur.execute(
            """
            UPDATE rag_knowledge_base
            SET status = 'deprecated'
            WHERE tenant_id = %s AND source = %s AND title = %s AND status = 'active'
            """,
            (tenant_id, source, title),
        )

        # 2. 插入新记录
        cur.execute(
            """
            INSERT INTO rag_knowledge_base
                (tenant_id, source, title, content, content_embedding, version, status, metadata)
            VALUES (%s, %s, %s, %s, %s::vector, %s, 'active', %s)
            RETURNING knowledge_id
            """,
            (
                tenant_id,
                source,
                title,
                content,
                embedding,
                version,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        knowledge_id = cur.fetchone()["knowledge_id"]
    return knowledge_id

# ==================== RAG 检索 ====================

def search_rag_knowledge(query: str, source: Optional[str] = None, limit: int = 5, tenant_id: str = "public") -> List[Dict[str, Any]]:
    """
    在外部知识库中做混合检索（向量 + BM25 + RRF 融合）。

    参数:
    - query: 用户查询
    - source: 可选，限定来源
    - limit: 返回条数
    - tenant_id: 租户 ID，用于多租户隔离，默认 public

    返回: 检索结果列表
    """
    # 获取外部知识库中的所有记录（或按 source 过滤）
    with get_cursor() as cur:
        if source:
            cur.execute(
                """
                SELECT knowledge_id, source, title, content, version, updated_at, metadata
                FROM rag_knowledge_base WHERE status = 'active' AND tenant_id = %s AND source = %s
                """,
                (tenant_id, source),
            )
        else:
            cur.execute(
                """
                SELECT knowledge_id, source, title, content, version, updated_at, metadata
                FROM rag_knowledge_base WHERE status = 'active' AND tenant_id = %s
                """,
                (tenant_id,),
            )
        rows = cur.fetchall()

    if not rows:
        return []

    # 构建临时向量检索结果和 BM25 检索结果
    # 注意：这里需要把外部知识库记录转换成与 vector_semantic 检索函数兼容的格式
    all_records = []
    for row in rows:
        all_records.append({
            "id": row["knowledge_id"],
            "content": f"{row['title']}：{row['content']}",
            "metadata": {
                "source": row["source"],
                "version": row["version"],
                "updated_at": str(row["updated_at"]),
            },
        })

    # 向量检索：我们需要基于 content_embedding 计算，而不是重新 embedding 所有记录
    # 简单实现：临时构建一个向量结果，用 embedding 字段
    query_embedding = _get_embedding(query)

    vector_scores = []
    with get_cursor() as cur:
        if source:
            cur.execute(
                """
                SELECT knowledge_id, source, title, content, version, updated_at, metadata,
                           content_embedding <=> %s::vector AS cosine_distance
                    FROM rag_knowledge_base WHERE status = 'active' AND tenant_id = %s AND source = %s
                    ORDER BY cosine_distance LIMIT %s
                    """,
                    (query_embedding, tenant_id, source, limit * 3),
                )
        else:
            cur.execute(
                """
                SELECT knowledge_id, source, title, content, version, updated_at, metadata,
                        content_embedding <=> %s::vector AS cosine_distance
                FROM rag_knowledge_base WHERE status = 'active' AND tenant_id = %s
                ORDER BY cosine_distance LIMIT %s
                """,
                (query_embedding, tenant_id, limit * 3),
            )
        rows = cur.fetchall()

    vector_results = []
    for row in rows:
        similarity = 1.0 - float(row["cosine_distance"])
        vector_results.append({
            "id": row["knowledge_id"],
            "content": f"{row['title']}：{row['content']}",
            "similarity": round(similarity, 4),
            "metadata": {
                "source": row["source"],
                "version": row["version"],
                "updated_at": str(row["updated_at"]),
            },
        })

    # BM25 检索
    corpus = [_tokenize(r["content"]) for r in all_records]
    from rank_bm25 import BM25Okapi
    bm25 = BM25Okapi(corpus)
    query_tokens = _tokenize(query)
    bm25_scores = bm25.get_scores(query_tokens)

    bm25_results = []
    scored = sorted(zip(all_records, bm25_scores), key=lambda x: -x[1])[:limit * 3]
    for record, score in scored:
        if score <= 0:
            continue
        bm25_results.append({
            "id": record["id"],
            "content": record["content"],
            "bm25_score": round(float(score), 4),
            "metadata": record["metadata"],
        })

    # RRF 融合
    fused = rrf_fusion(vector_results, bm25_results)
    return fused[:limit]


# ==================== 元数据快照机制 ====================

def snapshot_rag_metadata(
    entity_id: str,
    knowledge_id: int,
    version: str,
    session_id: Optional[str] = None,
    user_id: str = "user_default",
) -> int:
    """
    把 RAG 检索的版本快照写入 Ledger（按用户隔离）。

    这是解决 RAG 版本不稳定性的核心机制：
    每次 RAG 检索后，把版本号和时间戳写入 Ledger。
    下次调用时对比版本，不一致则重新检索并更新快照。
    """
    from event_bus import EventBus
    bus = EventBus()

    return bus.record_event(
        event_type="rag_retrieval",
        entity_id=entity_id,
        event_data={
            "knowledge_id": knowledge_id,
            "version": version,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        },
        source_agent="rag_layer",
        session_id=session_id,
        user_id=user_id,
    )


def get_latest_rag_snapshot(entity_id: str, knowledge_id: int, user_id: str = "user_default") -> Optional[Dict[str, Any]]:
    """获取某个知识条目的最新版本快照（按用户隔离）"""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT event_data
            FROM ledger
            WHERE user_id = %s AND entity_id = %s
                AND event_type = 'rag_retrieval'
                AND event_data->>'knowledge_id' = %s
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (user_id, entity_id, str(knowledge_id)),
        )
        row = cur.fetchone()
    if not row:
        return None
    return _to_dict(row.get("event_data"))


# ==================== 条件性 RAG 调用 ====================

def rag_conditional_search(
    entity_id: str,
    query: str,
    kv_result: Optional[Dict] = None,
    summary_result: Optional[Dict] = None,
    ledger_result: Optional[List] = None,
    session_id: Optional[str] = None,
    user_id: str = "user_default",
    tenant_id: str = "public",
) -> Dict[str, Any]:
    """
    RAG 条件性调用——完整闭环（按 user_id / tenant_id 隔离）。

    流程：
    1. 调用 Policy 判断是否需要 RAG
    2. 如果需要，检索外部知识库（按 tenant_id 过滤）
    3. 对比版本快照，如有新版本则写入新快照（按 user_id 隔离）
    4. 返回 RAG 结果

    返回: {"should_use_rag": bool, "rag_results": list, "snapshot_updated": bool}
    """
    # 1. Policy 判断（按用户隔离）
    should_rag = should_call_rag(
        entity_id=entity_id,
        query=query,
        kv_result=kv_result,
        summary_result=summary_result,
        ledger_result=ledger_result,
        user_id=user_id,
    )

    if not should_rag:
        return {
            "should_use_rag": False,
            "rag_results": [],
            "snapshot_updated": False,
            "reason": "内部记忆足够，无需 RAG",
        }

    # 2. 检索外部知识（按租户隔离）
    rag_results = search_rag_knowledge(query, tenant_id=tenant_id)

    # 3. 版本快照对比和更新（按用户隔离）
    snapshot_updated = False
    for result in rag_results:
        knowledge_id = result["id"]
        version = result["metadata"].get("version", "")
        latest_snapshot = get_latest_rag_snapshot(entity_id, knowledge_id, user_id=user_id)

        if not latest_snapshot or latest_snapshot.get("version") != version:
            # 新版本或首次检索，写入快照
            snapshot_rag_metadata(entity_id, knowledge_id, version, session_id, user_id=user_id)
            snapshot_updated = True

    return {
        "should_use_rag": True,
        "rag_results": rag_results,
        "snapshot_updated": snapshot_updated,
        "reason": f"内部记忆不足或有 RAG 引用，检索到 {len(rag_results)} 条外部知识",
    }


# ==================== 测试 ====================

def main():
    print("=" * 60)
    print("  RAG 外部知识层测试")
    print("=" * 60)

    # 测试 1：写入外部知识
    print("\n[测试1] 写入外部知识")
    add_external_knowledge(
        source="api-docs",
        title="JWT 认证最佳实践",
        content="JWT token 应设置合理的过期时间，使用 refresh token 续期。JWT 不宜存储敏感信息，因为 payload 仅 base64 编码，任何人可解码。",
        version="v1.0.0",
        tenant_id="public",
    )
    add_external_knowledge(
        source="api-docs",
        title="JWT 认证最佳实践",
        content="JWT token 应使用 RS256 非对称加密签名，过期时间建议 15 分钟，refresh token 建议 7 天。禁止在 payload 中存放密码等敏感信息。",
        version="v2.0.0",
        tenant_id="public",
    )
    add_external_knowledge(
        source="api-docs",
        title="JWT 认证最佳实践",
        content="JWT token 应使用 RS256 非对称加密签名，过期时间建议 10 分钟，refresh token 建议 14 天。禁止在 payload 中存放密码。新增：建议使用 key rotation 策略。",
        version="v3.0.0",
        tenant_id="public",
    )
    print("  已写入 3 条外部知识（v1 和 v2 和 v3）")

    # 测试 2：检索外部知识
    print("\n[测试2] 检索外部知识")
    '''
    print(row.keys())   # 如果 row 是字典
    print(row)          # 直接查看 row 的内容和类型
    '''
    results = search_rag_knowledge("JWT 认证 过期时间", tenant_id="public")
    for r in results:
        print(f"  [{r['metadata']['version']}] {r['content'][:80]}")

    # 测试 3：条件性调用——内部记忆不足
    print("\n[测试3] 条件性调用——内部记忆不足")
    result = rag_conditional_search(
        entity_id="user_123",
        query="JWT 认证最佳实践是什么",
        kv_result=None,       # 内部 KV 未命中
        summary_result=None,  # 内部 Summary 未命中
        ledger_result=[],     # 内部 Ledger 未命中
        user_id="user_default",
        tenant_id="public",
    )
    print(f"  should_use_rag: {result['should_use_rag']}")
    print(f"  reason: {result['reason']}")
    print(f"  snapshot_updated: {result['snapshot_updated']}")

    # 测试 4：版本快照对比
    print("\n[测试4] 版本快照对比")
    if result["rag_results"]:
        first_id = result["rag_results"][0]["id"]
        snapshot = get_latest_rag_snapshot("user_123", first_id, user_id="user_default")
        print(f"  最新快照: {snapshot}")

    print("\n✅ RAG 外部知识层验证完成")


if __name__ == "__main__":
    main()
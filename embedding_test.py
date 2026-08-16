# 编写 Embedding 写入和检索脚本
#!/usr/bin/env python3
"""Embedding 基础设施测试——文本向量化、写入、相似度检索"""

import json
import requests
import psycopg2

# ==================== 配置 ====================
OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "bge-m3"

PG_CONN = {
    "dbname": "memory_system",
    "user": "memory_user",
    "password": "memory_pass_2026",
    "host": "localhost",
    "port": 5432,
}


def get_embedding(text: str) -> list:
    """调用 Ollama 将文本转为向量"""
    response = requests.post(
        OLLAMA_EMBED_URL,
        json={"model": EMBED_MODEL, "input": text},
    )
    response.raise_for_status()
    data = response.json()
    return data["embeddings"][0]


def insert_embedding(entity_id: str, content: str, metadata: dict = None):
    """将文本向量化后写入 PostgreSQL"""
    embedding = get_embedding(content)
    conn = psycopg2.connect(**PG_CONN)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO memory_embedding (entity_id, content, embedding, metadata)
        VALUES (%s, %s, %s::vector, %s)
        """,
        (entity_id, content, embedding, json.dumps(metadata or {})),
    )
    conn.commit()
    cur.close()
    conn.close()
    return embedding


def search_similar(query: str, limit: int = 3):
    """用查询文本向量检索最相似的记录"""
    query_embedding = get_embedding(query)
    conn = psycopg2.connect(**PG_CONN)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT content, embedding <-> %s::vector AS distance
        FROM memory_embedding
        ORDER BY distance
        LIMIT %s
        """,
        (query_embedding, limit),
    )
    results = cur.fetchall()
    cur.close()
    conn.close()
    return results


def main():
    # 新增 多主题语义检索的区分度测试
    # 写入三条语义相关但用词不同的记忆
    print("写入测试向量...")
    insert_embedding("user_123", "MySQL 慢查询优化，给 user_id 加索引", {"category": "database"})
    insert_embedding("user_123", "React 组件性能优化，用 memo 避免重复渲染", {"category": "frontend"})
    insert_embedding("user_123", "Docker 容器内存泄漏排查", {"category": "devops"})
    print("✅ 三条向量已写入\n")

    # 测试语义检索
    print("查询: '数据库查询慢了'")
    for content, distance in search_similar("数据库查询慢了", limit=3):
        print(f"  距离 {distance:.4f} | {content}")

    print("\n查询: '页面卡顿'")
    for content, distance in search_similar("页面卡顿   ", limit=3):
        print(f"  距离 {distance:.4f} | {content}")

    print("\n查询: '容器内存问题'")
    for content, distance in search_similar("容器内存问题", limit=3):
        print(f"  距离 {distance:.4f} | {content}")
    """
    # 写入三条语义相关但用词不同的记忆
    print("写入测试向量...")
    insert_embedding("user_123", "JWT 认证失败，需要检查 token 过期时间", {"category": "debug"})
    insert_embedding("user_123", "数据库连接超时，需要增加连接池大小", {"category": "debug"})
    insert_embedding("user_123", "用户反馈登录后很快掉线", {"category": "bug"})
    print("✅ 三条向量已写入\n")

    # 测试语义检索
    print("查询: 'Token 过期问题'")
    for content, distance in search_similar("Token 过期问题", limit=3):
        print(f"  距离 {distance:.4f} | {content}")

    print("\n查询: '登录状态不稳定'")
    for content, distance in search_similar("登录状态不稳定", limit=3):
        print(f"  距离 {distance:.4f} | {content}")
    """

if __name__ == "__main__":
    main()
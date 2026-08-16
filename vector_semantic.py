# 创建语义提取与写入模块
#!/usr/bin/env python3
"""向量语义层——从 Ledger 提炼关键语义，写入 pgvector，实现混合检索

整合修正：
1. 去重写入：同一 ledger_id 不重复写入向量库
2. 增量同步：sync_new_events 只处理新事件
3. 中文分词：使用 jieba 提升 BM25 效果
4. 取最近事件：ascending=False 确保最新数据被处理
5. 重排序日志：打印 LLM 原始返回，确认重排序是否生效
"""

import json
import os
import re
from typing import Dict, Any, List, Optional

import jieba
import psycopg2
import requests
from psycopg2.extras import RealDictCursor
from rank_bm25 import BM25Okapi
from dotenv import load_dotenv

from ledger_api import get_entity_history

load_dotenv()

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

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# 混合检索权重
VECTOR_WEIGHT = 0.6
BM25_WEIGHT = 0.4

# 增量同步每次最多处理的事件数
SYNC_BATCH_SIZE = 100


# ==================== 数据库连接 ====================
# 更新使用连接池 连接。
from db_pool import get_cursor
'''def _get_conn():
    """创建数据库连接"""
    return psycopg2.connect(**PG_CONN, cursor_factory=RealDictCursor)
'''


def _get_embedding(text: str) -> List[float]:
    """调用 Ollama bge-m3 将文本转为 1024 维向量"""
    if not text or not text.strip():
        return [0.0] * 1024
    response = requests.post(
        OLLAMA_EMBED_URL,
        json={"model": EMBED_MODEL, "input": text},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["embeddings"][0]


def _to_dict(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


# ==================== 核心：语义提取 ====================

def extract_semantic_text(event: Dict[str, Any]) -> str:
    """
    从一条 Ledger 事件中提取"语义文本"。

    不同类型的事件，提取方式不同：
    - user_input: 提取用户输入内容
    - tool_call: 提取工具名 + 输入 + 输出摘要
    - skill_call: 提取技能名 + 触发条件 + 输出摘要
    - state_change: 提取字段名 + 变更后的值
    """
    event_type = event.get("event_type")
    event_data = _to_dict(event.get("event_data")) or {}

    if event_type == "user_input":
        return event_data.get("text", "")

    elif event_type == "tool_call":
        tool_name = event_data.get("tool_name", "")
        tool_input = event_data.get("input", "")
        tool_output = event_data.get("output", "")
        return f"工具调用 {tool_name}：输入 {tool_input}，输出 {tool_output}"

    elif event_type == "skill_call":
        skill_name = event_data.get("skill_name", "")
        trigger = event_data.get("trigger", "")
        output = event_data.get("output", "")
        return f"技能调用 {skill_name}：触发于 {trigger}，输出 {output}"

    elif event_type == "state_change":
        field = event_data.get("field", "")
        new_value = _to_dict(event.get("new_value")) or {}
        return f"状态变更 {field}：{new_value}"

    return ""


def _is_event_already_stored(user_id: str, entity_id: str, ledger_id: int) -> bool:
    """检查该 ledger_id 是否已存在于向量表（按用户隔离）"""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id FROM memory_embedding
            WHERE user_id = %s AND entity_id = %s AND metadata->>'ledger_id' = %s
            LIMIT 1
            """,
            (user_id, entity_id, str(ledger_id)),
        )
        return cur.fetchone() is not None


def _store_single_event(user_id: str, entity_id: str, event: Dict[str, Any]) -> bool:
    """存储单条事件的语义向量。返回 True 表示写入成功，False 表示跳过"""
    semantic_text = extract_semantic_text(event)
    if not semantic_text or not semantic_text.strip():
        return False

    ledger_id = event.get("ledger_id")
    if ledger_id and _is_event_already_stored(user_id, entity_id, ledger_id):
        return False  # 已存在，跳过

    embedding = _get_embedding(semantic_text)

    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory_embedding
                (user_id, entity_id, content, embedding, metadata)
            VALUES (%s, %s, %s, %s::vector, %s)
            """,
            (
                user_id,
                entity_id,
                semantic_text,
                embedding,
                json.dumps({
                    "event_type": event.get("event_type"),
                    "ledger_id": ledger_id,
                    "timestamp": str(event.get("timestamp")),
                }),
            ),
        )
    return True


def store_events_to_vector_db(entity_id: str, events: List[Dict[str, Any]], user_id: str = "user_default") -> int:
    """
    将一批事件写入向量库，自动去重。
    从 Ledger 中提取语义文本，embedding 后写入 memory_embedding 表。
    返回: 实际写入的记录数
    """
    stored_count = 0
    for event in events:
        if _store_single_event(user_id, entity_id, event):
            stored_count += 1
    return stored_count


def sync_new_events(entity_id: str, last_synced_ledger_id: int = 0, user_id: str = "user_default") -> Dict[str, Any]:
    """
    增量同步：只处理 ledger_id 大于 last_synced_ledger_id 的新事件。

    返回: {"processed": 处理数, "stored": 写入数, "last_ledger_id": 最新同步的ID}
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT ledger_id, event_type, entity_id, event_data,
                    old_value, new_value, timestamp, source_agent, session_id
            FROM ledger
            WHERE user_id = %s AND entity_id = %s AND ledger_id > %s
            ORDER BY ledger_id ASC
            LIMIT %s
            """,
            (user_id, entity_id, last_synced_ledger_id, SYNC_BATCH_SIZE),
        )
        rows = cur.fetchall()

    if not rows:
        return {"processed": 0, "stored": 0, "last_ledger_id": last_synced_ledger_id}

    stored = 0
    for row in rows:
        event = dict(row)
        event["old_value"] = _to_dict(event.get("old_value"))
        event["new_value"] = _to_dict(event.get("new_value"))
        event["event_data"] = _to_dict(event.get("event_data"))
        if _store_single_event(user_id, entity_id, event):
            stored += 1

    last_id = rows[-1]["ledger_id"]
    return {
        "processed": len(rows),
        "stored": stored,
        "last_ledger_id": last_id,
    }


# ==================== 检索：向量 / BM25 / 混合 / 重排序 ====================

def vector_search(entity_id: str, query: str, limit: int = 10, user_id: str = "user_default") -> List[Dict[str, Any]]:
    """纯向量相似度检索，返回最相似的语义记录（按用户隔离）。"""
    query_embedding = _get_embedding(query)

    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, entity_id, content, metadata,
                    embedding <=> %s::vector AS cosine_distance
            FROM memory_embedding
            WHERE user_id = %s AND entity_id = %s
            ORDER BY cosine_distance
            LIMIT %s
            """,
            (query_embedding, user_id, entity_id, limit),
        )
        rows = cur.fetchall()

    results = []
    for row in rows:
        cosine_distance = float(row["cosine_distance"])
        similarity = 1.0 - cosine_distance
        metadata = _to_dict(row.get("metadata"))
        results.append({
            "id": row["id"],
            "content": row["content"],
            "similarity": round(similarity, 4),
            "metadata": metadata,
        })
    return results

# === BM25 检索 ===
'''
旧分词 BM25 分词函数 _tokenize 只按标点符号分割，导致中文长串被当成一个整体 token。对于中文，需要更精确的分词。
def _tokenize(text: str) -> List[str]:
    """简单分词：按空格和中英文标点分割"""
    import re
    tokens = re.split(r'[\s,，。！？!?；;：:、]+', text)
    return [t for t in tokens if t]
'''
"""
jieba 分词的效果对比：

文本	旧分词（按标点）	jieba 分词
"数据库查询慢了"	["数据库查询慢了"]（一个token）	["数据库", "查询", "慢", "了"]
"帮我订一张去北京的机票"	["帮我订一张去北京的机票"]	["帮", "我", "订", "一张", "去", "北京", "的", "机票"]
jieba 让 BM25 能真正匹配关键词。 否则 BM25 对中文几乎失效。
"""
def _tokenize(text: str) -> List[str]:
    """使用 jieba 进行中文分词"""
    tokens = jieba.lcut(text)
    return [t for t in tokens if t.strip()]


def bm25_search(entity_id: str, query: str, limit: int = 10, user_id: str = "user_default") -> List[Dict[str, Any]]:
    """BM25 关键词检索（按用户隔离）"""
    # 获取该实体的所有语义记录
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, content, metadata
            FROM memory_embedding
            WHERE user_id = %s AND entity_id = %s
            """,
            (user_id, entity_id),
        )
        rows = cur.fetchall()

    if not rows:
        return []

    # 构建 BM25 语料
    corpus = [_tokenize(row["content"]) for row in rows]
    bm25 = BM25Okapi(corpus)
    query_tokens = _tokenize(query)
    scores = bm25.get_scores(query_tokens)

    # 按分数降序排列，取前 limit 条
    scored = sorted(zip(rows, scores), key=lambda x: -x[1])[:limit]

    results = []
    for row, score in scored:
        if score <= 0:
            continue
        metadata = _to_dict(row.get("metadata"))
        results.append({
            "id": row["id"],
            "content": row["content"],
            "bm25_score": round(float(score), 4),
            "metadata": metadata,
        })
    return results

# =================== 混合检索：向量 + BM25 ==================
# 旧版使用了简单的分数归一化和加权融合，效果一般，两层分数分布差异大
'''
def hybrid_search(entity_id: str, query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    混合检索：向量 + BM25 加权融合
    算法：
    1. 分别做向量检索和 BM25 检索
    2. 对每个结果做分数归一化
    3. 加权融合：final_score = VECTOR_WEIGHT * normalized_vector_score + BM25_WEIGHT * normalized_bm25_score
    4. 按融合分数降序返回
    """
    vector_results = vector_search(entity_id, query, limit=limit * 3)
    bm25_results = bm25_search(entity_id, query, limit=limit * 3)

    # 归一化向量分数
    if vector_results:
        max_v = max(r["similarity"] for r in vector_results)
        min_v = min(r["similarity"] for r in vector_results)
        v_range = max_v - min_v if max_v != min_v else 1.0
        for r in vector_results:
            r["norm_vector_score"] = (r["similarity"] - min_v) / v_range
    else:
        for r in vector_results:
            r["norm_vector_score"] = 0.0

    # 归一化 BM25 分数
    if bm25_results:
        max_b = max(r["bm25_score"] for r in bm25_results)
        min_b = min(r["bm25_score"] for r in bm25_results)
        b_range = max_b - min_b if max_b != min_b else 1.0
        for r in bm25_results:
            r["norm_bm25_score"] = (r["bm25_score"] - min_b) / b_range
    else:
        for r in bm25_results:
            r["norm_bm25_score"] = 0.0

    # 融合
    merged = {}
    for r in vector_results:
        merged[r["id"]] = r
        merged[r["id"]]["final_score"] = VECTOR_WEIGHT * r["norm_vector_score"]

    for r in bm25_results:
        if r["id"] in merged:
            merged[r["id"]]["bm25_score"] = r["bm25_score"]
            merged[r["id"]]["norm_bm25_score"] = r["norm_bm25_score"]
            merged[r["id"]]["final_score"] += BM25_WEIGHT * r["norm_bm25_score"]
        else:
            r["norm_vector_score"] = 0.0
            r["final_score"] = BM25_WEIGHT * r["norm_bm25_score"]
            merged[r["id"]] = r

    # 排序
    final_results = sorted(merged.values(), key=lambda x: -x["final_score"])[:limit]

    # 清理内部字段，只保留输出需要的
    output = []
    for r in final_results:
        output.append({
            "id": r["id"],
            "content": r["content"],
            "final_score": round(r["final_score"], 4),
            "vector_similarity": r.get("similarity", 0.0),
            "bm25_score": r.get("bm25_score", 0.0),
            "metadata": r.get("metadata"),
        })
    return output
'''
# ==================== RRF 融合算法 ====================
def rrf_fusion(vector_results: List[Dict], bm25_results: List[Dict], k: int = 60) -> List[Dict]:
    """
    RRF 融合：基于排名而非分数。

    参数:
    - vector_results: 向量检索结果（已按相似度降序）
    - bm25_results: BM25 检索结果（已按分数降序）
    - k: RRF 常数，通常 60

    返回: 融合后的结果，按 RRF 分数降序
    """
    rrf_scores = {}

    # 处理向量检索排名
    for rank, r in enumerate(vector_results):
        rrf_scores[r["id"]] = rrf_scores.get(r["id"], 0) + 1.0 / (k + rank + 1)

    # 处理 BM25 排名
    for rank, r in enumerate(bm25_results):
        rrf_scores[r["id"]] = rrf_scores.get(r["id"], 0) + 1.0 / (k + rank + 1)

    # 构建结果映射
    result_map = {}
    for r in vector_results + bm25_results:
        if r["id"] not in result_map:
            result_map[r["id"]] = r

    # 按 RRF 分数排序
    sorted_ids = sorted(rrf_scores.keys(), key=lambda x: -rrf_scores[x])

    output = []
    for record_id in sorted_ids:
        r = result_map[record_id]
        output.append({
            "id": r["id"],
            "content": r["content"],
            "rrf_score": round(rrf_scores[record_id], 6),
            "metadata": r.get("metadata"),
        })

    return output

def rerank_with_llm(query: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    用 LLM 对候选结果做重排序。带日志输出确认是否真正执行。
    混合检索只给出"相似度排序"，但最终哪个最相关，LLM 判断最准。
    这里用一个极简的 prompt 让 LLM 对候选做最终排序。
    """
    if not candidates:
        return []

    candidates_json = json.dumps(
        [{"rank": i, "content": c["content"][:200]} for i, c in enumerate(candidates)],
        ensure_ascii=False,
        indent=2,
    )

    prompt = f"""用户查询：{query}

以下是检索到的候选结果，请按与查询的相关度从高到低重新排序。

只输出排序结果，格式为 JSON 数组，每个元素包含 rank 和 original_index。

候选结果：
{candidates_json}

输出格式：
[{{"original_index": 2, "reason": "最直接相关"}}, ...]

只输出 JSON，不要输出其他内容。"""

    if not DEEPSEEK_API_KEY:
        print("  [重排序] 未配置 API Key，跳过 LLM 重排序")
        return candidates

    try:
        response = requests.post(
            DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": 500,
            },
            timeout=30,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        print(f"  [重排序] LLM 原始返回: {content[:200]}...")

        # 解析 JSON
        json_match = re.search(r'\[.*\]', content, re.DOTALL)
        if json_match:
            ranking = json.loads(json_match.group())
            # 按 LLM 给出的顺序重排
            reranked = []
            for item in ranking:
                idx = item.get("original_index", 0)
                if idx < len(candidates):
                    reranked.append(candidates[idx])
            print(f"  [重排序] 成功重排 {len(reranked)} 条")
            return reranked
        else:
            print("  [重排序] JSON 解析失败，返回原顺序")

    except Exception as e:
        print(f"  [重排序] 调用失败: {e}")

    return candidates


# ==================== 测试 ====================

def main():
    print("=" * 60)
    print("  向量语义层混合检索测试（整合修正版）")
    print("=" * 60)

    entity_id = "user_123"
    user_id = "user_default"

    # 测试 1：增量同步（处理最近 20 条，去重）
    print("\n[测试1] 增量同步（取最近 20 条，自动去重）")
    events = get_entity_history(user_id, entity_id, limit=20, ascending=False)
    stored = store_events_to_vector_db(entity_id, events, user_id=user_id)
    print(f"  本次写入 {stored} 条（重复事件已跳过）")

    # 测试 2：纯向量检索
    print("\n[测试2] 纯向量检索")
    query = "数据库查询慢了"
    vector_results = vector_search(entity_id, query, limit=5, user_id=user_id)
    for r in vector_results:
        print(f"  [{r['similarity']}] {r['content'][:80]}")

    # 测试 3：BM25 检索
    print("\n[测试3] BM25 关键词检索")
    bm25_results = bm25_search(entity_id, query, limit=5, user_id=user_id)
    for r in bm25_results:
        print(f"  [{r['bm25_score']}] {r['content'][:80]}")

    # 测试 4：混合检索
    print("\n[测试4] 混合检索")
    hybrid_results = rrf_fusion(vector_results, bm25_results, k=60)
    for r in hybrid_results:
        print(f"  [RRF {r['rrf_score']}] {r['content'][:80]}")

    # 测试 5：重排序
    print("\n[测试5] LLM 重排序")
    reranked = rerank_with_llm(query, hybrid_results)
    for i, r in enumerate(reranked[:3]):
        print(f"  重排 {i+1}: {r['content'][:80]}")

    print("\n✅ 向量语义层混合检索验证完成")


if __name__ == "__main__":
    main()
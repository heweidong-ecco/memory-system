# 创建统一检索编排器
#!/usr/bin/env python3
"""分层检索编排器——统一检索入口，按优先级路由，跨层回退"""

from typing import Dict, Any, Optional, List

from kv_api import get_profile_value
from summary_api import search_recent_summary
from ledger_api import get_entity_history
from skill_matcher_v2 import match_skill_by_description, load_skill_content
from rag_knowledge import rag_conditional_search


def unified_search(
    entity_id: str,
    query: str,
    session_id: Optional[str] = None,
    user_id: str = "user_default",
    tenant_id: str = "public",
) -> Dict[str, Any]:
    """
    统一检索入口。按以下优先级路由：

    1. KV 精准层（确定性查询，零噪声）
    2. Summary 近期摘要层（模糊语义）
    3. Ledger 账本层（时间回溯）
    4. Agent 自生 Skills 层（重复能力，跳过推理）
    5. RAG 外部知识层（条件性调用）

    参数:
    - entity_id: 实体 ID
    - query: 用户查询
    - session_id: 会话 ID
    - user_id: 用户 ID，内部各层按此隔离
    - tenant_id: 租户 ID，外部知识库按此隔离

    返回: 完整检索结果，包含各层命中情况和最终输出
    """
    result = {
        "entity_id": entity_id,
        "query": query,
        "layers_hit": [],
        "kv_result": None,
        "summary_result": None,
        "ledger_result": None,
        "skill_match": None,
        "rag_result": None,
        "final_output": None,
    }

    # ==================== 第 1 层：KV 精准读取 ====================
    kv_result = _search_kv(entity_id, query, user_id)
    if kv_result:
        result["layers_hit"].append("KV")
        result["kv_result"] = kv_result
        result["final_output"] = kv_result
        result["final_layer"] = "KV"
        return result

    # ==================== 第 2 层：Summary 近期摘要 ====================
    summary_result = search_recent_summary(entity_id, query, user_id=user_id)
    if summary_result:
        result["layers_hit"].append("Summary")
        result["summary_result"] = summary_result
        # Summary 命中后，继续向下层搜索更精确的结果
        # 但不立即返回——Ledger 可能有更准确的

    # ==================== 第 3 层：Ledger 回溯 ====================
    ledger_result = get_entity_history(user_id, entity_id, limit=5)
    if ledger_result:
        result["layers_hit"].append("Ledger")
        result["ledger_result"] = ledger_result

    # ==================== 第 4 层：Agent 自生 Skills ====================
    skill_match = match_skill_by_description(query, user_id=user_id)
    if skill_match:
        result["layers_hit"].append("Skills")
        result["skill_match"] = skill_match
        skill_content = load_skill_content(skill_match["skill_name"], user_id=user_id)
        skill_match["skill_content"] = skill_content
        # Skills 匹配后，优先使用（跳过推理）
        result["final_output"] = {
            "type": "skill",
            "skill_name": skill_match["skill_name"],
            "skill_content": skill_content,
        }
        result["final_layer"] = "Skills"
        return result

    # ==================== 第 5 层：RAG 条件性调用 ====================
    rag_result = rag_conditional_search(
        entity_id=entity_id,
        query=query,
        kv_result=kv_result,
        summary_result=summary_result,
        ledger_result=ledger_result,
        session_id=session_id,
        user_id=user_id,
        tenant_id=tenant_id,
    )
    result["rag_result"] = rag_result

    if rag_result["should_use_rag"]:
        result["layers_hit"].append("RAG")
        if rag_result["rag_results"]:
            result["final_output"] = rag_result["rag_results"]
            result["final_layer"] = "RAG"
            return result

    # ==================== 所有层都未命中，返回已有信息 ====================
    if summary_result:
        result["final_output"] = summary_result
        result["final_layer"] = "Summary"
    elif ledger_result:
        result["final_output"] = ledger_result
        result["final_layer"] = "Ledger"
    else:
        result["final_output"] = {"message": "未找到相关信息"}
        result["final_layer"] = "None"

    return result


def _search_kv(entity_id: str, query: str, user_id: str = "user_default") -> Optional[Dict[str, Any]]:
    """
    简化版 KV 检索：尝试从 query 中提取可能的 key 并查询（按用户隔离）。

    生产环境中，KV 层的检索应该由 Intent 解析器驱动。
    这里简化处理：如果 query 明确包含"位置"、"偏好"等字段名，则查询对应 KV。
    """
    kv_result = {}

    # 位置相关
    if "位置" in query or "在哪" in query or "location" in query.lower():
        location = get_profile_value(user_id, f"{entity_id}:location")
        if location:
            kv_result["location"] = location

    # 语言偏好
    if "偏好" in query or "语言" in query or "preference" in query.lower():
        lang = get_profile_value(user_id, f"{entity_id}:preference:language")
        if lang:
            kv_result["preference_language"] = lang

    return kv_result if kv_result else None


# ==================== 测试 ====================

def main():
    print("=" * 60)
    print("  分层检索编排器测试")
    print("=" * 60)

    entity_id = "user_123"

    test_queries = [
        "我的位置在哪？",
        "帮我订一张去上海的机票",
        "JWT 认证最佳实践是什么",
        "帮我写一段Python代码",
    ]

    for q in test_queries:
        print(f"\n{'─' * 50}")
        print(f"查询: {q}")
        result = unified_search(entity_id, q)
        print(f"  命中层: {result['layers_hit']}")
        print(f"  最终层: {result['final_layer']}")
        if result["final_output"]:
            if isinstance(result["final_output"], dict):
                if "skill_name" in result["final_output"]:
                    print(f"  结果: 加载 Skill '{result['final_output']['skill_name']}'")
                elif "message" in result["final_output"]:
                    print(f"  结果: {result['final_output']['message']}")
                elif "content" in result["final_output"]:
                    print(f"  结果: {str(result['final_output']['content'])[:100]}")
                else:
                    print(f"  结果: {str(result['final_output'])[:100]}")
            else:
                print(f"  结果: {str(result['final_output'])[:100]}")

    print(f"\n{'=' * 60}")
    print("✅ 分层检索编排器验证完成")


if __name__ == "__main__":
    main()
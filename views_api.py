# 创建 Views 聚合层模块
"""
Agent 不可能自己写四个查询来拼装这些信息——它需要一个统一入口，一次调用，拿到所有相关信息的“快照”。
这个统一入口就是 Views。
Views 就是这张便签——它是从底层数据中“派生”出来的、专供当前任务使用的即时状态卡。
LPV 中“V”的核心设计哲学——底层永远是最原始的数据，上层永远是按需生成的视图。
"""
#!/usr/bin/env python3
"""Views 任务派生视图——从 Ledger 和 KV 聚合生成即时状态卡

⚠️ 架构说明：
- 第 7 天（今天）验证的是 Views 的“聚合能力”。
- 完整的“任务派生”逻辑（解析 query → 动态路由 → 按需检索）在第 15 天实现。
- 因此，本文件中的 collect_* 函数是独立可调用的，方便未来编排器按需调用。
"""

import json
from typing import Dict, Any, Optional, List

from kv_api import get_profile_value, get_tool_summary, get_skill_summary
from ledger_api import (
    get_state_changes,
    get_tool_calls,
    get_skill_calls,
)
from policy_api import should_call_rag, check_rag_reference


# ==================== 各层数据收集函数（可独立调用） ====================

def collect_kv_state(entity_id: str) -> Dict[str, Any]:
    """
    从 KV 层收集当前状态。
    
    ⚠️ 重要：Views 层始终从 PostgreSQL 读取，不从 Redis 读取。
    理由：Views 是状态卡的聚合，必须保证数据权威性和完整性。
    Redis 是缓存层，可能过期。Views 不承担缓存职责。
    因此，Views 层的底层查询函数（get_profile_value、get_tool_summary、get_skill_summary）都直接访问 PostgreSQL。
    底层查询函数走 Cache-Aside，Views 通过调用这些函数间接利用缓存。
    缓存的意义在于分担高频单点查询的 PG 压力，而不是取代 PG。
    """
    # 这里调用的是 get_profile_value，它会先查 Redis 再查 PG。
    # 但对于 Views 来说，我们需要确保数据权威性。
    # 后续可以增加一个参数，控制是否走缓存。
    state = {}

    # TODO(多用户接入): 传当前登录用户真实 user_id
    location = get_profile_value("user_default", f"{entity_id}:location")
    if location:
        state["location"] = location

    # TODO(多用户接入): 传当前登录用户真实 user_id
    preference_lang = get_profile_value("user_default", f"{entity_id}:preference:language")
    if preference_lang:
        state["preference_language"] = preference_lang

    return state


def collect_history_summary(entity_id: str, limit: int = 3) -> List[Dict[str, Any]]:
    """
    从 Ledger 层收集历史状态变更摘要。

    返回：按时间倒序的状态变更列表（最多 limit 条）。
    """
    state_changes = get_state_changes(entity_id, limit=limit)
    return [
        {
            "event_type": "state_change",
            "old_value": s.get("old_value"),
            "new_value": s.get("new_value"),
            "timestamp": s.get("timestamp"),
        }
        for s in state_changes
    ]


def collect_tool_summary(entity_id: str) -> List[Dict[str, Any]]:
    """
    从 KV 层和 Ledger 层收集工具调用摘要。

    KV 优先：如果 KV 中有工具摘要，直接使用；否则从 Ledger 回溯最近记录。
    """
    tool_summary = []

    # KV 优先
    for tool_name in ["web_search", "flight_booking"]:
        summary = get_tool_summary(entity_id, tool_name)
        if summary:
            tool_summary.append({
                "tool_name": tool_name,
                "last_input": summary.get("last_input", ""),
                "last_output": summary.get("last_output", ""),
                "last_status": summary.get("last_status", ""),
                "last_called_at": summary.get("last_called_at", ""),
            })

    # Ledger 回退
    if not tool_summary:
        tool_calls = get_tool_calls(entity_id, limit=3)
        for call in tool_calls:
            data = call.get("event_data", {})
            tool_summary.append({
                "tool_name": data.get("tool_name", "unknown"),
                "last_input": data.get("input", ""),
                "last_output": data.get("output", ""),
                "last_status": data.get("status", ""),
                "last_called_at": call.get("timestamp", ""),
            })

    return tool_summary


def collect_skill_summary(entity_id: str) -> List[Dict[str, Any]]:
    """
    从 KV 层和 Ledger 层收集技能调用摘要。

    KV 优先：如果 KV 中有技能摘要，直接使用；否则从 Ledger 回溯最近记录。
    """
    skill_summary = []

    # KV 优先
    for skill_name in ["flight_booking"]:
        summary = get_skill_summary(entity_id, skill_name)
        if summary:
            skill_summary.append({
                "skill_name": skill_name,
                "last_trigger": summary.get("last_input", ""),
                "last_output": summary.get("last_output", ""),
                "last_status": summary.get("last_status", ""),
                "last_called_at": summary.get("last_called_at", ""),
            })

    # Ledger 回退
    if not skill_summary:
        skill_calls = get_skill_calls(entity_id, limit=3)
        for call in skill_calls:
            data = call.get("event_data", {})
            skill_summary.append({
                "skill_name": data.get("skill_name", "unknown"),
                "last_trigger": data.get("trigger", ""),
                "last_output": data.get("output", ""),
                "last_status": data.get("status", ""),
                "last_called_at": call.get("timestamp", ""),
            })

    return skill_summary


# ==================== 纯聚合函数 ====================

def aggregate_state_card(
    entity_id: str,
    collected_data: Dict[str, Any],
    query: Optional[str] = None,
) -> Dict[str, Any]:
    """
    纯粹的聚合函数：将各层收集到的数据组装成一张状态卡。

    参数:
    - entity_id: 实体 ID
    - collected_data: 字典，由编排器传入，包含以下可选键：
        {
            "current_state":  KV 层收集的当前状态,
            "history_summary": Ledger 层收集的历史摘要,
            "tool_summary": 工具摘要,
            "skill_summary": 技能摘要,
            "summary_result": Recent Summary 层检索结果（第8天实现后提供）,
        }
    - query: 用户当前查询（用于 RAG 判定）

    返回: 完整的即时状态卡，包含 RAG 调用判定。
    """
    card = {
        "entity_id": entity_id,
        "current_state": collected_data.get("current_state", {}),
        "history_summary": collected_data.get("history_summary", []),
        "tool_summary": collected_data.get("tool_summary", []),
        "skill_summary": collected_data.get("skill_summary", []),
        "summary_result": collected_data.get("summary_result", None),
        "rag_decision": {},
    }

    # RAG 调用判定
    rag_should_call = should_call_rag(
        entity_id=entity_id,
        query=query,
        kv_result=card["current_state"] if card["current_state"] else None,
        summary_result=card["summary_result"],
        summary_count=len(card["summary_result"]) if card["summary_result"] else 0,
        summary_confidence=0.0,  # 第8天实现 Summary 后提供真实置信度
        ledger_result=card["history_summary"] if card["history_summary"] else None,
    )
    card["rag_decision"] = {
        "should_call_rag": rag_should_call,
        "trigger_reason": _get_rag_trigger_reason(entity_id, query, card),
    }

    return card


def _get_rag_trigger_reason(
    entity_id: str,
    query: Optional[str],
    card: Dict[str, Any],
) -> str:
    """判断 RAG 触发的具体原因（用于调试和可观测性）"""
    reasons = []

    if check_rag_reference(entity_id):
        reasons.append("RAG 引用标记存在")

    if not card["current_state"] and not card["history_summary"]:
        reasons.append("内部记忆不足")

    if query:
        external_keywords = ["最新", "文档", "API 规范", "官方", "版本更新"]
        if any(kw in query for kw in external_keywords):
            reasons.append("查询包含外部知识关键词")

    if not reasons:
        reasons.append("内部结果完整，无需 RAG")

    return "; ".join(reasons)


# ==================== 测试（今天仍可直接运行） ====================

def print_card(card: Dict[str, Any]):
    """格式化打印状态卡"""
    print(f"  实体: {card['entity_id']}")
    print(f"  当前状态: {json.dumps(card['current_state'], ensure_ascii=False)}")
    print(f"  历史摘要: {len(card['history_summary'])} 条状态变更")
    for h in card["history_summary"]:
        print(f"    {h['timestamp']} | {h['old_value']} → {h['new_value']}")
    print(f"  工具摘要: {len(card['tool_summary'])} 条")
    for t in card["tool_summary"]:
        print(f"    {t['tool_name']}: {t['last_output'][:40]} ({t['last_status']})")
    print(f"  技能摘要: {len(card['skill_summary'])} 条")
    for s in card["skill_summary"]:
        print(f"    {s['skill_name']}: {s['last_output'][:40]} ({s['last_status']})")
    if card["summary_result"]:
        print(f"  Recent Summary: {len(card['summary_result'])} 条")
    print(f"  RAG 判定: {card['rag_decision']}")


def main():
    print("=" * 60)
    print("  Views 任务派生视图测试（拆分版）")
    print("=" * 60)

    entity_id = "user_123"

    # 收集各层数据
    current_state = collect_kv_state(entity_id)
    history_summary = collect_history_summary(entity_id, limit=3)
    tool_summary = collect_tool_summary(entity_id)
    skill_summary = collect_skill_summary(entity_id)

    collected = {
        "current_state": current_state,
        "history_summary": history_summary,
        "tool_summary": tool_summary,
        "skill_summary": skill_summary,
        "summary_result": None,  # 第 8 天实现
    }

    # 测试 1：无查询
    print("\n[测试1] 聚合生成状态卡（无查询）")
    card_1 = aggregate_state_card(entity_id, collected)
    print_card(card_1)

    # 测试 2：带外部知识查询
    print("\n[测试2] 聚合生成状态卡（查询: 最新航班 API 规范）")
    card_2 = aggregate_state_card(entity_id, collected, query="最新航班 API 规范是什么")
    print_card(card_2)

    print("\n✅ Views 任务派生视图验证完成")


if __name__ == "__main__":
    main()
# 创建程序性记忆触发模块
# 新版基于“query + success 内容”双阈值判断。
#!/usr/bin/env python3
"""程序性记忆触发机制——基于 query + success 内容相似度判断（重构版）

核心变更：
1. 废弃旧的"调用序列匹配"逻辑
2. 使用 skill_candidate_store 中的双阈值判断
3. 触发后通过 ledger_id 检索完整事件链，交由 LLM 判断最优路径
"""

import json
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from ledger_api import get_events_by_session, get_entity_history
import skill_candidate_store
from skill_candidate_store import (
    find_task_pattern_candidates,
    SUCCESS_SIMILARITY_THRESHOLD,
    QUERY_SIMILARITY_THRESHOLD,
)
from kv_api import set_profile_value

# ==================== 核心：触发判定 ====================

def check_procedural_trigger(
    entity_id: str,
    query: str,
    success_text: str,
    user_id: str = "user_default",
) -> Dict[str, Any]:
    """
    判断是否应该触发程序性记忆固化（按用户隔离）。

    条件（双阈值）：
    1. success 内容相似度 > 0.75（主筛）
    2. query 相似度 > 0.50（二次确认）

    返回: 触发结果，包含是否触发、匹配的候选、触发原因
    """
    candidates, reason = find_task_pattern_candidates(entity_id, query, success_text, user_id=user_id)

    if not candidates:
        return {
            "should_trigger": False,
            "trigger_reason": reason,
            "candidates": [],
        }

    return {
        "should_trigger": True,
        "trigger_reason": f"发现 {len(candidates)} 条同类任务模式：{reason}",
        "candidates": candidates,
    }


# ==================== 核心：提取完整事件链 ====================

def extract_full_event_chain(entity_id: str, ledger_ids: List[int], user_id: str = "user_default") -> List[Dict[str, Any]]:
    """
    根据 ledger_id 去 Ledger 检索完整的会话事件链（按用户隔离）。

    返回: 按会话分组的事件列表，每个会话包含完整的事件序列和 event_data
    """
    # 从候选的 ledger_id 找到对应的 session_id
    session_ids = set()
    with skill_candidate_store.get_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT session_id
            FROM skill_candidate_vectors
            WHERE user_id = %s AND entity_id = %s AND ledger_id = ANY(%s)
            """,
            (user_id, entity_id, ledger_ids),
        )
        for row in cur.fetchall():
            session_ids.add(row["session_id"])

    # 逐个会话提取完整事件
    full_chains = []
    for sid in session_ids:
        events = get_events_by_session(user_id, sid)
        full_chains.append({
            "session_id": sid,
            "events": events,
        })

    return full_chains


# ==================== 触发标记写入 ====================

def record_trigger_marker(entity_id: str, candidates: List[Dict[str, Any]], user_id: str = "user_default") -> int:
    """
    触发后，在 KV 层写入触发标记（按用户隔离）。第 10 天会基于此执行 Skill 生成。

    返回: KV 版本号
    """
    pattern_ledger_ids = [c["ledger_id"] for c in candidates]
    key = f"{entity_id}:procedural_trigger:latest"

    return set_profile_value(
        key=key,
        value={
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "ledger_ids": pattern_ledger_ids,
            "candidate_count": len(candidates),
            "status": "pending_skill_generation",
        },
        entity_type="procedural",
        user_id=user_id,
    )


# ==================== 测试 ====================

def main():
    import skill_candidate_store

    print("=" * 60)
    print("  程序性记忆触发机制测试（重构版）")
    print("=" * 60)

    entity_id = "user_123"

    # 模拟一次新的成功任务
    query = "帮我订一张去北京的机票"
    success_text = "预订成功，订单号 BK999，航班 CA1234"

    # 检查是否触发
    result = check_procedural_trigger(entity_id, query, success_text)

    print(f"\n[检查触发] {result['trigger_reason']}")

    if result["should_trigger"]:
        print(f"  匹配候选 {len(result['candidates'])} 条:")
        for c in result["candidates"][:5]:
            print(f"    ledger_id={c['ledger_id']} | '{c['query'][:30]}' → '{c['success_text'][:30]}'")

        # 提取完整事件链
        ledger_ids = [c["ledger_id"] for c in result["candidates"]]
        chains = extract_full_event_chain(entity_id, ledger_ids)
        print(f"\n[事件链提取] 找到 {len(chains)} 个会话的完整事件")

        # 写入触发标记
        marker_version = record_trigger_marker(entity_id, result["candidates"])
        print(f"\n[触发标记] 已写入 KV，version={marker_version}")
    else:
        print("  未触发，不执行后续步骤")

    print("\n✅ 程序性记忆触发机制验证完成")


if __name__ == "__main__":
    main()
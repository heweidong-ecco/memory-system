# 创建程序性记忆skills固化与快照模块

##!/usr/bin/env python3
"""程序性记忆固化——旧数据降级 + 简化版快照保存"""

import os
import re
from typing import Dict, Any, List, Optional

from db_pool import get_cursor


def mark_candidates_processed(entity_id: str, ledger_ids: List[int], user_id: str = "user_default") -> int:
    """将已用于生成 Skill 的候选向量标记为 'processed'（按用户隔离）"""
    if not ledger_ids:
        return 0

    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE skill_candidate_vectors
            SET status = 'processed'
            WHERE user_id = %s AND entity_id = %s AND ledger_id = ANY(%s) AND status = 'pending'
            """,
            (user_id, entity_id, ledger_ids),
        )
        return cur.rowcount


def extract_task_description(skill_content: str) -> str:
    """从 SKILL.md 内容中提取 description 字段"""
    match = re.search(r'description:\s*>-\s*\n((?:\s+.*\n?)*)', skill_content)
    if match:
        desc = match.group(1).strip()
        desc = "\n".join(line.strip() for line in desc.split("\n") if line.strip())
        return desc
    return ""


def save_skill_snapshot(
    entity_id: str,
    skill_name: str,
    task_description: str,
    source_ledger_ids: List[int],
    user_id: str = "user_default",
) -> int:
    """保存简化版快照到 skill_snapshots 表（按用户隔离）"""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO skill_snapshots
                (user_id, skill_name, entity_id, task_description, source_ledger_ids)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                skill_name,
                entity_id,
                task_description,
                source_ledger_ids,
            ),
        )
        snapshot_id = cur.fetchone()["id"]
    return snapshot_id


def finalize_skill(
    entity_id: str,
    skill_content: str,
    ledger_ids: List[int],
    user_id: str = "user_default",
) -> Dict[str, Any]:
    """固化 Skill 完整流程：提取名称+描述 → 保存快照 → 标记已处理（按用户隔离）"""
    # 提取技能名
    name_match = re.search(r'name:\s*([a-z0-9-]+)', skill_content)
    skill_name = name_match.group(1) if name_match else "generated-skill"

    # 提取任务描述
    task_description = extract_task_description(skill_content)

    # 保存快照
    snapshot_id = save_skill_snapshot(
        entity_id, skill_name, task_description, ledger_ids, user_id
    )

    # 标记已处理
    marked_count = mark_candidates_processed(entity_id, ledger_ids, user_id)

    return {
        "skill_name": skill_name,
        "snapshot_id": snapshot_id,
        "marked_processed_count": marked_count,
    }


if __name__ == "__main__":
    # 测试入口（需要传入实际参数）
    print("skill_finalizer.py 已更新为简化版")
    print("使用方式：")
    print("  from skill_finalizer import finalize_skill")
    print("  result = finalize_skill(entity_id, skill_content, ledger_ids)")
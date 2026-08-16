# 创建 Skill 生成器
#!/usr/bin/env python3
"""程序性记忆提炼——调用大模型从事件链中自动生成 SKILL.md"""

import json
import os
from typing import Dict, Any, List
from dotenv import load_dotenv
import requests

from ledger_api import get_events_by_session
from skill_candidate_store import _get_conn

load_dotenv()

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")


def get_complete_event_chains(entity_id: str, ledger_ids: List[int]) -> List[Dict[str, Any]]:
    """
    根据 ledger_id 列表，从 Ledger 提取完整的事件链。

    返回: 按会话分组的事件链列表
    """
    # 1. 从候选库找到对应的 session_id
    session_ids = set()
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT session_id
                FROM skill_candidate_vectors
                WHERE entity_id = %s AND ledger_id = ANY(%s)
                """,
                (entity_id, ledger_ids),
            )
            for row in cur.fetchall():
                session_ids.add(row["session_id"])

    # 2. 逐个会话提取完整事件
    chains = []
    for sid in session_ids:
        events = get_events_by_session(sid)
        chains.append({
            "session_id": sid,
            "events": events,
        })

    return chains


def generate_skill_prompt(chains: List[Dict[str, Any]]) -> str:
    """构建发送给 LLM 的 prompt，让 LLM 自动提炼并生成 SKILL.md"""
    chains_json = json.dumps(chains, ensure_ascii=False, default=str, indent=2)

    prompt = f"""你是一个程序性记忆提炼专家。以下是从多个成功会话中提取的完整事件链。

这些会话都被识别为"同一类任务"——它们的用户意图相似，执行成功后的结果也相似，但执行路径可能不同（使用了不同的工具或技能）。

你的任务是：
1. 分析这些会话的共同意图——用户想要完成什么任务？
2. 识别这个任务的关键步骤——哪些步骤是所有成功会话都包含的？
3. 给出最优的执行路径——从这些会话中，哪一条路径最合理、最可靠？
4. 生成一份 SKILL.md，符合 Anthropic Skill 标准格式。

下面是事件链数据：

{chains_json}

请按以下格式输出（严格遵守）：

=== SKILL.md ===
---
name: [kebab-case技能名称]
description: >-
  [技能功能描述]
  [触发条件：用户可能说什么、用什么关键词]
---

# [技能名称]

## 核心指令

[关键步骤，按顺序列出]

## 输出格式

[成功结果应该长什么样]

## 示例

**输入**：[用户可能的输入]
**输出**：[成功的结果]

=== 结束 ===

只输出 SKILL.md 的内容，不要输出任何解释。"""

    return prompt


def call_llm_to_generate_skill(chains: List[Dict[str, Any]]) -> str:
    """调用 LLM 生成 SKILL.md"""
    if not DEEPSEEK_API_KEY:
        return "⚠️ 未配置 DeepSeek API Key，无法自动生成 Skill"

    prompt = generate_skill_prompt(chains)

    try:
        response = requests.post(
            DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 2000,
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"⚠️ LLM 调用失败: {e}"


def save_skill(skill_content: str, skill_name: str) -> str:
    """将生成的 SKILL.md 保存到程序性记忆库目录"""
    skills_dir = "generated_skills"
    os.makedirs(skills_dir, exist_ok=True)
    filepath = os.path.join(skills_dir, skill_name, "SKILL.md")
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(skill_content)

    return filepath

def main():
    print("=" * 60)
    print("  程序性记忆提炼——自动生成 SKILL.md")
    print("=" * 60)

    entity_id = "user_123"

    # 从候选库中查询最近的触发标记
    from kv_api import get_profile_value
    trigger_marker = get_profile_value(f"{entity_id}:procedural_trigger:latest")

    if not trigger_marker:
        print("⚠️ 未找到触发标记。请先运行 procedural_trigger.py")
        return

    ledger_ids = trigger_marker.get("ledger_ids", [])
    print(f"触发标记中的 ledger_ids: {ledger_ids}")

    if not ledger_ids:
        print("⚠️ 触发标记中没有 ledger_ids")
        return

    # 提取完整事件链
    chains = get_complete_event_chains(entity_id, ledger_ids)
    print(f"提取到 {len(chains)} 个会话的完整事件链")

    # 调用 LLM 生成 SKILL.md
    print("\n调用 LLM 生成 SKILL.md...")
    skill_content = call_llm_to_generate_skill(chains)
    print(f"\n生成的 SKILL.md 内容：\n{skill_content}")

    # 保存
    # 简单提取技能名称（由 LLM 生成的 kebab-case 名称）
    import re
    name_match = re.search(r'name:\s*([a-z0-9-]+)', skill_content)
    skill_name = name_match.group(1) if name_match else "generated-skill"

    filepath = save_skill(skill_content, skill_name)
    print(f"\n✅ SKILL.md 已保存: {filepath}")


if __name__ == "__main__":
    main()
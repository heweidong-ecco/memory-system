# 创建 Skill 生成器
# 将生成 prompt 改为严格遵循 skill-creator 标准：
#!/usr/bin/env python3
"""程序性记忆提炼——调用大模型按 skill-creator 标准生成 SKILL.md（修改版）"""

import json
import os
from typing import Dict, Any, List
from dotenv import load_dotenv
import requests

from ledger_api import get_events_by_session
import skill_candidate_store

load_dotenv()

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# skill-creator 标准文件路径
STANDARD_FILE = "skill_standards/skill-creator-standard.md"


def get_complete_event_chains(entity_id: str, ledger_ids: List[int], user_id: str = "user_default") -> List[Dict[str, Any]]:
    """根据 ledger_id 列表，从 Ledger 提取完整事件链（按用户隔离）"""
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

    chains = []
    for sid in session_ids:
        events = get_events_by_session(user_id, sid)
        chains.append({
            "session_id": sid,
            "events": events,
        })
    return chains


def load_skill_creator_standard() -> str:
    """加载 skill-creator 标准内容"""
    if os.path.exists(STANDARD_FILE):
        with open(STANDARD_FILE, "r", encoding="utf-8") as f:
            return f.read()
    # 如果标准文件不存在，使用内置的精简标准
    return get_builtin_standard()


def get_builtin_standard() -> str:
    """内置的精简版 skill-creator 标准（备选）"""
    return """# Skill 标准格式

## YAML 元数据要求
- name: kebab-case，与文件夹名一致
- description: 必须同时包含 WHAT（做什么）和 WHEN（何时使用）

## 指令正文要求
- 使用清晰的层级结构（## 指令、### 步骤）
- 包含具体的执行步骤
- 包含输出格式说明
- 包含至少一个输入/输出示例
- 包含故障排除部分

## 命名规范
- 文件夹名 kebab-case
- SKILL.md 大小写敏感
"""


def generate_skill_prompt(chains: List[Dict[str, Any]]) -> str:
    """构建发送给 LLM 的 prompt，按 skill-creator 标准生成 SKILL.md，
    并首先判断任务复杂度是否值得固化。"""
    chains_json = json.dumps(chains, ensure_ascii=False, default=str, indent=2)
    standard = load_skill_creator_standard()

    prompt = f"""你是一个程序性记忆提炼专家。你的任务是分析多个成功会话的事件链，并生成一份严格符合 Anthropic Skill 标准的 SKILL.md。

以下是 skill-creator 标准格式说明（标准本身是英文，但你要生成的内容使用中文）：

{standard}

以下是多个被识别为"同一类任务"的成功会话完整事件链：

{chains_json}

请按以下顺序执行：

## 第一步：复杂度门槛判断（重要！）

在生成 SKILL.md 之前，先判断这个任务是否值得固化。

不值得固化的条件（任一满足即判定不值得）：
- 调用步骤总数 ≤ 2 步（工具调用 + 技能调用的总次数）
- 预估总 Token 消耗 < 2000
- 大模型直接推理 ≤ 5 秒即可完成，且不会遗漏关键步骤

如果判定为不值得固化，只输出：
NO_SKILL_NEEDED
不要输出任何其他内容。

如果判定为值得固化，继续执行第二步。

## 第二步：意图分析

分析这些会话的共同意图——用户想要完成什么任务？
这个意图是否足够复杂，需要多步骤的流程？

## 第三步：关键步骤识别

识别任务的关键步骤——哪些步骤是所有成功会话都包含的？
这些步骤之间是否有明确的依赖关系？

## 第四步：最优路径确定

从这些会话中，确定最优的执行路径——哪条路径最合理、最可靠？
如果不同会话使用了不同的工具/技能，选择那条最通用、最稳定的。

## 第五步：生成 SKILL.md

按以下规则生成完整的 SKILL.md：

- name 必须使用 kebab-case（英文小写+连字符），如 flight-booking
- description 使用中文编写，必须同时包含 WHAT（做什么）和 WHEN（何时使用）
- description 必须包含用户可能说的中文触发短语（如"订机票"、"飞往某地"）
- 指令正文全部使用中文
- 指令正文必须包含：核心指令、输出格式、示例、故障排除
- 输出格式、示例、故障排除部分也使用中文
- 代码示例中的变量名、函数名可保留英文

只输出 SKILL.md 的内容（从 --- 开始到结束），不要输出任何解释或其他文字。"""

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
                "model": "deepseek-v4-flash",
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


def save_skill_to_skills_dir(skill_content: str, user_id: str = None) -> str:
    """
    将生成的 SKILL.md 保存到 skills/ 目录。
    这是统一技能系统的入口，所有 Skills（外部+自生成）都在这里。

    多租户隔离：传入 user_id 时写入 skills/{user_id}/{skill_name}/SKILL.md，
    该技能只属于该用户；不传则写入全局 skills/{skill_name}/（内置/共享）。
    """
    import re
    name_match = re.search(r'name:\s*([a-z0-9-]+)', skill_content)
    skill_name = name_match.group(1) if name_match else "generated-skill"

    skills_dir = "skills"
    base_dir = os.path.join(skills_dir, user_id) if user_id else skills_dir
    skill_dir = os.path.join(base_dir, skill_name)
    os.makedirs(skill_dir, exist_ok=True)

    filepath = os.path.join(skill_dir, "SKILL.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(skill_content)

    return filepath


def main():
    print("=" * 60)
    print("  程序性记忆提炼——按 skill-creator 标准生成 SKILL.md")
    print("=" * 60)

    entity_id = "user_123"

    # 读取触发标记
    from kv_api import get_profile_value
    # TODO(多用户接入): 传当前登录用户真实 user_id
    trigger_marker = get_profile_value("user_default", f"{entity_id}:procedural_trigger:latest")

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
    print("\n调用 LLM 按 skill-creator 标准生成 SKILL.md...")
    skill_content = call_llm_to_generate_skill(chains)
    print(f"\n生成的 SKILL.md：\n{skill_content[:500]}...")

    # 保存到 skills/ 目录
    filepath = save_skill_to_skills_dir(skill_content)
    print(f"\n✅ SKILL.md 已保存: {filepath}")


if __name__ == "__main__":
    main()
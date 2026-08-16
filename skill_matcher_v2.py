# 创建基于 description 的 Skill 匹配器
# 使用 SKILL.md 的 description 做向量匹配：
#!/usr/bin/env python3
"""技能匹配器 v2——基于 SKILL.md 的 description 做语义匹配

参考 Claude Code SkillTool 的设计：
- 发现阶段：扫描 skills/ 目录，加载 name + description
- 匹配阶段：新 query 与 description 做向量相似度
- 加载阶段：匹配后按需读取完整 SKILL.md
"""

import os
import re
import yaml
from typing import Dict, Any, List, Optional

import requests

from vector_semantic import _get_embedding

from skill_loader import discover_skills, parse_skill_metadata, load_skill

# ==================== 配置 ====================
SKILLS_DIR = "skills"
QUERY_MATCH_THRESHOLD = 0.55  # 复用阈值（宁可不用，不能乱用）

OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "bge-m3"


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





def match_skill_by_description(query: str, user_id: str = None) -> Optional[Dict[str, Any]]:
    """
    根据新 query 与所有 Skills 的 description 做语义匹配（按用户隔离）。

    参数:
    - query: 用户查询
    - user_id: 用户 ID（可选），匹配时包含该用户的私有技能

    返回: 匹配的 Skill 信息，或 None
    """
    skills = discover_skills(user_id)
    if not skills:
        return None

    query_embedding = _get_embedding(query)

    best_match = None
    best_similarity = 0.0

    for skill in skills:
        desc = skill.get("description", "")
        if not desc:
            continue

        desc_embedding = _get_embedding(desc)
        similarity = _cosine_similarity(query_embedding, desc_embedding)

        if similarity > best_similarity:
            best_similarity = similarity
            best_match = {
                "skill_name": skill["name"],
                "description": desc,
                "similarity": round(similarity, 4),
            }

    if best_match and best_similarity >= QUERY_MATCH_THRESHOLD:
        return best_match

    return None


def load_skill_content(skill_name: str, user_id: str = None) -> Optional[str]:
    """加载匹配到的 Skill 的完整 SKILL.md 内容（按用户隔离，先查私有目录）"""
    return load_skill(skill_name, user_id)


# ==================== 测试 ====================

def main():
    print("=" * 60)
    print("  技能匹配器 v2 测试（基于 description）")
    print("=" * 60)

    print("\n[发现阶段] 扫描可用 Skills")
    skills = discover_skills()
    if not skills:
        print("  ⚠️ skills/ 目录为空")
        return
    for s in skills:
        print(f"  - {s['name']}: {s['description'][:60]}")

    print("\n[匹配阶段] 新 query 匹配")
    test_queries = [
        "帮我订一张去上海的机票",
        "我想飞广州",
        "帮我写一段Python代码",
    ]
    for q in test_queries:
        match = match_skill_by_description(q)
        if match:
            print(f"  ✅ '{q}' → 匹配 {match['skill_name']} (相似度 {match['similarity']})")
        else:
            print(f"  ❌ '{q}' → 无匹配")

    print("\n✅ 技能匹配器 v2 验证完成")


if __name__ == "__main__":
    main()
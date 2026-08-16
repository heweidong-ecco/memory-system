# 实现目录扫描 + description 加载 + 按需加载
#!/usr/bin/env python3
"""技能加载器——扫描 skills/ 目录，加载 description，按需加载 SKILL.md

参考 Claude Code SkillTool 的设计：
1. 发现阶段：扫描 skills/ 目录，只加载 name + description
2. 匹配阶段：由 Agent 根据 description 判断是否相关
3. 加载阶段：匹配后按需读取完整 SKILL.md
"""

import os
import re
import yaml
from typing import Dict, Any, List, Optional

SKILLS_DIR = "skills"


def discover_skills(user_id: str = None) -> List[Dict[str, str]]:
    """
    发现阶段：扫描 skills/ 目录，返回所有技能的 name + description。

    多租户隔离：传入 user_id 时额外扫描 skills/{user_id}/ 下的用户私有技能。
    全局 skills/ 根目录始终扫描（内置/共享技能），user_id 目录优先级更高。

    返回: [{"name": "...", "description": "..."}]
    """
    skills = []

    scan_roots = [SKILLS_DIR]
    if user_id:
        scan_roots.append(os.path.join(SKILLS_DIR, user_id))

    for root in scan_roots:
        if not os.path.isdir(root):
            continue
        for skill_folder in os.listdir(root):
            skill_path = os.path.join(root, skill_folder)
            skill_md_path = os.path.join(skill_path, "SKILL.md")

            if not os.path.isfile(skill_md_path):
                continue

            metadata = parse_skill_metadata(skill_md_path)
            if metadata:
                skills.append(metadata)

    return skills


def parse_skill_metadata(skill_md_path: str) -> Optional[Dict[str, str]]:
    """解析 SKILL.md 的 YAML 元数据，只提取 name 和 description"""
    try:
        with open(skill_md_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 提取 YAML 前置元数据
        match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
        if not match:
            return None

        yaml_content = match.group(1)
        metadata = yaml.safe_load(yaml_content)

        if not metadata or "name" not in metadata:
            return None

        return {
            "name": metadata.get("name", ""),
            "description": metadata.get("description", ""),
        }
    except Exception:
        return None


def load_skill(skill_name: str, user_id: str = None) -> Optional[str]:
    """
    加载阶段：按需读取完整 SKILL.md 内容。

    多租户隔离：先查用户私有目录 skills/{user_id}/{skill_name}/，再查全局根目录。

    参数: skill_name - 技能名称
          user_id   - 用户 ID（可选），先查该用户私有技能
    返回: SKILL.md 的完整内容
    """
    if user_id:
        user_path = os.path.join(SKILLS_DIR, user_id, skill_name, "SKILL.md")
        if os.path.isfile(user_path):
            with open(user_path, "r", encoding="utf-8") as f:
                return f.read()

    skill_md_path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")

    if not os.path.isfile(skill_md_path):
        return None

    with open(skill_md_path, "r", encoding="utf-8") as f:
        return f.read()


def get_skill_metadata_by_name(skill_name: str) -> Optional[Dict[str, str]]:
    """按名称获取技能的元数据"""
    skill_md_path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    return parse_skill_metadata(skill_md_path)


def list_skill_names() -> List[str]:
    """列出所有可用的技能名称"""
    return [s["name"] for s in discover_skills()]


def main():
    print("=" * 60)
    print("  技能加载器测试")
    print("=" * 60)

    # 发现阶段
    print("\n[发现阶段] 扫描 skills/ 目录")
    skills = discover_skills()
    if not skills:
        print("  ⚠️ skills/ 目录为空或不存在")
        return

    print(f"  发现 {len(skills)} 个技能：")
    for skill in skills:
        print(f"    - {skill['name']}: {skill['description'][:80]}")

    # 加载阶段
    print("\n[加载阶段] 按需加载完整 SKILL.md")
    if skills:
        first_skill = skills[0]["name"]
        content = load_skill(first_skill)
        if content:
            print(f"  已加载 {first_skill} 的 SKILL.md，共 {len(content)} 字符")
            print(f"  前 200 字符: {content[:200]}")

    print("\n✅ 技能加载器验证完成")


if __name__ == "__main__":
    main()
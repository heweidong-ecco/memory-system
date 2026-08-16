# 弃用模块归档

以下文件是第 9-11 天构建的旧方案，已被"参考 Claude Code SkillTool 的标准架构"替代。

## 弃用原因
1. 两套技能系统并存（外部 Skills vs 程序化生成 Skill）
2. 匹配目标错误（用下位实例而非上位概念 description）
3. 性能低（M×N 次 embedding）

## 保留价值
- 底层逻辑理解：双阈值判断、触发机制、成功经历检测
- 教育意义：理解为什么 description 是更好的匹配目标

## 替代方案
- skill_generator.py：改为按 skill-creator 标准生成 SKILL.md
- skill_loader.py：统一技能加载器（发现→匹配→加载）
- 所有 Skills 统一存储在 skills/ 目录

## 归档日期
2026-10-02

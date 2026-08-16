# 测试脚本：程序性记忆进化链路 + RAG 条件性调用 + Skills 多租户匹配
# 数据库连接与测试数据清理统一由 conftest.py 管理（clean_db autouse fixture）。
#
# 与真实代码的对应关系（重构说明）：
# - 进化链路：skill_candidate_store.add_candidate_vector（记录候选）
#             → procedural_trigger.check_procedural_trigger（触发提炼，内部固定查 user_default）
#             → skill_generator.save_skill_to_skills_dir（封装 SKILL.md）
#             → skill_finalizer.finalize_skill（固化快照）
#             → skill_matcher_v2.match_skill_by_description（复用，跳过推理）
# - RAG：policy_api.should_call_rag（条件判定）+ rag_knowledge.rag_conditional_search（调用与版本对比）
#        快照存在 ledger（event_type='rag_retrieval'），无 rag_metadata 表
# - Skills 隔离：候选层（skill_candidate_vectors 带 user_id）才是租户隔离点；
#                match_skill_by_description 是基于 skills/ 目录的全局匹配（无 user_id）
import os
import shutil
import pytest
from unittest.mock import patch

from skill_candidate_store import add_candidate_vector, find_task_pattern_candidates
from procedural_trigger import check_procedural_trigger
from skill_generator import save_skill_to_skills_dir
from skill_finalizer import finalize_skill
from skill_matcher_v2 import match_skill_by_description
from retrieval_orchestrator import unified_search
from policy_api import should_call_rag
from rag_knowledge import rag_conditional_search, add_external_knowledge, get_latest_rag_snapshot
from kv_api import set_profile_value
from db_pool import get_cursor
from cache_manager import redis_client, kv_key


# ==================== 场景 1：程序性记忆完整进化链路 ====================

def test_procedural_memory_evolution():
    """
    完整链路：重复成功任务 → 触发提炼 → 封装(SKILL.md) → 固化(快照) → 复用(跳过昂贵推理)

    说明：
    - 全程 user_id=user_test_evo，触发判定、技能文件、快照全部按该用户隔离
    - 生成阶段直接构造固定 SKILL.md 内容（真实场景由 call_llm_to_generate_skill 产出，这里 mock 保持确定性）
    - 复用阶段 patch 生成函数，断言其未被调用 → 证明跳过昂贵推理
    """
    user_id = "user_test_evo"
    entity_id = "evo_entity"
    q = "检查自动续费状态并取消续费"
    s = "已取消自动续费，订单 EVO-CANCEL-001"
    fake_ledger_ids = [93001, 93002, 93003]
    skill_name = "evo-test-skill"

    skill_content = f"""---
name: {skill_name}
description: >-
  检查自动续费状态并取消自动续费。当用户要求"检查自动续费"、"取消续费"、"处理自动扣费"时使用。
---

# {skill_name}

## 指令
1. 查询自动续费状态
2. 执行取消操作
"""

    try:
        # 1) 重复成功任务（3 次）→ 写入候选向量库（事件流总线在成功 tool/skill 后也调 add_candidate_vector）
        for i in range(3):
            add_candidate_vector(entity_id, f"evo_sess_{i}", fake_ledger_ids[i], q, s, user_id=user_id)

        # 2) 触发提炼：双阈值判定命中同类任务模式（success 相似 + query 相似，按用户隔离）
        trigger = check_procedural_trigger(entity_id, q, s, user_id=user_id)
        assert trigger["should_trigger"] is True, f"应触发提炼：{trigger['trigger_reason']}"
        assert len(trigger["candidates"]) >= 3, "应有 ≥3 条同类候选"

        # 3) 封装：写入 skills/{user_id}/{skill_name}/SKILL.md（真实场景由 LLM 生成内容）
        saved_path = save_skill_to_skills_dir(skill_content, user_id=user_id)
        assert os.path.isfile(saved_path), "SKILL.md 应写入该用户的 skills/ 目录"

        # 4) 固化：保存 skill_snapshots 快照（带 user_id）+ 标记候选已处理
        final = finalize_skill(entity_id, skill_content, fake_ledger_ids, user_id=user_id)
        assert final["skill_name"] == skill_name
        assert final["snapshot_id"] is not None

        # 5) 复用：匹配到已固化 Skill，且跳过昂贵推理（生成函数未被调用）
        with patch("skill_generator.call_llm_to_generate_skill") as mock_gen_reuse:
            matched = match_skill_by_description(q, user_id=user_id)
            assert matched is not None, "应匹配到已固化的 Skill"
            assert matched["skill_name"] == skill_name, f"应匹配 {skill_name}，实际 {matched}"

            # 统一检索入口：命中 Skills 层直接返回，不进入 RAG/生成
            res = unified_search(entity_id, q, user_id=user_id)
            assert res["final_layer"] == "Skills", f"应命中 Skills 层，实际 {res['final_layer']}"
            mock_gen_reuse.assert_not_called()  # 复用阶段未调用生成器 → 跳过昂贵推理

    finally:
        # 清理：用户私有 skills 目录（快照/候选由 conftest 的 clean_db 清理）
        shutil.rmtree(os.path.join("skills", user_id), ignore_errors=True)


# ==================== 场景 2：RAG 条件性调用 ====================

def test_rag_conditional_decision():
    """RAG 条件性判定：内部完整→不调用；内部不足→调用；外部关键词→调用；有引用→调用"""
    user_id = "user_test_ragdec"
    entity_id = "user_test_ragdec"
    ref_key = f"{entity_id}:rag_reference"

    try:
        # A) 内部完整命中 + 无引用 + 无外部关键词 → 完全不调用 RAG
        assert should_call_rag(entity_id, query="我的位置", kv_result={"location": "北京"},
                               summary_result=None, ledger_result=[{"x": 1}], user_id=user_id) is False

        # B) 内部完全未命中 → 无引用但需要外挂知识库 → 调用 RAG
        assert should_call_rag(entity_id, query="任意问题", kv_result=None,
                               summary_result=None, ledger_result=[], user_id=user_id) is True

        # C) 内部命中但 query 含外部知识关键词 → 调用 RAG
        assert should_call_rag(entity_id, query="JWT 认证最新文档规范", kv_result={"location": "北京"},
                               summary_result=None, ledger_result=[{"x": 1}], user_id=user_id) is True

        # D) 有 rag_reference 引用标记 → 调用 RAG（即使内部命中，按用户隔离）
        set_profile_value(ref_key, {"doc": "product_doc"}, user_id=user_id)
        assert should_call_rag(entity_id, query="我的位置", kv_result={"location": "北京"},
                               summary_result=None, ledger_result=[{"x": 1}], user_id=user_id) is True
    finally:
        # 清理引用标记（user_test_% 由 conftest 的 clean_db 统一清理，这里清缓存防串测）
        redis_client.delete(kv_key(user_id, entity_id, "rag_reference"))


def test_rag_version_comparison():
    """RAG 有引用→调用 + 版本对比：版本不变不更新快照，新版本更新快照"""
    user_id = "user_test_ragver"
    entity_id = "user_test_ragver"
    source = "test-rag-docs"
    title = "RAG 版本对比测试文档"
    ref_key = f"{entity_id}:rag_reference"

    try:
        # 引用标记（check_rag_reference 现在按 user_id 查询）
        set_profile_value(ref_key, {"doc": title}, user_id=user_id)

        # 写入知识 v1
        add_external_knowledge(source=source, title=title,
                               content="v1：JWT 过期时间建议 15 分钟", version="v1", tenant_id="public")

        # 首次调用 → 应调用 RAG 并写入版本快照
        r1 = rag_conditional_search(entity_id=entity_id, query="JWT 最新规范",
                                    kv_result=None, summary_result=None, ledger_result=[],
                                    user_id=user_id, tenant_id="public")
        assert r1["should_use_rag"] is True, "有 RAG 引用时应调用 RAG"
        assert r1["snapshot_updated"] is True, "首次检索应写入版本快照"

        # 再次调用（版本不变）→ 不更新快照
        r2 = rag_conditional_search(entity_id=entity_id, query="JWT 最新规范",
                                    kv_result=None, summary_result=None, ledger_result=[],
                                    user_id=user_id, tenant_id="public")
        assert r2["snapshot_updated"] is False, "版本未变不应重复写快照"

        # 升级到 v2 → 应更新快照
        add_external_knowledge(source=source, title=title,
                               content="v2：JWT 过期时间建议 10 分钟", version="v2", tenant_id="public")
        r3 = rag_conditional_search(entity_id=entity_id, query="JWT 最新规范",
                                    kv_result=None, summary_result=None, ledger_result=[],
                                    user_id=user_id, tenant_id="public")
        assert r3["snapshot_updated"] is True, "新版本应更新快照"

        # 验证快照版本跟随知识版本
        with get_cursor() as cur:
            cur.execute(
                "SELECT knowledge_id FROM rag_knowledge_base WHERE source=%s AND title=%s AND status='active' ORDER BY knowledge_id DESC LIMIT 1",
                (source, title),
            )
            kid = cur.fetchone()["knowledge_id"]
        snap = get_latest_rag_snapshot(entity_id, kid, user_id=user_id)
        assert snap is not None, "应存在版本快照"
        assert snap["version"] == "v2", f"快照应记录 v2，实际 {snap}"
    finally:
        # 清理：知识库测试行（引用标记 user_test_% 由 clean_db 清理）
        with get_cursor() as cur:
            cur.execute("DELETE FROM rag_knowledge_base WHERE source = %s", (source,))
        redis_client.delete(kv_key(user_id, entity_id, "rag_reference"))


# ==================== 场景 3：Skills 匹配多租户不串扰 ====================

def test_skills_candidate_no_cross_tenant():
    """技能候选匹配租户隔离：同一 entity/query 下，A 的候选对 B 不可见"""
    user_a, user_b = "user_test_skA", "user_test_skB"
    entity = "skill_entity"
    q = "技能隔离测试：订国际机票"
    s = "技能隔离：国际机票预订成功 SK-ISO-001"

    add_candidate_vector(entity, "sess_a", 94001, q, s, user_id=user_a)

    # find_task_pattern_candidates 返回 (candidates, reason) 元组
    matched_a, reason_a = find_task_pattern_candidates(entity, q, s, user_id=user_a)
    matched_b, reason_b = find_task_pattern_candidates(entity, q, s, user_id=user_b)

    assert len(matched_a) >= 1, f"A 应匹配到自己的候选：{reason_a}"
    assert len(matched_b) == 0, f"B 不应匹配到 A 的候选：{reason_b}"


def test_skill_description_matching():
    """description 语义匹配准确：相关 query 命中正确 Skill，无关 query 不命中"""
    # patch discover_skills，避免污染全局 skills/ 目录
    user_id = "user_test_skA"
    test_skills = [
        {"name": "test-flight-book", "description": "帮我订机票、查询航班、预订去某地的机票"},
        {"name": "test-code-review", "description": "审查代码、发现 bug、提交代码评审"},
    ]
    with patch("skill_matcher_v2.discover_skills", return_value=test_skills):
        hit = match_skill_by_description("帮我订一张去北京的机票", user_id=user_id)
        assert hit is not None and hit["skill_name"] == "test-flight-book", f"应命中订票技能，实际 {hit}"

        hit2 = match_skill_by_description("帮我审查这段 Python 代码", user_id=user_id)
        assert hit2 is not None and hit2["skill_name"] == "test-code-review", f"应命中代码评审技能，实际 {hit2}"

        # 无关 query → 低于阈值 → 无匹配
        miss = match_skill_by_description("今天天气怎么样", user_id=user_id)
        assert miss is None, f"无关 query 不应匹配：{miss}"


# ==================== 参数化：多用户 × 多任务描述，Skills 匹配不串扰 ====================

@pytest.mark.parametrize("user_id,task_desc,expected_skill,decoy_desc", [
    ("user_test_skp1", "帮我审查这段 Python 代码并找出 bug", "p-code-review", "编写 Python 脚本处理数据"),
    ("user_test_skp2", "帮我优化 MySQL 慢查询并添加索引", "p-db-tune", "设计数据库表结构与备份恢复"),
    ("user_test_skp3", "帮我写一份产品需求文档 PRD", "p-prd", "设计 UI 界面与交互流程"),
])
def test_skills_matching_no_cross_tenant_param(user_id, task_desc, expected_skill, decoy_desc):
    """
    参数化：多用户 × 多任务描述，验证 Skills 匹配不串扰。

    每个用例：
    - 给 user_id 固化一个专属 Skill（description 含该任务原文）
    - 给另一个用户 user_other 固化一个"诱饵" Skill（任务相关但不属于本用户）
    - 本用户按任务匹配 → 只命中自己的 Skill，绝不命中诱饵
    """
    user_other = f"{user_id}_other"
    own_content = f"""---
name: {expected_skill}
description: >-
  {task_desc} 的专用技能。当用户提出此任务时使用。
---

# {expected_skill}
"""
    decoy_content = f"""---
name: {expected_skill}-decoy
description: >-
  {decoy_desc} 的专用技能。
---

# {expected_skill}-decoy
"""
    try:
        # 每个用户固化专属 Skill + 另一个用户的诱饵 Skill（都在各自的私有目录）
        save_skill_to_skills_dir(own_content, user_id=user_id)
        save_skill_to_skills_dir(decoy_content, user_id=user_other)

        # 本用户只匹配到自己的技能，绝不命中诱饵
        hit = match_skill_by_description(task_desc, user_id=user_id)
        assert hit is not None, f"{user_id} 应匹配到 {expected_skill}"
        assert hit["skill_name"] == expected_skill, f"{user_id} 应只命中 {expected_skill}，实际 {hit}"
    finally:
        # 清理两个用户的私有技能目录
        shutil.rmtree(os.path.join("skills", user_id), ignore_errors=True)
        shutil.rmtree(os.path.join("skills", user_other), ignore_errors=True)

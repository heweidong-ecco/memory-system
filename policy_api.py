# Policy 控制策略模块
'''
Ledger（不可变账本）和 KV 层（精准读取）。但这两个层本身是“哑”的——它们不会判断什么该记、什么该读、什么该忘。如果没有规则，任何事件都会写入 Ledger，任何数据都会进入 KV 层，最终记忆库会膨胀成一堆无用的垃圾。

Policy 层的作用就是给记忆系统装上“规章制度”。它像公司的人事制度：什么样的人能进公司（写入规则）、什么样的文件能查（读取规则）、什么样的员工该被辞退（遗忘规则）。没有制度，公司会失控；没有 Policy，记忆系统会失控。

具体来说，Policy 回答三个问题：
什么事件应该写入 Ledger？（写入规则）
什么数据应该进入 KV 层？（提升规则）
什么数据应该从 KV 层移除？（降级/遗忘规则）
类比：Ledger 是账本，KV 是保险柜，Policy 是财务制度。账本记录一切，但只有重要的凭证才放进保险柜；
过期的凭证会被碎掉。制度让这一切有序。
'''
#!/usr/bin/env python3
"""Policy 控制策略层——记忆的写入、读取、遗忘规则"""

import json
import time
from typing import Dict, Any, Optional, List
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone

from kv_api import (
    set_profile_value,
    get_profile_value,
    get_profile_meta,
    delete_profile_value,
)
from ledger_api import get_entity_history


# ==================== 配置 ====================
PG_CONN = {
    "dbname": "memory_system",
    "user": "memory_user",
    "password": "memory_pass_2026",
    "host": "localhost",
    "port": 5432,
}


# ==================== 数据库连接 ====================
# 更新使用连接池 连接。
from db_pool import get_cursor
'''def _get_conn():
    """创建数据库连接"""
    return psycopg2.connect(**PG_CONN, cursor_factory=RealDictCursor)
'''


# ==================== Policy 核心规则 ====================

# 1. 写入规则：哪些事件类型应该写入 Ledger？
# 所有事件都写入 Ledger（不可变账本记录一切）
ALLOWED_EVENT_TYPES = {
    "state_change",      # 状态变更
    "tool_call",         # 工具调用
    "skill_call",        # Skills 调用
    "user_input",        # 用户输入
    "rag_retrieval",     # RAG 检索
}

# 2. KV 提升规则：满足什么条件的数据才应该写入 KV 层？
# 五条标准判定（第5天已讨论过）
KV_PROMOTION_RULES = {
    # 键确定性：必须有明确的 key
    "requires_key": True,
    # 高频：跨会话被访问频率（这里简化为需要 access_count >= 阈值）
    "min_access_count": 3,
    # 静态：变化率低（这里简化为需要 version 变化不超过阈值）
    "max_version": 10,
    # 规模：值大小不超过 10KB
    "max_value_size": 10 * 1024,
}

# 3. KV 降级/遗忘规则：什么情况下数据应该从 KV 层移除？
KV_DEMOTION_RULES = {
    # 访问频率衰减：超过 30 天未被访问
    "max_days_since_access": 30,
    # TTL 过期：expires_at 已过
    "check_expiry": True,
}


# ==================== Policy 决策函数 ====================

def should_write_to_ledger(event_type: str) -> bool:
    """判断一个事件是否应该写入 Ledger"""
    return event_type in ALLOWED_EVENT_TYPES


def should_promote_to_kv(
    key: str,
    value_size: int,
    access_count: int = 0,
    version: int = 1,
) -> bool:
    """
    判断一个数据项是否应该写入 KV 层。

    完整使用五条规则：
    1. 键确定性（key 非空）
    2. 规模限制（value_size <= max_value_size）
    3. 稳定性（version <= max_version）
    4. 访问频率（access_count >= min_access_count，新键默认允许）
    """
    # 规则1：键确定性
    if not key or not key.strip():
        return False

    # 规则2：规模限制
    if value_size > KV_PROMOTION_RULES["max_value_size"]:
        return False

    # 规则3：稳定性
    if version > KV_PROMOTION_RULES["max_version"]:
        return False

    # 规则4：访问频率
    # 如果键已存在（version > 1），说明之前写入过，此时检查 access_count
    if version > 1:
        min_access = KV_PROMOTION_RULES["min_access_count"]
        if access_count < min_access:
            return False

    # 新键（version == 1）默认允许进入 KV，后续通过降级规则淘汰
    return True

def should_demote_from_kv(key: str) -> bool:
    """
    判断一个 KV 项是否应该被降级/遗忘。

    规则:
    1. TTL 过期：expires_at 早于当前时间 → 降级
    2. 访问衰减：超过 max_days_since_access 天未访问 → 降级
    3. 【新增】零访问高变更：access_count == 0 且 version > 3 → 降级
    4. 低效键：access_count < 2 且 version > 5 → 降级
    """
    meta = get_profile_meta(key)
    if not meta:
        return False

    now = datetime.now(timezone.utc)

    # 规则1：TTL 过期
    expires_str = meta.get("expires_at")
    if expires_str:
        try:
            expires_dt = datetime.fromisoformat(str(expires_str))
            if expires_dt < now:
                return True
        except ValueError:
            pass

    # 规则2：访问衰减
    last_access_str = meta.get("last_accessed")
    if last_access_str:
        try:
            last_access_dt = datetime.fromisoformat(str(last_access_str))
            if (now - last_access_dt).days > KV_DEMOTION_RULES["max_days_since_access"]:
                return True
        except ValueError:
            pass

    # 规则3：零访问高变更（新增）
    access_count = meta.get("access_count", 0)
    version = meta.get("version", 1)
    if access_count == 0 and version > 3:
        return True

    # 规则4：低效键
    if access_count < 2 and version > 5:
        return True

    return False


def check_rag_reference(entity_id: str, user_id: str = "user_default") -> bool:
    """
    检测某个实体的 KV 或 Summary 中是否包含 RAG 引用标记（按用户隔离）。
    如果包含，则后续需要调用 RAG 检索最新版本。
    """
    # 检查 KV 中是否带有 RAG 引用的字段
    # 例如 key: user_123:rag_reference
    rag_ref = get_profile_value(user_id, f"{entity_id}:rag_reference")
    return rag_ref is not None


# ==================== 策略执行函数 ====================

def promote_to_kv(
    key: str,
    value: Dict[str, Any],
    entity_type: str = "user",
    ttl_seconds: Optional[int] = None,
) -> bool:
    """
    执行 KV 提升策略：判断是否应该写入 KV 层，如果是则写入。

    返回: True 表示成功写入，False 表示被策略拒绝
    """
    value_json = json.dumps(value, ensure_ascii=False)
    value_size = len(value_json.encode("utf-8"))

    if not should_promote_to_kv(key, value_size):
        print(f"  ⚠️ KV 提升被拒绝: {key} (大小 {value_size} bytes)")
        return False

    set_profile_value(key, value, entity_type, ttl_seconds)
    print(f"  ✅ KV 已写入: {key}")
    return True


def demote_from_kv(key: str) -> bool:
    """
    执行 KV 降级策略：判断是否应该从 KV 层移除。

    返回: True 表示已降级（删除），False 表示保留
    """
    if should_demote_from_kv(key):
        delete_profile_value(key)
        print(f"  🗑️ KV 已降级删除: {key}")
        return True
    return False


# ==================== RAG 调用判定规则 ====================
"""
完整的 RAG 调用策略总结
触发条件	场景	判定层
RAG 引用标记存在	之前检索过 RAG，可能已过时	Policy 规则
内部四层全部未命中	用户问了系统从未记录过的问题	Policy 规则
查询包含外部知识关键词	“最新文档”、“API 规范”等	Policy 规则
内部四层命中但结果不完整	KV 有部分数据但不够回答问题	Policy 规则（需要定义“不完整”标准）

“不完整”的标准怎么定义
“不完整”是一个模糊概念，必须量化为可判定的指标。先定义四个维度：
判定维度	不完整的表现	量化指标
结果数量不足	内部层返回的结果条数太少	KV 命中数 = 0 但 Summary 命中数 < 2 条
置信度不足	相似度分数太低，不可靠	Summary 最高相似度 < 0.6
字段覆盖不全	检索到的数据缺少回答所需的关键字段	缺少用户问题中提到的核心概念
时效性不满足	检索到的数据可能是过时的	数据时间戳早于某个阈值
简化落地：先用“结果数量”和“置信度”两个硬指标，字段覆盖和时效性后续再扩展。
"""
def is_result_complete(
    kv_result: Optional[Dict] = None,
    summary_result: Optional[Dict] = None,
    summary_count: int = 0,
    summary_confidence: float = 0.0,
) -> bool:
    """
    判断内部检索结果是否“完整”。

    判定标准：
    1. 如果 KV 层命中（有精确值），通常认为完整——除非用户需要的是开放知识
    2. 如果 Summary 层命中且置信度足够（>= 0.6），且返回数量 >= 2，认为完整
    3. 否则不完整

    参数:
    - kv_result: KV 层检索结果
    - summary_result: Summary 层检索结果
    - summary_count: Summary 层返回的结果条数
    - summary_confidence: Summary 层最高相似度分数

    返回: True = 完整（不需要 RAG），False = 不完整（需要 RAG）
    """
    # KV 层命中精确值，通常足够完整
    if kv_result is not None:
        return True

    # Summary 层命中且置信度足够且数量足够
    if summary_result is not None and summary_confidence >= 0.6 and summary_count >= 2:
        return True

    # 其他情况都认为不完整
    return False


def should_call_rag(
    entity_id: str,
    query: str = None,
    kv_result: Optional[Dict] = None,
    summary_result: Optional[Dict] = None,
    summary_count: int = 0,
    summary_confidence: float = 0.0,
    ledger_result: Optional[List] = None,
    user_id: str = "user_default",
) -> bool:
    """
    判断当前任务是否需要调用 RAG。

    四种触发条件（满足任意一个即调用）：
    1. KV/Summary 中有 rag_reference 标记（之前的 RAG 检索可能已过时）
    2. 内部四层检索结果为空（完全未命中）
    3. 内部四层有命中，但结果不完整（数量不足/置信度不足）
    4. 用户问题本身包含外部知识特征

    返回: True = 需要调用 RAG，False = 不需要
    """
    # 条件 1：RAG 引用标记
    if check_rag_reference(entity_id, user_id):
        return True

    # 条件 2：内部完全未命中
    if kv_result is None and summary_result is None and not ledger_result:
        return True

    # 条件 3：内部有命中但不完整（新增）
    # 只有当内部有部分命中（非完全未命中）时，才检查完整性
    if (kv_result is not None) or (summary_result is not None) or ledger_result:
        if not is_result_complete(kv_result, summary_result, summary_count, summary_confidence):
            return True

    # 条件 4：外部知识关键词
    if query:
        external_keywords = [
            "最新", "文档", "API 规范", "官方", "版本更新",
            "latest", "documentation", "API reference", "changelog",
        ]
        if any(keyword in query for keyword in external_keywords):
            return True

    return False

# ===== 对一个实体执行完整的 Policy 检查 函数======

def run_policy_check(entity_id: str):
    """
    对一个实体执行完整的 Policy 检查：
    1. 检查 RAG 引用
    2. 检查 KV 层中是否有需要降级的数据
    """
    print(f"\n[Policy 检查] entity_id: {entity_id}")
    
    # 检查 RAG 引用
    has_rag_ref = check_rag_reference(entity_id)
    print(f"  RAG 引用: {'有，需要调用 RAG' if has_rag_ref else '无，跳过 RAG'}")

    # 检查 KV 降级：遍历该实体的所有 KV 键
    with get_cursor() as  cur:
        cur.execute(
            """
            SELECT key FROM user_profile
            WHERE key LIKE %s
            """,
            (f"{entity_id}:%",),
        )
        keys = [row["key"] for row in cur.fetchall()]

    for key in keys:
        demote_from_kv(key)

# ==================== 测试 ====================

def main():
    print("=" * 60)
    print("  Policy 控制策略层测试")
    print("=" * 60)

    # 测试 1：写入规则
    print("\n[测试1] 写入规则")
    print(f"  state_change 允许写入: {should_write_to_ledger('state_change')}")
    print(f"  tool_call 允许写入: {should_write_to_ledger('tool_call')}")
    print(f"  unknown_event 允许写入: {should_write_to_ledger('unknown_event')}")

    # 测试 2：KV 提升策略
    print("\n[测试2] KV 提升策略")
    promote_to_kv(
        key="user_123:preference:theme",
        value={"theme": "dark", "font_size": 14},
    )
    promote_to_kv(
        key="user_123:large_data",
        value={"data": "x" * 20 * 1024},  # 20KB，超过阈值
    )

    # 测试 3：RAG 引用检测
    print("\n[测试3] RAG 引用检测")
    # 写入一个 RAG 引用标记
    set_profile_value(
        "user_123:rag_reference",
        {"rag_doc_id": "v1.0.0", "retrieved_at": "2026-09-26"},
        entity_type="rag",
    )
    has_rag = check_rag_reference("user_123")
    print(f"  检测结果: {'有 RAG 引用' if has_rag else '无 RAG 引用'}")

    # 测试 4：完整 Policy 检查
    print("\n[测试4] 完整 Policy 检查")
    run_policy_check("user_123")

    print("\n✅ Policy 控制策略层验证完成")


if __name__ == "__main__":
    main()
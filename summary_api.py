# 创建 Recent Summary 模块
"""
Recent Summary 就是 Agent 的“周报”——它告诉你“最近发生了什么”，而不是“每一秒发生了什么”。
摘要和 Ledger 的关系：

Ledger 是“原始流水”——每一笔都记录，永不删除
Summary 是“压缩归档”——把流水提炼为趋势和模式，替代原始上下文
摘要和 KV 的区别：

KV 是“精确单点”——某个 key 的当前值，零噪声
Summary 是“模糊全景”——一段时间的语义走向，允许模糊
Summary 独特的价值：它记录的不只是“状态变了什么”，还包括“Agent 在什么任务下调用了什么工具、什么技能、结果如何”。这些行为模式是 Agent 自我进化的重要原材料。
"""
#!/usr/bin/env python3
"""Recent Summary 近期摘要层——分层滚动记忆（生产级修正版）

修正内容：
1. 修复取数方向 bug：取最近的 N 条事件，而非最早的
2. 摘要多行存储，不覆盖旧摘要
3. 查询时返回最近 N 条摘要的组合视图
4. 控制滚动摘要总数，防止无限膨胀
"""

import json
import os
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import psycopg2
import requests
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

from ledger_api import get_entity_history

load_dotenv()

# ==================== 配置 ====================
PG_CONN = {
    "dbname": "memory_system",
    "user": "memory_user",
    "password": "memory_pass_2026",
    "host": "localhost",
    "port": 5432,
}

SUMMARY_TRIGGER_THRESHOLD = 10      # 事件数阈值，触发生成摘要
MAX_ROLLING_SUMMARIES = 5           # 最多保留的滚动摘要数量
SUMMARY_LOOKBACK_COUNT = 3          # Views 返回最近 N 条摘要
MAX_SUMMARY_LENGTH = 1500           # 单条摘要最大字符数

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")


# ==================== 数据库连接 ====================
# 更新使用连接池 连接。
from db_pool import get_cursor
'''def _get_conn():
    """创建数据库连接"""
    return psycopg2.connect(**PG_CONN, cursor_factory=RealDictCursor)
'''


def _to_dict(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


# ==================== 摘要生成 ====================

def generate_summary(entity_id: str, events: List[Dict]) -> Dict[str, Any]:
    """为指定事件列表生成摘要"""
    if not events:
        return {"content": "", "tool_call_pattern": "", "skill_call_pattern": ""}

    summary_text = _call_llm_for_summary(entity_id, events)
    tool_pattern = _extract_tool_pattern(events)
    skill_pattern = _extract_skill_pattern(events)

    return {
        "content": summary_text,
        "tool_call_pattern": tool_pattern,
        "skill_call_pattern": skill_pattern,
    }


def _call_llm_for_summary(entity_id: str, events: List[Dict]) -> str:
    """调用 LLM 生成结构化摘要"""
    if not DEEPSEEK_API_KEY:
        return _generate_rule_based_summary(entity_id, events)

    events_json = json.dumps(events, ensure_ascii=False, default=str)
    prompt = f"""请为实体 {entity_id} 生成一段近期摘要。

以下是最近 {len(events)} 条事件（按时间正序排列）：
{events_json}

请包含：
1. 状态变更总结
2. 工具调用模式
3. 技能调用模式

控制在 300 字以内。"""

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
                "temperature": 0.0,
                "max_tokens": 500,
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  ⚠️ LLM 摘要生成失败: {e}，回退到规则摘要")
        return _generate_rule_based_summary(entity_id, events)


def _generate_rule_based_summary(entity_id: str, events: List[Dict]) -> str:
    state_changes = [e for e in events if e.get("event_type") == "state_change"]
    tool_calls = [e for e in events if e.get("event_type") == "tool_call"]
    skill_calls = [e for e in events if e.get("event_type") == "skill_call"]

    parts = []
    if state_changes:
        latest = state_changes[-1]
        parts.append(f"最新状态: {latest.get('new_value')}")
    if tool_calls:
        parts.append(f"工具调用 {len(tool_calls)} 次")
    if skill_calls:
        parts.append(f"技能调用 {len(skill_calls)} 次")

    return "。".join(parts) + f"。（共 {len(events)} 条事件）"


def _extract_tool_pattern(events: List[Dict]) -> str:
    tool_calls = [e for e in events if e.get("event_type") == "tool_call"]
    if not tool_calls:
        return ""
    tool_names = {}
    for call in tool_calls:
        data = _to_dict(call.get("event_data"))
        name = data.get("tool_name", "unknown") if data else "unknown"
        tool_names[name] = tool_names.get(name, 0) + 1
    return ", ".join(f"{n}({c}次)" for n, c in sorted(tool_names.items(), key=lambda x: -x[1]))


def _extract_skill_pattern(events: List[Dict]) -> str:
    skill_calls = [e for e in events if e.get("event_type") == "skill_call"]
    if not skill_calls:
        return ""
    skill_names = {}
    for call in skill_calls:
        data = _to_dict(call.get("event_data"))
        name = data.get("skill_name", "unknown") if data else "unknown"
        skill_names[name] = skill_names.get(name, 0) + 1
    return ", ".join(f"{n}({c}次)" for n, c in sorted(skill_names.items(), key=lambda x: -x[1]))


# ==================== 摘要存储 ====================

def save_summary(
    entity_id: str,
    content: str,
    tool_pattern: str,
    skill_pattern: str,
    start_time: str,
    end_time: str,
    summary_type: str = "rolling",
    user_id: str = "user_default",
) -> int:
    """保存摘要，带时间范围（user_id 用于多用户隔离）"""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO summary
                (user_id, entity_id, content, summary_type, tool_call_pattern,
                    skill_call_pattern, start_timestamp, end_timestamp,
                    created_at, token_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
            RETURNING summary_id
            """,
            (
                user_id,
                entity_id,
                content,
                summary_type,
                tool_pattern,
                skill_pattern,
                start_time,
                end_time,
                len(content) // 4,
            ),
        )
        summary_id = cur.fetchone()["summary_id"]
    return summary_id


def get_recent_summaries(entity_id: str, limit: int = SUMMARY_LOOKBACK_COUNT, user_id: str = "user_default") -> List[Dict[str, Any]]:
    """获取最近 N 条摘要，按时间倒序（最新的在前），按用户隔离"""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT summary_id, entity_id, content, summary_type,
                tool_call_pattern, skill_call_pattern,
                start_timestamp, end_timestamp, created_at,
                token_count
            FROM summary
            WHERE user_id = %s AND entity_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (user_id, entity_id, limit),
        )
        rows = cur.fetchall()

    results = []
    for row in rows:
        r = dict(row)
        r["start_timestamp"] = str(r.get("start_timestamp"))
        r["end_timestamp"] = str(r.get("end_timestamp"))
        r["created_at"] = str(r.get("created_at"))
        results.append(r)
    return results


# ==================== 滚动更新（核心修正） ====================

def update_summary(entity_id: str, user_id: str = "user_default") -> Optional[Dict[str, Any]]:
    """
    从 Ledger 中取最近的 N 条事件生成摘要，保存为新行。

    修正点：
    - 先按 DESC 取最近 N 条，再反转为正序供 LLM 阅读
    - 不覆盖旧摘要，而是追加新行
    - 生成后清理最旧的滚动摘要
    """
    # 修复：先取最近的 N 条（DESC），再反转为正序
    events = get_entity_history(user_id, entity_id, limit=SUMMARY_TRIGGER_THRESHOLD, ascending=False)
    events = list(reversed(events))  # 正序排列

    if len(events) < SUMMARY_TRIGGER_THRESHOLD:
        print(f"  ⚠️ 事件数 {len(events)} < 阈值 {SUMMARY_TRIGGER_THRESHOLD}，跳过")
        return None

    # 时间范围
    start_time = events[0].get("timestamp")
    end_time = events[-1].get("timestamp")

    summary = generate_summary(entity_id, events)

    # 长度控制
    if len(summary["content"]) > MAX_SUMMARY_LENGTH:
        summary["content"] = summary["content"][:MAX_SUMMARY_LENGTH] + "\n\n[摘要过长，已截断]"

    # 保存新摘要（新增行，不覆盖）
    summary_id = save_summary(
        entity_id,
        summary["content"],
        summary["tool_call_pattern"],
        summary["skill_call_pattern"],
        start_time,
        end_time,
        summary_type="rolling",
        user_id=user_id,
    )

    # 清理旧的滚动摘要（只清理当前用户的）
    _prune_old_summaries(user_id, entity_id, max_keep=MAX_ROLLING_SUMMARIES)

    print(f"  ✅ 新摘要已保存 (id={summary_id})，覆盖 {start_time} ~ {end_time}")
    return summary


def _prune_old_summaries(user_id: str, entity_id: str, max_keep: int = MAX_ROLLING_SUMMARIES):
    """删除最旧的滚动摘要，只保留最近 max_keep 条（按用户隔离）"""
    with get_cursor() as cur:
        cur.execute(
            """
            DELETE FROM summary
            WHERE summary_id IN (
                SELECT summary_id FROM summary
                WHERE user_id = %s AND entity_id = %s AND summary_type = 'rolling'
                ORDER BY created_at DESC
                OFFSET %s
            )
            """,
            (user_id, entity_id, max_keep),
        )


# ==================== Views 层集成 ====================

def search_recent_summary(entity_id: str, query: str = None, user_id: str = "user_default") -> Optional[Dict[str, Any]]:
    """给 Views 层提供近期摘要的组合视图"""
    summaries = get_recent_summaries(entity_id, limit=SUMMARY_LOOKBACK_COUNT, user_id=user_id)
    if not summaries:
        return None

    combined_content = "\n\n".join(
        f"【{s['created_at']}】{s['content']}" for s in summaries
    )
    tool_patterns = "; ".join(s["tool_call_pattern"] for s in summaries if s["tool_call_pattern"])
    skill_patterns = "; ".join(s["skill_call_pattern"] for s in summaries if s["skill_call_pattern"])

    return {
        "content": combined_content,
        "tool_call_pattern": tool_patterns,
        "skill_call_pattern": skill_patterns,
        "summary_count": len(summaries),
        "latest_summary_time": summaries[0]["created_at"],
    }


# ==================== 测试 ====================

def main():
    print("=" * 60)
    print("  Recent Summary 分层滚动记忆测试（修正版）")
    print("=" * 60)

    entity_id = "user_123"
    user_id = "user_default"

    print("\n[测试1] 触发滚动更新")
    result = update_summary(entity_id, user_id=user_id)
    if result:
        print(f"  工具模式: {result['tool_call_pattern']}")
        print(f"  技能模式: {result['skill_call_pattern']}")

    print("\n[测试2] 查询最近 3 条摘要")
    summaries = get_recent_summaries(entity_id, limit=3, user_id=user_id)
    print(f"  共 {len(summaries)} 条摘要")
    for s in summaries:
        print(f"  [{s['created_at']}] {s['content'][:100]}...")

    print("\n[测试3] Views 集成读取")
    views_result = search_recent_summary(entity_id, user_id=user_id)
    if views_result:
        print(f"  摘要数: {views_result['summary_count']}")
        print(f"  内容前 300 字符:\n{views_result['content'][:300]}")

    print("\n✅ Recent Summary 分层滚动记忆验证完成")


if __name__ == "__main__":
    main()
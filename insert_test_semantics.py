# 测试数据没有覆盖查询意图
# vector_semantic.py 的测试数据上传
"""
你的查询是"数据库查询慢了"，但 Ledger 中根本没有数据库相关的记录。所有记录都是"查天气""查股市""订机票"。

这不是代码 bug，是测试数据问题。 检索系统只能返回它"见过"的内容。如果语料库中没有数据库相关内容，任何检索算法都返回不了正确结果。
"""
# 插入几条与数据库性能相关的测试数据
#!/usr/bin/env python3
"""插入有区分度的测试数据，验证混合检索效果"""

from event_bus import EventBus

bus = EventBus()
entity_id = "user_123"

test_events = [
    ("tool_call", {
        "tool_name": "database_profiler",
        "input": "分析慢查询日志",
        "output": "发现 user_id 字段缺少索引，导致全表扫描",
        "status": "success",
    }),
    ("skill_call", {
        "skill_name": "db_optimizer",
        "trigger": "数据库查询变慢",
        "output": "已为 user_id 添加索引，查询从 2.3s 降至 0.02s",
        "status": "success",
    }),
    ("tool_call", {
        "tool_name": "web_search",
        "input": "查询上海天气",
        "output": "小雨",
        "status": "success",
    }),
]

for event_type, data in test_events:
    bus.record_event(
        event_type=event_type,
        entity_id=entity_id,
        event_data=data,
        source_agent="test_agent",
        session_id="session_semantic_test",
    )

bus.close()
print("✅ 已插入 3 条有区分度的测试数据")
# 创建真实的成功任务数据

#!/usr/bin/env python3
"""插入真实的成功任务会话，用于测试程序性记忆提炼"""

from event_bus import EventBus

bus = EventBus()
entity_id = "user_123"

# 三个完整会话，签名各不相同但意图相同（订机票）
sessions = [
    {
        "session_id": "real_session_1",
        "query": "帮我订一张去北京的机票",
        "tool_call": {
            "tool_name": "web_search",
            "input": "去北京的航班",
            "output": "找到 3 个航班：CA1234、MU5678、CZ9012",
        },
        "skill_call": {
            "skill_name": "flight_booking",
            "trigger": "用户要求订机票",
            "output": "预订成功，订单号 BK-REAL-001，航班 CA1234",
        },
    },
    {
        "session_id": "real_session_2",
        "query": "我想飞北京，帮我订票",
        "tool_call": {
            "tool_name": "flight_search",   # 注意：不同的工具名
            "input": "北京航班",
            "output": "找到 5 个航班：CA1234、MU5678、CZ9012、HU3456、MF7890",
        },
        "skill_call": {
            "skill_name": "booking_service",  # 注意：不同的技能名
            "trigger": "订票请求",
            "output": "预订成功，订单号 BK-REAL-002，航班 CA1234",
        },
    },
    {
        "session_id": "real_session_3",
        "query": "订一张去北京的机票",
        "tool_call": {
            "tool_name": "web_search",
            "input": "去北京机票",
            "output": "找到 4 个航班：CA1234、MU5678、CZ9012、HU3456",
        },
        "skill_call": {
            "skill_name": "flight_booking",
            "trigger": "用户要求订机票",
            "output": "预订成功，订单号 BK-REAL-003，航班 CA1234",
        },
    },
]

for s in sessions:
    # 1. 写入 user_input 事件
    bus.record_event(
        event_type="user_input",
        entity_id=entity_id,
        event_data={"text": s["query"]},
        source_agent="travel_agent",
        session_id=s["session_id"],
    )

    # 2. 写入 tool_call 事件（成功）
    bus.record_event(
        event_type="tool_call",
        entity_id=entity_id,
        event_data={
            "tool_name": s["tool_call"]["tool_name"],
            "input": s["tool_call"]["input"],
            "output": s["tool_call"]["output"],
            "status": "success",
        },
        source_agent="travel_agent",
        session_id=s["session_id"],
    )

    # 3. 写入 skill_call 事件（成功）
    bus.record_event(
        event_type="skill_call",
        entity_id=entity_id,
        event_data={
            "skill_name": s["skill_call"]["skill_name"],
            "trigger": s["skill_call"]["trigger"],
            "output": s["skill_call"]["output"],
            "status": "success",
        },
        source_agent="travel_agent",
        session_id=s["session_id"],
    )

    print(f"✅ 已写入会话 {s['session_id']}")

bus.close()
print("\n完成。三个真实会话已写入 Ledger 和候选库。")
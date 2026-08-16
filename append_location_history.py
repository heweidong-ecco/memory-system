# 写入杭州 → 成都 → 北京的完整事件链
# 先确保 Ledger 中有这条完整的状态变更链。
# 在 event_bus.py 的测试主函数基础上，用以下脚本追加北京状态：
# append_location_history.py
from event_bus import EventBus

bus = EventBus()

# 模拟完整状态变更链：杭州 → 成都 → 北京
session_id = "session_20260924_001"

bus.record_event(
    event_type="state_change",
    entity_id="user_123",
    event_data={"field": "location"},
    old_value={"city": "成都"},
    new_value={"city": "北京"},
    source_agent="travel_agent",
    session_id=session_id,
)

print("已追加 北京 状态变更")
bus.close()
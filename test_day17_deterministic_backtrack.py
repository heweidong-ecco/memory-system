# 测试脚本：
# 覆盖确定性、回溯、多租户隔离。
# 数据库连接与测试数据清理统一由 conftest.py 管理（clean_db autouse fixture）。
import pytest
import time

from unified_api import record_event
from ledger_api import get_entity_history, get_current_state, get_events_by_time_range
from kv_api import get_profile_value, set_profile_value

def test_booking_scenario_杭州成都北京():
    """场景1：杭州→成都→北京订票，确定性查询和回溯"""
    user_id = "user_test_A"
    entity_id = "location"

    # 记录三次状态变更：杭州 → 成都 → 北京
    # 约定：state_change 事件的 event_data 必须含 field 键，get_current_state 按它定位
    record_event(
        user_id=user_id,
        event_type="state_change",
        entity_id=entity_id,
        event_data={"field": "location"},
        new_value={"location": "杭州"},
        source_agent="booking_agent",
        session_id="test_session_1",
    )
    time.sleep(0.01)  # 确保时间戳不同
    record_event(
        user_id=user_id,
        event_type="state_change",
        entity_id=entity_id,
        event_data={"field": "location"},
        old_value={"location": "杭州"},
        new_value={"location": "成都"},
        source_agent="booking_agent",
        session_id="test_session_1",
    )
    time.sleep(0.01)
    record_event(
        user_id=user_id,
        event_type="state_change",
        entity_id=entity_id,
        event_data={"field": "location"},
        old_value={"location": "成都"},
        new_value={"location": "北京"},
        source_agent="booking_agent",
        session_id="test_session_1",
    )

    # 验证当前状态：北京
    current = get_current_state(user_id, entity_id, "location")
    assert current is not None
    assert current.get("location") == "北京", f"当前状态应为北京，实际为{current}"

    # 验证历史回溯：完整且倒序
    history = get_entity_history(user_id, entity_id, limit=10, ascending=False)
    assert len(history) >= 3, "历史记录应至少3条"
    # 第一条应是最近的（北京）
    assert history[0]["new_value"].get("location") == "北京"
    # 最后一条应是最早的（杭州）
    assert history[-1]["new_value"].get("location") == "杭州"
    # 中间是成都
    assert history[1]["new_value"].get("location") == "成都"

def test_kv_zero_noise():
    """场景2：高频属性读取，KV 零噪声验证"""
    user_a = "user_test_A"
    user_b = "user_test_B"

    # 写入两个用户的同名key（模拟 entity_id 相同，但 user_id 隔离）
    key = "profile:age"
    set_profile_value(key, {"value": "30"}, user_id=user_a)
    set_profile_value(key, {"value": "25"}, user_id=user_b)

    # 用户A读取100次，结果始终为30
    for _ in range(100):
        val = get_profile_value(user_a, key)
        assert val == {"value": "30"}, f"用户A读取应为30，实际为{val}"

    # 用户B读取，结果为25，不受影响
    val_b = get_profile_value(user_b, key)
    assert val_b == {"value": "25"}, f"用户B读取应为25，实际为{val_b}"

    # 确保没有模糊匹配引入噪声：查询一个不存在的key应返回None
    val_none = get_profile_value(user_a, "profile:nonexistent")
    assert val_none is None

def test_ledger_backtrack():
    """场景3：Ledger 回溯查询，按时间戳倒序正确"""
    user_id = "user_test_C"
    entity_id = "order"

    # 插入三个事件，时间间隔稍长以便观察
    for i in range(1, 4):
        record_event(
            user_id=user_id,
            event_type="state_change",
            entity_id=entity_id,
            event_data={"step": i},
            new_value={"step": i},
            source_agent="test_agent",
            session_id="ledger_test",
        )
        time.sleep(0.01)

    # 查询全部历史
    history = get_entity_history(user_id, entity_id, limit=10, ascending=False)
    assert len(history) == 3
    # 倒序：step 3, 2, 1
    assert [h["new_value"]["step"] for h in history] == [3, 2, 1]

    # 测试时间范围查询（假设 get_events_by_time_range 存在）
    # 这里只验证 get_entity_history 的倒序正确
    # 如果实现了时间范围查询，可补充

def test_multi_tenant_isolation():
    """场景4：多租户隔离，用户A和B数据互不可见"""
    user_a = "user_test_A"
    user_b = "user_test_B"

    # 用户A写入位置
    record_event(
        user_id=user_a,
        event_type="state_change",
        entity_id="location",
        event_data={"field": "location"},
        new_value={"location": "上海"},
    )
    # 用户B写入位置（同一 entity_id，靠 user_id 隔离）
    record_event(
        user_id=user_b,
        event_type="state_change",
        entity_id="location",
        event_data={"field": "location"},
        new_value={"location": "深圳"},
    )

    # 用户A查询自己的位置，应为上海
    current_a = get_current_state(user_a, "location", "location")
    assert current_a["location"] == "上海"

    # 用户B查询自己的位置，应为深圳
    current_b = get_current_state(user_b, "location", "location")
    assert current_b["location"] == "深圳"

    # 隔离性验证完成：A 得到上海，B 得到深圳，同一 entity_id 互不串扰


# ==================== 补充测试 ====================

def test_kv_high_frequency_read_and_access_count():
    """场景2补充：连续读取同一 KV 键 1000 次，验证零噪声并检查 access_count 递增"""
    user_id = "user_test_KV"
    key = "profile:counter"

    # 写入初始值，确保 access_count = 0
    set_profile_value(key, {"value": "42"}, user_id=user_id)

    # 读取前查询 access_count（应为 0）
    from db_pool import get_cursor
    with get_cursor() as cur:
        cur.execute(
            "SELECT access_count FROM user_profile WHERE user_id=%s AND key=%s",
            (user_id, key),
        )
        row = cur.fetchone()
    assert row is not None, "写入后应能在 user_profile 中找到记录"
    initial_count = row["access_count"]
    assert initial_count == 0, f"初始 access_count 应为 0，实际为 {initial_count}"

    # 连续读取 1000 次。
    # 注意：get_profile_value 是 Cache-Aside——命中缓存时不会访问 DB，
    # access_count 只在 DB 命中时递增。set_profile_value 已预热缓存，
    # 因此这里每次读前清缓存，强制走 DB，验证 access_count 准确记录 DB 读取次数。
    from cache_manager import cache_delete, kv_key
    _entity, _field = key.split(":", 1)
    for i in range(1000):
        cache_delete(kv_key(user_id, _entity, _field))
        val = get_profile_value(user_id, key)
        assert val == {"value": "42"}, f"第 {i} 次读取结果错误：{val}"

    # 读取后查询 access_count（应为 1000）
    with get_cursor() as cur:
        cur.execute(
            "SELECT access_count FROM user_profile WHERE user_id=%s AND key=%s",
            (user_id, key),
        )
        row = cur.fetchone()
    final_count = row["access_count"]
    assert final_count == 1000, f"读取 1000 次后 access_count 应为 1000，实际为 {final_count}"


def test_ledger_append_only_enforced():
    """场景3补充：验证 Ledger 表禁止 UPDATE 和 DELETE，实现 Append-Only 约束"""
    user_id = "user_test_appendonly"
    entity_id = "append_test"

    # 先写入一条事件
    record_event(
        user_id=user_id,
        event_type="state_change",
        entity_id=entity_id,
        event_data={"field": "value"},
        new_value={"value": "original"},
    )

    # 查询该事件的 ledger_id
    from db_pool import get_cursor
    with get_cursor() as cur:
        cur.execute(
            "SELECT ledger_id FROM ledger WHERE user_id=%s AND entity_id=%s ORDER BY timestamp DESC LIMIT 1",
            (user_id, entity_id),
        )
        row = cur.fetchone()
    assert row is not None, "未找到刚写入的 Ledger 事件"
    ledger_id = row["ledger_id"]

    # 尝试 UPDATE，应抛出异常（触发器阻止）。
    # new_value 是 JSONB，需用 Json() 包装，否则 psycopg2 在参数适配阶段就报
    # "can't adapt type 'dict'"，触发器根本不会触发。包装后 SQL 才真正执行、被触发器拦截。
    from psycopg2.extras import Json
    with pytest.raises(Exception) as exc_info:
        with get_cursor() as cur:
            cur.execute(
                "UPDATE ledger SET new_value = %s WHERE ledger_id = %s",
                (Json({"value": "hacked"}), ledger_id),
            )
    # 触发器抛出 RaiseException，异常信息包含 "append-only"
    assert "append" in str(exc_info.value).lower() or "denied" in str(exc_info.value).lower(), \
        f"异常信息不符合预期：{exc_info.value}"

    # 尝试 DELETE，同样应被拒绝
    with pytest.raises(Exception) as exc_info2:
        with get_cursor() as cur:
            cur.execute(
                "DELETE FROM ledger WHERE ledger_id = %s",
                (ledger_id,),
            )
    assert "append" in str(exc_info2.value).lower() or "denied" in str(exc_info2.value).lower(), \
        f"异常信息不符合预期：{exc_info2.value}"

    # 验证原记录未被篡改
    with get_cursor() as cur:
        cur.execute(
            "SELECT new_value FROM ledger WHERE ledger_id=%s",
            (ledger_id,),
        )
        row = cur.fetchone()
    assert row is not None, "记录应仍然存在"
    assert row["new_value"]["value"] == "original", "原记录不应被修改"


# ==================== 参数化多租户隔离 ====================

@pytest.mark.parametrize("entity_id", ["location", "preference", "order"])
@pytest.mark.parametrize("user_a,user_b", [
    ("user_test_A", "user_test_B"),
    ("user_test_C", "user_test_D"),
])
def test_multi_tenant_isolation_param(user_a, user_b, entity_id):
    """多租户隔离（参数化）：多用户 × 多 entity_id，同一 entity_id 下各用户数据互不可见"""
    record_event(
        user_id=user_a,
        event_type="state_change",
        entity_id=entity_id,
        event_data={"field": "val"},
        new_value={"val": f"{user_a}-{entity_id}"},
    )
    record_event(
        user_id=user_b,
        event_type="state_change",
        entity_id=entity_id,
        event_data={"field": "val"},
        new_value={"val": f"{user_b}-{entity_id}"},
    )

    current_a = get_current_state(user_a, entity_id, "val")
    current_b = get_current_state(user_b, entity_id, "val")
    assert current_a["val"] == f"{user_a}-{entity_id}", f"A 应读到自己的数据：{current_a}"
    assert current_b["val"] == f"{user_b}-{entity_id}", f"B 应读到自己的数据：{current_b}"
    assert current_a != current_b, "同一 entity_id 下 A/B 不应互相串扰"


# ==================== Ledger 时间范围查询 ====================

def test_ledger_time_range_query():
    """Ledger 时间范围查询：插入带已知时间戳的事件，查询区间返回正确子集"""
    user_id = "user_test_time"
    entity_id = "timerange"

    # 直接 INSERT 指定时间戳。
    # 注意：get_entity_history 本身没有时间范围参数，时间范围查询走 get_events_by_time_range。
    # ledger 的 append-only 触发器只拦截 UPDATE/DELETE，INSERT 不受影响，因此可带显式 timestamp。
    from psycopg2.extras import Json
    from db_pool import get_cursor
    events = [
        ("2026-08-10 08:00:00+00:00", 1),
        ("2026-08-10 09:00:00+00:00", 2),
        ("2026-08-10 10:00:00+00:00", 3),
        ("2026-08-10 11:00:00+00:00", 4),
        ("2026-08-10 12:00:00+00:00", 5),
    ]
    with get_cursor() as cur:
        for ts, step in events:
            cur.execute(
                """
                INSERT INTO ledger (user_id, entity_id, event_type, event_data, new_value, timestamp)
                VALUES (%s, %s, 'state_change', %s, %s, %s)
                """,
                (user_id, entity_id, Json({"field": "step"}), Json({"step": step}), ts),
            )

    # 区间 09:00 ~ 11:00（含边界）→ step 2,3,4
    result = get_events_by_time_range(
        user_id, entity_id,
        "2026-08-10 09:00:00+00:00", "2026-08-10 11:00:00+00:00",
    )
    steps = [r["new_value"]["step"] for r in result]
    assert steps == [2, 3, 4], f"时间区间应返回 [2,3,4]，实际 {steps}"

    # 10:30 之后 → step 4,5
    result2 = get_events_by_time_range(
        user_id, entity_id,
        "2026-08-10 10:30:00+00:00", "2026-08-10 23:00:00+00:00",
    )
    assert [r["new_value"]["step"] for r in result2] == [4, 5]

    # 空区间 → 空列表
    result3 = get_events_by_time_range(
        user_id, entity_id,
        "2026-08-11 00:00:00+00:00", "2026-08-11 01:00:00+00:00",
    )
    assert result3 == []
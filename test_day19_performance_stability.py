# 测试脚本：性能与稳定性（匹配真实 API 的重写版）
# 数据库连接与测试数据清理统一由 conftest.py 管理（clean_db autouse fixture）。
# 本脚本只做测试，不改任何原代码。
import json
import time
import concurrent.futures
from datetime import datetime, timezone

from unified_api import record_event
from ledger_api import get_entity_history, get_current_state
from kv_api import get_profile_value, set_profile_value
from summary_api import get_recent_summaries, save_summary
from vector_semantic import vector_search, sync_new_events
from rag_knowledge import (
    add_external_knowledge,
    search_rag_knowledge,
    rag_conditional_search,
    get_latest_rag_snapshot,
)
from db_pool import get_cursor
from cache_manager import redis_client, tool_key, skill_key, cache_get


def time_func(func, *args, **kwargs):
    """执行函数并返回耗时（秒）和结果"""
    start = time.perf_counter()
    result = func(*args, **kwargs)
    elapsed = time.perf_counter() - start
    return elapsed, result


# ==================== 性能报表（JSON 归档，便于后续对比） ====================
REPORT_PATH = "perf_baselines.json"


def _append_perf_report(entry):
    """把一次性能/压力记录追加到 JSON 数组，保留历史便于对比"""
    try:
        with open(REPORT_PATH, "r", encoding="utf-8") as f:
            history = json.load(f)
        if not isinstance(history, list):
            history = [history]
    except (FileNotFoundError, json.JSONDecodeError):
        history = []
    history.append(entry)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"[report] 已写入 {REPORT_PATH}（累计 {len(history)} 条）")


# ==================== 场景 1：各层响应时间基线 ====================

def test_performance_baseline():
    """场景1：各层响应时间基线（KV / Summary / Ledger / Vector / RAG）"""
    user_id = "user_test_perf"
    entity_id = "perf_entity"

    # ---- 准备数据 ----
    # KV
    set_profile_value("profile:age", {"value": "30"}, user_id=user_id)
    # Ledger：5 条状态变更
    for i in range(5):
        record_event(user_id=user_id, event_type="state_change", entity_id=entity_id,
                     event_data={"field": "location"}, new_value={"location": f"city_{i}"},
                     session_id=f"perf_{i}")
    # Summary：直接写一条摘要（避免 LLM 依赖）
    save_summary(entity_id, "性能基线测试摘要", "web_search", "",
                 "2026-08-16 00:00:00+00:00", "2026-08-16 01:00:00+00:00",
                 user_id=user_id)
    # Vector：把 ledger 事件同步进向量库（sync_new_events 内部去重）
    sync_result = sync_new_events(entity_id, last_synced_ledger_id=0, user_id=user_id)
    # RAG：写入一条外部知识
    add_external_knowledge(source="test-perf-doc", title="性能测试文档",
                           content="这是用于性能基线的测试知识", version="v1", tenant_id="public")

    # ---- 逐层测耗时（每层 5 次取平均，打印原始耗时）----
    layers = {}

    def bench(name, fn):
        times = []
        for _ in range(5):
            el, _ = time_func(fn)
            times.append(el)
        avg = sum(times) / len(times)
        layers[name] = {"times": times, "avg": avg}
        print(f"[perf] {name}: raw={[round(t, 4) for t in times]}s  avg={avg:.4f}s")

    bench("KV", lambda: get_profile_value(user_id, "profile:age"))
    bench("Summary", lambda: get_recent_summaries(entity_id, limit=3, user_id=user_id))
    bench("Ledger", lambda: get_entity_history(user_id, entity_id, limit=5))
    bench("Vector", lambda: vector_search(entity_id, "位置", limit=3, user_id=user_id))
    bench("RAG", lambda: search_rag_knowledge("性能测试文档", tenant_id="public"))

    # ---- 断言（Vector/RAG 含 Ollama embedding，阈值放宽）----
    for layer, thr in [("KV", 0.5), ("Summary", 0.5), ("Ledger", 0.5), ("Vector", 5.0), ("RAG", 5.0)]:
        assert layers[layer]["avg"] < thr, f"{layer} 平均 {layers[layer]['avg']:.4f}s 超过阈值 {thr}s"
    assert layers["KV"]["avg"] <= layers["Ledger"]["avg"], "KV 应快于 Ledger"
    assert layers["RAG"]["avg"] > layers["KV"]["avg"], "RAG 应慢于 KV"
    print(f"[perf] sync_new_events 返回: {sync_result}")
    print(f"[perf] 基线汇总: { {k: round(v['avg'], 4) for k, v in layers.items()} }")

    # ---- JSON 归档（便于后续对比）----
    _append_perf_report({
        "type": "performance_baseline",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "thresholds_seconds": {"KV": 0.5, "Summary": 0.5, "Ledger": 0.5, "Vector": 5.0, "RAG": 5.0},
        "layers_avg_seconds": {k: round(v["avg"], 6) for k, v in layers.items()},
        "layers_raw_seconds": {k: [round(t, 6) for t in v["times"]] for k, v in layers.items()},
        "sync_new_events": sync_result,
    })


# ==================== 场景 2：跨层一致性 ====================

def test_consistency_across_layers():
    """场景2：单条 tool_call 事件在 Ledger / Redis 缓存 / 向量层的一致性"""
    user_id = "user_test_consist"
    entity_id = "consist_entity"
    tool_name = "web_search"
    tool_input = "搜索天气"
    tool_output = "晴天"

    record_event(user_id=user_id, event_type="tool_call", entity_id=entity_id,
                 event_data={"tool_name": tool_name, "input": tool_input,
                             "output": tool_output, "status": "success"},
                 new_value={"result": tool_output},
                 source_agent="test_agent", session_id="consist_test")

    # 1) Ledger：事件已入账
    history = get_entity_history(user_id, entity_id, limit=10)
    tool_events = [e for e in history if e["event_type"] == "tool_call"]
    assert len(tool_events) >= 1, "Ledger 中应有 tool_call 记录"
    ev = tool_events[0]
    assert ev["event_data"]["tool_name"] == tool_name
    assert ev["new_value"]["result"] == tool_output
    print(f"[consist] Ledger 事件: event_data={ev['event_data']} new_value={ev['new_value']}")

    # 2) Redis 热缓存：event_bus 自动写入 tool:{user_id}:{entity_id}:{tool_name}
    cache_key = tool_key(user_id, entity_id, tool_name)
    kv_val = cache_get(cache_key)
    assert kv_val is not None, "Redis 缓存应有该工具调用"
    assert kv_val.get("tool_name") == tool_name
    print(f"[consist] Redis 缓存 {cache_key}: {kv_val}")

    # 3) 向量层：sync_new_events 同步后，按输入检索能命中该事件
    sync_new_events(entity_id, last_synced_ledger_id=0, user_id=user_id)
    vec = vector_search(entity_id, tool_input, limit=5, user_id=user_id)
    found = any(tool_name in it.get("content", "") or tool_output in it.get("content", "")
                for it in vec)
    assert found, "向量层应包含该工具调用"
    print(f"[consist] Vector 检索({tool_input}) 命中数: {len(vec)}, 原始返回: {vec}")


def test_skill_call_consistency():
    """
    场景2补充：skill_call 事件在各层的一致性验证。

    实现覆盖情况（已核对 event_bus._update_redis_cache）：
    - Ledger：自动 ✓（record_event 必写账本）
    - Redis 缓存：自动 ✓（写入 skill:{user_id}:{entity_id}:{skill_name}）
    - 向量层：缺口 ✗（record_event 不会自动写入 memory_embedding，需显式 sync_new_events）
    - Summary：缺口 ✗（record_event 不会自动触发摘要更新，需显式摘要流程）
    """
    user_id = "user_test_skillconsist"
    entity_id = "skill_consist_entity"
    skill_name = "flight_booking"
    skill_output = "预订成功"

    record_event(user_id=user_id, event_type="skill_call", entity_id=entity_id,
                 event_data={"skill_name": skill_name, "trigger": "用户要求订票",
                             "output": skill_output, "status": "success"},
                 new_value={"result": skill_output},
                 source_agent="test_agent", session_id="skill_consist_test")

    # 1) Ledger（自动覆盖）
    history = get_entity_history(user_id, entity_id, limit=10)
    sk_events = [e for e in history if e["event_type"] == "skill_call"]
    assert len(sk_events) >= 1, "Ledger 中应有 skill_call 记录"
    assert sk_events[0]["event_data"]["skill_name"] == skill_name
    assert sk_events[0]["new_value"]["result"] == skill_output
    print(f"[skill_consist] Ledger 事件: {sk_events[0]['event_data']}")

    # 2) Redis 缓存（自动覆盖）
    ckey = skill_key(user_id, entity_id, skill_name)
    kv_val = cache_get(ckey)
    assert kv_val is not None, "Redis 缓存应有该 skill_call"
    assert kv_val.get("skill_name") == skill_name
    assert kv_val.get("output") == skill_output
    print(f"[skill_consist] Redis 缓存 {ckey}: {kv_val}")

    # 3) 向量层（缺口：需显式同步）——测试内补充同步逻辑以验证一致性可达
    sync_new_events(entity_id, last_synced_ledger_id=0, user_id=user_id)
    vec = vector_search(entity_id, skill_output, limit=5, user_id=user_id)
    found = any(skill_name in it.get("content", "") or skill_output in it.get("content", "")
                for it in vec)
    assert found, "向量层应包含该 skill_call（经显式同步）"
    print(f"[skill_consist] Vector 检索({skill_output}) 命中数: {len(vec)}")

    # 4) Summary（缺口：record_event 不自动更新摘要）——显式写入摘要验证技能模式可承载
    save_summary(entity_id, "技能调用一致性摘要", "", skill_name,
                 "2026-08-16 00:00:00+00:00", "2026-08-16 01:00:00+00:00",
                 user_id=user_id)
    summaries = get_recent_summaries(entity_id, limit=3, user_id=user_id)
    assert summaries, "摘要层应有记录"
    assert any(skill_name in (s.get("skill_call_pattern") or "") for s in summaries), \
        "摘要的 skill_call_pattern 应含该技能名"
    print(f"[skill_consist] Summary 最新 skill_call_pattern: {summaries[0].get('skill_call_pattern')}")


# ==================== 场景 3：RAG 版本同步 ====================

def test_rag_version_sync():
    """场景3：RAG 版本更新后快照自动更新，检索只返回 active 版本"""
    user_id = "user_test_ragversion"
    entity_id = "user_test_ragversion"
    source = "test-ragversion-doc"
    title = "版本同步测试文档"

    try:
        # v1：写入并检索 → 快照应为 v1
        add_external_knowledge(source=source, title=title, content="v1：产品文档内容",
                               version="v1", tenant_id="public")
        with get_cursor() as cur:
            cur.execute(
                "SELECT knowledge_id FROM rag_knowledge_base WHERE source=%s AND status='active' ORDER BY knowledge_id DESC LIMIT 1",
                (source,))
            kid1 = cur.fetchone()["knowledge_id"]

        r1 = rag_conditional_search(entity_id=entity_id, query=title, kv_result=None,
                                    summary_result=None, ledger_result=[],
                                    user_id=user_id, tenant_id="public")
        assert r1["should_use_rag"] is True, "应调用 RAG"
        assert r1["snapshot_updated"] is True, "首次检索应写快照"
        snap1 = get_latest_rag_snapshot(entity_id, kid1, user_id=user_id)
        assert snap1 is not None and snap1["version"] == "v1", f"首次快照应为 v1，实际 {snap1}"
        print(f"[ragver] r1.snapshot_updated={r1['snapshot_updated']}  快照: {snap1}")

        # v2：同 source+title 自动废弃 v1 → 再次检索应更新快照到 v2
        add_external_knowledge(source=source, title=title, content="v2：产品文档更新内容",
                               version="v2", tenant_id="public")
        with get_cursor() as cur:
            cur.execute(
                "SELECT knowledge_id FROM rag_knowledge_base WHERE source=%s AND status='active' ORDER BY knowledge_id DESC LIMIT 1",
                (source,))
            kid2 = cur.fetchone()["knowledge_id"]

        r2 = rag_conditional_search(entity_id=entity_id, query=title, kv_result=None,
                                    summary_result=None, ledger_result=[],
                                    user_id=user_id, tenant_id="public")
        assert r2["snapshot_updated"] is True, "新版本应触发快照更新"
        snap2 = get_latest_rag_snapshot(entity_id, kid2, user_id=user_id)
        assert snap2 is not None and snap2["version"] == "v2", f"更新后快照应为 v2，实际 {snap2}"
        print(f"[ragver] r2.snapshot_updated={r2['snapshot_updated']}  快照: {snap2}")

        # 检索只返回 active（v2），不含已废弃的 v1（按 source 过滤本测试文档）
        results = search_rag_knowledge(title, tenant_id="public")
        our_versions = [r["metadata"].get("version") for r in results if r["metadata"].get("source") == source]
        assert "v2" in our_versions, f"本测试文档应包含 v2，实际 {our_versions}"
        assert "v1" not in our_versions, f"本测试文档不应包含已废弃 v1，实际 {our_versions}"
        print(f"[ragver] 本测试文档检索版本: {our_versions}")
        print(f"[ragver] 检索原始返回: {results}")
    finally:
        # 清理知识库测试行（快照在 ledger，由 clean_db 清理 user_test_%）
        with get_cursor() as cur:
            cur.execute("DELETE FROM rag_knowledge_base WHERE source = %s", (source,))


# ==================== 场景 4：多租户并发稳定性 ====================

def test_multi_tenant_concurrency():
    """场景4：多租户并发压力测试（分档压力），验证隔离正确、连接池稳定，结果写入 JSON"""
    # 三档压力（保持适中，不压垮连接池）：低 / 中 / 高
    pressure_levels = [
        {"name": "low",    "users": 4, "repeat": 5,  "workers": 5},   # 20 查询
        {"name": "medium", "users": 5, "repeat": 10, "workers": 10},  # 50 查询
        {"name": "high",   "users": 8, "repeat": 10, "workers": 12},  # 80 查询
    ]
    level_summaries = []

    for level in pressure_levels:
        users = [f"user_test_conc_{level['name']}_{i}" for i in range(level["users"])]
        expected = {}
        for idx, user in enumerate(users):
            city = f"city_{idx}"
            expected[user] = city
            record_event(user_id=user, event_type="state_change", entity_id="location",
                         event_data={"field": "location"}, new_value={"location": city},
                         session_id=f"conc_{level['name']}_{idx}")

        def query_user(user):
            try:
                return user, get_current_state(user, "location", "location")
            except Exception as e:
                return user, f"ERROR: {e}"

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=level["workers"]) as executor:
            futures = [executor.submit(query_user, user)
                       for user in users for _ in range(level["repeat"])]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]
        duration = time.perf_counter() - start

        # 校验隔离：每个用户每次查询都返回自己的数据，无串扰/无错误
        errors = 0
        for user in users:
            user_results = [r for r in results if r[0] == user]
            assert len(user_results) == level["repeat"], \
                f"[{level['name']}] {user} 应有 {level['repeat']} 次，实际 {len(user_results)}"
            for _, val in user_results:
                if not (isinstance(val, dict) and val.get("location") == expected[user]):
                    errors += 1
        assert errors == 0, f"[{level['name']}] 出现 {errors} 次串扰/错误"

        print(f"[conc:{level['name']}] {len(results)} 次查询 / workers={level['workers']} / "
              f"耗时 {duration:.2f}s / 错误 {errors}")
        level_summaries.append({
            "level": level["name"],
            "queries": len(results),
            "workers": level["workers"],
            "duration_seconds": round(duration, 3),
            "errors": errors,
        })

    print(f"[conc] 压力分档汇总: {level_summaries}")
    print("[conc] 各档隔离正确、连接池稳定，无串扰/无错误")

    # JSON 归档（与基线同文件，便于对比）
    _append_perf_report({
        "type": "multi_tenant_concurrency",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "levels": level_summaries,
    })

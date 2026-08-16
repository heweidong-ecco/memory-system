# 第4天  写入用户偏好 KV 并查看元数据
请在你的 memory-system 目录下执行以下 Python 脚本：

```bash
cd ~/Desktop/ai-learning/重点教学内容/06.分层记忆系统架构/memory-system
source venv/bin/activate  # 如果已激活可跳过
python3 << 'EOF'
from kv_api import set_profile_value, get_profile_meta

# 写入用户偏好
set_profile_value(
    key="user_123:preference:language",
    value={"lang": "zh-CN"},
    entity_type="user",
)

# 查询元数据
meta = get_profile_meta("user_123:preference:language")
print("version:", meta["version"])
print("access_count:", meta["access_count"])
print("expires_at:", meta["expires_at"])
print("value:", meta["value"])
EOF
```
预期结果解读：

字段	值	含义
version	1	第一次写入，版本号为 1
access_count	0	尚未通过 get_profile_value 读取过
expires_at	None	未设置 TTL，永久有效
value	{'lang': 'zh-CN'}	成功写入的偏好值
如果之后再用 get_profile_value 读取一次，access_count 会增加到 1。你可以手动验证：

```python
from kv_api import get_profile_value, get_profile_meta
get_profile_value("user_123:preference:language")
meta = get_profile_meta("user_123:preference:language")
print(meta["access_count"])  # 应该变成 1
```
# 第4天 验证 user_123:location 的乐观锁版本递增

执行以下脚本：

```python
python3 << 'EOF'
from kv_api import get_profile_meta, set_profile_value

# 查询当前版本
meta_before = get_profile_meta("user_123:location")
print("更新前 version:", meta_before["version"] if meta_before else "key 不存在")

# 执行一次更新
new_version = set_profile_value("user_123:location", {"city": "北京"})
print("本次更新返回的新 version:", new_version)

meta_after = get_profile_meta("user_123:location")
print("更新后 version:", meta_after["version"])
EOF
```
预期结果：
如果 user_123:location 已存在（前几天测试写入过），version 会在原基础上 +1。
如果不存在，会新建并返回 version = 1。
每次调用 set_profile_value，version 都会严格递增。这就是乐观锁的基础——当多个 Agent 并发更新同一个 key 时，可以通过比较 version 来判断数据是否被其他 Agent 修改过。
验证成功标志：连续执行两次 set_profile_value，第二次返回的 version 大于第一次。例如第一次返回 5，第二次返回 6。
# 第5天 测试 修改 KV 提升规则中的 min_access_count 的值 判断高/低数据是否能 留在 KV 层。
当 min_access_count 从 3 改为 1 后：
原来 access_count=1 且 version=2 的键会被拒绝（min=3）
改后 access_count=1 会被允许（min=1）
这就让低频数据也能留在 KV 层。如果反向修改（min_access_count 提高到 5），则只有访问更频繁的数据才能留在 KV 层。

```python3 << 'EOF'
from policy_api import should_promote_to_kv

# 新键（version=1）应该被允许
print(should_promote_to_kv("user_123:new_key", value_size=100, version=1))  # True

# 已存在的键（version=2）但 access_count 很低
print(should_promote_to_kv("user_123:existing_key", value_size=100, version=2, access_count=1))  # False

# 已存在的键（version=2）且 access_count 足够高
print(should_promote_to_kv("user_123:popular_key", value_size=100, version=2, access_count=5))  # True
EOF
```
# 第6天 TTL 过期判断逻辑
```python3 << 'EOF'
from policy_api import set_profile_value, should_demote_from_kv

# 写入一个 1 秒后过期的 KV
set_profile_value(
    key="user_123:temp_data",
    value={"temp": True},
    entity_type="session",
    ttl_seconds=1,  # 1 秒后过期
)

import time
time.sleep(2)  # 等待 2 秒，确保过期

print(should_demote_from_kv("user_123:temp_data"))  # True（已过期）
```
# 第6天 测试 新增 KV 降级规则（access_count=0 且 version>3）
实际执行后的结果返回的 True，说明新规则生效——一个从未被访问但被反复修改的键，会被系统判定为“不值得保留”并降级删除。这条规则的意义在于：频繁变更但无人使用的数据，往往是噪音来源，应该被清理。

```python << 'EOF'
from policy_api import set_profile_value, should_demote_from_kv

# 写入一个键，并多次更新使其 version 超过 3，但从不读取（access_count 保持 0）
set_profile_value("user_123:zero_access", {"data": "v1"})
set_profile_value("user_123:zero_access", {"data": "v2"})
set_profile_value("user_123:zero_access", {"data": "v3"})
set_profile_value("user_123:zero_access", {"data": "v4"})  # version = 4

# 此时 access_count = 0，version = 4，应触发新规则
print(should_demote_from_kv("user_123:zero_access"))  # True
EOF
```
# 第7天 删除 KV value 中 user_123:rag_reference 标记
```python << 'EOF'
from kv_api import delete_profile_value
delete_profile_value("user_123:rag_reference")
EOF
```
# 第8天
## 模拟长期运行，触发滚动更新
你的 SUMMARY_TRIGGER_THRESHOLD = 10，意味着 Ledger 中该实体的记录数达到 10 条以上时才会触发摘要生成。为了让滚动更新真正触发，需要插入更多事件。
```python << 'EOF'
from event_bus import EventBus
import uuid

bus = EventBus()
entity_id = "user_123"
session_id = f"session_{uuid.uuid4().hex[:8]}"

# 模拟 Agent 后续的 8 条事件（工具调用 + 技能调用 + 状态变更）
events = [
    ("tool_call", {"tool_name": "web_search", "input": "查询今天天气", "output": "晴转多云", "status": "success"}),
    ("tool_call", {"tool_name": "web_search", "input": "查询股市行情", "output": "上证指数上涨", "status": "success"}),
    ("skill_call", {"skill_name": "flight_booking", "trigger": "用户要求订机票", "output": "预订成功", "status": "success"}),
    ("state_change", {"field": "preference"}, None, {"theme": "dark"}),
    ("tool_call", {"tool_name": "code_execution", "input": "计算 1+1", "output": "2", "status": "success"}),
    ("skill_call", {"skill_name": "flight_booking", "trigger": "用户要求改签", "output": "改签成功", "status": "success"}),
    ("state_change", {"field": "location"}, {"city": "北京"}, {"city": "上海"}),
    ("tool_call", {"tool_name": "web_search", "input": "查询上海天气", "output": "小雨", "status": "success"}),
]

for event in events:
    if event[0] == "state_change":
        bus.record_event(
            event_type="state_change",
            entity_id=entity_id,
            event_data=event[1],
            old_value=event[2],
            new_value=event[3],
            source_agent="travel_agent",
            session_id=session_id,
        )
    else:
        bus.record_event(
            event_type=event[0],
            entity_id=entity_id,
            event_data=event[1],
            source_agent="travel_agent",
            session_id=session_id,
        )

print("已插入 8 条新事件")
bus.close()
EOF
```
## 触发滚动更新
```python << 'EOF'
from summary_api import update_summary, get_recent_summaries

print("触发滚动更新...")
new_summary = update_summary("user_123")

print("\n最新摘要内容：")
latest = get_recent_summaries("user_123")
print(latest["content"][:500])
print("\n工具模式:", latest["tool_call_pattern"])
print("技能模式:", latest["skill_call_pattern"])
EOF
```
# 第9天 插入两个新会话，让模式达到阈值
当前 web_search → flight_booking 这个签名只出现了 1 次（session_20260923_001）。需要再插入 2 个新会话，每个都执行相同的调用序列，让成功次数达到 3。

在 memory-system/ 目录下执行：

```python  << 'EOF'
#!/usr/bin/env python3
"""插入两个新的成功会话，验证程序性记忆触发"""

from event_bus import EventBus
import uuid

bus = EventBus()
entity_id = "user_123"

# 模拟两个新的成功订票会话，签名均为 web_search → flight_booking
for i in range(2):
    session_id = f"session_booking_{uuid.uuid4().hex[:6]}"
    
    # 工具调用：web_search
    bus.record_event(
        event_type="tool_call",
        entity_id=entity_id,
        event_data={
            "tool_name": "web_search",
            "input": f"查询上海到广州的航班（第{i+1}次）",
            "output": "5 个航班结果",
            "status": "success",
        },
        source_agent="travel_agent",
        session_id=session_id,
    )
    
    # 技能调用：flight_booking
    bus.record_event(
        event_type="skill_call",
        entity_id=entity_id,
        event_data={
            "skill_name": "flight_booking",
            "trigger": "用户要求订机票",
            "output": "预订成功",
            "status": "success",
        },
        source_agent="travel_agent",
        session_id=session_id,
    )
    
    print(f"✅ 已插入会话 {session_id}: web_search → flight_booking (success)")

bus.close()
print("\n完成。现在有 3 个不同会话拥有相同的成功签名。")
EOF
```
运行后，重新执行 procedural_trigger.py：
bash
python3 procedural_trigger.py
预期结果：
测试3 应该检测到：
text
🔔 模式 [web_search → flight_booking] 成功 3 次，触发=是
这是因为现在有 3 个不同会话都拥有完全相同的签名 web_search → flight_booking，且全部成功，满足触发条件（成功次数 ≥ 3、全部成功、至少 2 步）。
如果仍然是“未发现重复模式”，请把插入脚本的输出和 procedural_trigger.py 的完整输出都发给我，我帮你排查。
# 第13天 添加 v3 并测试版本快照更新
第 1 步：添加 v3.0.0 外部知识
创建一个新的 Python 脚本或直接在终端执行：
```python
python3 << 'EOF'
from rag_knowledge import add_external_knowledge

# 添加 v3 版本（内容与 v2 相似，但有小更新）
add_external_knowledge(
    source="api-docs",
    title="JWT 认证最佳实践",
    content="JWT token 应使用 RS256 非对称加密签名，过期时间建议 10 分钟，refresh token 建议 14 天。禁止在 payload 中存放密码。新增：建议使用 key rotation 策略。",
    version="v3.0.0",
)
print("已添加 v3.0.0")
EOF
```

第 2 步：重新检索，观察 v3 是否排在前面
```python
python3 << 'EOF'
from rag_knowledge import search_rag_knowledge

results = search_rag_knowledge("JWT 认证 过期时间")
for r in results:
    print(f"  [{r['metadata']['version']}] {r['content'][:80]}")
EOF
```
预期：v3 应该排在最前，因为它的内容更完整、更新，与“过期时间”的语义更接近。

第 3 步：测试版本快照更新
```python
python3 << 'EOF'
from rag_knowledge import rag_conditional_search

result = rag_conditional_search(
    entity_id="user_123",
    query="JWT 认证最佳实践是什么",
    kv_result=None,
    summary_result=None,
    ledger_result=[],
)
print(f"should_use_rag: {result['should_use_rag']}")
print(f"snapshot_updated: {result['snapshot_updated']}")
print(f"reason: {result['reason']}")

if result['rag_results']:
    first_id = result['rag_results'][0]['id']
    from rag_knowledge import get_latest_rag_snapshot
    snapshot = get_latest_rag_snapshot("user_123", first_id)
    print(f"最新快照: {snapshot}")
EOF
```
预期：

snapshot_updated 为 True（因为首次检索这些知识，版本快照需要写入）
最新快照的 version 字段为 v3.0.0
关于 v1、v2、v3 的版本冲突处理

当前知识库中同时存在 v1、v2、v3 三条记录。这是一个测试场景，生产环境中不应该这样——同一份文档的新版本应该替换旧版本，而不是并存。

生产级版本管理策略：

策略	做法	适用场景
覆盖式更新	新版本插入前，标记旧版本为 deprecated	文档有明确版本号，旧版本不再使用
并存式	不同版本同时保留，检索时用元数据过滤	需要回滚旧版本，或多版本并行
时间戳过滤	检索时只取最新版本	简单，但可能丢失历史
当前测试代码没有实现版本替换，这会导致检索时 v1、v2、v3 同时返回，可能造成干扰。如果要处理，建议在 add_external_knowledge 中增加逻辑：同 source + 同 title 的旧记录标记为 deprecated。
# 第15天 
## 创建 db_pool.py，确认连接池正常工作。运行一个简单的查询，验证连接被复用。
验证连接池工作
```python
python3 << 'EOF'
from db_pool import get_cursor, get_pool
from cache_manager import (
    cache_set, cache_get, cache_delete,
    kv_key, tool_key, skill_key, sliding_key
)

print("连接池初始化...")
pool = get_pool()
print(f"初始状态：最小 {pool.minconn}, 最大 {pool.maxconn}, 当前空闲 {len(pool._pool)} 个")

# 第一次查询：从池中取出一个连接
with get_cursor() as cur:
    cur.execute("SELECT COUNT(*) FROM ledger")
    count = cur.fetchone()
    print(f"第一次查询：Ledger 中共 {count['count']} 条记录")

# 第二次查询：复用池中连接
with get_cursor() as cur:
    cur.execute("SELECT COUNT(*) FROM user_profile")
    count = cur.fetchone()
    print(f"第二次查询：user_profile 中共 {count['count']} 条记录")

# 验证连接被复用（池大小不变）
print(f"两次查询后，池中空闲连接数：{len(pool._pool)} 个")
print("✅ 连接池工作正常——两次查询使用了池中的连接，没有新建连接")
EOF
```
预期结果：两次查询都成功，池中连接数保持稳定，没有新建连接。
## 创建 cache_manager.py，测试 cache_set / cache_get / cache_delete 三个函数。
测试三个函数
```python
python3 << 'EOF'
from cache_manager import (
    cache_set, cache_get, cache_delete,
    kv_key, tool_key, skill_key, sliding_key
)
# 测试 1：写入和读取
test_key = kv_key("user_123", "location")
cache_set(test_key, {"city": "北京"}, ttl=60)
result = cache_get(test_key)
print(f"✅ cache_set / cache_get: {result}")

# 测试 2：读取不存在
result = cache_get("nonexistent_key")
print(f"✅ 读取不存在的 key: {result}")

# 测试 3：删除
cache_delete(test_key)
result = cache_get(test_key)
print(f"✅ cache_delete 后读取: {result}")

# 验证 Redis 中键的格式
print(f"\n键命名规范验证:")
print(f"  kv_key('user_123', 'location') = {kv_key('user_123', 'location')}")
print(f"  tool_key('user_123', 'web_search') = {tool_key('user_123', 'web_search')}")
print(f"  skill_key('user_123', 'flight_booking') = {skill_key('user_123', 'flight_booking')}")
EOF
```
### 修改 kv_api.py 的 get_profile_value 为 Cache-Aside 模式。先写一条 KV，然后观察 Redis 中是否出现了对应的缓存键。手动删除 Redis 缓存后再次读取，确认从 PG 回源并重新写入 Redis。
验证 Cache-Aside 模式
```python
python3 << 'EOF'
from kv_api import set_profile_value, get_profile_value
from cache_manager import cache_get, cache_delete, kv_key, redis_client

# 步骤 1：写入一条 KV
set_profile_value("user_123:location", {"city": "北京"}, entity_type="user")
print("✅ 已写入 PG")

# 步骤 2：读取，观察 Redis 中是否出现缓存键
value = get_profile_value("user_123:location")
print(f"读取结果: {value}")

redis_key = kv_key("user_123", "location")
cached = cache_get(redis_key)
print(f"Redis 缓存键 {redis_key} 的值: {cached}")

# 步骤 3：手动删除 Redis 缓存
cache_delete(redis_key)
print("✅ 已手动删除 Redis 缓存")

# 步骤 4：再次读取，确认从 PG 回源并重新写入 Redis
value_again = get_profile_value("user_123:location")
print(f"再次读取（应该走 PG）: {value_again}")

cached_again = cache_get(redis_key)
print(f"Redis 缓存重新出现: {cached_again}")

if value_again == value and cached_again is not None:
    print("\n✅ Cache-Aside 模式验证通过")
    print("   第 1 次读取：Redis 未命中 → 查 PG → 回写 Redis")
    print("   删除缓存后读取：Redis 未命中 → 查 PG → 重新回写 Redis")
EOF
```
## 修改 event_bus.py 的 _update_redis_cache，使用统一键命名。写入一条 tool_call 事件，确认 Redis 中的键格式为 tool:{entity_id}:{tool_name}。
测试写入 tool_call 事件

```python
python3 << 'EOF'
from event_bus import EventBus
from cache_manager import tool_key, cache_get

bus = EventBus()

# 写入一条 tool_call 事件
ledger_id = bus.record_event(
    event_type="tool_call",
    entity_id="user_123",
    event_data={
        "tool_name": "web_search",
        "input": "查询北京天气",
        "output": "晴转多云",
        "status": "success",
    },
    source_agent="test_agent",
    session_id="session_cache_test",
)

print(f"✅ 已写入 tool_call 事件，ledger_id={ledger_id}")

# 验证 Redis 键格式
redis_key = tool_key("user_123", "web_search")
cached = cache_get(redis_key)
print(f"Redis 键: {redis_key}")
print(f"缓存内容: {cached}")

if cached and cached.get("tool_name") == "web_search":
    print("✅ 键格式正确: tool:{entity_id}:{tool_name}")
else:
    print("❌ 键格式或缓存内容不正确")

bus.close()
EOF
```
预期结果：Redis 中键为 tool:user_123:web_search，内容是完整的 event_data。
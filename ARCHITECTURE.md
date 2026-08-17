# 分层记忆系统架构 — 竣工文档

> 定位：生产级 AI Agent 记忆系统，多租户隔离，程序性记忆自进化，RAG 条件性调用。
> 数据来源：`memory-system/` 实际代码 + 测试实测数据（2026-08-16）。
> 本文档与代码同步，改动代码需同步更新。

---

## 1. 系统概述

### 核心理念
- **工具调用与 Skills 调用是贯穿所有层的一等公民**——事件流总线统一入口
- **程序性记忆是 Agent 自我进化的产物**——从自己的成功经历提炼，不是外部 Skills
- **RAG 是条件性调用**——不默认触发，避免无谓的外部查询

### 检索优先级（`unified_search` 实际实现，**5 层**）
```
KV（确定）→ Summary（模糊）→ Ledger（回溯）→ Agent 自生 Skills（重复复用）→ RAG（条件性）
```

### 记忆存储（**6 类**）
| 存储 | 表 | 用途 |
|---|---|---|
| KV 精准 | `user_profile` | 确定 key 的当前值，零噪声 |
| Summary | `summary` | 一段时间语义走向（滚动摘要） |
| Ledger | `ledger` | 不可变账本，全部事件流水 |
| 向量语义 | `memory_embedding` | 语义/关键词混合检索（pgvector） |
| 程序性记忆 | `skill_candidate_vectors` + `skill_snapshots` + `skills/` 目录 | 成功任务候选→固化→复用 |
| RAG 外部知识 | `rag_knowledge_base` | 外部知识，按租户隔离 |

> 说明：检索入口走 5 层优先级；`memory_embedding` 是独立的语义存储（`vector_search`/`bm25_search`），不作为检索优先级链的一层，而是被 RAG 检索与独立语义查询使用。

---

## 2. 记忆存储体系（实测延迟，2026-08-16）

| 存储 | 写入路径 | 典型延迟（实测平均） | 影响因素 |
|---|---|---|---|
| KV | `record_event` / `set_profile_value` | **0.45 ms** | Redis 命中率；命中缓存零 DB |
| Summary | `update_summary` / `save_summary` | **1.28 ms** | 索引、滚动摘要数量 |
| Ledger | `record_event`（Append-Only） | **1.13 ms** | `user_id`+`entity_id` 索引、数据量 |
| 向量语义 | `sync_new_events`（Ollama embedding） | **260 ms** | **Ollama embedding 占绝对大头**；向量维度 1024 |
| 程序性记忆 | `add_candidate_vector` → 触发 → 固化 | 复用匹配 **~260 ms**（含 embedding） | embedding、候选规模、阈值 |
| RAG | `add_external_knowledge` | **434 ms** | embedding + BM25 + RRF 融合 + 外部服务 |

> 关键结论：**纯 DB/缓存层毫秒级；向量/RAG 层被本地 Ollama embedding 主导（百毫秒级）**，不等于数据库性能。

---

## 3. LPV 骨架

| 组件 | 模块 | 职责 |
|---|---|---|
| **L**edger | `ledger_api.py` + `event_bus.py` | 不可变账本，Append-Only；`ledger_append_only_trigger`（BEFORE DELETE OR UPDATE）拦截篡改 |
| **P**olicy | `policy_api.py` | 读写规则、KV 升降级（`promote_to_kv`/`should_promote_to_kv`）、RAG 条件判定（`should_call_rag`）、RAG 引用检测（`check_rag_reference`） |
| **V**iews | `views_api.py` | 从 Ledger 聚合生成状态卡，调用底层函数间接享受缓存 |

---

## 4. 多租户设计

- **所有表含 `user_id` 列**，所有查询 `WHERE user_id = %s`
- **Redis 缓存键格式**：`kv:{user_id}:{entity_id}:{field}`（含隔离维度，防止同 entity 跨用户串）
- **RAG 知识库用 `tenant_id`**：`tenant_id='public'` 共享，私有按租户隔离
- **技能文件按用户私有目录**：`skills/{user_id}/{skill_name}/SKILL.md`，全局根目录保留内置/共享技能
- **全链路透传**：`event_bus.record_event` / `ledger_api` 8 查询 / `kv_api` / `summary_api` / `vector_semantic` / `skill_candidate_store` / `rag_knowledge` / `unified_search` 均带 `user_id`；`check_procedural_trigger`/`check_rag_reference`/`should_call_rag` 亦按 user_id 查询
- **公共 RAG 知识库**产品阶段拆分为独立服务（REST API/MCP），当前阶段不实施

---

## 5. 缓存策略

- **模式**：Cache-Aside（先 Redis → 未命中 PG → 回写 Redis）
- **分层 TTL**（`cache_manager.py`）：KV 60s / Summary 3600s / Skills 1 年（86400×365）
  - ⚠️ `TTL_RAG = 300` **已定义但未被任何代码使用**（`search_rag_knowledge` 不缓存）——潜在待落地项
- **连接池**：`db_pool.py` `SimpleConnectionPool(5, 20)`，`get_cursor()` 上下文管理器保证归还
- ⚠️ 注意：`SimpleConnectionPool` **非线程安全**，多线程并发访问连接池存在竞态风险（当前并发测试在低档位通过）

---

## 6. 检索优先级实现

- 入口：`unified_api.query_memory()` → `retrieval_orchestrator.unified_search(entity_id, query, session_id, user_id, tenant_id)`
- 顺序：KV → Summary → Ledger → Skills → RAG
- **RAG 触发条件**（`policy_api.should_call_rag`，满足任意一个即调用）：

| 条件 | 说明 |
|---|---|
| 1. RAG 引用标记 | KV 中存在 `{entity_id}:rag_reference` 键（`check_rag_reference`） |
| 2. 内部完全未命中 | KV/Summary/Ledger 全空（需外挂知识） |
| 3. 内部命中但不完整 | `is_result_complete` 判定不足 |
| 4. 外部知识关键词 | query 含"最新/文档/API 规范/官方/版本更新/latest/..." |

> ⚠️ 原草稿写"前四层出现 entity_type='rag' 标记"——**不准确**。实际是 KV 中 `rag_reference` 键 + 上述 4 条件。

---

## 7. 程序性记忆进化

- **触发**：`check_procedural_trigger` 用**双阈值相似度**判定（success 相似 ≥0.85 主筛 + query 相似 ≥0.65 二次确认），**不是"重复≥3次"的计数**；重复成功任务会累积候选，命中同类模式即触发
- **流程**：`skill_candidate_store`（候选写入/双阈值检索）→ `skill_generator`（提炼 SKILL.md）→ `skill_finalizer`（固化快照 + 标记已处理）→ `skill_loader`（发现/加载）→ `skill_matcher_v2`（description 语义匹配复用，跳过昂贵推理）
- 复用阶段：`unified_search` 第 4 层命中 Skills 即返回，不进入 RAG/生成

---

## 8. 版本管理（RAG 快照）

- **快照存于 Ledger**（`event_type='rag_retrieval'`），**不是 `rag_metadata` 表**（该表不存在）
- 接口：`snapshot_rag_metadata()` 写入、`get_latest_rag_snapshot(entity_id, knowledge_id, user_id)` 读取
- 流程：检索前对比 `get_latest_rag_snapshot` 版本 → 不一致则重新检索并更新快照
- `search_rag_knowledge` 只返回 `status='active'` 的条目（`add_external_knowledge` 同 source+title 自动废弃旧版）

---

## 9. 统一 API

```python
# 写入（所有事件必经）
record_event(user_id, event_type, entity_id, event_data,
             old_value=None, new_value=None, source_agent=None,
             session_id=None, policy_version=None) -> ledger_id

# 读取（所有查询必经）
query_memory(user_id, query, session_id=None) -> Dict
```

---

## 10. 测试体系（26 用例全绿）

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `test_day17_deterministic_backtrack.py` | 13 | 确定性、时间回溯、KV 零噪声、多租户隔离、Append-Only、参数化隔离、时间范围 |
| `test_day18_evolution_conditional.py` | 8 | 程序性记忆进化链路、RAG 条件判定、RAG 版本对比、技能候选隔离、description 匹配、参数化不串扰 |
| `test_day19_performance_stability.py` | 5 | 性能基线、tool_call/skill_call 一致性、RAG 版本同步、并发压力分档 |

- 清理统一在 `conftest.py`（`clean_db` autouse），前缀 `user_test_%`；含 ledger 临时禁用触发器、`memory_embedding`、`rag_knowledge_base`（`source LIKE 'test-%'`）
- 性能归档：`perf_baselines.json`（追加式，保留历史对比）

---

## 11. 已知缺口 / 待优化

1. **tool_call / skill_call 不自动传播到向量层与 Summary**——`event_bus` 只写 Ledger + Redis 缓存；向量需 `sync_new_events`，摘要需 `update_summary` 显式同步
2. **`TTL_RAG` 定义未使用**
3. **`SimpleConnectionPool` 非线程安全**——生产并发需 `ThreadedConnectionPool`
4. **`unified_search` 未把 `memory_embedding`（向量语义）纳入检索优先级**——向量层是独立查询
5. 程序性记忆自动触发机制（当前靠手动调用触发/生成流程）

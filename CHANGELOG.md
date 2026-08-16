# 更新日志

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 风格，记录对可观察行为有影响的变更。

## [0.4.0] - 2026-08-16（文档与生产化）

### 新增
- `README.md`：项目门面，含 Mermaid 架构图与快速开始
- `FAQ.md`：常见问题与排查
- `生产化准备清单.md`：git 前待办清单
- `pytest.ini`、`.env.example`、`.gitignore`
- `CHANGELOG.md` 本文件

### 修正
- `requirements.txt`：去掉 `pip`/`setuptools` 版本锁定；修正 `requests` 版本号多余空格；补充 `python-dotenv`（此前 `from dotenv import load_dotenv` 会缺失依赖）

## [0.3.0] - 2026-08-16（多租户收尾 + 测试体系）

### 变更
- 多租户全链路透传：`check_procedural_trigger` / `check_rag_reference` / `should_call_rag` / `unified_search` 去掉硬编码 `user_default`
- 技能文件按用户私有目录：`skills/{user_id}/`（`save_skill_to_skills_dir`/`discover_skills`/`match_skill_by_description` 支持 user_id）
- `user_profile` 主键 `(key)` → `(user_id, key)`，KV 层 DB 隔离真正成立
- `conftest.py` 统一清理：新增 `memory_embedding` / `rag_knowledge_base`（`source LIKE 'test-%'`）
- `retrieval_orchestrator.unified_search` / `_search_kv` / `unified_api.query_memory` 支持 `user_id`/`tenant_id`

### 新增
- 测试用例扩至 26：参数化多租户隔离、时间范围查询、skill_call 一致性、并发压力分档、性能基线 JSON 归档

### 修复
- `get_events_by_session` / `get_entity_history` / `kv_key` 旧签名调用点导致的 TypeError
- 测试文件引用不存在的函数/表（`maybe_trigger_extraction`、`find_best_skill`、`get_rag_version_snapshot`、`rag_metadata`）

## [0.2.0] - 2026-08-15（多租户隔离改造）

### 变更
- Ledger 8 个查询、KV、Summary、向量、技能候选、RAG 全部查询加 `WHERE user_id = %s`
- Redis 缓存键含 `user_id` 维度：`kv:{user_id}:{entity_id}:{field}`
- `event_bus._append_to_ledger` 显式写入 `user_id`

## [0.1.0] - 2026-08-14（六层记忆系统初版）

### 新增
- 六类记忆存储：KV（`user_profile`）/ Summary / Ledger / 向量语义（`memory_embedding`）/ 程序性记忆（`skill_candidate_vectors` + `skill_snapshots` + `skills/`）/ RAG（`rag_knowledge_base`）
- 五层检索编排 `unified_search`：KV → Summary → Ledger → Skills → RAG
- 统一 API：`record_event` / `query_memory`
- Ledger Append-Only 触发器（`ledger_append_only_trigger`）
- Cache-Aside 缓存 + 连接池 + 分层 TTL

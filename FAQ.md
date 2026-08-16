# 常见问题（FAQ）

## 1. 启动 / 环境

**Q: 启动时 `connection refused` / psycopg2 连接失败？**
A: PostgreSQL 或 Redis 未启动。先 `docker compose up -d`（PG 映射 5432，Redis 映射 6379）。连接参数在 `db_pool.py` / `cache_manager.py` 硬编码，与 docker-compose.yml 一致。

**Q: 装依赖失败？**
A: 用 `pip install -r requirements.txt`。若报 `requests` 版本错误，说明用的是旧版 requirements（曾有多余空格），更新到当前版本。项目要求 Python 3.9+。

**Q: 提示缺 `dotenv` 模块？**
A: 需要 `python-dotenv`（requirements.txt 已补）。`pip install python-dotenv`。

## 2. 向量 / 语义能力

**Q: 向量检索/技能匹配很慢或报错？**
A: 依赖 Ollama embedding。确认 `ollama pull bge-m3` 且服务在线（`curl http://localhost:11434/api/tags`）。当前耗时被本地 embedding 主导（向量层 ~260ms、RAG ~430ms），属正常。

**Q: 没配 DEEPSEEK_API_KEY 会怎样？**
A: 摘要生成降级为规则摘要，SKILL 生成返回占位提示，LLM 重排序跳过——其余功能不受影响。在 `.env` 配置后重启生效。

## 3. 数据 / 多租户

**Q: 为什么所有查询都要带 user_id？**
A: 多租户隔离。所有表含 `user_id` 列，`WHERE user_id = %s` 保证不同租户数据互不可见。缓存键也含隔离维度：`kv:{user_id}:{entity_id}:{field}`。

**Q: 两个用户写同一个 key 会不会冲突？**
A: 不会。`user_profile` 主键为 `(user_id, key)`，各用户独立成行。旧版主键只有 `key` 时会在 DB 层互相覆盖（缓存掩盖了问题），已修复。

**Q: RAG 知识库按什么隔离？**
A: `tenant_id`。`public` 为共享租户，私有知识按租户隔离。产品阶段会把公共 RAG 拆为独立服务。

**Q: 技能文件为什么分 `skills/{user_id}/`？**
A: 用户自生成的程序性技能按用户私有，避免跨租户泄漏；全局 `skills/` 根目录保留内置/共享技能。

## 4. 账本 / 触发器

**Q: 为什么 Ledger 无法 UPDATE/DELETE？**
A: Append-Only 设计。`ledger_append_only_trigger`（BEFORE DELETE OR UPDATE）拒绝篡改，保证审计完整性。测试清理需临时 `DISABLE TRIGGER` 后再恢复，**生产环境严禁**。

**Q: 测试残留数据怎么清理？**
A: `conftest.py` 的 `clean_db` autouse fixture 自动清理 `user_test_%` 前缀数据（ledger、user_profile、memory_embedding、skill_candidate_vectors、skill_snapshots、Redis、rag_knowledge_base 的 `test-%` 行）。

## 5. RAG / 版本

**Q: 快照存在哪？为什么没有 rag_metadata 表？**
A: 快照存在 **Ledger**（`event_type='rag_retrieval'`），接口 `get_latest_rag_snapshot()`。`rag_metadata` 表是早期文档中的虚构概念，实际不存在。

**Q: RAG 什么时候会被调用？**
A: `should_call_rag` 四条件满足任一：KV 有 `{entity_id}:rag_reference` 引用标记；内部四层完全未命中；内部命中但不完整；query 含"最新/文档/API 规范"等外部知识关键词。

**Q: 检索只返回最新版本？**
A: 是。`add_external_knowledge` 同 source+title 写入新版本时自动废弃旧版；`search_rag_knowledge` 只返回 `status='active'`。

## 6. 已知缺口（详见 ARCHITECTURE.md）

**Q: 事件为什么没有自动进向量/摘要？**
A: `record_event` 只写 Ledger + Redis 缓存。向量需显式 `sync_new_events`，摘要需 `update_summary`。这是当前实现的已知缺口。

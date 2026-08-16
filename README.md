# 分层记忆系统架构（Memory-System）

**License: MIT** · [LICENSE](LICENSE)

生产级 AI Agent 记忆系统：**多租户隔离 + 程序性记忆自进化 + RAG 条件性调用**。

- 六类记忆存储：KV 精准 / Summary 摘要 / Ledger 账本 / 向量语义 / 程序性记忆 / RAG 外部知识
- 五层检索优先级：KV → Summary → Ledger → Skills → RAG
- 统一入口：`record_event()`（写入） / `query_memory()`（读取）
- 完整测试：26 个用例（确定性 / 回溯 / 隔离 / 进化 / 性能）

## 快速开始

### 1. 启动依赖（PostgreSQL + Redis）

```bash
docker compose up -d            # PG16+pgvector + Redis，见 docker-compose.yml
```

### 2. 安装依赖

```bash
python -m venv memory-system-venv
source memory-system-venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # 填写 DEEPSEEK_API_KEY（可选，缺失时自动降级为规则摘要）
```

### 3. 确认 Ollama（可选，向量/语义能力依赖）

```bash
ollama pull bge-m3              # embedding 模型，用于向量检索与技能匹配
```

### 4. 跑测试

```bash
pytest                          # 26 个用例（配置见 pytest.ini）
```

## 架构一览

```mermaid
flowchart LR
    subgraph 入口
        A[unified_api.record_event] --> B[event_bus.EventBus]
        C[unified_api.query_memory] --> D[retrieval_orchestrator.unified_search]
    end

    B --> L[ledger 账本<br/>Append-Only 触发器]
    B --> R[redis 热缓存<br/>kv/tool/skill:{user_id}:...]

    D --> E1[1 KV 精准]
    D --> E2[2 Summary 摘要]
    D --> E3[3 Ledger 回溯]
    D --> E4[4 Skills 复用<br/>skills/{user_id}/]
    D --> E5[5 RAG 条件性<br/>tenant_id 隔离]

    E1 --> K[(user_profile)]
    E2 --> S[(summary)]
    E3 --> L
    E4 --> P[(skill_candidate_vectors<br/>skill_snapshots)]
    E5 --> G[(rag_knowledge_base)]

    B --成功 tool/skill--> P
```

**五层检索优先级**：KV（确定）→ Summary（模糊）→ Ledger（回溯）→ Skills（重复复用，跳过推理）→ RAG（条件性，4 种触发条件）。

## 目录结构

```
memory-system/
├── unified_api.py            # 统一入口：record_event / query_memory
├── event_bus.py              # 事件流总线（Ledger + Redis + 候选向量）
├── db_pool.py                # 连接池（SimpleConnectionPool 5-20）
├── cache_manager.py          # Cache-Aside + 分层 TTL + 键命名
├── ledger_api.py             # 账本查询（历史/当前状态/时间范围，user_id 隔离）
├── kv_api.py                 # KV 精准读写（user_id+key 联合主键）
├── summary_api.py            # 滚动摘要
├── vector_semantic.py        # 向量/BM25 混合检索（pgvector）
├── policy_api.py             # Policy：升降级、RAG 条件判定
├── views_api.py              # Views：从 Ledger 聚合状态卡
├── procedural_trigger.py     # 程序性记忆触发（双阈值）
├── skill_candidate_store.py  # 成功任务候选向量
├── skill_generator.py        # SKILL.md 提炼
├── skill_finalizer.py        # 固化快照
├── skill_loader.py           # 技能发现/加载（skills/{user_id}/）
├── skill_matcher_v2.py       # description 语义匹配复用
├── rag_knowledge.py          # RAG 外部知识 + 版本快照
├── retrieval_orchestrator.py # 五层检索编排
├── test_day17/18/19_*.py     # 测试（26 用例）
├── conftest.py               # 测试统一清理（user_test_%）
└── docs/*.md                 # 架构 / 性能 / 阶段总结 / FAQ
```

## 多租户设计要点

- 所有表含 `user_id`，所有查询 `WHERE user_id = %s`
- 缓存键含隔离维度：`kv:{user_id}:{entity_id}:{field}`
- RAG 知识库按 `tenant_id` 隔离（`public` 共享）
- 技能按用户私有目录：`skills/{user_id}/`
- `user_profile` 联合主键 `(user_id, key)`，杜绝 KV DB 层串数据

## 技术栈

PostgreSQL 16 + pgvector · Redis 7 · Python 3.9 · Ollama（bge-m3 embedding）· DeepSeek API

## 文档导航

| 文档 | 内容 |
|---|---|
| `ARCHITECTURE.md` | 竣工架构（5 层检索 + 6 类存储 + LPV + 多租户 + 已知缺口） |
| `性能基线与容量规划.md` | 实测延迟、并发压力、一致性维度 |
| `阶段总结-分层记忆系统架构.md` | 关键决策、踩坑教训、下一步 |
| `生产化准备清单.md` | 生产化待办与文件清单 |
| `FAQ.md` | 常见问题排查 |

## 已知缺口

tool_call/skill_call 不自动传播到向量层与 Summary（需显式同步）；`TTL_RAG` 定义未使用；`SimpleConnectionPool` 非线程安全（生产用 `ThreadedConnectionPool`）；向量层未纳入五层检索链。

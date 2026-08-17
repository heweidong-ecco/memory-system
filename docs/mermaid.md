# 总体架构图 README.md#### 2. 总体架构图
```mermaid
flowchart TB
    subgraph API_Layer["API 接入层"]
        A1[unified_api.record_event]
        A2[unified_api.query_memory]
    end

    subgraph Core_Layer["核心处理层"]
        B[EventBus 事件总线]
        C[Retrieval Orchestrator<br/>unified_search]
    end

    subgraph Write_Path["写入路径"]
        direction LR
        W1[(Ledger 账本<br/>Append-Only)]
        W2[(Redis 热缓存)]
        W3[(Skill Candidate Vectors<br/>+ Skill Snapshots)]
    end

    subgraph Query_Path["查询路径"]
        direction LR
        Q1[(User Profile<br/>KV 精准)]
        Q2[(Summary 摘要)]
        Q3[(Ledger 回溯)]
        Q4[(Skills 复用)]
        Q5[(RAG Knowledge Base<br/>tenant 隔离)]
    end

    A1 --> B
    B --> W1
    B --> W2
    B -- "tool/skill 成功" --> W3

    A2 --> C
    C --> Q1
    C --> Q2
    C --> Q3
    C --> Q4
    C --> Q5

    W1 -.-> Q3
    W3 -.-> Q4
```

# 写入流程 README.md##### 4.1 写入流程（record_event）
```mermaid
sequenceDiagram
    participant API as unified_api
    participant Bus as EventBus
    participant Ledger as Ledger
    participant Redis as Redis
    participant Skill as Skill Store

    API->>Bus: record_event(event)
    Bus->>Ledger: append(event)  // 持久化原始事件
    Bus->>Redis: update_cache(key, value)  // 更新热缓存
    Bus-->>Skill: 若 event.type in [tool, skill] 且 success
    Skill->>Skill: 生成候选向量/快照
```

# 查询流程（query_memory）README.md######-4.2-查询流程
查询流程-README.md######-4.2-查询流程（query_memory）
```mermaid
flowchart TD
    Start([query_memory]) --> C{unified_search}
    C --> S1[1. KV 精准查询]
    S1 -- 命中 --> R[返回结果]
    S1 -- 未命中 --> S2[2. Summary 摘要查询]
    S2 -- 命中 --> R
    S2 -- 未命中 --> S3[3. Ledger 回溯查询]
    S3 -- 命中 --> R
    S3 -- 未命中 --> S4[4. Skills 复用查询]
    S4 -- 命中 --> R
    S4 -- 未命中 --> S5[5. RAG 条件性查询]
    S5 --> R
    R --> F[结果融合与排序]
    F --> End([返回最终结果])
```

# 简版架构图 README.md####8. 总结
简版架构图-README.md####-8. 总结
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
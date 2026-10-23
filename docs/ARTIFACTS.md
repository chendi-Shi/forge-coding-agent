# 原始观察的持久化与跨 worker 读取

Forge 把工具观察的完整 UTF-8 文本存入服务端 PostgreSQL `forge_artifacts` 表。checkpoint 的消息只保存有界预览和 `_artifact_reference`，新 worker 使用新的有效租约从 API 读取原文，不依赖原 worker 的磁盘。这解决的是观察原文随 worker 丢失的问题；任务能否恢复，仍受预算、项目版本、租约和不确定工具状态约束。

这里的 artifact 是序列化后的观察文本，包括工具结果和检索结果，不是通用文件上传服务。工作区 ZIP、Git diff、模型决策日志各有自己的保存路径，不能据此宣称所有运行资料都已进入统一对象存储。

## 数据与接口

[Artifact 模型](../src/agent_backend/coding/models.py#L106) 以 `(task_id, artifact_id)` 为复合主键，保存 `kind`、原文 SHA-256、UTF-8 字节数、`content` 和创建时间。Task 外键配置 `ON DELETE CASCADE`；目前没有自动删除任务或产物的保留策略。新表通过现有 [迁移入口](../src/agent_backend/migrate.py#L20) 的 `create_all` 创建。

| 接口 | 请求 | 返回与约束 |
|---|---|---|
| `PUT /api/coding/tasks/{task_id}/artifacts/{artifact_id}` | JSON `{kind, content}`；Bearer 身份及 `X-Lease-Token` | 原文不可覆盖；相同内容重复写返回 `created: false`，不重复占用配额 |
| `GET /api/coding/tasks/{task_id}/artifacts/{artifact_id}` | `offset` 默认 0；`limit` 默认及最大 8000；同样验证身份与租约 | 返回原文片段、元数据、`next_offset`；偏移单位为 Unicode 字符，不是 UTF-8 字节 |

两个接口均要求 owner 拥有该 Task，且 Task 是已准备好工作区、未请求取消、处于 running 状态的 Forge 任务，租约必须有效。知道内容哈希不等于获得访问权限，也不能跨任务取用同哈希的数据。GET 是执行中 worker 的读取接口，目前没有供终态审核使用的独立下载接口。[鉴权与状态检查](../src/agent_backend/forge/artifacts.py#L37)、[PUT](../src/agent_backend/forge/artifacts.py#L49)、[GET](../src/agent_backend/forge/artifacts.py#L78)。

两种哈希的含义不同，不能混用：[定义](../src/agent_backend/forge/artifact_data.py#L15)。

```text
artifact_id = SHA256(UTF8(kind) || NUL separator || UTF8(content))
uri         = artifact://{artifact_id}
sha256      = SHA256(UTF8(content))
```

URI 用于内容寻址，`sha256` 用于核对原文字节。URI 不包含本地路径、访问密钥或可任意抓取的外部 URL。原文不允许 NUL 字符或无效 UTF-8；哈希中的 NUL 仅是种类与内容之间的内部定界符。

[固定限制](../src/agent_backend/forge/artifact_data.py#L7) 为：单项原文最多 **1 MiB**，每任务累计原文最多 **16 MiB**，每任务最多 **256 项**。字节上限按 UTF-8 编码计算；中文等多字节内容不会按字符数低估。PUT 在同一 Task 行锁下验证租约、查询已有内容并计算配额，再提交新内容，因此多个 API 副本不能同时越过配额检查。这是应用层任务配额，不是 PostgreSQL、Docker 卷或租户全部历史数据的磁盘硬配额。

## 先保存原文，再发布引用

`ContextBuilder.observation()` 生成预览和引用。它的 `put()` 接口是同步的；生产实现 [ServerArtifacts](../src/agent_backend/forge/artifact_client.py#L29) 在此阶段只暂存原文，网络上传是异步操作，不能把 `put()` 返回当成持久化成功。

实际 worker 的顺序是：

1. 工具返回结果，生成内容哈希、受限预览和 `_artifact_reference`。
2. [发布 observation 事件前](../scripts/forge_worker.py#L53)，先等待 `flush()` 上传原文；不可先发布指向尚未提交原文的事件引用。
3. [保存 checkpoint 前](../scripts/forge_worker.py#L58)，调用 `prepare_checkpoint()`，确保新原文已提交，并核对已有引用；随后才发送 checkpoint CAS 请求。
4. API 在 Task 行锁、当前租约和 expected revision 检查之后执行 [validate_checkpoint_artifacts](../src/agent_backend/forge/artifacts.py#L93)，核对该任务已有记录及完整元数据，再一起提交 runtime state 和新 revision。[挂接位置](../src/agent_backend/forge/api.py#L61)。

checkpoint 的原有上限仍是 1,000,000 字节；完整观察不随每次状态更新反复上传。校验针对消息中的显式 `_artifact_reference`，不会把工具输出里恰好出现的 URI 文本当成可信引用。

原文上传与 checkpoint 是两个事务，并非跨请求的原子提交：

| 中断位置 | 恢复语义 |
|---|---|
| 原文提交前 | 不发布对应 checkpoint；旧检查点仍保留原 pending 动作 |
| 原文已经提交，上传确认丢失 | 内容相同的 PUT 可幂等重试；不生成第二份记录 |
| 原文提交后、checkpoint 提交前 | 可能留下尚未被 checkpoint 引用的原文；旧 pending 和工具操作日志仍决定如何继续 |
| checkpoint 提交后 | 新 worker 按引用从服务器分页读取；不需要原 worker 本地文件 |
| 引用不存在或元数据被替换 | checkpoint 被拒绝，不能把不可读或不匹配的引用作为新恢复状态发布 |

这些规则不改变外部副作用的故障窗口。若 Docker 操作已执行、但操作结果未提交，仍不能据 artifact 哈希推断它成功，也不能自动再次执行未知结果的修改。

## 恢复和旧版本迁移

失败任务显式 resume 并领取新租约后，新建的 `ServerArtifacts` 从 API 验证保存的引用，`read_artifact` 也改为 [读取服务器](../scripts/forge_worker.py#L84)。旧租约不能继续上传或发起新的授权读取。GET 在请求开始时验证权限；已经通过验证的不可变内容读取，可能在并发取消提交之后才返回，不承诺中断在途响应。

旧版本 checkpoint 可能引用尚未上传的本地 `<digest>.txt`。[迁移逻辑](../src/agent_backend/forge/artifact_client.py#L74) 先查服务器；只有返回 404 时才尝试当前 worker 的旧目录。文件名由严格校验的 digest 构造，检查大小和符号链接，读取后必须与保存的 `kind`、原文哈希、字节数及 URI 一致，才能上传。不是看到一个文件就无条件信任它。

若原 worker 永久离线、旧文件已经删除或内容不一致，就无法凭摘要恢复原文。实现返回 `LEGACY_ARTIFACT_UNAVAILABLE` 或完整性错误，不把预览冒充完整内容，也不重跑命令伪造历史观察。升级前已有的本地数据不会因新表上线而自动出现。

## 可复核证据与尚存边界

[功能回归](../tests/test_forge_artifacts.py) 使用真实 SQLAlchemy 查询和 ASGI 请求，覆盖换租约后的独立客户端读取、租户隔离、过期或取消状态、幂等上传、哈希不符、UTF-8 字节限制、任务数量与总量限制、checkpoint 引用校验及旧文件迁移。数据库使用 SQLite，仅用于这些功能契约，不能证明 PostgreSQL 并发锁行为。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_forge_artifacts.py -q
```

[服务端恢复探针](../scripts/check_forge_artifacts.py) 使用真实 HTTP/PostgreSQL、替换租约和没有本地文件的独立客户端；[循环恢复探针](../scripts/check_forge_recovery.py) 使用脚本决策制造已提交修改后的丢响应。运行时应与其他 `isolation-test` 队列探针串行，结果写入各自 `results/forge-*.json`。本页不预填实机结果；以实际生成报告和 [验证记录](VALIDATION.md) 为准。

仍需明确以下限制：

- 目前没有真实多物理主机迁移、旧主机永久销毁或数据库灾备恢复的验证证据。独立客户端、空本地缓存证明的是读取协议，不等于多机容灾证明。
- 工具结果仍可能同时保存在 `ToolOperation.result` 和事件 payload。独立 artifact 表减少 checkpoint 反复携带原文，没有消除数据库内全部重复存储。
- 没有 retention / GC，也没有自动回收上传成功但未被 checkpoint 引用的原文。每任务限制不能约束所有已结束任务长期累积的总容量。
- PostgreSQL 仍是原文的可用性依赖；数据库不可达时读取明确失败。没有另建对象存储、跨地域复制或服务端文件缓存。

## 参考机制及许可

实现参考了上游的持久化分层与发布顺序，接口和租约授权由本项目自己定义。

- OpenHands SDK 固定提交 `60192ed54db73ac658ac1a07462916126c32d3a0`：[EventLog](https://github.com/OpenHands/software-agent-sdk/blob/60192ed54db73ac658ac1a07462916126c32d3a0/openhands-sdk/openhands/sdk/conversation/event_store.py) 先写事件 payload 再更新索引，且明确说明本地文件锁不能视作可靠 NFS 分布式协调；[许可 MIT](https://github.com/OpenHands/software-agent-sdk/blob/60192ed54db73ac658ac1a07462916126c32d3a0/LICENSE)。
- LangGraph 固定提交 `9d68e5b3eb1a63027b05e9f0152149ea0284dcdb`：[Postgres checkpoint / blob 表](https://github.com/langchain-ai/langgraph/blob/9d68e5b3eb1a63027b05e9f0152149ea0284dcdb/libs/checkpoint-postgres/langgraph/checkpoint/postgres/base.py#L50) 将元数据与版本化值分离；[写入逻辑](https://github.com/langchain-ai/langgraph/blob/9d68e5b3eb1a63027b05e9f0152149ea0284dcdb/libs/checkpoint-postgres/langgraph/checkpoint/postgres/aio.py#L273) 按类型和变化版本处理，并非自动外置所有长字符串。其 namespace 也不能替代本项目 owner 鉴权；[许可 MIT](https://github.com/langchain-ai/langgraph/blob/9d68e5b3eb1a63027b05e9f0152149ea0284dcdb/LICENSE)。

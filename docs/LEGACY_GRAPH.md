# Agent API 后端

历史记录：以下描述旧版 LangGraph mock 原型及当时的启动方式；当前 Forge 实现、启动命令和实测范围见 [项目 README](../README.md)。

基于 **Aegra 0.10.8 + FastAPI + LangGraph + PostgreSQL + Redis**。默认部署两个 API/Worker 实例，每实例 50 个任务槽位，目标为约 100 个独立 Agent 任务同时执行。Nginx 统一提供 HTTP 和 SSE 入口。

本机完整部署已实测 **100/100 任务成功、同时执行峰值 100、两个实例各 50**。负载是每任务 10 秒的异步 mock I/O；它不等于 100 QPS，也不代表真实模型的吞吐或提供商额度。完整数据见验证记录和本地 `results/load-test.json`。

本次已执行检查与适用范围见 [旧版验证记录](LEGACY_VALIDATION.md)。

## 启动

需要 Docker Engine/Compose。第一次使用会下载镜像并构建应用。Windows 可直接运行：

```powershell
.\scripts\start.ps1
```

**这台电脑已经配置 WSL Docker Engine**（Ubuntu-24.04）。本地 `.runtime.json` 选择该引擎，启动脚本会自动启动 WSL 中的 Docker 服务，不依赖损坏的 Docker Desktop。Windows 上管理本项目统一使用 `scripts/compose.ps1`，例如：

```powershell
.\scripts\compose.ps1 ps
.\scripts\compose.ps1 logs --tail 100 api
.\scripts\compose.ps1 stop
.\scripts\start.ps1
```

该包装脚本只指定本项目目录和所选引擎；普通 Linux 部署直接使用 `docker compose`。新机器的 `.runtime.json` 不随 Git 传播，缺省使用 Docker Desktop/当前 Docker 上下文。

WSL 模式启动时会创建隐藏的保活进程，防止退出终端后 WSL 自动停机；重复启动不会重复保活。全栈 `stop`/`down` 后自动释放该进程。电脑重启后运行 `scripts/start.ps1` 恢复服务。

或者在本目录执行以下跨平台命令：

```bash
python scripts/init_env.py
docker compose up --build -d --wait --wait-timeout 240
```

- API 文档：<http://127.0.0.1:2026/docs>
- 健康检查：<http://127.0.0.1:2026/health>
- `.env` 自动生成随机数据库密码和 API key，不会覆盖已有配置，也不会把密钥打印到终端。
- Swagger 的 **Authorize** 输入 `.env` 中 `API_KEYS_JSON` 对应的 token。直接请求用 `Authorization: Bearer <token>`。
- 入口默认只绑定本机；数据库与 Redis 不发布宿主机端口。
- 暂停：Windows 用 `scripts/compose.ps1 stop`，Linux 用 `docker compose stop`。`down` 保留数据卷；不要对需要保留的数据库执行 `down -v`。

## 两种运行模式

默认 `AGENT_MODE=mock`：异步模拟 I/O，不访问模型、不消耗模型额度。可以验证队列、状态保存、并发和流式传输，但回答只是模拟输出。

接入真实模型时修改 `.env`：

```dotenv
AGENT_MODE=llm
OPENAI_API_KEY=<你的模型服务密钥>
OPENAI_BASE_URL=<兼容接口的基础地址>
LLM_MODEL=<该服务支持工具调用的模型名称>
```

然后执行 `scripts/compose.ps1 up -d --force-recreate api`（Linux 用 `docker compose`）。真实模式有异步模型调用、多轮上下文和一个 UTC 时间工具；业务工具在 `src/agent_backend/graph.py` 添加。模型调用超时、重试和输出长度可通过 `.env` 配置。工作流由 Aegra 注入 PostgreSQL checkpointer，不使用进程内会话存储。

## API 使用

本项目支持下列 v1 HTTP/SSE 接口；上游 v2 commands 协议已关闭，以保持统一的鉴权和任务冲突语义。

最快的调用方式：

```powershell
.\.venv\Scripts\python.exe scripts/run_agent.py "你好"
# 后续加 --thread-id 上一次输出的ID，继续同一会话。
```

每个独立会话创建一个 thread；同一会话同时只允许一个 pending/running 任务，竞争提交返回 HTTP 409。PostgreSQL 部分唯一索引保证跨副本原子性。本项目仅支持 `multitask_strategy=reject`（也可省略）；其他策略返回 422。示例请求：

```http
POST /threads
Authorization: Bearer <token>
Content-Type: application/json

{}
```

取得 `thread_id` 后提交后台任务：

```http
POST /threads/{thread_id}/runs
Authorization: Bearer <token>
Content-Type: application/json

{
  "assistant_id": "agent",
  "input": {"messages": [{"role": "user", "content": "现在几点？"}]},
  "multitask_strategy": "reject",
  "durability": "sync"
}
```

| 操作 | 接口 |
|---|---|
| 查询任务状态 | `GET /threads/{thread_id}/runs/{run_id}` |
| 查看持久化输出 | `GET /threads/{thread_id}/state` |
| 提交并流式返回 | `POST /threads/{thread_id}/runs/stream` |
| 订阅已有任务 | `GET /threads/{thread_id}/runs/{run_id}/stream` |
| 取消任务 | `POST /threads/{thread_id}/runs/{run_id}/cancel` |
| 运行模式与单实例槽位 | `GET /v1/info` |

流式提交时增加 `"stream_mode":["messages","updates"]` 和 `"on_disconnect":"continue"`。后者让任务在浏览器断线后继续运行。不要自动重发提交请求，否则可能创建重复任务。

API key 映射到稳定身份；不同身份的 thread/run 由 Aegra 的数据库 `user_id` 约束隔离。`API_KEYS_JSON` 可以配置多组身份与密钥；同一身份共享访问范围。当前是服务端 API key 认证，尚未实现终端用户登录、计费或组织管理。Assistant 定义由代码部署、对客户端只读；Store 与 Cron 默认关闭访问，按业务需要加入权限策略。

## 验证约 100 并发

安装本地测试依赖（Python 3.12–3.14）：

```bash
python -m venv .venv
# Windows 使用 .venv\Scripts\python.exe；Linux/macOS 使用 .venv/bin/python
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q
```

启动完整服务后：

```powershell
.\.venv\Scripts\python.exe scripts/smoke_test.py
.\.venv\Scripts\python.exe scripts/load_test.py --concurrency 100 --delay 10 --min-peak 100
.\.venv\Scripts\python.exe scripts/reliability_test.py
.\.venv\Scripts\python.exe scripts/recovery_test.py --inject-crash
```

默认从本地 `.env` 读取 token；也可设置 `AUTH_TOKEN`、`BASE_URL`。测试脚本强制检查 mock 模式，避免意外发出 100 路真实模型请求。

并发脚本先创建 100 个独立 thread，再统一提交任务。它检查每个任务的成功状态、唯一输入/输出关联，并用 **Agent 节点的服务端开始/结束时间** 计算重叠执行峰值。只要有失败或峰值不足，就以非零退出码结束。报告包含 p50/p95、每实例峰值和错误摘要，默认保存为 `results/load-test.json`，不保存密钥或原始响应。

默认保留测试 thread 供核查；传 `--cleanup` 只删除该轮测试创建的 thread。跨机器测试需同步系统时钟。若机器启动/提交较慢，可增加 `--delay 20`；不要降低峰值标准来宣称达到 100。

`smoke_test.py` 验证 HTTP 鉴权、后台执行、状态持久化、SSE，以及同 thread 的 8 路竞争中仅一个提交成功。`reliability_test.py` 从 `.env` 读取两个不同身份，验证跨身份读写拒绝和重连后输出不变；新环境需先在 `API_KEYS_JSON` 配置第二个随机 token，也可指定 `SECOND_AUTH_TOKEN`。

`recovery_test.py --inject-crash` 会杀死**本项目中实际执行测试任务的 API 容器**，验证另一副本接管后，再在 `finally` 恢复原容器，并从恢复的容器重新读取持久化结果。仅在 mock 模式和两个健康副本下运行，勿与其他验收并行执行。不加该参数只预检，不注入故障。报告分别保存至 `results/reliability.json`、`results/recovery.json`。

## 并发与部署

```text
客户端 → Nginx → API/Worker × 2
                    ↕       ↕
                Redis     PostgreSQL
              任务/事件     运行/会话/checkpoint
```

默认 `API_REPLICAS=2`、`WORKER_COUNT=5`、`N_JOBS_PER_WORKER=10`，总槽位为 `2 × 5 × 10 = 100`。超额任务在队列等待；这套初版没有全局队列长度上限、按用户配额或模型 RPM/TPM 分配器。真实业务上线时需按提供商限额补充准入与限流，不能仅靠增加槽位提高吞吐。

调整副本数后执行 `docker compose up -d`。Nginx 使用 Docker DNS 动态更新后端地址；SSE 关闭代理缓冲。每个实例使用一个 Uvicorn 进程，避免把进程数和 Worker 数相乘而意外超出目标。

网关的 upstream 空闲连接 2 秒过期，早于 Uvicorn 的默认 5 秒。通信故障最多尝试两个后端，已发送的 POST/PATCH 不会自动重放；客户端仍应避免盲目重复提交任务。

迁移服务先完成 Alembic、checkpoint 和 store 初始化，再启动 API，避免空数据库上的双实例建表竞态。Postgres 上限为 150 条连接，两个实例主要连接池总预算约 50 条；大幅扩容时需同时核对数据库、Redis 连接数和内存。

Aegra 提供租约、心跳与 checkpoint 恢复机制。工具执行可能因故障被重试，涉及写入外部系统的工具必须自行实现幂等。不要把 `asyncio` 并发当成 CPU 并行：重 CPU 工具应移到专用进程/任务服务。

## 文件与依赖

- `compose.yaml`：数据库、Redis、单次迁移、两个 API 实例和网关。
- `src/agent_backend/graph.py`：mock 与真实 Agent，业务扩展入口。
- `src/agent_backend/auth.py`、`security.py`、`server.py`：身份、访问规则、独立 HTTP 守卫和启动校验；上游鉴权加载失败会拒绝启动。
- `scripts/load_test.py`：服务端重叠时间测量，非仅客户端连接数计数。
- `src/agent_backend/concurrency.py`：数据库活跃任务唯一约束与 HTTP 409 映射。
- `uv.lock`：跨平台依赖锁；`requirements.lock`：带哈希的运行时依赖导出，Docker 构建使用它。

依赖更新后用 `uv lock`、`uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements.lock` 更新锁，再重新测试与构建。

上游依据：[Aegra 0.10.8](https://github.com/aegra/aegra/releases/tag/v0.10.8)、[Worker 架构](https://github.com/aegra/aegra/blob/v0.10.8/docs/guides/worker-architecture.mdx)。

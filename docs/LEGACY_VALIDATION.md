# 验证记录

日期：2026-10-23（Asia/Shanghai）。完整服务已在本机部署并运行，入口为 http://127.0.0.1:2026/docs。

## 部署与验收

环境：Windows + WSL Ubuntu-24.04 Docker Engine；两个 Aegra 0.10.8 API/Worker 实例、PostgreSQL 17/pgvector、Redis 7.4、Nginx 1.28。每实例 5 个 worker，每 worker 10 个任务槽位，运行模式为 mock。

| 检查 | 实际结果 |
|---|---|
| Docker 镜像构建、依赖安装、完整服务启动 | 通过 |
| Alembic、checkpoint/store 初始化、应用唯一索引 | 通过 |
| 两个 API 实例、Postgres、Redis | 全部 healthy，网关正常运行 |
| 未携带或携带错误 token | HTTP 401 |
| 后台任务、持久化状态读取、SSE 流式运行 | 通过 |
| 同会话普通/stream/wait 重复提交 | HTTP 409 |
| 同会话 8 路竞争提交 | 恰好 1 个成功、7 个 HTTP 409 |
| 非 reject 策略 | HTTP 422 |
| v2 commands | 有效命令请求返回禁用提示 HTTP 503，不执行 |
| 两个身份隔离 | 7 项检查通过；跨身份读取 thread/state/run 及删除 thread 均为 404 |
| 全栈 stop/up 与 WSL 保活 | 停止后释放保活锁，重新启动恢复健康 |
| 退出交互 WSL 命令后的 Windows HTTP 可达性 | 连续 60.45 秒，5 次健康请求全部 200 |

## 100 并发实测

网关修复后，在最终应用版本上连续执行两轮：每轮预先创建 100 个独立 thread，再统一提交 100 个任务；每个 Agent 节点执行 10 秒异步等待。

| 指标 | 第 1 轮 | 第 2 轮 |
|---|---:|---:|
| 成功 | 100/100 | 100/100 |
| 失败 | 0 | 0 |
| 服务端节点同时执行峰值 | 100 | 100 |
| 每实例峰值 | 50 + 50 | 50 + 50 |
| 端到端 p50 | 15.71 秒 | 18.37 秒 |
| 端到端 p95 | 17.19 秒 | 19.91 秒 |

峰值根据每个成功任务的服务端节点开始/结束时间计算，所有输出均校验唯一请求标识，验证没有串会话。延迟从统一提交开始，到最终输出核验结束，包含排队、执行与轮询，不包含预先创建 thread 的时间。两个实例使用同一个 WSL 内核时钟。

本地原始报告：[第 1 轮](../results/load-test-first.json)、[第 2 轮](../results/load-test.json)。测试 thread 保留供核查。

这些结果证明本机完成了 **100 路 mock I/O 并发执行**；不代表 100 QPS，也不代表真实 LLM 或 CPU 密集工具的吞吐。

## 实例崩溃恢复

已实际执行 `scripts/recovery_test.py --inject-crash`：

1. 创建 40 秒 mock 任务，通过 Postgres claimed_by 找到实际执行容器。
2. 验证另一副本健康后，对执行容器发送 SIGKILL。
3. 原容器保持停止，另一副本接管并完成同一 run，输出标识与预期一致。
4. 从故障注入至成功观察约 **78.52 秒**，包含租约回收等待及重跑 40 秒节点；这不是恢复时间 SLA。
5. 恢复原容器并等待健康，直接通过该容器自己的 API 再读 run/state，结果保持一致。

报告中 passed、crash_injected、recovered_before_restart、restored_healthy、persisted_on_restarted_instance 均为 true，见 [故障恢复报告](../results/recovery.json)。这证明 API 实例故障恢复，不覆盖整个数据库或宿主机故障；有外部写入副作用的工具仍须幂等。

## 代码检查

| 检查 | 结果 |
|---|---|
| python -m pytest -q | 38 passed，3 subtests passed |
| python -m ruff check src scripts tests | 通过 |
| python -m pip check | 无依赖冲突 |
| uv lock --check | 104 个包锁定一致 |
| python -m compileall -q src scripts tests | 通过 |
| 鉴权加载失败场景 | 拒绝启动，不回退匿名访问 |
| 本地配置保护 | .env、.runtime.json、.venv 被 Git 忽略；报告不含 token |

## 验收中修复的问题

- 上游 v1 没有原子同线程冲突检查：增加 PostgreSQL 活跃任务部分唯一索引，冲突映射为 409。失败事务整体回滚，只有提交成功的任务才入队。恢复修改同一 run，不受索引阻止。
- 上游 v2 使用不同的策略和错误封装：本项目关闭 v2，统一提供文档中的 v1 API。
- WSL 在交互命令退出后停机：增加隐藏、项目独占的保活进程，完整停止项目时释放；已实测启停和后台可达性。
- 一轮复测发生 upstream 连接重置，默认一次失败便摘除副本，继而出现 no live upstreams。现象与复用过期连接相符：将 upstream 空闲超时设为 2 秒，低于 Uvicorn 默认 5 秒；提高摘除阈值，并允许通信故障时有限重试。不启用 non_idempotent，保留对已发出 POST/PATCH 的重试保护。修复后两轮 100 并发全部通过。[Nginx 连接设置](https://nginx.org/en/docs/http/ngx_http_upstream_module.html#keepalive_timeout)、[重试语义](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_next_upstream)。

## 真实模型接入范围

已实现异步模型调用、工具调用循环、多轮会话与持久化接入。当前没有用户提供的模型密钥，因此没有发出真实模型请求；特定模型兼容性、RPM/TPM 配额和实际回答质量未验收。

在 .env 设置 AGENT_MODE=llm、OPENAI_API_KEY、LLM_MODEL，按需设置 OPENAI_BASE_URL，再重建 API 容器即可接入。操作和复现命令见 [README](../README.md)。

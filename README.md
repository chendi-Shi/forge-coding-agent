# Forge · 可恢复的 Coding Agent 后端

面向校招／初级 Agent 应用开发的工程项目：自己实现结构化决策循环、版本化代码检索、上下文预算、持久化工具调用与检查点恢复，并用真实开源历史 bug 和隐藏测试评估修复。

现有 Codex 登录可以直接运行；模型 API key 不是本机演示的前提。工作台：[http://127.0.0.1:2026/studio](http://127.0.0.1:2026/studio)。

## 本机登录

工作台和 Coding API 使用 `.env` 中 `API_KEYS_JSON` 的 `local-client` 密钥，API 请求头为 `Authorization: Bearer <本机密钥>`。这是后端访问密钥，不是模型供应商的 API key。

本机部署的实际密钥保存在未跟踪的 `README.local.md` 和 `.env` 中，不随 GitHub 仓库发布。新环境执行 `python scripts/init_env.py` 会生成自己的随机密钥；不要复用他人的部署密钥。

## 实际实现

| 能力 | 实现与可查看证据 |
|---|---|
| 自主执行循环 | `forge/runtime.py`：模型每步返回一个 schema 校验动作；工具执行、状态、停止条件由项目控制 |
| 代码检索 | `forge/retrieval.py`：BM25、路径/符号加权，Python AST，版本/文件摘要/行号引用；编辑后刷新索引 |
| 上下文管理 | `forge/context.py`：有界窗口、完整工具交换、观察外置、确定性摘要、重复调用检测 |
| 可恢复执行 | CAS 检查点、持久化 pending call、已完成结果去重、旧租约隔离、不确定副作用拒绝重放 |
| 修复闭环 | finish 强制固定测试，失败反馈修复；服务端再次独立验收，人工审核后推进项目版本 |
| 工程后端 | 双 API、副本间原子队列准入、SSE 回放、项目与身份隔离、Git diff、ZIP、接受/拒绝/follow-up |
| 沙箱 | Forge 所有代码读写、命令和测试通过内部 broker 在 Docker 中执行 |
| 评测 | 冻结上游父提交、独立隐藏 oracle、参考修复校准、保护原测试、保存所有实际模型事件 |

模型入口支持 `codex_bridge` 和 `openai_compatible`。当前真实评测用的是 **Codex 单步决策桥接**，HTTP 模型入口已实现但因没有 key 未做真实调用验证。旧版完整 Codex CLI 执行器可选作基线，详见 [模型适配器](docs/PROVIDERS.md)。

## 实测结果

2026-10-23，Windows + WSL Docker，真实 Forge worker 容量 2：

| 首次提交的真实历史问题 | 模型决策数 | 任务耗时 | 冻结独立评测 |
|---|---:|---:|---|
| cachetools cachedmethod / autospec 回归 | 18 | 335.94 秒 | 63 passed + 36 subtests |
| boltons Pearson 边界零分母 | 14 | 267.00 秒 | 44 passed |

两个候选都保留原测试，新增回归测试；隐藏测试与参考修复没有提供给 Agent。**这是两题历史 bug pilot，存在训练数据污染可能，不能当作 SWE-bench 或普遍成功率。** cachetools 首次候选被审查发现 introspection 回归，已拒绝。后续两次尝试预算耗尽；第四次任务在明确反馈和一次部署中断恢复后完成，最终 **87 passed + 36 subtests，兼容性复审 3 passed**。全部失败与消耗保留，不能把这条修复链写成首次自动成功。

本项目自动化 **245 passed、2 skipped**；已部署 Studio **24/24**，JS/console 错误 0。可审阅 [实测摘要与报告哈希](benchmarks/forge-run-summary.json)、[最终 cachetools 补丁](benchmarks/example-patches/cachetools-reviewed.patch) 和 [boltons 补丁](benchmarks/example-patches/boltons-first.patch)。

控制层实测：101 个同时提交、100 个入队、1 个 429、100 个唯一租约；真实故障注入验证丢失响应后复用已完成修改，未重复执行副作用。上述控制检查使用脚本，不消耗模型调用。检索消融在固定小型合成集上，hybrid 与纯 lexical 同分，不能声称 hybrid 优于 BM25。详见 [验证记录](docs/VALIDATION.md) 与 [评测来源](benchmarks/BENCHMARKS.md)。

## 启动与配置

Windows 需要 Python 3.12+、Git、已登录 Codex CLI、Docker Engine/Compose。本机已经配置 `.venv` 与 WSL Ubuntu-24.04 Docker。新环境先执行 `python -m pip install -e '.[dev]'`。

```powershell
.\scripts\start.ps1
.\scripts\compose.ps1 ps
.\scripts\stop.ps1
```

start 会启动后端和 Forge worker。本机入口只绑定 localhost，数据库、Redis 和 broker 不发布宿主机端口。重启电脑后重新运行 start。不要用 `down -v` 删除需要保留的任务与源码卷。

`.env` 默认 `FORGE_PROVIDER=codex_bridge`，正常使用本机 Codex 登录，不复制登录凭据。若以后获得模型 API，可设置：

```dotenv
FORGE_PROVIDER=openai_compatible
FORGE_BASE_URL=https://your-provider.example/v1
FORGE_API_KEY=your-local-key
FORGE_MODEL=your-model
FORGE_TOKEN_PARAMETER=max_completion_tokens
```

然后重启 Forge worker。不同供应商的结构化输出与 token 参数兼容性须实际验证。Linux CLI 主机可运行 `python scripts/forge_worker.py --concurrency 2`；Compose 用标准 `docker compose`。旧执行器通过 `scripts/start_coding_worker.ps1` 手动启动，任务 engine 需选 `codex`。

## 如何演示

打开工作台，选择 `OSS · cachetools-autospec` 或 `OSS · boltons-pearson`，查看真实任务的运行与工具记录、diff、固定测试和模型总结。导入新项目后选择 Forge，填写需求和测试命令即可运行。失败且仍有预算、无不确定工具状态的任务可从检查点恢复；其他情况可以新建 follow-up。

详细设计和取舍见 [架构说明](docs/ARCHITECTURE.md)；如何讲清项目及可据实写入简历的内容见 [演示与面试讨论](docs/INTERVIEW.md)。项目是工程作品，不代表任何公司的录用标准。

## 100 并发的范围

每个身份最多 100 个未完成任务；本机默认 2 个真实决策任务槽位和 2 个 Docker 并行槽位。队列容量、worker 并发和模型供应商配额是不同限制。单 worker 可配置 1–100，多机器可用不同 executor_id 消费同一身份队列，但目前没有验证 100 个真实模型任务同时运行。

`.env` 的 `CODING_WORKER_CONCURRENCY` 控制 launcher 的执行槽位，`SANDBOX_CONCURRENCY` 控制 Docker 并行数。Forge 队列直接使用 PostgreSQL；Redis 由保留的旧 LangGraph 示例使用。

## 验证与复现

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check .
.venv\Scripts\python.exe scripts/check_forge_control.py
.venv\Scripts\python.exe scripts/check_forge_recovery.py
.venv\Scripts\python.exe scripts/check_coding_queue.py
.venv\Scripts\python.exe scripts/evaluate_retrieval.py
.venv\Scripts\python.exe scripts/prepare_issue_benchmarks.py verify
```

真实模型评测：`python scripts/check_forge_issues.py`；默认读取已保存任务 ID，避免重复提交。候选独立评分：

```powershell
python scripts/prepare_issue_benchmarks.py evaluate --case cachetools-autospec --candidate results/forge-cachetools-autospec.zip --report results/forge-cachetools-autospec-oracle.json
```

控制层脚本使用 `.env` 中的 `isolation-test` 身份。原始本地报告和模型事件位于 `results/`；完整说明在 `docs/VALIDATION.md`，固定考题定义与结构化摘要在 `benchmarks/`。本机已有所有报告时，可运行 `python scripts/export_forge_evidence.py` 重新导出不含认证配置的实测摘要。

## API 与边界

接口文档：[http://127.0.0.1:2026/docs](http://127.0.0.1:2026/docs)。Coding API 要求 bearer token，按身份隔离。新增 Forge 接口为 `tasks/{id}/tools`、`/tool-operations`、`/checkpoint` 和 `/resume`；完整任务流程仍支持 import → queue → run → verify → review。

Docker 默认非 root、无网络、只读根目录、1 CPU / 1 GiB / 128 PID，验收使用临时复制卷。依赖需预装进沙箱镜像；源码限制为压缩 16 MiB、展开 20 MiB、2000 文件、单文件 2 MiB。非法路径/敏感路径会被拒绝，不能承诺任意仓库可直接导入。

取消属于协作式取消：阻止后续动作，已提交到 broker 的命令仍可能运行至自身超时。持久卷没有磁盘硬配额，也未实现恶意多租户强隔离。原始观察的部分 artifact 存在 worker 本地，跨机器恢复可能需要重新读取源码。全部限制和故障窗口见架构说明。

旧版全自动 CLI 的结果保留于 [旧 Coding 验证](docs/CODEX_BASELINE_VALIDATION.md)，旧 LangGraph 模拟压测保留于 [历史验证](docs/LEGACY_VALIDATION.md)，不混入当前真实 Forge 结果。

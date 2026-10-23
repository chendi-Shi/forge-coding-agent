# 本机 Codex Coding Worker

这个 worker 拉取后端任务，在每个任务的独立 Git 副本中调用真实 Codex CLI，读取仓库、修改文件、运行命令和测试。完成后将代码 ZIP 上传到后端；后端把产物复制进独立、禁用网络的 Docker 验收环境，重新运行指定测试。

**模型命令实际运行在 Windows 的 Codex `workspace-write` 沙箱中。Docker 用于后端独立验收，不是本机模型命令的执行容器。** 本部署适合受信任用户的本机开发任务；Windows worker 不是面向互不信任租户的完整容器隔离服务。

## 启动与停止

先保证后端运行，所选 Python 安装了 `httpx`，并已在同一 Windows 用户下正常登录 Codex：

```powershell
codex --version
codex login status
./scripts/start_coding_worker.ps1
./scripts/stop_coding_worker.ps1
```

启动脚本优先使用项目 `.venv/Scripts/python.exe`，没有时使用 PATH 中第一个 Python。默认 API 地址为 `http://127.0.0.1:2026`、并发为 2；读取项目 `.env` 中的 `HOST_PORT` 和 `CODING_WORKER_CONCURRENCY`，显式参数优先：

```powershell
./scripts/start_coding_worker.ps1 -BaseUrl http://127.0.0.1:2026 -Concurrency 2
```

脚本使用隐藏进程，并记录 PID、启动时间、完整脚本路径、Python 路径和随机实例标识。重复启动会识别已运行的本项目 worker。停止时重新核对身份，先请求正常取消，默认等待 30 秒，然后仅在身份仍然匹配时结束该进程树。不会按进程名称批量停止其它 Python 或 Codex。

直接运行用于排查：

```powershell
python scripts/coding_worker.py --base-url http://127.0.0.1:2026 --concurrency 2
```

## 认证与沙箱

- 后端认证优先使用环境变量 `AUTH_TOKEN`，否则读取 `.env` 的 `API_KEYS_JSON["local-client"]`。不会把此 token 输出到日志或传给 Codex 子进程。
- Codex 复用自己的正常登录。worker 不读取、复制或打印 Codex token 文件，不替换 `CODEX_HOME`。Windows 的已有登录不会自动成为 Docker/WSL 的登录。
- CLI 使用 `-a never exec --json --sandbox workspace-write --ignore-user-config`，任务通过 stdin 传入，始终使用 argv 调用。任务未设置 `model` 时不传模型参数。
- Windows 显式添加 `-c windows.sandbox="unelevated"`，启用受限令牌沙箱。忽略用户配置时也必须指定这一设置，否则当前 CLI 可能退回只读。没有使用危险的沙箱绕过选项。
- `--ignore-user-config` 避免继承用户 `config.toml` 的全局 MCP 设置；它不等于关闭所有组织策略、插件或项目说明。后端和模型 API 密钥、数据库连接信息等不传入子进程环境。
- Git 基线忽略系统和用户全局 Git 配置，禁用交互凭据提示。输入中的 `.git`、`.codex`、`.env` 等敏感路径不会进入任务副本。

## 任务生命周期

worker 每 2 秒尝试领取任务。领取后立即启动每 5 秒一次的租约心跳，再下载源码并建立本地 Git 基线。源码准备和完成验收 HTTP 请求的超时均为 300 秒；完成请求等待后端验收期间心跳仍持续。executor 自身每 15 秒注册在线状态。

每条 Codex JSONL 事件保存到本地，并分批上传。服务端按任务和事件序号去重。大事件上传时提供截断预览，本地日志保留较完整内容；stderr 最多保留 64 KiB，已知后端/租约凭据及常见 Bearer/API-key 文本会脱敏。

取消、执行超时、租约丢失和 worker 停止都会结束执行进程。Windows 使用带 `KILL_ON_JOB_CLOSE` 的 Job Object；启动门控确保命令执行前已纳入管理，包括其子孙进程。不会自动 `resume --last` 或重做过期任务。后端租约失效后将任务标为失败，避免不确定执行被重复提交。

CLI 正常退出及 `turn.completed` 只说明模型回合结束。模型可能报告测试失败或环境受限；真正的验收结果取决于后端独立测试。成功产物仍需用户检查差异并接受。

## 本地文件与产物限制

默认目录是项目的 `.coding-workspaces/`：

```text
.coding-workspaces/
  worker.pid.json
  worker.stdout.log
  worker.stderr.log
  <task-id>-<random>/
    repo/
    events.jsonl
    stderr.log
```

任务副本和日志会保留，便于复核失败和未上传的事件。清理时先停止 worker，并仅清理已确认无需保留的任务目录。

ZIP 不包含 `.git`、`.codex`、`.env*`、`.sandbox*` 元数据、软链接/目录联接/硬链接、`node_modules`、虚拟环境和常见缓存。输入和输出限制与后端严格限额对齐：单文件 2 MiB、文件内容合计 20 MiB、最多 2,000 个文件，压缩 ZIP 最多 16 MiB。超限任务报告失败，不截断源代码文件。

自定义 `--workspaces` 应使用当前用户拥有、沙箱可遍历的普通目录。Python 3.14 在 Windows 上建立的私有 `tempfile.mkdtemp()` 父目录可能阻止受限沙箱遍历；测试中已观察到这一点。默认项目目录通过真实执行验证。

## 并发与验证记录

`--concurrency` / `-Concurrency` 支持 1–100，默认 2。可以在不同主机启动多个使用同一后端身份的 worker，并给每个 worker 唯一 `executor_id`；每台主机需安装并正常登录自己的 Codex CLI。提高配置值不会提高账号模型额度，也不会自动提供对应 CPU、内存和执行隔离能力。

2026-10-23 在本机 Codex CLI 0.154.0、正常 ChatGPT 登录、Windows Python 3.14 下完成真实 preflight：

1. 在独立临时 Git 根目录创建 `hello.txt`，真实执行 Python 断言成功。
2. 经完整 worker 路径在默认 `.coding-workspaces` 建立新副本：初始 `add(a,b)` 返回 `0`，Codex 阅读源码和已有测试，修改为 `a + b`，并真实运行 `unittest`。
3. worker 返回后再次独立执行同一测试，退出码为 0；获得 session ID、14 条事件和代码归档。

这些是实际引擎及 worker 集成验证，不是 100 路真实模型并发测试，也不代替最终部署的 Docker 验收记录。

自动化测试覆盖归档路径/大小限制、敏感文件排除、凭据环境隔离、Unicode 事件分批、下载失败、超时、完成期间心跳，以及真实子进程和孙进程的取消清理：

```powershell
python -m unittest discover -s tests -p test_coding_worker.py -v
```

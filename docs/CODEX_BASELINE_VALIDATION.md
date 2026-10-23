# Coding Agent 验证记录

日期：2026-10-23。环境：Windows + WSL Ubuntu-24.04 Docker Engine；双 FastAPI/Aegra API、PostgreSQL17、Redis7.4、Nginx、独立 sandbox broker、Windows Codex worker（容量2）。

## 真实模型端到端

通过工作台同一后端创建两个独立项目和任务，使用本机已登录的 Codex CLI 0.154.0。没有预填模型答案、代替模型写修复、删除或放宽测试。

| 项目 | 修改前 | Codex 修改 | Docker 独立验收 | 原有测试 | 持久化事件 |
|---|---|---|---|---|---:|
| Python 订单金额 | exit 1 | 修复舍入、运费门槛、输入校验 | 5 项通过，exit 0 | 未修改 | 23 |
| Node.js 分页工具 | exit 1 | 修复一基页码、尾页、参数校验 | 6 项通过，exit 0 | 未修改 | 27 |

两个实际模型回合重叠 **66.65 秒**，由已持久化 thread.started 与 turn.completed 的时间计算。任务均停留在待审核，用户可查看真实命令日志、diff、修改前后测试及模型总结。

- Python task：`c1fd6082-70cc-4f55-a95a-2479991ae129`，diff 1154 bytes。
- Node task：`24b4e558-6068-411f-adaa-e62ada790acd`，diff 696 bytes。
- Node 的 Windows 模型沙箱中，默认 Node 测试子进程出现 spawn EPERM；模型如实记录，采用同进程 test-isolation=none 完成本地测试。后端 Linux 容器使用原始 `node --test test/paginate.test.cjs` 独立验收通过。
- 报告：[real-coding.json](../results/real-coding.json)。复核脚本：`scripts/check_real_coding.py`；重跑默认使用已保存任务 ID，不重复提交付费模型任务。

## 队列和控制层

实机向独立 isolation-test 身份同时提交101个任务，100个入队，第101个HTTP429。20个模拟执行器以每个5槽位领取，得到100个不同租约；没有启动100个模型进程。

| 检查 | 结果 |
|---|---|
| 100个并发提交及跨副本唯一领取 | 通过 |
| 第101个任务容量拒绝 | HTTP429 |
| client_request_id 幂等 | 返回原任务 |
| 跨身份任务读取 | HTTP404 |
| 错误租约上报事件 | HTTP409 |
| 同序号事件重试 | 只存一条 |
| 100个任务取消与完成 | 全部 cancelled |
| 失败基线→独立验收通过 | 通过 |
| 测试写文件不污染审核产物 | 通过，临时验证副本已删除 |
| 继续修复保留全部前次变更 | 通过 |
| 接受后项目版本递增 | 通过 |
| 旧版本补丁接受 | HTTP409 |
| complete 幂等重试 | 通过 |
| SSE历史回放及结束事件 | 通过 |
| 完成后重取任务源码 | HTTP409，快照保持不变 |
| 非法归档不能假装成功 | failed，未验收旧代码 |
| GitHub真实导入和读取源码 | octocat/Hello-World master 通过 |

报告：[coding-queue.json](../results/coding-queue.json)、[coding-flow.json](../results/coding-flow.json)、[github-import.json](../results/github-import.json)。队列与控制层检查使用确定性的工作区上传，不调用模型；与上方真实 Codex 验证分别计量。

## Sandbox 实机检查

`scripts/check_sandbox_live.py` **14/14通过，26.32秒**，验证以下行为：

- 文件CRUD、fork、restore、ZIP下载、包含删除与二进制文件的Git diff。
- fork建立新基线，同来源重试幂等；恶意ZIP四种场景HTTP422且不改原树。
- Python/pytest/Node命令真实执行；UID10001、network none、只读根目录和.git。
- 1CPU/1GiB/128PID限制、命令超时、输出截断。
- 测试执行容器、输入卷和测试工作区均清理。

报告：[sandbox-live.json](../results/sandbox-live.json)。Docker隔离适用于服务端验收；模型开发命令使用本机Codex沙箱。持久卷无磁盘硬配额。

## 自动化检查

- `pytest -q`：**147 passed，1 skipped，9 subtests passed**。跳过项是Windows缺少创建符号链接权限；实机Linux容器中另行验证了归档拒绝。
- `ruff check src scripts tests`：通过。
- `node --check src/agent_backend/studio/app.js`：通过。
- Worker隐藏启动、重复启动识别、正常停止：实测通过。
- 两API、数据库、Redis、broker健康；网关服务正常。入口：[工作台](http://127.0.0.1:2026/studio)。

浏览器实机验收 **19/19通过**，0 JavaScript/console/API错误。覆盖桌面与390px手机布局、登录/退出、项目与状态筛选、导入表单、真实任务日志、diff、独立测试与结果；见 [studio-check.json](../results/studio-check.json)，5张截图位于同目录。移动导入/退出入口和任务设置布局已修复并复验。

## 能力边界

当前验证证明：真实模型并行2路，以及100任务入队/领取控制。没有测量100个真实Codex同时执行，不将旧LangGraph模拟负载结果用于推断真实模型吞吐。增加worker机器和账号额度后，仍需对实际代码任务重新压测；验收容器并发独立配置。

旧版本mock压测和Aegra恢复验证已移到 [LEGACY_VALIDATION.md](LEGACY_VALIDATION.md)，不计入本次真实Coding Agent结果。

# Forge 验证记录

日期：2026-10-23。环境：Windows Python 3.14；WSL Ubuntu-24.04 Docker；Python 3.12 沙箱；双 FastAPI/Aegra API、PostgreSQL17、Redis7.4、Nginx。Forge worker 容量 2，Docker 工具并行容量 2。以下分别记录模型效果、控制层、检索和界面，不相互替代。

## 真实开源问题：首次提交

Agent 使用 `CodexDecisionProvider`，由 Forge 自己控制每一次结构化动作、Docker 工具执行和完成条件。没有模型 API key；HTTP provider 仅有协议测试。没有替模型直接写修复，参考代码和隐藏测试只提供给独立评测器。

| 指标 | cachetools-autospec | boltons-pearson |
|---|---:|---:|
| Forge 决策次数 | 18 | 14 |
| 任务总耗时 | 335.94 秒 | 267.00 秒 |
| 累计模型调用等待 | 281.91 秒 | 222.99 秒 |
| CLI 报告 input tokens | 310,472 | 226,432 |
| CLI 报告 output tokens | 3,532 | 2,383 |
| 持久化事件 | 94 | 74 |
| 固定验收命令 | exit 0 | exit 0 |
| 冻结 hidden + 原测试 + 新增测试 | 63 passed + 36 subtests | 44 passed |
| 原有测试文件 | 字节不变 | 字节不变 |
| 额外代码审查 | **发现兼容性回归** | 未发现明确阻断 |

两个任务并行运行，完整任务 ID：cachetools `c0367281-a328-4327-b24d-fed6fff99b42`，boltons `18e68fbb-e7a9-4835-b83d-55ee3f6d6dcb`。cachetools 首次候选已因下述兼容性回归标记 rejected；boltons 保持 awaiting_review。没有自动接受或发布。

这是两个人工选择的公开历史 bug，不是 SWE-bench，也不能作为通用成功率。存在训练数据污染可能。CLI 每步包含自身提示开销，input token 不是源码检索文本长度；没有价格或账单证据，因此不据此估算费用。没有将其与不同任务、不同执行环境的旧 CLI 示例做不公平的效率比较。

## 测试通过后的真实失败案例

cachetools 的首次补丁通过给 `_MethodDescriptor.__class__` 返回 `types.FunctionType` 解决原 issue，但使 Python introspection 把 descriptor 当成真实函数，随后访问不存在的 `__code__`。

冻结 hidden 保留原评分，不在看过候选后改题。另行固定三个 post-hoc review 检查：对原始 descriptor 调用 `mock.create_autospec`、`inspect.iscoroutinefunction`、`inspect.isgeneratorfunction`。Docker 中父版本 **3 passed**、上游参考 **3 passed**、首次候选 **3 failed**。因此首次结果应表述为“2/2 冻结 hidden 通过，其中 1/2 被后续代码审查阻断”，不能表述为两个可直接合并的修复。

复现定义位于 `benchmarks/cachetools-autospec/post_hoc_review/`，脚本 `scripts/review_cachetools_candidate.py`。首轮证据：`results/forge-cachetools-autospec-review-first.json`。后续把明确失败复现反馈给 Forge，不覆盖首次成绩，也不再宣称盲测。

## cachetools 后续修复：完整尝试链

| 尝试 | 已报告决策 | input / output tokens | 结果 |
|---|---:|---:|---|
| 1：首次提交 | 18 | 310,472 / 3,532 | 冻结评测通过；额外兼容性审查 3 项失败，已拒绝 |
| 2：反馈兼容性复现 | 21 | 360,392 / 2,555 | TOKEN_BUDGET_EXHAUSTED；文件内容完全未变 |
| 3：加入工作笔记后重试 | 21 | 353,009 / 7,375 | 源码通过原测试 + hidden 的 61 项及兼容性 3 项；新增测试 6 项预期错误，预算耗尽 |
| 4：反馈新增测试的具体错误 | 6 | 79,440 / 1,125 | 显式恢复后完成；综合评测 **87 passed + 36 subtests**，兼容性 **3 passed** |

尝试 2 总耗时 317.15 秒，模型等待 282.82 秒；尝试 3 总耗时 531.03 秒，模型等待 502.03 秒。尝试 4 从首次领取到完成为 613.65 秒，包含部署中断、租约失效与人工排查等待；恢复后已报告模型等待 86.27 秒。它先在 API 更新期间遇到 502，未完成结果提交，之后因心跳过期标记失败；保存故障快照并确认工具操作表为空后，显式恢复同一任务和原预算，没有新建第五次 cachetools 任务。中断期间没有可用 usage，不能当作零消耗；表中只统计已报告完成调用。

尝试 3 新增测试混用了位置参数和关键字参数，却错误要求只计算一次；相同新增测试在父版本、上游参考版本也各有 6 项失败。尝试 4 只修正这一个新增测试文件，结合保持不变的既有测试，验证两种键各自的 miss/hit；源码、上游原测试和第一次新增测试逐字不变。最终固定项目命令为 **80 passed + 36 subtests**，再加冻结隐藏用例为 **87 passed + 36 subtests**。三个兼容性检查是在反馈后已知的回归检查，不能称为新的 holdout。

最终任务 `1b774bc8-2b54-4a25-8491-0a6f07f84e15` 为 awaiting_review。这一链条展示了有审查反馈的修复与恢复流程；不能据此声称首次自动修复成功，或把第三次变化归因于记忆机制带来确定提升。第三次没有受控消融，仍有预算耗尽与错误测试。

可随项目保留的 [结构化实测摘要](../benchmarks/forge-run-summary.json) 包含每次结果、输入哈希和评测输出；[最终 cachetools 补丁](../benchmarks/example-patches/cachetools-reviewed.patch) 与 [boltons 补丁](../benchmarks/example-patches/boltons-first.patch) 可供审阅。原始归档、事件与中断前快照保存在 `results/`，后者名为 `forge-cachetools-test-repair-pre-resume.*`。导出脚本 `scripts/export_forge_evidence.py` 会核对最终候选哈希与评分报告一致。

## 基准有效性与来源

详见 [冻结来源与许可](../benchmarks/BENCHMARKS.md)。两个项目分别固定父提交、修复提交、逐文件哈希、oracle 哈希和 Docker image ID。

| Case | 父版本已有测试 | 父版本 + hidden | 参考源代码 + 原测试 + hidden |
|---|---:|---:|---:|
| cachetools | 54 passed | 4 failed / 57 passed | 61 passed |
| boltons | 4 passed | 5 failed / 15 passed | 20 passed |

可见测试在有缺陷版本上本来通过，因此独立 oracle 是必要的。候选评测额外纳入模型新增测试。评测只覆盖相关模块，不声称运行整个上游仓库所有测试。容器无网络、不挂认证信息，禁用 pytest 外部插件自动发现和 conftest 加载。

## 执行恢复与控制层

`check_forge_control.py` 在真实 API/PostgreSQL/Docker 上验证：engine 分流、CAS revision 冲突、工具 call ID 去重、同 ID 不同参数 409、恢复保留工作区、旧租约 409、跨身份 404，以及 HTTP 响应丢失后重复请求复用结果。计数器保持 **1**，未发生第二次副作用。

`check_forge_recovery.py` 把同一故障注入项目自己的 AgentRuntime：工具修改完成并提交之后、观察检查点写入之前抛出连接异常；任务失败、显式 resume、新租约重新运行。调用缓存序列为 `[false, true, false]`（首次修改、恢复复用修改结果、固定测试），数据库只有 2 条工具操作，最终独立验收 exit 0。此项使用 2 个脚本决策、**0 个模型调用**，不当作模型效果。

`scripts/check_coding_flow.py` 复验版本推进、过期版本拒绝、测试副作用不污染产物、follow-up 完整 diff、SSE 回放、完成幂等、非法归档拒绝。旧 CLI 上传路径独立保留兼容。

操作日志不能提供跨 PostgreSQL/Docker 的 exactly-once。若外部副作用已开始却没有结果记录，系统拒绝自动重放；协作式取消也不能瞬时回滚已运行命令。详见 [架构与故障窗口](ARCHITECTURE.md)。

## 本轮生命周期缺陷与修复前后对照

审查发现两个此前没有充分覆盖的缺陷：工作区先标记 ready、后保存基线，中断后可能漏掉修改前证据；完成接口最终读取状态未加行锁，可能忽略并发取消或让旧租约结果覆盖新任务运行。此次修复引入冻结初始快照、独立初始化锁，以及完成前的加锁刷新与租约复核。半初始化快照明确拒绝，不根据后来变动的源码重建“修改前”状态。

同一个 `scripts/check_coding_lifecycle.py` 分别加载初始发布提交 `5a2fefb3da9ada9c6aec8669209425730498da53` 和修复后的 API 源码，在真实 PostgreSQL 的独立会话中控制并发顺序：

| 检查 | 修复前 | 修复后 |
|---|---|---|
| 基线中断后重试 | ready 已提交，基线缺失 | 保持未就绪，从冻结初始内容重试 |
| 初始化外部操作期间的心跳 | PostgreSQL 行锁等待 | 心跳可提交 |
| 历史 ready 任务缺少基线 | 返回工作区，缺失未提示 | 409，拒绝伪造基线 |
| 完成读取最终状态后，取消同时到达 | 取消已提交，最终仍为 awaiting_review 且标志矛盾 | 取消真实等待行锁；按事务先后决定终态 |
| 验证异常发生前已提交取消 | 异常分支覆盖成 failed | 保留 cancelled |

结果为修复前 **0/5**、修复后 **5/5**。可公开复核的原始报告：[修复前](evidence/coding-lifecycle-before.json)、[修复后](evidence/coding-lifecycle-after.json)，均包含 API 源码 SHA-256；`api_source_ref` 由运行时解析的提交或 WORKTREE 标识，哈希限定实际受测内容。

这些探针使用**真实数据库、真实 API 函数和可控 broker/验证替身**，通过 PostgreSQL 锁等待确认竞争顺序；不是完整 HTTP/Docker 测试，也没有杀 worker 进程，模型调用为 0。每次只清理自己新建的 UUID / owner 范围数据。单元回归另外覆盖旧租约迟到完成、取消发生在基线期间、已有修改的恢复、原 seed 消失和不完整快照拒绝。源码与测试见 [生命周期回归](../tests/test_coding_lifecycle.py)。

修复部署后，另外复跑真实 HTTP/PostgreSQL/Docker 的 [完整流程检查](evidence/coding-flow-after.json) 和 [AgentRuntime 丢应答恢复](evidence/forge-recovery-after.json)，全部通过；后者仍为 `[false, true, false]` 的工具结果复用、2 条持久操作、最终验收 exit 0。两项均为脚本决策、0 模型调用。两个 API 副本的已安装源码 SHA-256 均与修复后探针报告一致：`cae2f103eb28f8f47d6c1741c248fe6b0e7ac3956a4081d8fac35f4c9ba50d45`。

冻结快照增加了持久卷占用，尚无完整的保留期限与垃圾回收策略；最终状态一致也不代表已运行的容器会立即终止。通用任务测试防篡改、提示注入和跨机器产物恢复等剩余边界列于 [面试讨论](INTERVIEW.md)。

## 100 任务队列实测

单个 localhost 突发批次，使用独立 isolation-test 身份：101 个并发提交，100 个入队，第 101 个 HTTP429；20 个脚本执行器以每个 5 槽领取，得到 **100 个唯一租约**。之后全部取消。未启动 100 个模型进程。

| HTTP 客户端观测 | p50 | p95 | 最大 |
|---|---:|---:|---:|
| 提交 | 2,353.88 ms | 3,952.77 ms | 4,138.34 ms |
| 领取 | 2,984.49 ms | 4,438.86 ms | 4,872.36 ms |

提交批次墙钟 4.205 秒，领取批次 4.979 秒。包含客户端排队、网络与服务端处理；这是单次突发，不是持续 QPS 或生产 SLO。原始报告 `results/coding-queue.json`。实际模型并发验证仍为 2 路。

## 检索消融

固定 9 个合成源码文件、12 条查询、15 个相关路径标签；各方法使用相同输入、5 条命中和 1800 估算 token 预算，按去重文件排名计算宏平均。

| 方法 | Recall@3 | MRR@5 |
|---|---:|---:|
| naive path | 0.375 | 0.373611 |
| lexical BM25 | 1.000 | 1.000 |
| hybrid 路径/符号加权 | 1.000 | 1.000 |

没有证明 hybrid 优于 lexical。该小型合成回归集用于检索行为回归，不能代表真实大仓库或最终修复质量。复跑：`python scripts/evaluate_retrieval.py`。报告保存输入与检索源码 SHA-256、逐查询排名和相关性标签：`results/retrieval-ablation.json`。

## 自动化与界面

- 最终 Python 自动化：**256 passed、2 skipped、9 subtests passed**；本轮增加 11 项生命周期回归。跳过项为真实 provider 的 opt-in 调用、Windows 符号链接权限；实际模型任务和 Linux 沙箱另有独立证据。
- Ruff `check .` 通过；冻结的上游参考源文件保留原字节，不参与本项目格式检查。
- Studio 已部署资源 + 真实只读 API 最终验收 **24/24**，JS/console 错误 0，报告 `results/studio-deployed-final/studio-check.json`。覆盖桌面、390px 手机、真实 Forge diff/测试/检查点/工具记录；隔离 mock 另测创建 engine、resume、不确定操作禁用和请求竞态，不冒充真实服务写入测试。
- 沙箱原实机检查 14/14 通过，详见 [底层沙箱与旧 CLI 记录](CODEX_BASELINE_VALIDATION.md)。

最新双 API、数据库、网关和 broker 部署健康，Forge worker 配置 2 槽位。原始模型事件、ZIP、patch、故障注入和 UI 截图保留在本机 `results/`；首次模型结果与中断快照单独保留。

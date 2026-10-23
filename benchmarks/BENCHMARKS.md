# 两个真实历史 bug 的独立评测集

这里固定的是两个开源项目的真实父提交和公开修复，不是手写的故障示例。样本是人工挑选的两项，不代表 SWE-bench 分数或通用修复成功率；公开历史问题也不能排除模型训练数据污染。

| case | 公开来源 | Agent 可见父版本 | 参考修复 | 许可证 |
|---|---|---|---|---|
| cachetools-autospec | [issue 443](https://github.com/tkem/cachetools/issues/443) | `eba200635273fe45b67e669b84cb45f995b27c24` | `95e6addbd82afd75f81cb5650f32f6d74af2f1f9` | MIT |
| boltons-pearson | [PR 434 的后续边界修复](https://github.com/mahmoud/boltons/pull/434) | `3913220988e1e7c80620796fdcb4f74eb598d179` | `73e22c90de25aaae3e6aed57f7438ffe57349339` | BSD-3-Clause |

两条提交与父子关系由 GitHub 插件读取核实。每个目录的 `provenance.json` 保存来源、固定 SHA 和限制；`LICENSE` 保留原许可证。源归档来自 GitHub 官方 codeload，参考源文件来自固定 SHA 的 raw URL，并重算 Git blob SHA 与插件返回值比对。

## 输入与答案隔离

**只把各 case 的 `project.zip` 导入 Agent，把 `task.md` 作为任务说明。不要导入整个 benchmark 目录。**

- `project.zip`：父提交源代码、文档和原测试；不包含新增 oracle、参考代码、参考 patch 或修复 SHA。去除 `.git`、`.github`、其它隐藏目录、Agent 指令文件、密钥后缀与构建缓存，拒绝链接、路径穿越、重复文件、加密文件和超限归档。
- `oracle/test_regression.py`：独立编写的隐藏回归测试；不包含参考实现。
- `oracle/reference_files/` 与 `oracle/reference.patch`：只供评测器验证基准有效性。参考验证只替换修复涉及的源文件，保留父版本全部原测试。
- `source_manifest.json`：逐文件 SHA-256、ZIP/上游归档/任务说明/oracle 的哈希与过滤记录。重新 `prepare` 才会更新冻结值。
- `verification.json`：离线 Docker 的基线与参考验证日志，不是模型运行成绩。

候选评测前会校验全部既有 `tests/` 文件与 `conftest.py` 未被修改或删除；允许新测试文件，并把新增的 `tests/test_*.py` 纳入候选验证。隐藏测试只在模型提交候选后注入临时评测容器。若将 oracle 失败日志送回模型继续改，就不再是一次独立 holdout 评测，应另记尝试次数。

## 准备、验证与候选评分

在项目根目录执行：

```powershell
.venv/Scripts/python.exe scripts/prepare_issue_benchmarks.py prepare
.venv/Scripts/python.exe scripts/prepare_issue_benchmarks.py verify
.venv/Scripts/python.exe scripts/prepare_issue_benchmarks.py evaluate --case cachetools-autospec --candidate results/forge-cachetools-autospec.zip --report results/cachetools-hidden.json
.venv/Scripts/python.exe scripts/prepare_issue_benchmarks.py evaluate --case boltons-pearson --candidate results/forge-boltons-pearson.zip --report results/boltons-hidden.json
```

`prepare` 可联网下载固定公开源码，后续使用本地缓存。`verify` 和 `evaluate` 不安装依赖、不访问网络。默认使用已有 `agent-backend-sandbox:0.1.0` 镜像，并在开始时解析为不可变 image ID；支持 `--image` 覆盖。Windows 的 WSL 配置从本项目 `.runtime.json` 读取，或者用 `--docker-prefix` 明确传 JSON argv 数组。全程不用 shell 拼接外部参数。

评测容器采用 `--network none`、只读根文件系统、非 root 用户、丢弃 capabilities、禁提权、内存/CPU/PID/超时限制，以及临时工作目录；不挂宿主目录、Docker socket 或认证信息。原项目 pytest 插件自动发现与 conftest 加载被关闭，避免无关依赖影响这个范围明确的回归集。只执行相关上游测试文件与 oracle，不宣称运行了两项目的完整测试集。

## 已完成的基准有效性验证

2026-10-23，Python 3.12 Docker，网络关闭：

| case | 父版本原测试 | 父版本 + hidden oracle | 单个参考源文件 + 原测试 + oracle |
|---|---:|---:|---:|
| cachetools-autospec | 54 passed | 4 failed / 57 passed，均为 autospec TypeError | 61 passed |
| boltons-pearson | 4 passed | 5 failed / 15 passed，均为边界 ZeroDivisionError | 20 passed |

cachetools oracle 检查两种 autospec 创建方式、位置/关键字调用，以及真实缓存、类访问与继承。boltons oracle 检查精确矩边界、Normal/Gamma 优先级、对称族、舍入与固定样本 IV/V/VI。精确矩使用公开可写统计属性构造，未声称来自某组真实样本估计；实数矩下不可达的正 `c2` 零分母分支没有伪造数值对象来刷覆盖。

后续 Agent 运行应另外报告 provider、模型、实际步骤数、工具记录、总耗时、token 用量、预算终止原因、补丁，以及 `evaluate` 产生的 hidden 结果。无 API key 时的 Codex 单步桥接只证明该入口的真实运行，不能当作原生 HTTP 模型调用；ScriptedProvider 不计入模型效果。

## 首次真实 Forge 运行

2026-10-23，自有 Forge 决策/工具循环使用 `codex_cli_bridge` 完成两项候选，均保留全部既有测试。模型只接收 `project.zip` 和 `task.md`；运行结束后第一次执行 hidden oracle 即通过。随后额外执行模型新增测试也全部通过，没有向模型回传 oracle 后继续修复。

| case | 单步模型调用 | 输入 / 输出 tokens | 原测试 + 新测试 + hidden |
|---|---:|---:|---:|
| cachetools-autospec | 18 | 310472 / 3532 | 63 passed，另 36 subtests passed |
| boltons-pearson | 14 | 226432 / 2383 | 44 passed |

原始运行记录见 `../results/forge-issues.json`，独立结果见 `../results/forge-cachetools-autospec-oracle.json` 与 `../results/forge-boltons-pearson-oracle.json`。缓存命中、价格与账单成本没有根据 token 数自行推算。两项通过只说明这个小型冻结集合上的表现。

后续额外审查确实发现 cachetools 首次补丁的兼容性缺陷，已拒绝该候选。三次 follow-up 中前两次预算耗尽，最后一次任务经明确反馈和部署中断后的显式恢复，取得原测试 + 全部新增测试 + frozen hidden **87 passed / 36 subtests passed**，另有已知兼容性回归 **3 passed**。这不是首次成功，也不是新的盲测。完整失败链见 [验证记录](../docs/VALIDATION.md)、[兼容性审查](cachetools-autospec/post_hoc_review/REVIEW.md) 和 [结构化实测摘要](forge-run-summary.json)；最终候选与 boltons 候选保持待审核，没有自动合并或发布。

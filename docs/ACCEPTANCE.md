# 版本化独立验收

旧流程只冻结了测试命令，测试文件和 pytest 配置仍在候选仓库中。Agent 删除测试、改变收集规则，甚至把命令改成 `true`，都可能得到无意义的 exit 0。本轮把“提交候选”和“判定候选通过”分开。

## 配置与使用

在 Studio 选择项目，点击“验收策略”，上传 `test_*.py` 或 `*_test.py` 文件，填写受保护文件的精确相对路径、源码导入目录和最少测试数，再创建任务。保护路径的当前内容会被记录为基线摘要。样例项目自动配置五项独立测试并保护原测试文件。

API 支持更复杂的测试包（包括辅助 Python 文件）。以下请求发送至 `POST /api/coding/projects/{project_id}/acceptance-suites`，使用本机后端 bearer 密钥：

```json
{
  "name": "answer-contract-v1",
  "files": {
    "test_contract.py": "from app import answer\n\ndef test_answer():\n    assert answer == 42\n"
  },
  "test_paths": ["test_contract.py"],
  "protected_paths": ["tests/test_app.py"],
  "python_paths": ["."],
  "minimum_tests": 1,
  "timeout_seconds": 120
}
```

`protected_paths` 必须指向项目已有文件；空列表允许显式不保护公开文件。suite 源码不进入候选工作区。GET 同一路径只返回版本、名称、ID、digest 和时间，任务日志只返回计数、违规保护路径与候选摘要，不返回隐藏测试源码、测试名称、assert 内容或 traceback。

策略由可信的项目操作者编写，是验收标准而非自动生成的正确性证明。身份密钥的持有人可发布新策略；模型运行器不持有这把密钥。系统尚未区分独立的 reviewer/worker 角色。

## 固定版本与接受条件

1. 发布策略时锁住 Project，保存不可变 suite 版本并原子更新 active ID。suite digest 包含测试源码、测试入口、保护清单、导入路径、最少数量和超时设置。
2. 新 Task 固定当时的 suite ID/digest。修改前基线、运行时 `run_tests`、finish 后服务端验证均使用这个固定版本，任务自填的命令不能跳过它。开发测试命令仍可运行并失败阻断，但不能替代独立验收。
3. 验证先复制候选，按路径、字节长度和内容 SHA-256 生成稳定摘要；ZIP 时间戳不影响结果。受保护文件变化或缺失立即失败。
4. broker 在临时容器中把测试包只读挂载到 `/acceptance`，固定镜像内 pytest 及参数。通过必须同时满足子进程退出 0、存在有效结构化报告、收集数达到下限、全部测试通过、没有 failed/errors/skipped/xfail/xpass/deselected、没有超时。
5. 人工接受时锁住 Project/Task，检查项目版本、当前 suite 与任务 pin、验证结果和现有候选摘要，再推进项目版本。验证后改源码、旧策略、缺失证据都会拒绝。

代码入口：[策略与判定](../src/agent_backend/coding/verification.py)、[任务生命周期](../src/agent_backend/coding/api.py)、[固定运行器](../src/agent_backend/sandbox/acceptance_runner.py)。本轮不迁移或伪造历史测试证据。**历史无 suite 的任务仍能查看、下载和 follow-up，但不能新接受；先配置策略，再创建后续任务。** 已接受历史任务的幂等读取保持原状态。

## 实际防住什么

固定运行器以 `python -I` 启动，先加载镜像中的 pytest，再暴露候选导入路径。禁用自动插件、conftest、候选配置与 `PYTEST_ADDOPTS`；显式指定测试文件和 rootdir；只看结构化计数和进程状态，不解析候选 stdout 的“passed”。测试容器使用独立复制卷，测试修改不会污染待审产物。

这能拦截仓库级配置绕过、删除/篡改保护文件、空测试与跳过测试、假日志、旧策略接受和验收后换内容。实机脚本分别为 [API 验收](../scripts/check_acceptance.py)、[Docker 绕过检查](../scripts/check_acceptance_sandbox.py)、[真实历史产物回归](../scripts/check_acceptance_oss.py)。最后一项不调用新模型，使用既有 cachetools 坏/好候选，包含已向模型披露的复审测试，不能宣传成新的盲测成功率。

## 限制和取舍

- 目前只支持 Python/pytest。源码依赖须预装；执行无网络，单次最多 120 秒。测试包最多 64 文件、总计 1 MiB、单文件 256 KiB；最少测试数范围 1–10000。UI 简化为最多 50 个平铺测试文件；辅助模块或子目录请用 API。
- 不加载 conftest 或任意插件，依赖它们的项目须改为独立测试包或增加受控运行器。候选顶层模块/包与标准库、已加载模块或镜像已有包冲突时拒绝验证，避免实际测到镜像包却给候选记分。`pytest.py` 等干扰文件被忽略，不用于替换运行器。
- 保护清单是显式文件快照；并不自动保护所有测试，也不禁止添加文件。需要新的预期或修改保护文件时，应由操作者发布新策略，再创建任务。
- 候选和 pytest 在同一个 Python 进程中。恶意候选仍可能读取测试、操纵解释器或通过其它返回通道泄漏内容。只读挂载和隐藏日志不是对恶意 Python 的隔离证明。下一步应将评分权与被测进程分离，并建立真实模型提示注入/攻击评测集。
- 相同身份的可信操作者可更换标准；目前没有 reviewer RBAC、策略审批流程或签名。复制候选与读归档有开销；进程崩溃后可能遗留临时卷，尚无自动 GC。

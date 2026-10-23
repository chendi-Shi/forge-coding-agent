# 首轮候选后的兼容性审查

2026-10-23，首轮候选 `1add1c30b2df863e627768a280a0f3575f38aeadc3151d5e232a0c071f6cb933` 通过原有 hidden 集合后，额外代码审查发现它新增的 `__class__` 属性将 descriptor 伪装成 `types.FunctionType`。`inspect` / `unittest.mock` 因而进入函数分支，读取 descriptor 并不存在的 `__code__`。

这里是 **post-hoc review**，不修改 `oracle/test_regression.py`、冻结 manifest 或首轮 hidden 得分。可以将复现交给后续 Agent；此后这三个检查就是已知回归检查，不能再宣称为未见过的 holdout。原问题的修复仍需另行通过冻结 hidden 评测。

## 可以交给后续 Agent 的最小复现

```python
import inspect
from unittest import mock
from cachetools import cachedmethod

class Service:
    def __init__(self):
        self.cache = {}

    @cachedmethod(lambda self: self.cache)
    def cached(self, key):
        return key

raw = inspect.getattr_static(Service, "cached")
mock.create_autospec(raw)
assert inspect.iscoroutinefunction(raw) is False
assert inspect.isgeneratorfunction(raw) is False
```

三个调用应分别检查：首轮候选每一项均抛出缺少 `__code__` 的 `AttributeError`；一个异常不应阻止其它检查运行。`test_descriptor_introspection.py` 为此分成三个独立 pytest 用例。

修复必须同时保留原 issue 的类 / 实例 autospec 正确传参、真实缓存与类访问行为。新增回归测试，不删除或改写既有测试。不需要向 Agent 提供参考实现。

## 对候选 ZIP 复跑

在项目根目录：

```powershell
.venv/Scripts/python.exe scripts/review_cachetools_candidate.py --candidate results/forge-cachetools-autospec.zip --report results/forge-cachetools-autospec-review-first.json
```

脚本在三个独立的无网络 Docker 容器中执行相同复审用例：冻结父版本、仅替换已核验上游源文件的参考版本、候选。复用基准脚本的归档限制、镜像 ID 固定、非 root、只读根文件系统、资源限制和不挂载宿主文件的执行器。报告保存三个对照的完整输出及输入哈希。只有两个对照和候选全部通过，脚本才返回 0。原始测试仍需单独运行 `prepare_issue_benchmarks.py evaluate`。

后续候选使用新的 ZIP 与新的 `--report` 路径，保留首轮审查失败证据。脚本不写入或覆盖 frozen oracle、原始候选、hidden 报告或参考源文件。

## 首轮复审的实测对照

2026-10-23，Python 3.12，无网络 Docker，固定镜像 ID `sha256:cd455c26af83f951889616a79a0e742a5b70af0afa145f858be7163cb5198ec7`：

| 输入 | 三项 post-hoc 检查 |
|---|---|
| 父版本 `eba200635273fe45b67e669b84cb45f995b27c24` | 3 passed |
| 仅替换上游修复源文件，Git blob `596483aaf6753e5e3178ce8340f0f60db03260bc` | 3 passed |
| 首轮模型候选 | 3 failed，均为 `AttributeError` |

完整日志见 `results/forge-cachetools-autospec-review-first.json`。同一首轮候选原先取得的 `63 passed / 36 subtests passed` hidden / public 结果仍保留在原报告中；本次新增审查显示该有限测试集合遗漏了兼容性缺陷。

## 后续尝试记录

第一次 follow-up 的任务 ID 为 `3e90ac9a-f3b8-4ed0-a0be-f50e52dea64e`。模型收到上述三个明确复现，但在 21 次单步调用后以 `TOKEN_BUDGET_EXHAUSTED` 结束；记录的输入 / 输出 token 为 `360392 / 2555`。逐文件比较确认 ZIP 内全部路径和文件内容与首轮候选相同（ZIP 容器字节不同），没有形成兼容性修复，不能计作通过。任务、事件和归档保存在 `results/forge-cachetools-followup.*`。

此后运行时增加工作笔记再尝试，属于又一次模型尝试。由于样本很少且未做受控对照，无论结果如何，都不能仅据这次重试归因于记忆机制或声称证明了效果提升。

这次 memory-retry 的任务 ID 为 `c268336a-4581-4ed1-8e2a-37982797c072`，同样以 `TOKEN_BUDGET_EXHAUSTED` 结束：21 次调用，输入 / 输出 token `353009 / 7375`。候选去掉类型伪装，改用真实绑定方法。单独执行冻结原测试 + hidden 得到 **61 passed**，三项 post-hoc review 全部通过；但把它新增的测试计入后为 **6 failed / 81 passed / 36 subtests passed**，不能计作完成。

六项失败均来自它新增的 `tests/test_cachedmethod_descriptor_inspection.py::test_inspection_preserves_cache_behavior` 的六组参数。该测试混用位置参数与关键字参数，错误地断言方法只执行一次。默认缓存键区分这两种调用，因此实际执行两次。对原始父版本和已核验的上游参考版本分别注入同一个新增测试文件，二者都得到 **6 failed / 18 passed**，同样在该断言失败。既有上游测试及首轮候选新增测试在 memory-retry 中均未修改。

独立结果分别保存在 `results/forge-cachetools-memory-retry-oracle.json`、`results/forge-cachetools-memory-retry-review.json`；冻结测试与错误新增断言的父版本 / 参考对照见 `results/forge-cachetools-memory-retry-frozen-and-test-review.json`。任何后续聚焦修复都必须另记一次尝试，不覆盖这些结果。

第四个任务 `1b774bc8-2b54-4a25-8491-0a6f07f84e15` 收到错误测试的明确定位，属于非盲测的聚焦修复。第一次收集到的终态为 `failed`，原因为 `Executor heartbeat expired`。事件只记录了基线测试、step 0 的上下文构建和 executor lost，没有持久化的模型决策；`usage` 为空，不能当作实际零 token 使用。归档与 memory-retry 逐文件相同，未产生修复。

对此归档重跑综合回归仍为 **6 failed / 81 passed / 36 subtests passed**，post-hoc 三项则全部通过。源码、全部上游既有测试及首轮新增测试没有变化。在显式恢复同一任务前，全部归档和报告保存为 `results/forge-cachetools-test-repair-pre-resume*`，包括 `-oracle.json`、`-review.json` 与 `-change-review.json`。这一基础设施故障必须保留，不能将未改变的产物计作完成。

确认没有 in-flight tool operation 后，对第四个任务调用显式 `/resume`，进入恢复轮次 r1，没有新建第五个任务。恢复前缺失的模型 usage 仍然未知；恢复后的可观测调用只能按日志中的已记录值报告。

恢复后的第四任务进入 `awaiting_review`：记录了 6 次完成的模型决策，输入 / 输出 token `79440 / 1125`，模型调用累计 `86.265` 秒。新候选 SHA-256 为 `0f499d927134be5d30a6dd3294396cb79816761be8906af4948ee0ac79a9b810`。后端独立验收得到 **80 passed / 36 subtests passed**；本评测器加入冻结 hidden 后为 **87 passed / 36 subtests passed**。三项 post-hoc review 全部通过，父版本和参考版本对照也各 3 passed。

与 memory-retry 逐文件比较，只改了 `tests/test_cachedmethod_descriptor_inspection.py`；库源码、上游既有测试和首轮新增测试均未变。修改增加了分阶段断言：位置参数首次调用后 `calls == 1`；首次关键字调用后 `calls == 2`；再次关键字调用后仍为 2。它保留了缓存命中验证，并未仅删掉或放宽断言。该函数本身没有重复位置参数调用；位置参数重复命中由未变的 `test_cachedmethod_autospec.py::test_real_instance_and_class_access` 覆盖，先类调用、再实例调用同一位置参数，断言只执行一次，覆盖 info / lock / condition / 继承组合。两种 key 各自命中的检查均在最终套件实际执行。

恢复后的报告保存在 `results/forge-cachetools-test-repair-oracle.json`、`results/forge-cachetools-test-repair-review.json` 与 `results/forge-cachetools-test-repair-change-review.json`。`pre-resume*` 仍保存原心跳故障。最终通过的是经历明确审查反馈和多次修复后的候选，不是一次盲测成功。

| 任务记录 | 已记录完成决策 | 结果 |
|---|---:|---|
| 首轮 | 18 | 冻结集合通过，额外兼容性审查失败 |
| 第一次 follow-up | 21 | token 预算耗尽，无文件修改 |
| memory-retry | 21 | 兼容性修复通过；错误新增测试失败；token 预算耗尽 |
| test-repair，恢复前 | 未知 | 心跳超时，没有已持久化模型决策，产物未变 |
| 同一 test-repair，显式恢复 r1 | 6 | 87 tests / 36 subtests 与 3 项复审通过，待人工接受 |

以上是四个任务、其中第四个有一次显式恢复。日志未覆盖的中断期间模型消耗不能补成零。最终结果不能用来证明工作笔记提升了成功率，也不能掩盖首轮兼容性缺陷、两次预算终止或执行器中断。

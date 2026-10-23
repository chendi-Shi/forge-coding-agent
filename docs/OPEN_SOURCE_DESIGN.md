# 开源对照与本项目改造

2026-10-23 通过 GitHub 读取以下固定提交的实现和许可，再据此修改 Forge。它们是设计参考；本轮没有复制这些项目的实现，也没有把接入框架当成自己的 Agent 能力。

| 项目 / 固定提交（MIT） | 阅读的实现 | 本项目采用的原则 |
|---|---|---|
| [SWE-bench](https://github.com/SWE-bench/SWE-bench/tree/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e) | [run_evaluation.py](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/run_evaluation.py#L288-L368) 把评测脚本单独放进候选环境；[grading.py](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/grading.py#L130-L177) 检查测试证据和退出状态 | 独立测试包、固定运行器、实际收集数量与结果共同判断通过。空测试、跳过、超时、仅 stdout 声称通过都不能接受。 |
| [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent/tree/04d809ceab9df28f9adaed044884180159172930) | [swebench.py](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/run/benchmarks/swebench.py#L133-L189) 保存候选 patch 与轨迹，交由外部评测 | 模型 finish 是提出候选；后端验收、人工接受是独立阶段。保留真实失败链，不把口头完成等同于解决。 |
| [OpenHands software-agent-sdk](https://github.com/OpenHands/software-agent-sdk/tree/60192ed54db73ac658ac1a07462916126c32d3a0) | [run-eval.yml](https://github.com/OpenHands/software-agent-sdk/blob/60192ed54db73ac658ac1a07462916126c32d3a0/.github/workflows/run-eval.yml#L186-L212) 固定 SDK_SHA，推理和评测分开；[event_store.py](https://github.com/OpenHands/software-agent-sdk/blob/60192ed54db73ac658ac1a07462916126c32d3a0/openhands-sdk/openhands/sdk/conversation/event_store.py) 在 FileStore 锁中先保存 payload，再发布索引；[files.py](https://github.com/OpenHands/software-agent-sdk/blob/60192ed54db73ac658ac1a07462916126c32d3a0/openhands-sdk/openhands/sdk/utils/files.py) 使用临时文件与原子替换 | 验收策略与候选都按摘要绑定；原文先持久化，检查点后引用。其本地文件锁不能推导为 NFS 分布式锁或 PostgreSQL/Docker exactly-once。 |
| [LangGraph](https://github.com/langchain-ai/langgraph/tree/9d68e5b3eb1a63027b05e9f0152149ea0284dcdb) | [Postgres schema](https://github.com/langchain-ai/langgraph/blob/9d68e5b3eb1a63027b05e9f0152149ea0284dcdb/libs/checkpoint-postgres/langgraph/checkpoint/postgres/base.py#L50) 分离 JSONB checkpoint 和版本化 BYTEA blob；[异步写入](https://github.com/langchain-ai/langgraph/blob/9d68e5b3eb1a63027b05e9f0152149ea0284dcdb/libs/checkpoint-postgres/langgraph/checkpoint/postgres/aio.py#L273) 只写变更版本 | Forge 自有循环把长观察外置到 PostgreSQL，检查点只留摘要和引用。不能把 LangGraph namespace 当成租户授权，也不能声称其自动外置所有大字符串。 |

## 从参考到具体实现

**验收链路**见 [ACCEPTANCE.md](ACCEPTANCE.md)：suite 作为不可变版本保存在服务端；创建任务时固定 suite ID/digest；保护文件按基线哈希检查；独立容器只读挂载测试；接受时重算候选内容哈希，拒绝旧策略与过期结果。Agent 可见计数反馈，拿不到测试源码。工作台可以上传测试包和查看策略绑定。

**恢复链路**见 [ARTIFACTS.md](ARTIFACTS.md)：长观察以任务内不可变内容地址存储；当前租约和身份约束读写；写检查点前验证引用已经存在且元数据相符；新 worker 从 API 分页取回原文，不要求旧 worker 目录仍在。

## 为什么没有继续堆框架

这里保留已有的单步 provider、自有决策循环、工具操作表和 CAS 检查点，便于解释故障窗口。新增编排框架并不会自动解决候选通过自己的测试、悬空 artifact 引用或旧租约迟到写入。设计的验收标准是这些故障是否能复现并被拦截，证据见 [VALIDATION.md](VALIDATION.md)。

当前独立验收是 Python/pytest 的受限实现，不是 SWE-bench 全集复现，也不是 OpenHands 完整替代品。候选 Python 与 pytest 仍共享解释器，不承诺抵御恶意代码对解释器的主动篡改；依赖预装、无网络、多租户强隔离和持久卷回收等边界仍需明确说明。

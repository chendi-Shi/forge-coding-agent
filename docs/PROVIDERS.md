# 单步模型适配器

`src/agent_backend/forge/providers.py` 只负责“根据已有观察选择下一步”。读取仓库、修改文件、运行测试、检索、重试、预算和完成验收都由 Forge 自有运行时负责。适配器不会把一个完整开发任务交给另一个 Agent 自动完成。

## 统一接口

```python
result = await provider.decide(messages, decision_schema, model=None)
# {
#   "decision": {
#       "action": "tool",       # 或 finish
#       "tool": "read_file",    # finish 时为空字符串
#       "arguments": {"path": "main.py"},
#       "summary": "读取实现以定位错误"
#   },
#   "usage": {"input_tokens": 123, "output_tokens": 45},
#   "latency_ms": 720,
#   "provider": "codex_cli_bridge"
# }
```

`messages` 是 `[{"role": "system|developer|user|assistant", "content": "纯文本"}]`。工具观察由运行时作为文本消息传入，不是供应商原生 `tool` 消息。每次调用只返回一个决策；`finish` 必须使用空 `tool` 和空 `arguments`。供应商的 token 字段原样保留数值字段，不把缺失计数伪造为零。

传输层使用固定的严格 schema：`action`、`tool`、`arguments_json`、`summary`。其中 `arguments_json` 是参数对象的 JSON 字符串。这样可以兼容 Codex 强制严格 schema 的输出要求，同时允许运行时定义不同工具的参数。适配器解析后还原 `arguments`，再用 `Draft202012Validator` 校验运行时传入的 schema；运行时仍需对具体工具名及参数做第二次校验。拒绝重复 JSON 键、非有限数值、远程 schema 引用和多余顶层字段。

## 正式 HTTP 入口：OpenAICompatibleProvider

这是直接调用模型的单步 HTTP 适配器。请求发往配置的 `/chat/completions`，不声明任何执行工具，不运行供应商返回的 `tool_calls`。收到原生工具调用会报 `tool_activity_rejected`。

```python
import os
from agent_backend.forge.providers import OpenAICompatibleProvider

provider = OpenAICompatibleProvider(
    base_url=os.environ["FORGE_MODEL_BASE_URL"],  # API 根路径，结尾例如 /v1 或 /api/v3
    api_key=os.environ.get("FORGE_MODEL_API_KEY"),
    model=os.environ.get("FORGE_MODEL"),
    timeout_seconds=90,
    max_output_tokens=4096,
)
```

没有 API key 会立即产生 `missing_api_key`，不会读取 Codex 登录凭据来冒充模型 API key。未指定模型会产生 `missing_model`，不替用户猜模型名称。ARK 等兼容服务还需要配置其实际 API 根路径与模型/接入点 ID；是否支持严格 `json_schema` 取决于具体服务与模型，并未在本机实测。对只支持 JSON mode 的服务可显式设置 `response_format="json_object"`；对使用旧 token 参数的服务可设置 `token_parameter="max_tokens"`。默认是 `json_schema` 与 `max_completion_tokens`。不自动降级输出协议。

只有 HTTPS 或 localhost HTTP 被接受；禁止 URL 内嵌凭据、query 和 fragment。HTTP 不继承环境代理、不跟随重定向、不隐式重试。端到端超时默认 90 秒，最终 JSON 上限 64 KiB，HTTP 响应上限 1 MiB，请求上下文上限 256 KiB。总时间和响应字节上限也约束慢速或异常响应。

## 当前无 key 的兼容入口：CodexDecisionProvider

这个入口明确标记为 `codex_cli_bridge`，不是原生纯模型 API。它复用正常的 Codex CLI 登录，不读取、复制或导出认证 token。每一步启动新的 ephemeral 会话，stdin 只传该步上下文，在独立空目录中执行；不传入任务仓库，不 resume 旧会话。

已核实本机 CLI 0.154.0 使用这些约束：

- `-a never exec --json --ephemeral --ignore-user-config --strict-config --sandbox read-only --skip-git-repo-check --output-schema ...`。
- `web_search="disabled"`、`mcp_servers={}`、`project_doc_max_bytes=0`，Windows 明确使用正常 `windows.sandbox="unelevated"`。
- 关闭 `shell_tool`、`shell_snapshot`、`apps`、`plugins`、`hooks`、`multi_agent`、浏览器/电脑/图片工具、`sleep_tool`、`goals`、`code_mode`、`workspace_dependencies` 与 `skill_search`。
- 不使用任何 sandbox bypass 参数；不复制现有 `CODEX_HOME`。子进程环境白名单不传后端 bearer、模型 API key、数据库凭据。

目前没有核实到能保证关闭 CLI 全部内置工具的总开关；`apply_patch_freeform` 在本机已被移除，不能拿它当有效禁用开关。关闭 `code_mode_host` 会导致本机 CLI 初始化错误，因此保留该 host 默认值。**这些开关与只读沙箱并不构成“绝不可能读取本机文件或执行内置工具”的证明。** 严格事件校验只能拒绝已观察到的工具活动，不能撤销工具事件发出前可能发生的副作用。独立空目录也不等同于文件系统隔离。需要纯模型协议时应使用 HTTP 适配器。

桥接层持续读取 JSONL，只接受会话/轮次开始、文本消息/推理项和轮次结束。任何命令、文件变更、MCP、搜索、委派、计划列表或未知事件都立即失败并终止整个子进程树；一轮必须恰有一个完整 assistant 消息。Windows 使用启动门控与 Kill-on-close Job Object，POSIX 使用独立进程组。超时、取消、非法输出和工具事件都执行清理。原始 stderr 丢弃，临时 schema 目录结束后删除；CLI 自身正常认证和系统日志仍受 CLI 的行为约束。

```python
from agent_backend.forge.providers import CodexDecisionProvider

provider = CodexDecisionProvider(timeout_seconds=90)
result = await provider.decide(messages, decision_schema)
# model=None 不传 --model，使用正常 CLI 默认模型；调用方可明确覆盖。
```

## 测试替身与错误分类

`ScriptedProvider([decision1, decision2, ...])` 每次消费一个预设决策，输出 `provider="scripted_test_only"`。它用于可复现的运行时状态、工具权限、恢复和预算测试，**不代表真实模型理解或代码修复能力**。脚本耗尽报 `script_exhausted`。

`ProviderError` 只含稳定 `code`、`retryable` 和可选 `status_code`；异常文本不带远端 body、prompt、stderr 或凭据。主要分类：

| code | 含义 |
|---|---|
| `missing_api_key` / `missing_model` / `invalid_configuration` / `cli_not_found` | 配置未就绪 |
| `invalid_request` / `request_too_large` / `invalid_schema` / `external_schema_reference` | 输入或 schema 无效 |
| `authentication` / `http_rejected` | 上游拒绝请求，不自动重试 |
| `rate_limit` / `upstream_unavailable` / `transport` / `timeout` | 可由运行时按预算重试 |
| `invalid_response` / `invalid_decision` / `schema_validation` / `invalid_transcript` | 输出未通过协议校验 |
| `tool_activity_rejected` / `refused` / `output_limit` | 工具活动、拒答或输出超过限制 |
| `cli_failed` / `cli_transport` / `process_isolation` | CLI 或进程管理失败 |

不记录原始内容意味着 `cli_failed` 只能确定为 CLI 失败，无法仅凭公共异常区分登录过期、账户额度和不支持的模型等更细原因。需要诊断时在正常 CLI 中检查登录与可用模型；适配器不把含私密内容的 CLI 原文写入任务事件。

## 已验证范围（2026-10-23）

离线测试 33 项通过；HTTP 全部使用 `httpx.MockTransport`，未调用付费 API，未宣称 HTTP 真实效果。覆盖单步 JSON 解码、schema 校验、凭据不进入请求上下文、无重定向/重试、错误分类、字节限额、工具事件立即拒绝，以及真实子进程树超时清理。

本机正常登录的 Codex 0.154.0 完成了真实探针：9.683 秒，事件只有 `thread.started → turn.started → item.completed(agent_message) → turn.completed`，结果为 `finish / decision-probe-ok`，input 8797 tokens、output 29 tokens，stderr 0 字节。随后正式 `CodexDecisionProvider` 的 opt-in 集成测试通过（9.63 秒）。这证明这两次请求完成且未出现可观察工具活动，不是模型修复项目的效果评测，也不是所有未来请求都无工具的保证。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_forge_providers.py -q
# 明确授权后才运行下面的真实模型测试；会消耗当前 Codex 账户用量。
$env:RUN_CODEX_PREFLIGHT = "1"
.venv/Scripts/python.exe -m pytest tests/test_forge_providers.py -k real_codex -q
Remove-Item Env:RUN_CODEX_PREFLIGHT
```

官方依据：[CLI 参数](https://learn.chatgpt.com/docs/developer-commands?surface=cli)、[Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)、[Chat Completions 参数](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)。本机 `codex exec --help`、`codex features list` 与真实 JSONL 探针补充了文档未确认的行为；未知事件按失败处理，升级 CLI 后应重新执行 opt-in 检查。

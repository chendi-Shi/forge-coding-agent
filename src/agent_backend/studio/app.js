/* Forge: owner-scoped coding tasks. All untrusted content is rendered as text. */
"use strict";
const $ = (id) => document.getElementById(id);
const state = {token: sessionStorage.getItem("forge-token") || "", projects: [], tasks: [], project: "", filter: "all", detail: null, events: [], operations: [], operationsError: "", cursor: 0, tab: "activity", detailRenderKey: "", detailRequest: 0, refreshRequest: 0, parent: null, busy: false};
const active = new Set(["queued", "running", "verifying"]);
const labels = {queued: "排队中", running: "执行中", verifying: "独立验收中", awaiting_review: "待审核", accepted: "已接受", failed: "失败", cancelled: "已取消", rejected: "已拒绝"};
function node(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $("toast").hidden = true, 5000); }
async function api(path, options = {}) {
  const response = await fetch(`/api/coding${path}`, {...options, headers: {Authorization: `Bearer ${state.token}`, ...(options.body ? {"Content-Type": "application/json"} : {}), ...options.headers}});
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try { const body = await response.json(); message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body); } catch (_) {}
    if (response.status === 401) logout();
    throw new Error(message);
  }
  return options.raw ? response : response.json();
}
const post = (path, body = {}) => api(path, {method: "POST", body: JSON.stringify(body)});
function disableByPolicy(button, disabled) { button.dataset.policyDisabled = String(Boolean(disabled)); button.disabled = Boolean(disabled) || button.dataset.pending === "true"; }
async function action(button, fn) { button.dataset.pending = "true"; button.disabled = true; try { await fn(); } catch (e) { toast(e.message); } finally { delete button.dataset.pending; button.disabled = button.dataset.policyDisabled === "true"; } }
function date(value) { return value ? new Date(value).toLocaleString("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"}) : ""; }
function statusBadge(task) { return node("span", `status ${task.status}`, task.cancel_requested && active.has(task.status) ? "取消中" : labels[task.status] || task.status); }
const engineOf = task => task.engine || "codex";
const engineLabel = task => engineOf(task) === "forge" ? "Forge 自有运行时" : "Codex 基线";
function engineBadge(task) { return node("span", `engine-badge ${engineOf(task)}`, engineOf(task) === "forge" ? "FORGE" : "CODEX"); }
function selectEngine(engine) { $("task-engine").value = engine; $("engine-hint").textContent = engine === "forge" ? "Forge 管理检索、工具调用、上下文预算与恢复；模型每次只选择一个行动。" : "Codex 基线执行器在独立项目副本中工作；保留原有流程供对照，最终仍运行独立验收。"; }
function logout() { state.token = ""; state.detail = null; state.detailRequest++; state.refreshRequest++; sessionStorage.removeItem("forge-token"); $("app").hidden = true; $("login").hidden = false; $("detail").hidden = true; }
async function login() { await refresh(); sessionStorage.setItem("forge-token", state.token); $("login").hidden = true; $("app").hidden = false; $("api-key").value = ""; }
$("login-form").onsubmit = async (event) => { event.preventDefault(); state.token = $("api-key").value.trim(); $("login-error").textContent = ""; const button = event.submitter; button.disabled = true; try { await login(); } catch (e) { $("login-error").textContent = e.message; } finally { button.disabled = false; } };
$("logout").onclick = logout;
$("mobile-logout").onclick = logout;
function clearFollowup() { state.parent = null; $("followup-label").hidden = true; $("composer-title").textContent = "安排一个开发任务"; }
function selectProject(id) { state.project = id; $("project-select").value = id; clearFollowup(); const project = state.projects.find(p => p.id === id); $("test-command").value = project?.test_command || ""; renderProjects(); renderTasks(); }
function renderProjects() {
  $("projects").replaceChildren();
  for (const project of state.projects) {
    const button = node("button", `project${state.project === project.id ? " active" : ""}`);
    button.append(node("span", "", "◇"), node("span", "", project.name), node("small", "", `v${project.version}`));
    button.onclick = () => selectProject(project.id); $("projects").append(button);
  }
  const previous = $("project-select").value;
  $("project-select").replaceChildren(node("option", "", "选择项目")); $("project-select").firstChild.value = "";
  for (const project of state.projects) { const option = node("option", "", project.name); option.value = project.id; $("project-select").append(option); }
  $("project-select").value = previous || state.project;
  $("project-title").textContent = state.projects.find(p => p.id === state.project)?.name || "全部项目";
  $("all-tasks").classList.toggle("active", !state.project);
}
function renderTasks() {
  const tasks = state.tasks.filter(t => (!state.project || t.project_id === state.project) && (state.filter === "all" || (state.filter === "active" ? active.has(t.status) : t.status === state.filter)));
  $("tasks").replaceChildren();
  for (const task of tasks) {
    const row = node("div", "task-row"); row.tabIndex = 0; row.setAttribute("role", "button");
    const info = node("div"); info.append(node("div", "task-title", task.prompt));
    const sub = node("div", "task-sub"); sub.append(node("span", "", task.project_name), engineBadge(task), node("span", "", `v${task.project_version}`), node("span", "", date(task.created_at)));
    if (task.verification) sub.append(node("span", "", task.verification.exit_code === 0 ? "✓ 验收通过" : "✕ 验收未通过"));
    info.append(sub); const right = node("div", "task-right"); right.append(statusBadge(task), node("span", "", "›"));
    row.append(node("div", "task-icon", task.status === "accepted" ? "✓" : "⌘"), info, right);
    row.onclick = () => openDetail(task.id).catch(e => toast(e.message)); row.onkeydown = e => { if (e.key === "Enter") row.click(); }; $("tasks").append(row);
  }
  $("empty").hidden = tasks.length > 0; $("list-summary").textContent = `${tasks.length} 个任务${state.tasks.length >= 200 ? " · 最近 200 项内筛选" : ""}`; $("task-count").textContent = state.tasks.length;
  $("empty").querySelector("h3").textContent = state.tasks.length >= 200 ? "最近 200 个任务中没有匹配项" : state.tasks.length ? "没有符合筛选条件的任务" : "让第一个任务开始工作";
}
async function refresh() {
  const requestId = ++state.refreshRequest;
  const [status, projects, tasks] = await Promise.all([api("/status"), api("/projects"), api("/tasks")]);
  if (requestId !== state.refreshRequest) return;
  state.projects = projects.projects; state.tasks = tasks.tasks;
  const executors = status.executors || [], forgeSlots = executors.filter(e => e.engine === "forge").reduce((n, e) => n + e.capacity, 0), codexSlots = executors.filter(e => (e.engine || "codex") === "codex").reduce((n, e) => n + e.capacity, 0);
  $("engine-dot").classList.toggle("green", status.executor_online); $("engine-name").textContent = status.executor_online ? [forgeSlots ? `Forge ${forgeSlots} 槽位` : "", codexSlots ? `Codex ${codexSlots} 槽位` : ""].filter(Boolean).join(" · ") || "执行器在线" : "执行器离线 · 任务可排队";
  $("capacity").textContent = `最多 ${status.capacity.queue_limit} 个未完成任务 · ${status.capacity.sandbox_commands} 个容器执行槽位`;
  $("capacity").title = status.execution_note || "Forge 工具与独立验收在隔离容器中执行。";
  $("metric-running").textContent = (status.counts.running || 0) + (status.counts.verifying || 0);
  $("metric-review").textContent = status.counts.awaiting_review || 0; $("metric-accepted").textContent = status.counts.accepted || 0;
  renderProjects(); renderTasks(); if (state.detail) await updateDetail();
}
$("refresh").onclick = () => action($("refresh"), refresh);
$("all-tasks").onclick = () => selectProject("");
$("project-select").onchange = () => selectProject($("project-select").value);
$("settings-toggle").onclick = () => $("task-settings").hidden = !$("task-settings").hidden;
$("task-engine").onchange = () => selectEngine($("task-engine").value);
document.querySelectorAll("[data-filter]").forEach(button => button.onclick = () => { state.filter = button.dataset.filter; document.querySelectorAll("[data-filter]").forEach(b => b.classList.toggle("active", b === button)); renderTasks(); });
$("submit-task").onclick = () => action($("submit-task"), async () => {
  const project = $("project-select").value, prompt = $("prompt").value.trim();
  if (!project || prompt.length < 5) throw new Error("请选择项目，并填写至少 5 个字的开发目标。");
  const task = await post("/tasks", {project_id: project, prompt, engine: $("task-engine").value, test_command: $("test-command").value.trim(), model: $("model").value.trim() || null, timeout_seconds: Number($("task-timeout").value), client_request_id: crypto.randomUUID(), parent_task_id: state.parent});
  $("prompt").value = ""; clearFollowup(); await refresh(); await openDetail(task.id); toast("任务已加入持久化队列");
});
async function openDetail(id) { state.detail = {id}; state.events = []; state.operations = []; state.operationsError = ""; state.cursor = 0; state.tab = "activity"; state.detailRenderKey = ""; $("detail").hidden = false; $("detail-content").replaceChildren(node("p", "muted", "正在加载任务…")); document.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("active", b.dataset.tab === state.tab)); await updateDetail(); }
$("detail-close").onclick = () => { state.detail = null; $("detail").hidden = true; };
async function updateDetail() {
  const id = state.detail?.id; if (!id) return;
  const requestId = ++state.detailRequest;
  const current = () => state.detail?.id === id && requestId === state.detailRequest;
  const [task, events] = await Promise.all([api(`/tasks/${id}`), api(`/tasks/${id}/events?after=${state.cursor}`)]);
  if (!current()) return;
  let operations = [], operationsError = "";
  if (engineOf(task) === "forge") { try { operations = (await api(`/tasks/${id}/tool-operations`)).operations; } catch (error) { operationsError = error.message; } }
  if (!current()) return;
  state.detail = task; state.operations = operations; state.operationsError = operationsError;
  const seen = new Set(state.events.map(event => event.id)); state.events.push(...events.events.filter(event => !seen.has(event.id))); state.events.sort((a, b) => a.id - b.id); if (state.events.length) state.cursor = state.events.at(-1).id;
  $("detail-title").textContent = task.prompt; $("detail-meta").replaceChildren(statusBadge(task), engineBadge(task), node("span", "", `${task.project_name} · v${task.project_version}`), node("span", "checkpoint-label", engineOf(task) === "forge" ? `检查点 r${task.checkpoint_revision || 0}` : date(task.created_at)));
  $("accept-task").hidden = task.status !== "awaiting_review"; disableByPolicy($("accept-task"), !!task.verification && task.verification.exit_code !== 0);
  $("reject-task").hidden = task.status !== "awaiting_review"; $("cancel-task").hidden = !active.has(task.status); disableByPolicy($("cancel-task"), task.cancel_requested);
  $("follow-task").hidden = active.has(task.status) || !task.workspace_ready; $("download-patch").hidden = !task.diff; $("download-zip").hidden = !task.workspace_ready;
  $("resume-task").hidden = !(engineOf(task) === "forge" && task.status === "failed" && task.checkpoint_revision > 0 && task.workspace_ready);
  disableByPolicy($("resume-task"), !!operationsError || operations.some(operation => operation.status === "running"));
  $("resume-task").title = operationsError ? "请先刷新工具操作状态" : $("resume-task").disabled ? "有工具结果尚未确认，需检查工作区后新建后续任务" : "保留已保存状态与预算，将任务重新排队";
  $("event-count").textContent = state.events.length; renderDetail();
}
function eventText(event) {
  const p = event.payload || {}, item = p.item;
  if (item?.type === "agent_message") return item.text || "";
  if (item?.type === "command_execution") return `${item.command || ""}\n${item.aggregated_output || ""}${item.exit_code !== undefined ? `\nexit ${item.exit_code}` : ""}`;
  if (item?.type === "file_change") return (item.changes || []).map(c => `${c.kind}  ${c.path}`).join("\n");
  if (event.kind === "runtime.context") return `第 ${p.step} 步 · 上下文估算 ${p.estimated_tokens} / ${p.limit} tokens\n已移出窗口 ${p.dropped_messages} 条历史消息。计数为字节估算，非模型 tokenizer 实测。`;
  if (event.kind === "runtime.decision") return `第 ${p.step} 步 · ${p.action === "finish" ? "申请完成并验收" : p.tool}\n${p.summary || ""}\n模型用量：${JSON.stringify(p.usage || {})}`;
  if (event.kind === "runtime.observation") return `工具：${p.tool}\n原始观察：${p.artifact || "未提供引用"}${p.loop?.blocked ? `\n重复循环已停止：${p.loop.reason}` : ""}`;
  if (event.kind === "tool.started") return `工具：${p.tool} · call ${p.call_id}\n参数：${JSON.stringify(p.arguments || {}, null, 2)}`;
  if (event.kind === "tool.completed") return `工具：${p.tool} · call ${p.call_id} · ${p.status}\n${JSON.stringify(p.result || {}, null, 2)}`;
  return JSON.stringify(p, null, 2);
}
const eventLabels = {"tool.started": "工具开始", "tool.completed": "工具结果已提交", "runtime.context": "上下文预算", "runtime.decision": "模型单步决策", "runtime.observation": "观察与原始记录", "runtime.repair": "验收失败后修复", "runtime.resumed": "从检查点恢复", "runtime.completed": "执行循环完成", "runtime.finished": "运行时结束", "runtime.failed": "运行时失败"};
function runtimePanel(panel, task) {
  const context = [...state.events].reverse().find(event => event.kind === "runtime.context")?.payload;
  const decisions = state.events.filter(event => event.kind === "runtime.decision");
  const usage = task.usage && Object.keys(task.usage).length ? task.usage : decisions.reduce((sum, event) => ({input_tokens: sum.input_tokens + (event.payload.usage?.input_tokens || 0), output_tokens: sum.output_tokens + (event.payload.usage?.output_tokens || 0)}), {input_tokens: 0, output_tokens: 0});
  const hasUsage = task.usage?.input_tokens != null || task.usage?.output_tokens != null || decisions.some(event => event.payload.usage?.input_tokens != null || event.payload.usage?.output_tokens != null);
  const facts = node("dl", "runtime-facts");
  for (const [label, value] of [["执行引擎", engineLabel(task)], ["检查点", engineOf(task) === "forge" ? `r${task.checkpoint_revision || 0}` : "Codex session"], ["输入 / 输出（执行器上报）", hasUsage ? `${usage.input_tokens ?? "—"} / ${usage.output_tokens ?? "—"} tokens` : "尚无用量记录"], ["任务超时", `${task.timeout_seconds} 秒`], ["最近上下文窗口", context ? `${context.estimated_tokens} / ${context.limit} tokens（估算）` : "尚无窗口记录"], ["决策进度", decisions.length ? `已记录到第 ${decisions.at(-1).payload.step} 步` : "尚无决策记录"]]) { const item = node("div"); item.append(node("dt", "", label), node("dd", "", value)); facts.append(item); }
  panel.append(facts);
  if (engineOf(task) !== "forge") { panel.append(node("p", "hint", "此任务使用 Codex 基线执行器。详细命令与文件变更见执行过程；自有运行时的检查点与工具操作不适用于该任务。")); return; }
  if (state.operationsError) panel.append(node("p", "error-text", `工具操作加载失败：${state.operationsError}`));
  panel.append(node("h3", "runtime-heading", `已持久化工具操作 · ${state.operations.length}`), node("p", "hint", "相同调用 ID 复用已提交结果；结果未确认时不会自动重放。本地代码检索与原始记录读取见执行过程。"));
  if (!state.operations.length && !state.operationsError) panel.append(node("p", "hint", "尚无服务端工具操作。"));
  for (const operation of state.operations) {
    const card = node("details", `operation-card ${operation.status}`), title = node("summary"), status = {running: "执行中 / 结果待确认", succeeded: "已返回", failed: "失败"}[operation.status] || operation.status;
    title.append(node("strong", "", operation.name), node("span", "", `${operation.call_id} · ${status}${operation.result?.exit_code != null ? ` · exit ${operation.result.exit_code}` : ""}`));
    card.append(title, node("p", "hint", `${date(operation.started_at)}${operation.finished_at ? ` → ${date(operation.finished_at)}` : " · 尚未提交结果"}`));
    if (operation.status === "running") card.append(node("p", "error-text", "此操作尚无已提交结果。任务失败时，请检查工作区后再决定如何继续。"));
    card.append(node("pre", "", operation.result ? JSON.stringify(operation.result, null, 2).slice(0, 16000) : "等待结果")); panel.append(card);
  }
}
function renderDetail() {
  const task = state.detail; if (!task) return;
  // Keep selected text, scroll position and expanded tool records stable when a
  // poll returns unchanged content. Events are immutable and identified by ID.
  const content = state.tab === "activity" ? state.events.map(event => event.id)
    : state.tab === "diff" ? task.diff
    : state.tab === "tests" ? [task.test_command, task.baseline_test, task.verification]
    : state.tab === "runtime" ? [task.engine, task.checkpoint_revision, task.usage, task.timeout_seconds, state.events.map(event => event.id), state.operations, state.operationsError]
    : [task.engine, task.error, task.summary, task.session_id, task.checkpoint_revision, task.usage];
  const renderKey = JSON.stringify([task.id, state.tab, content]);
  if (renderKey === state.detailRenderKey) return;
  state.detailRenderKey = renderKey;
  const panel = $("detail-content"), oldScroll = panel.scrollTop, nearBottom = panel.scrollHeight - oldScroll - panel.clientHeight < 80;
  panel.replaceChildren();
  if (state.tab === "activity") {
    if (!state.events.length) panel.append(node("p", "hint", "等待执行事件…"));
    for (const event of state.events) { const e = node("div", "event"), title = node("div", "event-title"); e.dataset.eventKind = event.kind; title.append(node("span", "", event.payload?.item?.type || eventLabels[event.kind] || event.kind), node("time", "", date(event.created_at))); e.append(title, node("pre", "", eventText(event).slice(0, 12000))); panel.append(e); }
  } else if (state.tab === "runtime") {
    runtimePanel(panel, task);
  } else if (state.tab === "diff") {
    if (!task.diff) panel.append(node("p", "hint", "任务完成后会显示与项目基线的差异。"));
    else { const code = node("div", "code-panel"); for (const line of task.diff.split("\n")) code.append(node("div", `diff-line ${line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : ""}`, line)); panel.append(code); }
  } else if (state.tab === "tests") {
    panel.append(node("p", "hint", `验收命令：${task.test_command || "未配置"}。以下结果由隔离的 Docker 容器独立运行，不使用模型的口头结论。`));
    for (const [title, result] of [["修改前 · 基线测试", task.baseline_test], ["修改后 · 独立验收", task.verification]]) {
      const card = node("div", "test-card"), heading = node("h3", "", title); heading.append(node("span", "", result ? `exit ${result.exit_code}${result.timed_out ? " · 超时" : ""}` : "尚无结果")); card.append(heading, node("pre", "", result ? `${result.stdout || ""}${result.stderr || ""}${result.truncated ? "\n[输出已截断]" : ""}` : "未运行")); panel.append(card);
    }
  } else {
    if (task.error) panel.append(node("p", "error-text", task.error));
    panel.append(node("div", "summary", task.summary || "任务完成后显示最终说明。"), node("hr"), node("p", "hint", `${engineLabel(task)}${engineOf(task) === "forge" ? ` · 检查点 r${task.checkpoint_revision || 0}` : ` · session ${task.session_id || "尚未建立"}`}\n任务 ID：${task.id}\n使用量：${JSON.stringify(task.usage || {})}`), node("p", "hint", "接受变更会生成项目新版本。当前宿主机仓库不会被直接覆盖；可下载补丁或完整项目。"));
  }
  panel.scrollTop = state.tab === "activity" && nearBottom ? panel.scrollHeight : oldScroll;
}
document.querySelectorAll("[data-tab]").forEach(button => button.onclick = () => { state.tab = button.dataset.tab; document.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("active", b === button)); $("detail-content").scrollTop = 0; renderDetail(); });
for (const [id, operation, message] of [["accept-task", "accept", "已接受：项目版本已更新"], ["reject-task", "reject", "变更已拒绝"], ["cancel-task", "cancel", "取消请求已送达"]]) $(id).onclick = () => action($(id), async () => { await post(`/tasks/${state.detail.id}/${operation}`); await refresh(); toast(message); });
$("resume-task").onclick = () => action($("resume-task"), async () => { await post(`/tasks/${state.detail.id}/resume`); await refresh(); toast("已按保存的检查点重新排队，历史预算继续累计"); });
$("follow-task").onclick = () => { const task = state.detail; selectProject(task.project_id); selectEngine(engineOf(task)); const current = state.projects.find(p => p.id === task.project_id); if (current?.version === task.project_version) { state.parent = task.id; $("followup-label").textContent = `继续 ${task.id.slice(0, 8)}`; $("followup-label").hidden = false; $("composer-title").textContent = "在现有变更上继续工作"; } else toast("项目已更新，将基于当前最新版本开始。"); $("test-command").value = task.test_command; $("detail-close").click(); $("prompt").focus(); window.scrollTo({top: 0, behavior: "smooth"}); };
for (const [id, endpoint, extension] of [["download-patch", "patch", "patch"], ["download-zip", "archive", "zip"]]) $(id).onclick = () => action($(id), async () => { const taskId = state.detail.id; const response = await api(`/tasks/${taskId}/${endpoint}`, {raw: true}); const url = URL.createObjectURL(await response.blob()); const a = node("a"); a.href = url; a.download = `${taskId}.${extension}`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 10000); });
for (const id of ["import-open", "empty-import", "mobile-import"]) $(id).onclick = () => $("import-dialog").showModal();
$("import-close").onclick = () => $("import-dialog").close();
document.querySelectorAll('[name="import-kind"]').forEach(input => input.onchange = () => { const zip = document.querySelector('[name="import-kind"]:checked').value === "zip"; $("github-fields").hidden = zip; $("zip-fields").hidden = !zip; });
$("import-form").onsubmit = async event => {
  event.preventDefault(); $("import-error").textContent = ""; $("import-submit").disabled = true;
  try {
    const body = {name: $("import-name").value.trim(), test_command: $("import-test").value.trim()};
    if (document.querySelector('[name="import-kind"]:checked').value === "zip") {
      const file = $("import-zip").files[0]; if (!file || file.size > 16 * 1024 * 1024) throw new Error("请选择不超过 16 MiB 的 ZIP 文件。");
      const bytes = new Uint8Array(await file.arrayBuffer()); let binary = ""; for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192)); body.archive_base64 = btoa(binary);
    } else { body.repository_url = $("import-url").value.trim(); body.ref = $("import-ref").value.trim() || "HEAD"; }
    const project = await post("/projects", body); await refresh(); selectProject(project.id); $("import-dialog").close(); toast("项目已导入，可开始开发任务");
  } catch (e) { $("import-error").textContent = e.message; } finally { $("import-submit").disabled = false; }
};
$("sample-create").onclick = () => action($("sample-create"), async () => { const project = await post("/projects/sample"); await refresh(); selectProject(project.id); $("prompt").value = project.suggested_prompt; toast("已载入示例，点击开始执行即可修复失败测试"); });
setInterval(async () => { if (!state.token || state.busy || document.hidden) return; state.busy = true; try { await refresh(); } catch (e) { $("engine-name").textContent = "连接中断 · 自动重试"; } finally { state.busy = false; } }, 2500);
if (state.token) login().catch(e => { logout(); $("login-error").textContent = e.message; });

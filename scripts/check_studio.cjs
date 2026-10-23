#!/usr/bin/env node
'use strict';

// Read-only UI acceptance. No task/project creation, acceptance, or cancellation.
// Node + Playwright are available in the bundled Codex runtime on this machine.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

const projectRoot = path.resolve(__dirname, '..');
const bundledModules = path.join(os.homedir(),
  '.cache', 'codex-runtimes', 'codex-primary-runtime', 'dependencies', 'node', 'node_modules');
let chromium;
try {
  ({ chromium } = require('playwright'));
} catch {
  ({ chromium } = require(path.join(bundledModules, 'playwright')));
}

function argumentsFrom(argv) {
  const options = { baseUrl: 'http://127.0.0.1:2026', output: path.join(projectRoot, 'results'),
    minTasks: 2, taskIds: [], timeout: 45_000, localAssets: false };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag === '--help') {
      process.stdout.write('Usage: node scripts/check_studio.cjs [--base-url URL] [--task-id UUID ...] [--min-tasks 2] [--output-dir PATH] [--local-assets]\n');
      process.exit(0);
    }
    if (flag === '--local-assets') { options.localAssets = true; continue; }
    const value = argv[++i];
    if (value === undefined) throw new Error(`Missing value for ${flag}`);
    if (flag === '--base-url') options.baseUrl = value.replace(/\/$/, '');
    else if (flag === '--output-dir') options.output = path.resolve(value);
    else if (flag === '--task-id') options.taskIds.push(value);
    else if (flag === '--min-tasks') options.minTasks = Number(value);
    else if (flag === '--timeout-ms') options.timeout = Number(value);
    else throw new Error(`Unknown argument: ${flag}`);
  }
  const url = new URL(options.baseUrl);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error('base URL must not contain credentials, query, or fragment');
  }
  assert(Number.isInteger(options.minTasks) && options.minTasks >= 1 && options.minTasks <= 10,
    '--min-tasks must be between 1 and 10');
  assert(Number.isFinite(options.timeout) && options.timeout >= 1000 && options.timeout <= 300000,
    '--timeout-ms must be between 1000 and 300000');
  return options;
}

function localToken() {
  // Never log the file, token, HTTP headers, browser storage, or filled input.
  const lines = fs.readFileSync(path.join(projectRoot, '.env'), 'utf8').replace(/^\uFEFF/, '').split(/\r?\n/);
  for (const line of lines) {
    const match = line.match(/^\s*(?:export\s+)?API_KEYS_JSON\s*=\s*(.*?)\s*$/);
    if (!match) continue;
    let value = match[1];
    if (value.length > 1 && ['"', "'"].includes(value[0]) && value[0] === value.at(-1)) value = value.slice(1, -1);
    const token = JSON.parse(value)['local-client'];
    if (typeof token !== 'string' || !token.trim()) throw new Error('Missing local-client API credential');
    return token.trim();
  }
  throw new Error('API_KEYS_JSON is missing from .env');
}

async function main() {
  const options = argumentsFrom(process.argv.slice(2));
  const token = localToken();
  const scrub = (value) => String(value).split(token).join('[REDACTED]')
    .replace(/(Bearer\s+)[A-Za-z0-9._~+\/-]+=*/gi, '$1[REDACTED]')
    .replace(/\bsk-[A-Za-z0-9_-]{16,}/g, '[REDACTED]');
  const safeUrl = (value) => {
    try { const url = new URL(value); return scrub(url.origin + url.pathname); }
    catch { return scrub(value); }
  };
  const report = {
    checked_at: new Date().toISOString(), base_url: safeUrl(options.baseUrl),
    scope: 'Read-only live UI plus isolated mocked Forge create/resume and acceptance-policy flows; the mocked flows never reach the real API.',
    asset_source: options.localAssets ? 'local workspace static files' : 'deployed API static files',
    browser: 'Microsoft Edge headless / Playwright', checks: [], tasks: [], screenshots: [],
    browser_errors: [], console_errors: [], failed_requests: [], failed_api_responses: [], blocked_mutations: [],
  };
  fs.mkdirSync(options.output, { recursive: true });
  let browser;

  async function check(name, action, continueOnError = false) {
    try {
      const details = await action();
      report.checks.push({ name, passed: true, ...(details === undefined ? {} : { details }) });
      return details;
    } catch (error) {
      report.checks.push({ name, passed: false, error: scrub(error.message).slice(0, 5000) });
      if (!continueOnError) throw error;
      return undefined;
    }
  }

  async function observe(context, surface) {
    // Enforce the task's read-only boundary even if the UI accidentally submits.
    await context.route('**/api/coding/**', async (route) => {
      const request = route.request();
      if (request.method() !== 'GET') {
        report.blocked_mutations.push({ surface, method: request.method(), url: safeUrl(request.url()) });
        await route.abort('blockedbyclient');
        return;
      }
      await route.continue();
    });
    context.on('page', (page) => {
      page.on('pageerror', (error) => report.browser_errors.push({ surface, error: scrub(error.stack || error.message).slice(0, 5000) }));
      page.on('console', (message) => {
        if (message.type() === 'error') report.console_errors.push({ surface, message: scrub(message.text()).slice(0, 3000),
          url: safeUrl(message.location().url || '') });
      });
      page.on('requestfailed', (request) => {
        const error = request.failure()?.errorText || 'unknown';
        if (!error.includes('ERR_ABORTED')) report.failed_requests.push({ surface, method: request.method(),
          url: safeUrl(request.url()), error: scrub(error) });
      });
      page.on('response', (response) => {
        if (response.status() >= 400 && new URL(response.url()).pathname.startsWith('/api/coding/')) {
          report.failed_api_responses.push({ surface, status: response.status(), url: safeUrl(response.url()) });
        }
      });
    });
  }

  async function localAssets(context) {
    await context.route(/\/studio(?:\/[^?]*)?(?:\?.*)?$/, async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      const assets = {'/studio': ['index.html', 'text/html'], '/studio/': ['index.html', 'text/html'],
        '/studio/static/app.js': ['app.js', 'text/javascript'], '/studio/static/style.css': ['style.css', 'text/css']};
      if (!assets[pathname]) return route.fallback();
      const [filename, contentType] = assets[pathname];
      await route.fulfill({path: path.join(projectRoot, 'src', 'agent_backend', 'studio', filename), contentType});
    });
  }

  async function screenshot(page, name) {
    // Do not capture a populated password control or any accidentally reflected
    // credential; all screenshots are taken after successful login clears it.
    assert.equal(await page.locator('#api-key').inputValue(), '', 'Credential input was not cleared');
    assert.equal(await page.evaluate((secret) => document.body.innerText.includes(secret), token), false,
      'A credential was reflected in visible UI; screenshot refused');
    const filename = path.join(options.output, name);
    await page.screenshot({ path: filename, fullPage: true, animations: 'disabled',
      mask: [page.locator('#api-key')] });
    report.screenshots.push(filename);
  }

  async function signIn(page) {
    await page.goto(options.baseUrl + '/studio', { waitUntil: 'domcontentloaded', timeout: options.timeout });
    await page.locator('#login').waitFor({ state: 'visible' });
    const projectsResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname === '/api/coding/projects' && response.request().method() === 'GET');
    const tasksResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname === '/api/coding/tasks' && response.request().method() === 'GET');
    await page.locator('#api-key').fill(token);
    await page.locator('#login-form button[type="submit"]').click();
    const [projectsReply, tasksReply] = await Promise.all([projectsResponse, tasksResponse]);
    assert.equal(projectsReply.status(), 200, 'Project list login request failed');
    assert.equal(tasksReply.status(), 200, 'Task list login request failed');
    const [projects, tasks] = await Promise.all([projectsReply.json(), tasksReply.json()]);
    await page.locator('#app').waitFor({ state: 'visible' });
    await page.locator('#login').waitFor({ state: 'hidden' });
    assert.equal(await page.locator('#api-key').inputValue(), '');
    return { projects: projects.projects, tasks: tasks.tasks };
  }

  async function waitRowCount(page, count) {
    await page.waitForFunction((expected) => document.querySelectorAll('#tasks .task-row').length === expected,
      count, { timeout: options.timeout });
  }

  async function openTask(page, taskId, knownTasks) {
    await page.locator('#all-tasks').click();
    await page.locator('[data-filter="all"]').click();
    await waitRowCount(page, knownTasks.length);
    const index = knownTasks.findIndex((task) => task.id === taskId);
    assert(index >= 0, 'Requested task was not found in visible list');
    const response = page.waitForResponse((reply) => new URL(reply.url()).pathname === `/api/coding/tasks/${taskId}`);
    await page.locator('#tasks .task-row').nth(index).click();
    const detailResponse = await response;
    assert.equal(detailResponse.status(), 200);
    const detail = await detailResponse.json();
    await page.locator('#detail').waitFor({ state: 'visible' });
    await page.waitForFunction((prompt) => document.getElementById('detail-title').textContent === prompt, detail.prompt);
    return detail;
  }

  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const desktop = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN',
      timezoneId: 'Asia/Shanghai', reducedMotion: 'reduce' });
    desktop.setDefaultTimeout(options.timeout);
    await observe(desktop, 'desktop');
    if (options.localAssets) await localAssets(desktop);
    const page = await desktop.newPage();
    const data = await check('desktop_login_and_workspace', async () => {
      const snapshot = await signIn(page);
      assert(Array.isArray(snapshot.projects) && Array.isArray(snapshot.tasks));
      await waitRowCount(page, snapshot.tasks.length);
      assert.equal(Number(await page.locator('#task-count').innerText()), snapshot.tasks.length);
      assert.equal(await page.locator('#project-title').innerText(), '全部项目');
      assert(await page.locator('#engine-name').isVisible());
      assert(await page.locator('#prompt').isVisible());
      return snapshot;
    });
    // Avoid saving complete API task payloads (prompts/logs) in the report.
    report.checks.at(-1).details = { projects: data.projects.length, tasks: data.tasks.length,
      executor_label: await page.locator('#engine-name').innerText() };

    await check('live_acceptance_policy_metadata_dialog', async () => {
      const project = data.projects[0];
      assert(project, 'A project is needed to inspect the policy dialog');
      await page.locator('#project-select').selectOption(project.id);
      const response = page.waitForResponse(reply => new URL(reply.url()).pathname === `/api/coding/projects/${project.id}/acceptance-suites`);
      await page.locator('#acceptance-open').click();
      const reply = await response;
      assert.equal(reply.status(), 200);
      const metadata = await reply.json();
      assert(Array.isArray(metadata.suites));
      assert(metadata.suites.every(suite => !('files' in suite) && !('policy' in suite)));
      await page.locator('#acceptance-dialog').waitFor({state: 'visible'});
      assert((await page.locator('#acceptance-current').innerText()).includes(project.name));
      assert.equal(await page.locator('#acceptance-files').getAttribute('accept'), '.py');
      await page.locator('#acceptance-close').click();
      await page.locator('#all-tasks').click();
      return {project_id: project.id, policy_versions: metadata.suites.length, submitted: false};
    });

    await check('forge_is_default_and_codex_baseline_is_selectable', async () => {
      assert.equal(await page.locator('#task-engine').inputValue(), 'forge');
      await page.locator('#task-engine').selectOption('codex');
      assert((await page.locator('#engine-hint').innerText()).includes('基线'));
      await page.locator('#task-engine').selectOption('forge');
      assert((await page.locator('#engine-hint').innerText()).includes('上下文预算'));
      return {default_engine: 'forge', alternative_engine: 'codex', submitted: false};
    });

    await check('github_and_zip_import_dialog_without_submission', async () => {
      await page.locator('#import-open').click();
      await page.locator('#import-dialog').waitFor({ state: 'visible' });
      assert(await page.locator('#github-fields').isVisible());
      assert.equal(await page.locator('#zip-fields').isVisible(), false);
      assert.equal(await page.locator('#import-ref').inputValue(), 'HEAD');
      await page.locator('input[name="import-kind"][value="zip"]').check();
      await page.locator('#zip-fields').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#github-fields').isVisible(), false);
      assert.equal(await page.locator('#import-zip').getAttribute('accept'), '.zip');
      assert((await page.locator('#zip-fields').innerText()).includes('16 MiB'));
      await page.locator('input[name="import-kind"][value="github"]').check();
      await page.locator('#github-fields').waitFor({ state: 'visible' });
      await page.locator('#import-close').click();
      await page.locator('#import-dialog').waitFor({ state: 'hidden' });
      return { github_fields: true, zip_fields: true, submitted: false };
    });

    const selected = await check('completed_real_coding_tasks_available', async () => {
      // A reviewer may reject a completed patch while its immutable artifacts
      // remain available. Read-only UI acceptance must still inspect that task.
      const complete = data.tasks.filter((task) => ['awaiting_review', 'accepted', 'rejected'].includes(task.status)
        && ((task.engine || 'codex') === 'forge' ? task.checkpoint_revision > 0 : task.session_id) && task.diff && task.verification);
      const tasks = options.taskIds.length ? options.taskIds.map((id) => complete.find((task) => task.id === id))
        : complete.slice(0, options.minTasks);
      assert(tasks.every(Boolean), 'A requested task is not completed with a session, diff, and independent verification');
      assert(tasks.length >= options.minTasks, `Need at least ${options.minTasks} completed real coding tasks`);
      return tasks;
    });
    report.checks.at(-1).details = { count: selected.length, task_ids: selected.map((task) => task.id) };

    await check('project_filter', async () => {
      const ids = [...new Set(selected.map((task) => task.project_id))];
      const results = [];
      for (const id of ids) {
        const project = data.projects.find((item) => item.id === id);
        assert(project, 'Task references a missing project');
        await page.locator('#project-select').selectOption(id);
        const expected = data.tasks.filter((task) => task.project_id === id).length;
        await waitRowCount(page, expected);
        assert.equal(await page.locator('#project-title').innerText(), project.name);
        assert.equal(await page.locator('#projects button.active').count(), 1);
        const names = await page.locator('#tasks .task-sub span:first-child').allTextContents();
        assert(names.every((name) => name === project.name));
        const visibleTasks = data.tasks.filter(task => task.project_id === id);
        for (const [index, task] of visibleTasks.entries()) {
          if (!task.verification) continue;
          const label = task.acceptance_suite_id ? '独立验收' : '项目命令';
          const outcome = task.verification.exit_code === 0 ? '通过' : '未通过';
          const row = await page.locator('#tasks .task-row').nth(index).innerText();
          assert(row.includes(label + outcome), 'Task list mislabels the verification source');
          if (!task.acceptance_suite_id) assert(!row.includes('独立验收通过'), 'Legacy command result was presented as acceptance proof');
        }
        results.push({ project_id: id, visible_tasks: expected });
      }
      await page.locator('#all-tasks').click();
      await waitRowCount(page, data.tasks.length);
      return results;
    });

    await check('status_filter', async () => {
      await page.locator('[data-filter="awaiting_review"]').click();
      const count = data.tasks.filter((task) => task.status === 'awaiting_review').length;
      await waitRowCount(page, count);
      const badges = await page.locator('#tasks .status').allTextContents();
      assert(badges.every((badge) => badge === '待审核'));
      await page.locator('[data-filter="all"]').click();
      await waitRowCount(page, data.tasks.length);
      return { awaiting_review: count };
    });
    await screenshot(page, 'studio-desktop.png');

    for (let index = 0; index < selected.length; index += 1) {
      const task = await openTask(page, selected[index].id, data.tasks);
      report.tasks.push({ id: task.id, project_id: task.project_id, status: task.status, engine: task.engine || 'codex', checkpoint_revision: task.checkpoint_revision || 0,
        baseline_exit_code: task.baseline_test?.exit_code, verification_exit_code: task.verification?.exit_code,
        has_session_id: Boolean(task.session_id), diff_characters: task.diff.length });

      await check(`task_${index + 1}_execution_log`, async () => {
        await page.locator('[data-tab="activity"]').click();
        await page.locator('#detail-content .event').first().waitFor();
        const events = await page.locator('#detail-content .event-title > span').allTextContents();
        const commands = (task.engine || 'codex') === 'forge'
          ? await page.locator('#detail-content .event[data-event-kind="tool.started"]').count()
          : events.filter((type) => type === 'command_execution').length;
        assert(commands > 0, 'No real command execution event was shown');
        assert.equal(Number(await page.locator('#event-count').innerText()), events.length);
        return { events: events.length, command_events: commands };
      });

      await check(`task_${index + 1}_code_diff`, async () => {
        await page.locator('[data-tab="diff"]').click();
        await page.locator('#detail-content .code-panel').waitFor();
        const lines = await page.locator('#detail-content .diff-line').allTextContents();
        assert.equal(lines.join('\n'), task.diff, 'Rendered diff differs from the stored task artifact');
        assert(lines.some((line) => line.startsWith('+') && !line.startsWith('+++')),
          'The task diff contains no added source lines');
        if (index === 0) await screenshot(page, 'studio-detail.png');
        return { lines: lines.length };
      });

      await check(`task_${index + 1}_independent_tests`, async () => {
        await page.locator('[data-tab="tests"]').click();
        assert.equal(await page.locator('#detail-content .test-card').count(), 2);
        assert(task.baseline_test, 'No baseline test result was saved');
        assert(task.verification, 'No independent verification result was saved');
        assert.equal(task.verification.exit_code, 0, 'Independent verification did not pass');
        const headings = await page.locator('#detail-content .test-card h3').allTextContents();
        assert(headings[0].includes(`exit ${task.baseline_test.exit_code}`));
        assert(headings[1].includes('exit 0'));
        const verificationLabel = task.acceptance_suite_id ? '独立验收' : '项目命令验证';
        assert(headings[1].includes(`修改后 · ${verificationLabel}`), 'Test heading mislabels the verification source');
        if (!task.acceptance_suite_id) {
          assert((await page.locator('#detail-content').innerText()).includes('此任务未绑定独立验收包，属于项目命令验证。'));
          assert.equal(await page.locator('#accept-task').isEnabled(), false, 'A legacy command-only result enabled acceptance');
        }
        const output = await page.locator('#detail-content .test-card').nth(1).locator('pre').first().innerText();
        assert(output.trim().length > 0, 'Independent test output is empty');
        if (index === 0) {
          assert.equal(await page.evaluate(async () => {
            const originalCard = document.querySelector('#detail-content .test-card');
            await updateDetail();
            return originalCard.isConnected;
          }), true, 'An unchanged poll rebuilt the test output and interrupted inspection');
          await page.locator('#detail-content .test-card').nth(1).scrollIntoViewIfNeeded();
          await screenshot(page, 'studio-tests.png');
        }
        return { baseline_exit_code: task.baseline_test.exit_code, verification_exit_code: task.verification.exit_code,
          verification_source: task.acceptance_suite_id ? 'pinned acceptance suite' : 'legacy project command' };
      });

      await check(`task_${index + 1}_summary`, async () => {
        await page.locator('[data-tab="summary"]').click();
        assert.equal(await page.locator('#detail-content .summary').innerText(), task.summary);
        assert((await page.locator('#detail-content').innerText()).includes((task.engine || 'codex') === 'forge' ? `检查点 r${task.checkpoint_revision}` : task.session_id));
        return { has_summary: Boolean(task.summary), engine: task.engine || 'codex' };
      });
      await check(`task_${index + 1}_runtime_and_tools`, async () => {
        await page.locator('[data-tab="runtime"]').click();
        assert.equal(await page.locator('#detail-content .runtime-facts > div').count(), 6);
        if ((task.engine || 'codex') === 'forge') {
          assert((await page.locator('#detail-content').innerText()).includes(`r${task.checkpoint_revision}`));
          assert.equal(await page.locator('#resume-task').isVisible(), false);
        }
        return {engine: task.engine || 'codex', operations: await page.locator('.operation-card').count()};
      });
      await page.locator('#detail-close').click();
      await page.locator('#detail').waitFor({ state: 'hidden' });
    }

    const forgeTask = data.tasks.find(task => task.engine === 'forge');
    if (forgeTask) await check('live_forge_checkpoint_and_tool_operations', async () => {
      const detail = await openTask(page, forgeTask.id, data.tasks);
      await page.locator('[data-tab="runtime"]').click();
      assert((await page.locator('#detail-content').innerText()).includes(`r${detail.checkpoint_revision || 0}`));
      const canResume = detail.status === 'failed' && detail.checkpoint_revision > 0 && detail.workspace_ready;
      assert.equal(await page.locator('#resume-task').isVisible(), Boolean(canResume));
      const operations = await page.locator('.operation-card').count();
      await screenshot(page, 'studio-forge-runtime.png');
      await page.locator('#detail-close').click();
      return {task_id: detail.id, checkpoint_revision: detail.checkpoint_revision || 0, operations, mutation: false};
    });

    await check('logout_clears_session_and_stays_logged_out_after_reload', async () => {
      await page.locator('#logout').click();
      await page.locator('#login').waitFor({ state: 'visible' });
      await page.locator('#app').waitFor({ state: 'hidden' });
      assert.equal(await page.evaluate(() => sessionStorage.getItem('forge-token') === null), true);
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.locator('#login').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#api-key').inputValue(), '');
      assert.equal(await page.locator('#app').isVisible(), false);
    });

    const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1,
      isMobile: true, hasTouch: true, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', reducedMotion: 'reduce' });
    mobile.setDefaultTimeout(options.timeout);
    await observe(mobile, 'mobile');
    if (options.localAssets) await localAssets(mobile);
    const phone = await mobile.newPage();
    await check('mobile_login_and_project_filter', async () => {
      const snapshot = await signIn(phone);
      const id = selected[0].project_id;
      await phone.locator('#project-select').selectOption(id);
      const count = snapshot.tasks.filter((task) => task.project_id === id).length;
      await waitRowCount(phone, count);
      assert.equal(await phone.locator('#project-select').inputValue(), id);
      assert(await phone.locator('#submit-task').isVisible());
      return { width: 390, height: 844, project_id: id, visible_tasks: count };
    });
    // Capture before the layout assertion so a failure has a useful artifact.
    await screenshot(phone, 'studio-mobile.png');
    await check('mobile_layout_without_horizontal_overflow', async () => {
      const metrics = await phone.evaluate(() => ({ viewport: innerWidth,
        document_width: document.documentElement.scrollWidth, body_width: document.body.scrollWidth,
        controls: ['project-select', 'task-engine', 'settings-toggle', 'acceptance-open', 'submit-task'].map((id) => {
          const rect = document.getElementById(id).getBoundingClientRect();
          return { id, left: rect.left, right: rect.right, width: rect.width, height: rect.height };
        }) }));
      report.mobile_layout = metrics;
      assert(metrics.document_width <= metrics.viewport + 1, 'Mobile document scrolls horizontally');
      assert(metrics.controls.every((control) => control.left >= -1 && control.right <= metrics.viewport + 1),
        'A mobile composer control is outside the viewport');
      assert(metrics.controls.find((control) => control.id === 'settings-toggle').height <= 44,
        'Task settings label wraps vertically because the mobile control is too narrow');
      return metrics;
    }, true);

    await check('mobile_task_detail_tabs_and_layout', async () => {
      // A newer queued follow-up may be the first project row. Exercise the
      // completed task selected for acceptance, not whichever task sorts first.
      const target = phone.locator('#tasks .task-row').filter({has: phone.getByText(selected[0].prompt, {exact: true})});
      assert.equal(await target.count(), 1, 'The selected completed task is not uniquely visible on mobile');
      const reply = phone.waitForResponse(response => new URL(response.url()).pathname === `/api/coding/tasks/${selected[0].id}`);
      try {
        await target.click();
        assert.equal((await reply).status(), 200);
        await phone.locator('#detail').waitFor({ state: 'visible' });
        await phone.locator('#detail-content .event').first().waitFor();
        await phone.locator('[data-tab="diff"]').click();
        await phone.locator('#detail-content .code-panel').waitFor();
        const bounds = await phone.locator('#detail').boundingBox();
        assert(bounds && bounds.x >= -1 && bounds.x + bounds.width <= 391, 'Mobile detail exceeds viewport');
        await screenshot(phone, 'studio-mobile-detail.png');
        await phone.locator('[data-tab="tests"]').click();
        assert.equal(await phone.locator('#detail-content .test-card').count(), 2);
        return {task_id: selected[0].id, detail_width: bounds.width, diff_and_tests_visible: true};
      } finally {
        if (await phone.locator('#detail').isVisible()) await phone.locator('#detail-close').click();
        await phone.locator('#detail').waitFor({ state: 'hidden' });
      }
    }, true);

    await check('mobile_import_and_logout_remain_available', async () => {
      const imports = phone.locator('#mobile-import:visible, #import-open:visible');
      const logout = phone.locator('#mobile-logout:visible, #logout:visible');
      const available = { import_button: await imports.count() > 0, logout_button: await logout.count() > 0 };
      report.mobile_navigation = available;
      assert(available.import_button, 'Mobile layout hides every project import entry point');
      assert(available.logout_button, 'Mobile layout hides every logout entry point');
      await imports.first().click();
      await phone.locator('#import-dialog').waitFor({ state: 'visible' });
      await phone.locator('#import-close').click();
      await phone.locator('#import-dialog').waitFor({ state: 'hidden' });
      await logout.first().click();
      await phone.locator('#login').waitFor({ state: 'visible' });
      assert.equal(await phone.evaluate(() => sessionStorage.getItem('forge-token') === null), true);
      return available;
    }, true);

    await check('isolated_mock_forge_creation_resume_and_uncertain_operation_guard', async () => {
      const fixture = await browser.newContext({viewport: {width: 1200, height: 900}, locale: 'zh-CN'});
      await localAssets(fixture);
      const createdAt = '2026-10-23T08:00:00Z';
      const project = {id: 'fixture-project', name: 'UI fixture only', version: 1, test_command: 'python -m pytest -q'};
      const sampleTask = {id: 'fixture-failed', project_id: project.id, project_name: project.name, project_version: 1,
        engine: 'forge', status: 'failed', checkpoint_revision: 12, workspace_ready: true, prompt: 'Fixture task with saved checkpoint',
        created_at: createdAt, timeout_seconds: 900, test_command: project.test_command, usage: {}, diff: '', summary: '', error: 'Fixture interruption'};
      const fixtureTasks = [sampleTask, {...sampleTask, id: 'fixture-uncertain', prompt: 'Fixture with uncertain tool'}];
      const submitted = [], resumed = [];
      let postponeResume = false, resumeArrived, releaseResume;
      await fixture.route('**/api/coding/**', async (route) => {
        const request = route.request(), pathname = new URL(request.url()).pathname.replace('/api/coding', '');
        let body;
        if (request.method() === 'POST' && pathname === '/tasks') {
          const payload = request.postDataJSON(); submitted.push(payload);
          body = {...sampleTask, ...payload, id: `fixture-created-${submitted.length}`, status: 'queued', checkpoint_revision: 0}; fixtureTasks.unshift(body);
        } else if (request.method() === 'POST' && pathname.endsWith('/resume')) {
          const id = pathname.split('/')[2]; resumed.push(id); body = fixtureTasks.find(task => task.id === id); body.status = 'queued';
          if (postponeResume) { resumeArrived(); await new Promise(resolve => { releaseResume = resolve; }); }
        } else if (request.method() !== 'GET') {
          return route.fulfill({status: 405, json: {detail: 'Fixture rejects every other mutation'}});
        } else if (pathname === '/status') body = {executor_online: true, executors: [{engine: 'forge', capacity: 2}, {engine: 'codex', capacity: 1}], capacity: {queue_limit: 100, sandbox_commands: 2}, counts: {failed: 2}};
        else if (pathname === '/projects') body = {projects: [project]};
        else if (pathname === '/tasks') body = {tasks: fixtureTasks};
        else if (pathname.endsWith('/events')) body = {events: [
          {id: 1, kind: 'runtime.context', created_at: createdAt, payload: {step: 3, estimated_tokens: 240, limit: 12000, dropped_messages: 2}},
          {id: 2, kind: 'tool.completed', created_at: createdAt, payload: {tool: 'read_file', call_id: 'step-2', status: 'succeeded', result: {content: '<script>window.forgeProbeXss=true</script>'}}},
        ]};
        else if (pathname.endsWith('/tool-operations')) body = {operations: pathname.includes('fixture-uncertain') ? [{name: 'run_command', call_id: 'step-3', status: 'running', started_at: createdAt, result: null}] : []};
        else body = fixtureTasks.find(task => task.id === pathname.split('/')[2]);
        await route.fulfill({status: body ? 200 : 404, json: body || {detail: 'Unknown fixture resource'}});
      });
      const screen = await fixture.newPage();
      screen.on('pageerror', error => report.browser_errors.push({surface: 'isolated-forge-fixture', error: scrub(error.message)}));
      try {
        const snapshot = await signIn(screen);
        assert.equal(await screen.locator('#task-engine').inputValue(), 'forge');
        await openTask(screen, 'fixture-failed', snapshot.tasks);
        await screen.locator('#resume-task').waitFor({state: 'visible'});
        assert.equal(await screen.locator('#resume-task').isEnabled(), true);
        assert((await screen.locator('#detail-meta').innerText()).includes('检查点 r12'));
        assert.equal(await screen.evaluate(() => window.forgeProbeXss === true), false);
        await screen.locator('[data-tab="runtime"]').click();
        assert((await screen.locator('#detail-content').innerText()).includes('240 / 12000'));
        const resumeRequest = screen.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/resume'));
        await screen.locator('#resume-task').click(); await resumeRequest;
        await screen.locator('#resume-task').waitFor({state: 'hidden'});
        assert.deepEqual(resumed, ['fixture-failed']);
        await screen.locator('#detail-close').click();
        await openTask(screen, 'fixture-uncertain', fixtureTasks);
        assert.equal(await screen.locator('#resume-task').isVisible(), true);
        assert.equal(await screen.locator('#resume-task').isEnabled(), false);
        await screen.locator('[data-tab="runtime"]').click();
        assert.equal(await screen.locator('.operation-card.running').count(), 1);
        // A response for the previous task must not remove the current task's
        // uncertain-operation guard when its button action finally settles.
        await screen.locator('#detail-close').click();
        sampleTask.status = 'failed';
        await openTask(screen, sampleTask.id, fixtureTasks);
        postponeResume = true;
        const enteredResume = new Promise(resolve => { resumeArrived = resolve; });
        await screen.locator('#resume-task').click(); await enteredResume;
        await screen.locator('#detail-close').click();
        await openTask(screen, 'fixture-uncertain', fixtureTasks);
        releaseResume();
        await screen.waitForFunction(() => document.querySelector('#resume-task').dataset.pending !== 'true');
        assert.equal(await screen.locator('#resume-task').isEnabled(), false,
          'Completing the previous task action removed the current task safety guard');

        // Deliver an older checkpoint response after a newer one, matching a
        // manual refresh racing with polling; it must not roll the UI backward.
        await screen.evaluate(() => { state.busy = true; });
        let detailRequests = 0, detailArrived, releaseDetail;
        const enteredDetail = new Promise(resolve => { detailArrived = resolve; });
        const olderDetailGate = new Promise(resolve => { releaseDetail = resolve; });
        const detailRoute = /\/api\/coding\/tasks\/fixture-uncertain$/;
        await fixture.route(detailRoute, async route => {
          detailRequests += 1;
          const first = detailRequests === 1;
          if (first) { detailArrived(); await olderDetailGate; }
          await route.fulfill({json: {...fixtureTasks.find(task => task.id === 'fixture-uncertain'), checkpoint_revision: first ? 1 : 12}});
        });
        await screen.evaluate(() => { window.olderDetailPromise = updateDetail(); });
        await enteredDetail;
        await screen.evaluate(() => updateDetail());
        releaseDetail(); await screen.evaluate(() => window.olderDetailPromise);
        assert((await screen.locator('#detail-meta').innerText()).includes('检查点 r12'),
          'An older poll overwrote the latest checkpoint');
        await fixture.unroute(detailRoute);
        await screen.evaluate(() => { state.busy = false; });
        await screen.locator('#detail-close').click();
        await screen.locator('#project-select').selectOption(project.id);
        await screen.locator('#prompt').fill('Fixture creation must use the selected Forge engine.');
        await screen.locator('#submit-task').click();
        await screen.waitForFunction(() => document.querySelector('#detail-title').textContent.includes('Fixture creation'));
        assert.equal(submitted[0].engine, 'forge');
        assert.equal(submitted.length, 1);
        return {transport: 'all API calls mocked, no live mutations', default_engine: submitted[0].engine,
          checkpoint_displayed: 12, resumed_fixture_tasks: resumed.length, uncertain_resume_disabled: true,
          late_action_preserves_current_guard: true, stale_detail_response_discarded: true, untrusted_output_rendered_as_text: true};
      } finally { await fixture.close(); }
    });

    const acceptanceFixture = await browser.newContext({viewport: {width: 1200, height: 900}, locale: 'zh-CN'});
    acceptanceFixture.setDefaultTimeout(options.timeout);
    // This context serves workspace assets and fulfills EVERY Coding API call;
    // the upload below cannot publish a policy on a real project.
    await localAssets(acceptanceFixture);
    const policyDigest = 'a'.repeat(64), candidateDigest = 'c'.repeat(64);
    const policyProject = {id: 'policy-fixture-project', name: 'Acceptance fixture only', version: 2,
      test_command: 'python -m pytest -q', acceptance_suite_id: 'suite-current'};
    const policy = {id: 'suite-current', project_id: policyProject.id, name: 'Fixture policy', digest: policyDigest,
      minimum_tests: 2, created_at: '2026-10-23T08:00:00Z'};
    const proof = {passed: true, suite_id: policy.id, policy_digest: policyDigest, candidate_digest: candidateDigest,
      tests: {collected: 2, passed: 2, failed: 0, errors: 0, skipped: 0, xfailed: 0, xpassed: 0}, violations: []};
    const policyTask = {id: 'policy-valid-proof', project_id: policyProject.id, project_name: policyProject.name, project_version: 2,
      engine: 'forge', status: 'awaiting_review', checkpoint_revision: 8, workspace_ready: true,
      prompt: 'Fixture with matching acceptance proof', created_at: policy.created_at, timeout_seconds: 900,
      test_command: policyProject.test_command, usage: {}, diff: '+answer = 42', summary: 'Fixture only',
      acceptance_suite_id: policy.id, acceptance_suite_digest: policyDigest,
      baseline_test: {exit_code: 1, stdout: 'Fixture baseline failure'},
      verification: {exit_code: 0, stdout: 'Fixture final result', acceptance: proof}};
    const policyTasks = [
      {...policyTask, id: 'policy-missing', prompt: 'Fixture without an acceptance suite', acceptance_suite_id: null,
        acceptance_suite_digest: null, verification: {exit_code: 0, stdout: 'Legacy command passed'}},
      {...policyTask, id: 'policy-old', prompt: 'Fixture pinned to a replaced policy', acceptance_suite_id: 'suite-old',
        acceptance_suite_digest: 'b'.repeat(64), verification: {exit_code: 0, stdout: 'Old policy passed',
          acceptance: {...proof, suite_id: 'suite-old', policy_digest: 'b'.repeat(64)}}},
      {...policyTask, id: 'policy-proof-mismatch', prompt: 'Fixture with mismatched proof digest',
        verification: {exit_code: 0, stdout: 'Mismatched proof', acceptance: {...proof, policy_digest: 'f'.repeat(64)}}},
      policyTask,
    ];
    const policySubmissions = [], fixtureRejections = [];
    await acceptanceFixture.route('**/api/coding/**', async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname.replace('/api/coding', '');
      let body;
      if (request.method() === 'POST' && pathname === `/projects/${policyProject.id}/acceptance-suites`) {
        policySubmissions.push(request.postDataJSON()); body = policy;
      } else if (request.method() !== 'GET') {
        fixtureRejections.push({method: request.method(), path: pathname});
        return route.fulfill({status: 405, json: {detail: 'Unexpected fixture mutation'}});
      } else if (pathname === '/status') body = {executor_online: true, executors: [{engine: 'forge', capacity: 2}],
        capacity: {queue_limit: 100, sandbox_commands: 2}, counts: {awaiting_review: policyTasks.length}};
      else if (pathname === '/projects') body = {projects: [policyProject]};
      else if (pathname === '/tasks') body = {tasks: policyTasks};
      else if (pathname === `/projects/${policyProject.id}/acceptance-suites`) body = {suites: [policy]};
      else if (pathname.endsWith('/events')) body = {events: []};
      else if (pathname.endsWith('/tool-operations')) body = {operations: []};
      else body = policyTasks.find(task => pathname === `/tasks/${task.id}`);
      await route.fulfill({status: body ? 200 : 404, json: body || {detail: 'Unknown acceptance fixture resource'}});
    });
    const policyScreen = await acceptanceFixture.newPage();
    policyScreen.on('pageerror', error => report.browser_errors.push({surface: 'isolated-acceptance-fixture', error: scrub(error.message)}));
    try {
      await signIn(policyScreen);
      await check('isolated_mock_acceptance_upload_request', async () => {
        await policyScreen.locator('#project-select').selectOption(policyProject.id);
        await policyScreen.locator('#acceptance-open').click();
        await policyScreen.locator('#acceptance-dialog').waitFor({state: 'visible'});
        await policyScreen.locator('#acceptance-name').fill('  UI fixture regression  ');
        const files = {'test_contract.py': 'def test_contract():\n    assert 2 + 2 == 4\n',
          'test_other.py': 'def test_other():\n    assert True\n'};
        await policyScreen.locator('#acceptance-files').setInputFiles(Object.entries(files).map(([name, content]) =>
          ({name, mimeType: 'text/x-python', buffer: Buffer.from(content, 'utf8')})));
        await policyScreen.locator('#acceptance-protected').fill(' tests/test_existing.py \n\n pyproject.toml ');
        await policyScreen.locator('#acceptance-python-paths').fill(' ., src,  ');
        await policyScreen.locator('#acceptance-min-tests').fill('2');
        const submitted = policyScreen.waitForResponse(response => response.request().method() === 'POST'
          && new URL(response.url()).pathname === `/api/coding/projects/${policyProject.id}/acceptance-suites`);
        await policyScreen.locator('#acceptance-submit').click();
        assert.equal((await submitted).status(), 200);
        await policyScreen.locator('#acceptance-dialog').waitFor({state: 'hidden'});
        assert.deepEqual(policySubmissions, [{name: 'UI fixture regression', files, test_paths: Object.keys(files),
          protected_paths: ['tests/test_existing.py', 'pyproject.toml'], python_paths: ['.', 'src'], minimum_tests: 2, timeout_seconds: 120}]);
        assert.equal(await policyScreen.locator('#acceptance-files').inputValue(), '');
        return {transport: 'all Coding API calls fulfilled locally', submitted_files: 2, live_mutations: 0};
      });
      await check('isolated_mock_acceptance_missing_or_replaced_policy_blocks_accept', async () => {
        for (const id of ['policy-missing', 'policy-old', 'policy-proof-mismatch']) {
          await openTask(policyScreen, id, policyTasks);
          assert.equal(await policyScreen.locator('#accept-task').isVisible(), true);
          assert.equal(await policyScreen.locator('#accept-task').isEnabled(), false, `Accept enabled for ${id}`);
          assert((await policyScreen.locator('#accept-task').getAttribute('title')).includes('验收'));
          await policyScreen.locator('#detail-close').click();
        }
        return {missing_suite_disabled: true, replaced_policy_disabled: true, mismatched_proof_disabled: true};
      });
      await check('isolated_mock_acceptance_matching_proof_enables_accept', async () => {
        await openTask(policyScreen, policyTask.id, policyTasks);
        assert.equal(await policyScreen.locator('#accept-task').isVisible(), true);
        assert.equal(await policyScreen.locator('#accept-task').isEnabled(), true);
        // Do not click Accept: enabling is a UI assertion, not backend proof.
        return {enabled: true, accepted: false, backend_verification_not_bypassed: true};
      });
      await check('isolated_mock_acceptance_tests_show_counts_and_digests', async () => {
        await policyScreen.locator('[data-tab="tests"]').click();
        const panel = await policyScreen.locator('#detail-content').innerText();
        assert(panel.includes(policy.id) && panel.includes(policyDigest));
        assert.equal(await policyScreen.locator('.test-card').count(), 2);
        const proofText = await policyScreen.locator('.test-card').nth(1).locator('pre').last().innerText();
        const shown = JSON.parse(proofText);
        assert.deepEqual(shown.tests, proof.tests);
        assert.deepEqual(shown.violations, []);
        assert.equal(shown.candidate_digest, candidateDigest);
        assert((await policyScreen.locator('.test-card').nth(1).innerText()).includes('受保护文件校验通过'));
        assert.deepEqual(fixtureRejections, []);
        return {counts: shown.tests, full_policy_digest_visible: true, full_candidate_digest_visible: true};
      });
    } finally { await acceptanceFixture.close(); }

    await check('no_mutations_or_javascript_errors', async () => {
      assert.equal(report.blocked_mutations.length, 0, 'UI attempted a mutation during read-only testing');
      assert.equal(report.browser_errors.length, 0, 'Browser JavaScript exceptions were recorded');
      assert.equal(report.failed_api_responses.length, 0, 'UI API requests failed');
    });
  } catch (error) {
    report.error = scrub(error.message).slice(0, 5000);
  } finally {
    if (browser) await browser.close();
    report.passed = !report.error && report.checks.every((item) => item.passed)
      && report.browser_errors.length === 0 && report.blocked_mutations.length === 0;
    const target = path.join(options.output, 'studio-check.json');
    fs.writeFileSync(target, scrub(JSON.stringify(report, null, 2)) + '\n', 'utf8');
    // Only a small summary goes to the terminal; detailed, scrubbed errors stay
    // in the report. Do not enable Playwright tracing or HTTP header logging.
    process.stdout.write(JSON.stringify({ passed: report.passed, checks: report.checks.length,
      failed_checks: report.checks.filter((item) => !item.passed).map((item) => item.name),
      javascript_errors: report.browser_errors.length, console_errors: report.console_errors.length,
      screenshots: report.screenshots, report: target }) + '\n');
    process.exitCode = report.passed ? 0 : 1;
  }
}

main().catch(() => {
  // Early setup failures may involve parsing .env; do not print their input.
  process.stderr.write('Studio UI check could not initialize. Verify .env, bundled Playwright, and Edge.\n');
  process.exitCode = 1;
});

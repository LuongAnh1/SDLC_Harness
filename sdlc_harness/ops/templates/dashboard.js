const ACTION_LABELS = {
  answer_clarification: "Trả lời câu hỏi",
  approve_checkpoint: "Duyệt checkpoint",
  continue_run: "Chạy tiếp",
  sync_run: "Đồng bộ lại",
  revalidate_design: "Kiểm tra lại design",
  restart_design: "Chạy lại design từ Requirement",
  revalidate_implementation_plan: "Kiểm tra lại plan",
  revalidate_implementation: "Kiểm tra lại implementation",
  adopt_run_workspace: "Nhận workspace",
  cleanup_run_workspace: "Dọn workspace",
};

const browserSession = typeof window !== "undefined" ? window.sessionStorage : {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};

const state = {
  health: null,
  schema: null,
  snapshot: null,
  config: null,
  configSection: "api",
  configDraft: {},
  configRestartKeys: [],
  selectedRun: null,
  operations: [],
  events: [],
  eventCursor: 0,
  eventHasMore: false,
  detailTab: "overview",
  search: "",
  statusFilter: "all",
  sourceType: "jira",
  activeOperationId: browserSession.getItem("sdlc.activeOperation") || "",
  retryWrite: null,
  loadingRunId: "",
  lastSnapshotAt: 0,
  lastRunAt: 0,
};

const byId = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const formatTime = (value) => value ? new Date(value).toLocaleString("vi-VN", { hour12: false }) : "—";
const terminalOperation = (status) => ["succeeded", "failed", "interrupted"].includes(status);

export function stageForRun(run, schema = null) {
  const phase = String(run?.current_phase || "").toLowerCase();
  const status = String(run?.status || "").toLowerCase();
  const workflow = schema?.workflow || {};
  if (workflow.status_nodes?.[status]) return workflow.status_nodes[status];
  if (workflow.phase_nodes?.[phase]) return workflow.phase_nodes[phase];
  if (phase.startsWith("code_review") || status.startsWith("code_review")) return "review";
  if (phase.startsWith("qa_") || status.startsWith("qa_") || status === "implementation_traced") return "qa";
  if (status === "implementation_tested" || status.includes("validation")) return "validator";
  if (phase === "implementation_testing" || status.includes("testing") || status === "implementation_written") return "verification";
  if (phase.startsWith("implementation_plan") || phase === "implementation_planning") return "plan";
  if (phase.startsWith("implementation_") || phase === "qa_repair") return "developer";
  if (phase.startsWith("design_") || phase === "design_review") return "design";
  return "requirement";
}

export function toneForStatus(status, lastError = null) {
  const value = String(status || "").toLowerCase();
  if (lastError || /failed|blocked|exceeded|error|rejected/.test(value)) return "bad";
  if (/waiting|clarification|approval|paused|repair_needed/.test(value)) return "warn";
  if (/approved|passed|success|done|traced|tested/.test(value)) return "ok";
  return "live";
}

export function visibleActions(run, schema, busy = false) {
  if (busy || !run || !schema) return [];
  const supported = schema.actions || {};
  return (run.allowed_next_actions || []).filter((action) => action !== "get_status" && supported[action]);
}

export function configFieldDisabled(field) {
  return !field || field.editable !== true;
}

export function configUpdateValues(sections, draft) {
  const updates = {};
  for (const section of sections || []) {
    for (const field of section.fields || []) {
      const name = String(field.name || "");
      if (!name || configFieldDisabled(field) || !Object.hasOwn(draft || {}, name)) continue;
      const rawValue = draft[name];
      if (field.secret && (rawValue === "" || rawValue === "********")) continue;
      let value = rawValue;
      if (field.kind === "boolean") value = Boolean(rawValue);
      else if (field.kind === "integer" && rawValue !== "") value = Number(rawValue);
      else if (typeof rawValue === "string") value = rawValue.trim();
      const current = field.value;
      const unchanged = field.kind === "boolean"
        ? Boolean(current) === value
        : field.kind === "integer"
          ? Number(current) === value
          : String(current ?? "") === String(value ?? "");
      if (!unchanged || field.secret) updates[name] = value;
    }
  }
  return updates;
}

export function timelineEntries(events, operations) {
  const eventEntries = (events || []).map((event) => ({
    kind: "event",
    id: `event-${event.sequence}`,
    timestamp: event.timestamp,
    title: event.message || event.type || "Event",
    status: event.status,
    meta: `${event.category || "event"} · ${event.type || "unknown"}`,
    data: event.data || {},
  }));
  const operationEntries = (operations || []).map((operation) => ({
    kind: "operation",
    id: operation.operation_id,
    timestamp: operation.finished_at || operation.started_at || operation.created_at,
    title: ACTION_LABELS[operation.command] || operation.command || "Operation",
    status: operation.operation_status,
    meta: operation.operation_id,
    data: { requested_by: operation.requested_by, sync_mode: operation.sync_mode, error_code: operation.error_code },
  }));
  return [...eventEntries, ...operationEntries].sort((a, b) => String(b.timestamp || "").localeCompare(String(a.timestamp || "")));
}

export function repairSummary(run, events, schema) {
  const defaults = schema?.workflow?.repair_defaults || {};
  const latestLimits = {};
  for (const event of events || []) {
    if (event.type !== "repair_iteration_started") continue;
    const phase = String(event.data?.repair_phase || "");
    const rawLimit = event.data?.limit;
    if (!phase || rawLimit === undefined || rawLimit === null || rawLimit === "") continue;
    const limit = Number(rawLimit);
    if (Number.isFinite(limit) && limit >= 0) latestLimits[phase] = limit;
  }
  const quality = run?.quality_reports || {};
  return [
    { id: "design", label: "Design", used: Number(quality.design?.repair_iterations ?? quality.design_validation?.repair_iterations ?? 0), limit: latestLimits.design ?? defaults.design ?? 0 },
    { id: "plan", label: "Implementation Plan", used: Number(quality.implementation_plan?.repair_iterations ?? quality.implementation_plan_review?.repair_iterations ?? 0), limit: latestLimits.implementation_plan ?? defaults.plan ?? 0 },
    { id: "implementation", label: "Implementation", used: Number(run?.implementation_repair_iterations ?? quality.implementation_repair?.repair_iterations ?? 0), limit: latestLimits.implementation ?? defaults.implementation ?? 0 },
    { id: "qa", label: "QA", used: Number(run?.qa_repair_iterations ?? quality.qa?.repair_iterations ?? 0), limit: latestLimits.qa ?? defaults.qa ?? 0 },
  ];
}

class ApiError extends Error {
  constructor(status, payload) {
    super(payload?.message || `HTTP ${status}`);
    this.status = status;
    this.payload = payload || {};
  }
}

function authHeaders() {
  const token = browserSession.getItem("sdlc.apiToken") || "";
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...authHeaders(), ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch (error) {
    throw new ApiError(0, { error_code: "network_error", message: "Không kết nối được tới SDLC API." });
  }
  const payload = await response.json().catch(() => ({ ok: false, error_code: "invalid_response", message: "API trả response không hợp lệ." }));
  if (!response.ok) {
    if (response.status === 401) openAuth();
    throw new ApiError(response.status, payload);
  }
  return payload;
}

function showNotice(message, tone = "", retry = null) {
  const notice = byId("notice");
  notice.hidden = false;
  notice.className = `notice ${tone}`.trim();
  notice.replaceChildren(document.createTextNode(message));
  if (retry) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "quiet";
    button.textContent = "Thử lại an toàn";
    button.addEventListener("click", retry, { once: true });
    notice.append(" ", button);
  }
}

function clearNotice() { byId("notice").hidden = true; }

function openAuth() {
  byId("token-input").value = browserSession.getItem("sdlc.apiToken") || "";
  if (!byId("auth-dialog").open) byId("auth-dialog").showModal();
}

function currentRoute() {
  const value = (location.hash || "#/runs").slice(2);
  const [name, id] = value.split("/");
  return { name: name || "runs", id: id || "" };
}

function setRouteChrome() {
  const route = currentRoute();
  document.querySelectorAll("[data-route]").forEach((link) => link.classList.toggle("active", link.dataset.route === route.name));
  byId("new-run-button").hidden = route.name === "config";
  if (route.name === "config") {
    byId("page-title").textContent = "Cấu hình";
    byId("page-subtitle").textContent = "Điều chỉnh runtime theo schema; secret luôn được che và OS env có quyền ưu tiên cao hơn.";
  } else if (route.name === "timeline") {
    byId("page-title").textContent = "Hoạt động";
    byId("page-subtitle").textContent = "Operation gần đây trên toàn bộ workflow run.";
  } else if (route.id) {
    byId("page-title").textContent = route.id;
    byId("page-subtitle").textContent = "State, checkpoint, artifact và bằng chứng thực thi của run.";
  } else {
    byId("page-title").textContent = "Runs";
    byId("page-subtitle").textContent = "Theo dõi checkpoint, chất lượng và vòng lặp sửa lỗi.";
  }
}

function renderMetrics() {
  const strip = byId("summary-strip");
  if (currentRoute().name === "config") {
    strip.hidden = true;
    return;
  }
  strip.hidden = false;
  const runs = state.snapshot?.runs || [];
  const operations = state.snapshot?.operations || [];
  const metrics = [
    ["Runs", runs.length, "runtime hiện có"],
    ["Đang chạy", operations.filter((item) => ["queued", "running"].includes(item.operation_status)).length, "operation"],
    ["Chờ duyệt", runs.filter((item) => item.pending_checkpoint).length, "checkpoint"],
    ["Bị chặn", runs.filter((item) => toneForStatus(item.status, item.last_error) === "bad").length, "cần xử lý"],
  ];
  byId("summary-strip").innerHTML = metrics.map(([label, value, note]) => `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><div class="meta">${escapeHtml(note)}</div></div>`).join("");
  byId("run-count").textContent = String(runs.length);
  byId("active-count").textContent = String(metrics[1][1] || "");
}

function render() {
  setRouteChrome();
  renderMetrics();
  const route = currentRoute();
  if (!state.snapshot || !state.schema) {
    byId("app").innerHTML = '<div class="loading"><span class="sr-only">Đang tải</span></div>';
    return;
  }
  if (route.name === "config") renderConfig();
  else if (route.name === "timeline") renderGlobalTimeline();
  else if (route.id) renderRunDetail();
  else renderRuns();
}

function filteredRuns() {
  const search = state.search.trim().toLowerCase();
  return (state.snapshot?.runs || []).filter((run) => {
    const matchesText = !search || [run.run_id, run.source_ref, run.current_phase, run.status].some((value) => String(value || "").toLowerCase().includes(search));
    const matchesStatus = state.statusFilter === "all" || toneForStatus(run.status, run.last_error) === state.statusFilter;
    return matchesText && matchesStatus;
  });
}

function runRow(run) {
  const tone = toneForStatus(run.status, run.last_error);
  return `<button class="run-row" type="button" data-open-run="${escapeHtml(run.run_id)}">
    <div class="row-head"><span class="run-id">${escapeHtml(run.run_id)}</span><span class="badge ${tone}">${escapeHtml(run.status || "unknown")}</span></div>
    <div class="source-ref">${escapeHtml(run.source_ref || "Chưa có source")}</div>
    <div class="meta">${escapeHtml(run.current_phase || "—")} · ${escapeHtml(formatTime(run.updated_at))}</div>
  </button>`;
}

function renderRuns() {
  const runs = filteredRuns();
  byId("app").innerHTML = `
    <div class="toolbar">
      <input id="run-search" type="search" value="${escapeHtml(state.search)}" placeholder="Tìm run, source, phase hoặc status" aria-label="Tìm run">
      <select id="status-filter" aria-label="Lọc trạng thái">
        <option value="all">Tất cả trạng thái</option><option value="live">Đang chạy</option><option value="warn">Chờ xử lý</option><option value="bad">Bị chặn</option><option value="ok">Đã qua</option>
      </select>
    </div>
    ${runs.length ? `<div class="run-list">${runs.map(runRow).join("")}</div>` : '<div class="empty">Chưa có run phù hợp. Anh có thể tạo run từ Jira hoặc dataset.</div>'}`;
  byId("status-filter").value = state.statusFilter;
}

export function workflowNodeStates(run, schema = null) {
  const nodes = schema?.workflow?.nodes || [];
  const current = stageForRun(run, schema);
  const currentIndex = Math.max(0, nodes.findIndex((node) => node.id === current));
  const tone = toneForStatus(run?.status, run?.last_error);
  const hasCheckpoint = Boolean(run?.pending_checkpoint);
  const presentations = nodes.map((node, index) => {
    let nodeState = index < currentIndex ? "done" : index === currentIndex ? "current" : "future";
    if (index === currentIndex && tone === "bad") nodeState = "blocked";
    else if (index === currentIndex && hasCheckpoint) nodeState = "checkpoint";
    const caption = index < currentIndex ? "Đã qua" : index === currentIndex ? String(run?.status || "Hiện tại") : "Chưa chạy";
    return { node, nodeState, caption };
  });

  const reports = run?.quality_reports || {};
  const applyEvidence = (nodeId, report, passedStatuses) => {
    if (!report || typeof report !== "object") return;
    const status = String(report.status || "").toLowerCase();
    const hasEvidence = Object.hasOwn(report, "ok") || Boolean(status);
    if (!hasEvidence) return;
    const item = presentations.find(({ node }) => node.id === nodeId);
    if (!item) return;
    const passed = report.ok === true || passedStatuses.includes(status);
    item.nodeState = passed ? "done" : "blocked";
    item.caption = passed ? "Đã qua" : (status || "failed");
  };
  applyEvidence("verification", reports.verification, ["passed", "success", "succeeded"]);
  applyEvidence("validator", reports.implementation_traceability, ["passed", "valid", "success"]);
  applyEvidence("qa", reports.qa_acceptance, ["passed", "pass", "success"]);
  return presentations;
}

function workflowGraph(run) {
  const presentations = workflowNodeStates(run, state.schema);
  return `<div class="workflow-wrap"><div class="workflow">${presentations.map(({ node, nodeState, caption }) => (
    `<div class="workflow-node ${nodeState}"><strong>${escapeHtml(node.label)}</strong><small>${escapeHtml(caption)}</small></div>`
  )).join("")}</div></div>`;
}

function detailOverview(run) {
  const repairs = repairSummary(run, state.events, state.schema);
  const actions = visibleActions(run, state.schema, Boolean(state.activeOperationId));
  const error = run.last_error ? `<div class="error-box"><strong>${escapeHtml(run.last_error.error_code || "error")}</strong><div>${escapeHtml(run.last_error.message || "Run đang bị chặn.")}</div></div>` : "";
  const checkpoint = run.pending_checkpoint ? `<div class="checkpoint-box"><strong>${escapeHtml(run.pending_checkpoint.type || "checkpoint")}</strong><div class="meta">${escapeHtml(run.pending_checkpoint.checkpoint_id || "")}</div></div>` : "";
  return `
    <section class="panel"><div class="panel-head"><h2>Trạng thái workflow</h2><span class="badge ${toneForStatus(run.status, run.last_error)}">${escapeHtml(run.status)}</span></div><div class="panel-body">
      <div class="split"><div><div class="source-ref">${escapeHtml(run.source_ref || "—")}</div><div class="meta">Cập nhật ${escapeHtml(formatTime(run.updated_at))}</div></div><button class="quiet" type="button" data-copy="${escapeHtml(run.run_id)}">Copy run ID</button></div>
      <p class="status-copy">${escapeHtml(run.summary_for_user || "Chưa có mô tả trạng thái.")}</p>${checkpoint}${error}
    </div></section>
    <section class="panel"><div class="panel-head"><h2>Luồng Phase 1–4</h2><span class="meta">current: ${escapeHtml(stageForRun(run, state.schema))}</span></div><div class="panel-body">${workflowGraph(run)}
      <div class="repair-grid">${repairs.map((item) => `<div class="repair-item"><span class="meta">${escapeHtml(item.label)}</span><strong>${escapeHtml(item.used)} / ${escapeHtml(item.limit)}</strong><span class="meta">iteration / limit</span></div>`).join("")}</div>
    </div></section>
    <section class="panel"><div class="panel-head"><h2>Action khả dụng</h2><span class="meta">sync mặc định: mock</span></div><div class="panel-body"><div class="action-row">${actions.length ? actions.map((action) => `<button type="button" data-run-action="${escapeHtml(action)}" class="${action === "approve_checkpoint" ? "primary" : action === "cleanup_run_workspace" ? "danger" : ""}">${escapeHtml(ACTION_LABELS[action] || action)}</button>`).join("") : '<span class="meta">Không có action API khả dụng ở trạng thái này.</span>'}</div></div></section>`;
}

function detailArtifacts(run) {
  const artifacts = Object.entries(run.latest_artifacts || {});
  const quality = Object.entries(run.quality_reports || {});
  return `<section class="panel"><div class="panel-head"><h2>Artifacts công khai</h2><span class="meta">${artifacts.length} artifact</span></div><div class="panel-body">
    ${artifacts.length ? `<div class="artifact-grid">${artifacts.map(([key, item]) => `<div class="artifact"><strong>${escapeHtml(key)}</strong><span class="meta">${escapeHtml(item.media_type)} · ${escapeHtml(item.size_bytes)} bytes</span><button type="button" class="quiet" data-artifact-url="${escapeHtml(item.url)}" data-artifact-key="${escapeHtml(key)}">Xem nội dung</button></div>`).join("")}</div>` : '<div class="empty">Run chưa công bố artifact nào có thể xem.</div>'}
  </div></section>
  <section class="panel"><div class="panel-head"><h2>Quality reports</h2><span class="meta">public projection</span></div><div class="panel-body">
    ${quality.length ? `<div class="quality-grid">${quality.map(([key, value]) => `<div class="artifact"><strong>${escapeHtml(key)}</strong><pre class="timeline-data">${escapeHtml(JSON.stringify(value, null, 2).slice(0, 2200))}</pre></div>`).join("")}</div>` : '<div class="empty">Chưa có quality report.</div>'}
  </div></section>`;
}

function timelineMarkup(entries) {
  if (!entries.length) return '<div class="empty">Chưa có operation hoặc event.</div>';
  return `<div class="timeline">${entries.map((entry) => {
    const tone = toneForStatus(entry.status, /failed|interrupted/.test(String(entry.status)) ? {} : null);
    return `<div class="timeline-item"><span class="timeline-dot ${tone}"></span><div class="timeline-copy"><div class="split"><strong>${escapeHtml(entry.title)}</strong><span class="meta">${escapeHtml(formatTime(entry.timestamp))}</span></div><div class="meta">${escapeHtml(entry.meta)}</div><div class="timeline-data">${escapeHtml(JSON.stringify(entry.data || {}, null, 2))}</div></div></div>`;
  }).join("")}</div>`;
}

function detailTimeline() {
  const entries = timelineEntries(state.events, state.operations);
  return `<section class="panel"><div class="panel-head"><h2>Operation và event</h2><span class="meta">cursor ${escapeHtml(state.eventCursor)}</span></div><div class="panel-body">${timelineMarkup(entries)}${state.eventHasMore ? '<button type="button" class="quiet" id="load-events">Tải thêm events</button>' : ""}</div></section>`;
}

function renderRunDetail() {
  const route = currentRoute();
  if (!state.selectedRun || state.selectedRun.run_id !== route.id) {
    byId("app").innerHTML = '<div class="loading"><span class="sr-only">Đang tải run</span></div>';
    if (state.loadingRunId !== route.id) loadRun(route.id);
    return;
  }
  const run = state.selectedRun;
  byId("app").innerHTML = `<div class="tabs" role="tablist"><button type="button" data-detail-tab="overview" class="${state.detailTab === "overview" ? "active" : ""}">Tổng quan</button><button type="button" data-detail-tab="artifacts" class="${state.detailTab === "artifacts" ? "active" : ""}">Artifacts & Quality</button><button type="button" data-detail-tab="timeline" class="${state.detailTab === "timeline" ? "active" : ""}">Timeline</button></div><div>${state.detailTab === "artifacts" ? detailArtifacts(run) : state.detailTab === "timeline" ? detailTimeline() : detailOverview(run)}</div>`;
}

function configSourceLabel(source) {
  return ({ default: "mặc định", ".env": ".env", runtime: "runtime", os: "OS env", override: "invocation" })[source] || source || "unknown";
}

function configDraftValue(field) {
  if (Object.hasOwn(state.configDraft, field.name)) return state.configDraft[field.name];
  if (field.secret) return "";
  return field.value ?? "";
}

function configControl(field, busy) {
  const disabled = configFieldDisabled(field) || busy;
  const disabledAttr = disabled ? " disabled" : "";
  const id = `config-${field.name}`;
  const value = configDraftValue(field);
  if (field.kind === "enum") {
    return `<select id="${escapeHtml(id)}" data-config-input="${escapeHtml(field.name)}"${disabledAttr}>${(field.options || []).map((option) => `<option value="${escapeHtml(option)}"${String(value) === String(option) ? " selected" : ""}>${escapeHtml(option)}</option>`).join("")}</select>`;
  }
  if (field.kind === "boolean") {
    return `<label class="config-switch" for="${escapeHtml(id)}"><input id="${escapeHtml(id)}" type="checkbox" data-config-input="${escapeHtml(field.name)}"${Boolean(value) ? " checked" : ""}${disabledAttr}><span>${Boolean(value) ? "Đang bật" : "Đang tắt"}</span></label>`;
  }
  const type = field.kind === "integer" ? "number" : field.secret ? "password" : "text";
  const range = field.kind === "integer" ? `${field.minimum != null ? ` min="${escapeHtml(field.minimum)}"` : ""}${field.maximum != null ? ` max="${escapeHtml(field.maximum)}"` : ""}` : "";
  const placeholder = field.secret && field.is_set ? "Đang có giá trị — để trống để giữ nguyên" : "";
  return `<input id="${escapeHtml(id)}" type="${type}" value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}" autocomplete="off" data-config-input="${escapeHtml(field.name)}"${range}${disabledAttr}>`;
}

function renderConfig() {
  const config = state.config;
  if (!config) {
    byId("app").innerHTML = '<div class="loading"><span class="sr-only">Đang tải cấu hình</span></div>';
    return;
  }
  const sections = config.sections || [];
  if (!sections.some((section) => section.id === state.configSection)) state.configSection = sections[0]?.id || "";
  const section = sections.find((item) => item.id === state.configSection);
  if (!section) {
    byId("app").innerHTML = '<div class="empty">API chưa công bố schema cấu hình.</div>';
    return;
  }
  const queue = state.health?.queue || {};
  const snapshotBusy = (state.snapshot?.operations || []).some((item) => ["queued", "running"].includes(item.operation_status));
  const busy = Boolean(state.activeOperationId) || snapshotBusy || Number(queue.queued || 0) > 0 || Number(queue.running || 0) > 0;
  const restartBanner = state.configRestartKeys.length
    ? `<div class="config-banner"><strong>Cần khởi động lại API</strong><span>Các thay đổi sau chỉ có hiệu lực sau restart: ${escapeHtml(state.configRestartKeys.join(", "))}</span></div>`
    : "";
  const busyBanner = busy
    ? '<div class="config-banner warn"><strong>Đang khóa chỉnh sửa</strong><span>Chờ operation đang xếp hàng hoặc đang chạy hoàn tất rồi lưu lại.</span></div>'
    : "";
  const fields = (section.fields || []).map((field) => {
    const locked = configFieldDisabled(field);
    const lockCopy = field.bootstrap ? "bootstrap · chỉ đọc" : field.source === "os" ? "OS env · đã khóa" : configSourceLabel(field.source);
    return `<div class="config-field">
      <div class="config-field-copy"><label for="config-${escapeHtml(field.name)}">${escapeHtml(field.label)}</label><code>${escapeHtml(field.name)}</code><p>${escapeHtml(field.help || "")}</p></div>
      <div class="config-control">${configControl(field, busy)}<span class="config-source ${locked ? "locked" : ""}">${escapeHtml(lockCopy)}</span></div>
      <button type="button" class="quiet config-reset" data-reset-config-key="${escapeHtml(field.name)}"${!field.resettable || busy ? " disabled" : ""}>Đặt lại</button>
    </div>`;
  }).join("");
  const sectionHasOverride = (section.fields || []).some((field) => field.resettable);
  byId("app").innerHTML = `${restartBanner}${busyBanner}<div class="config-layout">
    <nav class="config-sections" aria-label="Nhóm cấu hình">${sections.map((item) => `<button type="button" data-config-section="${escapeHtml(item.id)}" class="${item.id === section.id ? "active" : ""}"><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml((item.fields || []).length)} trường</span></button>`).join("")}</nav>
    <section class="panel config-panel"><div class="panel-head"><div><h2>${escapeHtml(section.label)}</h2><p>${escapeHtml(section.description || "")}</p></div><span class="meta">${escapeHtml(config.path || ".runtime/config.json")}</span></div>
      <form id="config-form"><div class="config-fields">${fields}</div><div class="config-actions"><button type="button" class="quiet" data-reset-config-section="${escapeHtml(section.id)}"${!sectionHasOverride || busy ? " disabled" : ""}>Đặt lại nhóm</button><button type="submit" class="primary"${busy ? " disabled" : ""}>Lưu thay đổi</button></div></form>
    </section>
  </div>`;
}

async function loadConfig() {
  try {
    state.config = await api("/api/v1/config");
    render();
  } catch (error) {
    showNotice(`${error.payload?.error_code || "config_error"}: ${error.message}`, "error");
  }
}

async function applyConfigMutation(request) {
  try {
    const payload = await request();
    state.config = payload;
    state.configDraft = {};
    const restartKeys = payload.mutation?.restart_keys || [];
    state.configRestartKeys = [...new Set([...state.configRestartKeys, ...restartKeys])];
    state.schema = await api("/api/v1/dashboard/schema");
    showNotice(payload.mutation?.changed_keys?.length ? "Đã cập nhật cấu hình runtime." : "Không có giá trị nào thay đổi.", "success");
    render();
  } catch (error) {
    showNotice(`${error.payload?.error_code || "config_error"}: ${error.message}`, "error");
  }
}

async function saveConfig() {
  const values = configUpdateValues(state.config?.sections || [], state.configDraft);
  if (!Object.keys(values).length) {
    showNotice("Không có giá trị nào thay đổi.", "success");
    return;
  }
  await applyConfigMutation(() => api("/api/v1/config", { method: "PUT", body: JSON.stringify({ values }) }));
}

async function resetConfig({ key = "", section = "" } = {}) {
  const target = key || `nhóm ${section}`;
  if (!window.confirm(`Đặt lại ${target} về nguồn cấu hình có ưu tiên thấp hơn?`)) return;
  const body = key ? { keys: [key] } : { section };
  await applyConfigMutation(() => api("/api/v1/config/reset", { method: "POST", body: JSON.stringify(body) }));
}

function renderGlobalTimeline() {
  const entries = timelineEntries([], state.snapshot?.operations || []);
  byId("app").innerHTML = `<section class="panel"><div class="panel-head"><h2>Operation gần đây</h2><span class="meta">toàn bộ runs</span></div><div class="panel-body">${timelineMarkup(entries)}</div></section>`;
}

async function loadBootstrap() {
  try {
    state.health = await api("/api/v1/health");
    byId("health-dot").className = "health-dot ok";
    byId("health-copy").textContent = "API sẵn sàng";
    const [schema, snapshot, config] = await Promise.all([
      api("/api/v1/dashboard/schema"),
      api("/api/v1/dashboard/snapshot?limit=100"),
      api("/api/v1/config"),
    ]);
    state.schema = schema;
    state.snapshot = snapshot;
    state.config = config;
    state.lastSnapshotAt = Date.now();
    clearNotice();
    render();
    const route = currentRoute();
    if (route.id) await loadRun(route.id);
    if (state.activeOperationId) monitorOperation(state.activeOperationId);
  } catch (error) {
    byId("health-dot").className = "health-dot bad";
    byId("health-copy").textContent = error.status === 401 ? "Cần API token" : "Mất kết nối";
    showNotice(error.message, "error");
    render();
  }
}

async function loadSnapshot() {
  try {
    state.snapshot = await api("/api/v1/dashboard/snapshot?limit=100");
    state.lastSnapshotAt = Date.now();
    render();
  } catch (error) { showNotice(error.message, "error"); }
}

async function loadRun(runId, incremental = false) {
  if (!runId || state.loadingRunId === runId) return;
  state.loadingRunId = runId;
  try {
    const after = incremental && state.selectedRun?.run_id === runId ? state.eventCursor : 0;
    const [run, operations, events] = await Promise.all([
      api(`/api/v1/runs/${encodeURIComponent(runId)}`),
      api(`/api/v1/runs/${encodeURIComponent(runId)}/operations?limit=100`),
      api(`/api/v1/runs/${encodeURIComponent(runId)}/events?after=${after}&limit=200`),
    ]);
    if (currentRoute().id !== runId) return;
    state.selectedRun = run;
    state.operations = operations.operations || [];
    state.events = after ? [...state.events, ...(events.events || [])] : (events.events || []);
    state.eventCursor = Number(events.next_cursor || after);
    state.eventHasMore = Boolean(events.has_more);
    state.lastRunAt = Date.now();
    render();
  } catch (error) {
    showNotice(error.message, "error");
    if (error.status === 404) location.hash = "#/runs";
  } finally {
    if (state.loadingRunId === runId) state.loadingRunId = "";
  }
}

async function loadMoreEvents() {
  const runId = state.selectedRun?.run_id;
  if (!runId) return;
  try {
    const page = await api(`/api/v1/runs/${encodeURIComponent(runId)}/events?after=${state.eventCursor}&limit=200`);
    state.events.push(...(page.events || []));
    state.eventCursor = Number(page.next_cursor || state.eventCursor);
    state.eventHasMore = Boolean(page.has_more);
    render();
  } catch (error) { showNotice(error.message, "error"); }
}

function idempotencyKey(kind) {
  const uuid = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `dashboard-${kind}-${uuid}`;
}

async function submitWrite(path, body, key = idempotencyKey("write")) {
  const attempt = async () => {
    try {
      const receipt = await api(path, { method: "POST", headers: { "Idempotency-Key": key }, body: JSON.stringify(body) });
      state.retryWrite = null;
      state.activeOperationId = receipt.operation_id;
      browserSession.setItem("sdlc.activeOperation", receipt.operation_id);
      showNotice(`Operation ${receipt.operation_id} đã được xếp hàng.`, "success");
      render();
      await monitorOperation(receipt.operation_id);
    } catch (error) {
      if (error.status === 0) {
        state.retryWrite = attempt;
        showNotice("Chưa xác định server đã nhận request hay chưa. Retry sẽ dùng lại Idempotency-Key.", "error", attempt);
      } else showNotice(`${error.payload?.error_code || "request_failed"}: ${error.message}`, "error");
    }
  };
  await attempt();
}

async function monitorOperation(operationId) {
  if (!operationId) return;
  try {
    const operation = await api(`/api/v1/operations/${encodeURIComponent(operationId)}`);
    if (!terminalOperation(operation.operation_status)) {
      setTimeout(() => monitorOperation(operationId), Number(state.schema?.refresh_seconds?.operation || 2) * 1000);
      return;
    }
    state.activeOperationId = "";
    browserSession.removeItem("sdlc.activeOperation");
    const runId = operation.run_id || operation.result?.run_id;
    if (operation.operation_status === "succeeded") {
      showNotice(`Operation ${operationId} đã hoàn tất.`, "success");
      await loadSnapshot();
      if (runId) {
        if (currentRoute().id !== runId) location.hash = `#/runs/${encodeURIComponent(runId)}`;
        else await loadRun(runId);
      }
    } else {
      const error = operation.error || {};
      showNotice(`${error.error_code || operation.operation_status}: ${error.message || "Operation không hoàn tất."}`, "error");
      if (runId) await loadRun(runId);
      await loadSnapshot();
    }
  } catch (error) {
    showNotice(`Không đọc được operation ${operationId}: ${error.message}`, "error");
  } finally { render(); }
}

function setCreateSource(type) {
  state.sourceType = type;
  document.querySelectorAll("[data-source-type]").forEach((button) => button.classList.toggle("active", button.dataset.sourceType === type));
  byId("jira-fields").hidden = type !== "jira";
  byId("dataset-fields").hidden = type !== "dataset";
  byId("create-form").elements.issue_key.required = type === "jira";
  byId("create-form").elements.dataset.required = type === "dataset";
  byId("create-form").elements.use_case.required = type === "dataset";
  updateCreateConfirmation();
}

function createSourceIdentifier() {
  const form = byId("create-form");
  return state.sourceType === "jira" ? form.elements.issue_key.value.trim() : `${form.elements.dataset.value.trim()}:${form.elements.use_case.value.trim()}`;
}

function updateCreateConfirmation() {
  const form = byId("create-form");
  const risky = form.elements.sync_mode.value !== "mock";
  const field = byId("create-confirmation-field");
  field.hidden = !risky;
  field.firstChild.textContent = risky ? `Nhập lại “${createSourceIdentifier() || "source"}” để xác nhận ghi thật` : "Xác nhận source";
  form.elements.confirmation.required = risky;
  if (!risky) form.elements.confirmation.value = "";
}

async function submitCreateForm() {
  const form = byId("create-form");
  if (!form.reportValidity()) return;
  const identifier = createSourceIdentifier();
  if (form.elements.sync_mode.value !== "mock" && form.elements.confirmation.value.trim() !== identifier) {
    showNotice("Chuỗi xác nhận source chưa khớp.", "error");
    return;
  }
  const source = state.sourceType === "jira"
    ? { type: "jira", issue_key: form.elements.issue_key.value.trim() }
    : { type: "dataset", dataset: form.elements.dataset.value.trim(), use_case: form.elements.use_case.value.trim() };
  const body = { source, requested_by: form.elements.requested_by.value.trim(), sync_mode: form.elements.sync_mode.value };
  browserSession.setItem("sdlc.operator", body.requested_by);
  await submitWrite("/api/v1/runs", body, idempotencyKey("create"));
}

function openAction(action) {
  const run = state.selectedRun;
  const spec = state.schema?.actions?.[action];
  if (!run || !spec) return;
  const form = byId("action-form");
  form.dataset.action = action;
  byId("action-title").textContent = ACTION_LABELS[action] || action;
  byId("action-description").textContent = `Run ${run.run_id} · ${run.status}`;
  const question = run.pending_checkpoint?.current_question || {};
  const questionId = question.id || question.question_id || "";
  const questionText = question.question || question.text || question.prompt || "Câu hỏi làm rõ hiện tại";
  byId("action-fields").innerHTML = action === "answer_clarification"
    ? `<div class="checkpoint-box"><strong>${escapeHtml(questionId)}</strong><div>${escapeHtml(questionText)}</div></div><label>Câu trả lời<textarea name="answer" required></textarea></label>`
    : "";
  byId("override-fields").innerHTML = (spec.integer_overrides || []).map((name) => `<label>${escapeHtml(name)}<input type="number" min="${name.includes("trials") || name.includes("budget") ? 1 : 0}" name="${escapeHtml(name)}" placeholder="Giữ mặc định"></label>`).join("");
  byId("override-panel").hidden = !(spec.integer_overrides || []).length;
  form.elements.sync_mode.value = "mock";
  updateActionConfirmation();
  byId("action-dialog").showModal();
}

function updateActionConfirmation() {
  const form = byId("action-form");
  const action = form.dataset.action;
  const confirmation = state.schema?.actions?.[action]?.confirmation || "standard";
  const typed = confirmation === "type_run_id" || form.elements.sync_mode.value !== "mock";
  const field = byId("confirmation-field");
  field.hidden = !typed;
  field.firstChild.textContent = typed ? `Nhập lại “${state.selectedRun?.run_id || "run_id"}” để xác nhận` : "Xác nhận";
  form.elements.confirmation.required = typed;
  if (!typed) form.elements.confirmation.value = "";
}

async function submitActionForm() {
  const form = byId("action-form");
  if (!form.reportValidity()) return;
  const action = form.dataset.action;
  const run = state.selectedRun;
  const spec = state.schema?.actions?.[action];
  if (!run || !spec) return;
  if (!byId("confirmation-field").hidden && form.elements.confirmation.value.trim() !== run.run_id) {
    showNotice("Run ID xác nhận chưa khớp.", "error");
    return;
  }
  const operator = browserSession.getItem("sdlc.operator") || "dashboard:operator";
  const args = {};
  if (action === "answer_clarification") {
    const question = run.pending_checkpoint?.current_question || {};
    args.question_id = String(question.id || question.question_id || "");
    args.answer = form.elements.answer.value.trim();
    args.answered_by = operator;
  }
  if (action === "approve_checkpoint" && run.pending_checkpoint?.checkpoint_id) args.checkpoint_id = run.pending_checkpoint.checkpoint_id;
  for (const name of spec.integer_overrides || []) {
    const value = form.elements[name]?.value;
    if (value !== "" && value != null) args[name] = Number(value);
  }
  await submitWrite(`/api/v1/runs/${encodeURIComponent(run.run_id)}/actions`, {
    action,
    requested_by: operator,
    sync_mode: form.elements.sync_mode.value,
    arguments: args,
  }, idempotencyKey(action));
}

async function openArtifact(url, key) {
  byId("artifact-title").textContent = key;
  byId("artifact-content").textContent = "Đang tải…";
  byId("artifact-dialog").showModal();
  try {
    const payload = await api(url);
    byId("artifact-content").textContent = typeof payload.content === "string" ? payload.content : JSON.stringify(payload.content, null, 2);
  } catch (error) { byId("artifact-content").textContent = `${error.payload?.error_code || "artifact_error"}: ${error.message}`; }
}

if (typeof document !== "undefined") {
document.addEventListener("click", (event) => {
  const target = event.target.closest("button, [data-open-run]");
  if (!target) return;
  if (target.dataset.openRun) location.hash = `#/runs/${encodeURIComponent(target.dataset.openRun)}`;
  if (target.dataset.configSection) { state.configSection = target.dataset.configSection; render(); }
  if (target.dataset.resetConfigKey) resetConfig({ key: target.dataset.resetConfigKey });
  if (target.dataset.resetConfigSection) resetConfig({ section: target.dataset.resetConfigSection });
  if (target.dataset.detailTab) { state.detailTab = target.dataset.detailTab; render(); }
  if (target.dataset.runAction) openAction(target.dataset.runAction);
  if (target.dataset.artifactUrl) openArtifact(target.dataset.artifactUrl, target.dataset.artifactKey);
  if (target.dataset.copy) navigator.clipboard?.writeText(target.dataset.copy).then(() => showNotice("Đã copy run ID.", "success"));
  if (target.id === "load-events") loadMoreEvents();
});

document.addEventListener("input", (event) => {
  if (event.target.id === "run-search") { state.search = event.target.value; renderRuns(); }
  if (event.target.dataset.configInput) {
    state.configDraft[event.target.dataset.configInput] = event.target.type === "checkbox" ? event.target.checked : event.target.value;
    if (event.target.type === "checkbox") event.target.nextElementSibling.textContent = event.target.checked ? "Đang bật" : "Đang tắt";
  }
  if (event.target.closest("#create-form")) updateCreateConfirmation();
});
document.addEventListener("change", (event) => {
  if (event.target.id === "status-filter") { state.statusFilter = event.target.value; renderRuns(); }
  if (event.target.name === "sync_mode" && event.target.closest("#action-form")) updateActionConfirmation();
});

byId("refresh-button").addEventListener("click", async () => {
  state.health = await api("/api/v1/health").catch(() => state.health);
  if (currentRoute().name === "config") {
    await loadConfig();
    return;
  }
  await loadSnapshot();
  if (currentRoute().id) await loadRun(currentRoute().id, true);
});
byId("auth-button").addEventListener("click", openAuth);
byId("new-run-button").addEventListener("click", () => {
  byId("create-form").elements.requested_by.value = browserSession.getItem("sdlc.operator") || "dashboard:operator";
  setCreateSource("jira");
  byId("create-dialog").showModal();
});
document.querySelectorAll("[data-source-type]").forEach((button) => button.addEventListener("click", () => setCreateSource(button.dataset.sourceType)));
byId("forget-token").addEventListener("click", () => { browserSession.removeItem("sdlc.apiToken"); byId("token-input").value = ""; showNotice("Đã xóa token của phiên trình duyệt.", "success"); });
byId("auth-form").addEventListener("close", () => {});
byId("auth-dialog").addEventListener("close", () => {
  if (byId("auth-dialog").returnValue === "save") {
    const token = byId("token-input").value.trim();
    if (token) browserSession.setItem("sdlc.apiToken", token); else browserSession.removeItem("sdlc.apiToken");
    loadBootstrap();
  }
});
document.addEventListener("submit", (event) => {
  if (event.target.id !== "config-form") return;
  event.preventDefault();
  saveConfig();
});
byId("create-form").addEventListener("submit", (event) => {
  if (event.submitter?.value !== "submit") return;
  event.preventDefault();
  const form = byId("create-form");
  const confirmation = form.elements.confirmation;
  const expected = createSourceIdentifier();
  confirmation.setCustomValidity(form.elements.sync_mode.value !== "mock" && confirmation.value.trim() !== expected ? "Chuỗi xác nhận source chưa khớp." : "");
  if (!form.reportValidity()) return;
  byId("create-dialog").close("submitted");
  submitCreateForm();
});
byId("action-form").addEventListener("submit", (event) => {
  if (event.submitter?.value !== "submit") return;
  event.preventDefault();
  const form = byId("action-form");
  const confirmation = form.elements.confirmation;
  const expected = state.selectedRun?.run_id || "";
  confirmation.setCustomValidity(!byId("confirmation-field").hidden && confirmation.value.trim() !== expected ? "Run ID xác nhận chưa khớp." : "");
  if (!form.reportValidity()) return;
  byId("action-dialog").close("submitted");
  submitActionForm();
});

window.addEventListener("hashchange", () => {
  const route = currentRoute();
  state.detailTab = "overview";
  render();
  if (route.name === "config" && !state.config) loadConfig();
  else if (route.id) loadRun(route.id);
});

setInterval(() => {
  if (document.hidden) return;
  const now = Date.now();
  const refresh = state.schema?.refresh_seconds || { snapshot: 5, active_run: 3 };
  if (now - state.lastSnapshotAt >= Number(refresh.snapshot || 5) * 1000) loadSnapshot();
  const route = currentRoute();
  if (route.id && now - state.lastRunAt >= Number(refresh.active_run || 3) * 1000) loadRun(route.id, true);
}, 2000);

loadBootstrap();
}

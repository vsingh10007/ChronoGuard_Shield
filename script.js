"use strict";

// ChronoGuard UI: visualization mirrors a backend simulation persisted in PostgreSQL.
const API_BASE = window.CHRONOGUARD_API_BASE || "http://127.0.0.1:8000";
const $ = (id) => document.getElementById(id);
let graphZoom = 1;
const state = {
  shieldEnabled: true,
  activeSource: "primary",
  recoveryCount: 0,
  incidents: [],
  selected: { type: "node", id: "primary" },
  lastEvent: null,
  nodes: {
    client: { label: "Client / Browser", short: "CLIENT", type: "Request origin / response receiver", healthy: true, available: true, faulty: false, x: 105, y: 208, description: "Sends a form or API request to the server and receives the server response.", data: { requestId: "req-1042", method: "POST", path: "/api/submit", payload: { name: "Rahul", email: "rahul@example.com" }, response: "200 OK" }, action: "Sends request payload and waits for a validated response." },
    app: { label: "Application Server", short: "API SERVER", type: "Request processor", healthy: true, available: true, faulty: false, x: 325, y: 208, description: "Receives client requests, checks fields, runs application logic, and creates a response.", data: { requestId: "req-1042", received: true, requiredFields: ["name", "email"], validation: "passed", processing: "complete" }, action: "Validate input, process the request, and return a response." },
    shield: { label: "ChronoGuard Shield", short: "SHIELD", type: "Protection controller", healthy: true, available: true, faulty: false, x: 535, y: 208, description: "Inspects request data and timestamps, blocks invalid payloads, and coordinates recovery.", data: { mode: "enabled", checks: ["required fields", "timestamp", "exception safety"], decision: "allow" }, action: "Validate data before it reaches dependent processing." },
    primary: { label: "Primary Data Store", short: "PRIMARY DB", type: "Primary server dependency", healthy: true, available: true, faulty: false, x: 770, y: 115, description: "Represents the primary data/time dependency used by the application server.", data: { connection: "connected", transaction: "committed", source: "primary", timestamp: "valid" }, action: "Serve validated reads and writes when healthy." },
    backup: { label: "Backup Service", short: "BACKUP", type: "Fallback dependency", healthy: true, available: true, faulty: false, x: 770, y: 315, description: "Represents the trusted fallback dependency selected if the primary dependency fails.", data: { connection: "ready", mode: "standby", sync: "current" }, action: "Remain ready for protected failover." }
  },
  edges: [
    { id: "client-app", from: "client", to: "app", label: "1 · Client request", path: "M 169 208 C 210 208, 220 208, 261 208" },
    { id: "app-shield", from: "app", to: "shield", label: "2 · Payload / validation", path: "M 389 208 L 471 208" },
    { id: "primary-shield", from: "shield", to: "primary", label: "3 · Primary query", path: "M 599 188 C 650 150, 675 130, 706 120" },
    { id: "backup-shield", from: "shield", to: "backup", label: "4 · Failover path", path: "M 599 228 C 650 270, 675 295, 706 308" },
    { id: "app-client", from: "app", to: "client", label: "5 · Server response", path: "M 261 230 C 220 270, 210 270, 169 230" }
  ],
  stages: ["Monitor", "Detect", "Contain", "Recover", "Verify"],
  stage: "Ready",
  toastTimer: null,
  stopped: false
};

function timeNow() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
function pretty(value) { return JSON.stringify(value, null, 2); }
function healthyCount() { return Object.values(state.nodes).filter(n => n.healthy).length; }
function sourceName(id) { return id === "primary" ? "Primary Data Store" : "Backup Service"; }
function toast(message) {
  const el = $("toast"); el.textContent = message; el.classList.remove("opacity-0", "translate-y-2"); el.classList.add("opacity-100", "translate-y-0");
  clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => { el.classList.add("opacity-0", "translate-y-2"); el.classList.remove("opacity-100", "translate-y-0"); }, 2600);
}
function addIncident(event, source, decision, outcome, level) {
  state.incidents.unshift({ time: timeNow(), event, source, decision, outcome, level });
  state.incidents = state.incidents.slice(0, 50);
}
function setStage(stage, message, status = "neutral") {
  state.stage = stage;
  $("stageCounter").textContent = stage.toUpperCase();
  $("processMessage").textContent = message;
  $("processMessage").className = "mt-3 rounded-lg px-3 py-2.5 text-xs leading-5 " + (status === "bad" ? "border border-red-200 bg-red-50 text-red-800" : status === "good" ? "border border-emerald-200 bg-emerald-50 text-emerald-800" : "bg-slate-100 text-slate-700");
  const idx = state.stages.indexOf(stage);
  $("stageList").innerHTML = state.stages.map((name, i) => {
    const done = idx >= 0 && i < idx;
    const current = name === stage;
    const cls = current && status === "bad" ? "border-red-300 bg-red-50 text-red-800" : current && status === "good" ? "border-emerald-300 bg-emerald-50 text-emerald-800" : current ? "border-blue-300 bg-blue-50 text-blue-800" : done ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-slate-300 bg-slate-50 text-slate-500";
    return `<div class="rounded-lg border p-2 ${cls}"><div class="text-[9px] font-extrabold tracking-wider">${String(i + 1).padStart(2, "0")}</div><div class="mt-1 text-xs font-bold">${name}</div></div>`;
  }).join("");
}
function edgeStatus(edge) {
  const from = state.nodes[edge.from], to = state.nodes[edge.to];
  if (!from.healthy || !to.healthy) return "bad";
  if (edge.from === "primary" && state.activeSource !== "primary") return "neutral";
  if (edge.from === "backup" && state.activeSource !== "backup") return "neutral";
  if (edge.id === "primary-shield" && state.activeSource !== "primary" && state.shieldEnabled) return "neutral";
  if (edge.id === "backup-shield" && state.activeSource !== "backup" && state.shieldEnabled) return "neutral";
  return "good";
}
function renderGraph() {
  const edgesLayer = $("edgesLayer"), nodesLayer = $("nodesLayer");
  edgesLayer.replaceChildren(); nodesLayer.replaceChildren();
  state.edges.forEach(edge => {
    const status = edgeStatus(edge), group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    group.setAttribute("class", `edge ${state.selected.type === "edge" && state.selected.id === edge.id ? "selected" : ""}`);
    group.setAttribute("tabindex", "0"); group.setAttribute("role", "button"); group.setAttribute("aria-label", `${edge.label}, ${status}`);
    const hit = document.createElementNS("http://www.w3.org/2000/svg", "path"); hit.setAttribute("d", edge.path); hit.setAttribute("class", "edge-hit"); hit.setAttribute("fill", "none"); hit.setAttribute("stroke", "transparent"); hit.setAttribute("stroke-width", "18");
    const line = document.createElementNS("http://www.w3.org/2000/svg", "path"); line.setAttribute("d", edge.path); line.setAttribute("class", `edge-line ${status}`); line.setAttribute("marker-end", `url(#arrow${status === "good" ? "Good" : status === "bad" ? "Bad" : "Neutral"})`);
    group.append(hit, line); group.addEventListener("click", () => selectItem("edge", edge.id)); group.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectItem("edge", edge.id); } }); edgesLayer.append(group);
    const midpoint = edge.id === "client-app" ? [215, 190] : edge.id === "app-shield" ? [430, 190] : edge.id === "primary-shield" ? [665, 137] : edge.id === "backup-shield" ? [665, 285] : [215, 255];
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text"); label.setAttribute("x", midpoint[0]); label.setAttribute("y", midpoint[1]); label.setAttribute("text-anchor", "middle"); label.setAttribute("font-size", "10"); label.setAttribute("font-weight", "700"); label.setAttribute("fill", status === "bad" ? "#b91c1c" : status === "good" ? "#166534" : "#64748b"); label.textContent = edge.label; edgesLayer.append(label);
  });
  Object.entries(state.nodes).forEach(([id, node]) => {
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    group.setAttribute("class", `node ${state.selected.type === "node" && state.selected.id === id ? "selected" : ""}`); group.setAttribute("tabindex", "0"); group.setAttribute("role", "button"); group.setAttribute("aria-label", `${node.label}: ${node.healthy ? "healthy" : "faulty"}`);
    const x = node.x - 64, y = node.y - 37;
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect"); rect.setAttribute("x", x); rect.setAttribute("y", y); rect.setAttribute("width", "128"); rect.setAttribute("height", "74"); rect.setAttribute("rx", "12"); rect.setAttribute("stroke-width", "2"); rect.setAttribute("class", `node-card ${!node.healthy ? "node-bad" : id === "shield" && !state.shieldEnabled ? "node-off" : "node-good"}`);
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle"); dot.setAttribute("cx", x + 15); dot.setAttribute("cy", y + 16); dot.setAttribute("r", "4.5"); dot.setAttribute("fill", node.healthy ? "#16a34a" : "#dc2626"); if (!node.healthy) dot.setAttribute("class", "pulse");
    const title = document.createElementNS("http://www.w3.org/2000/svg", "text"); title.setAttribute("x", node.x); title.setAttribute("y", y + 35); title.setAttribute("text-anchor", "middle"); title.setAttribute("font-size", id === "shield" ? "11" : "12"); title.setAttribute("font-weight", "800"); title.setAttribute("fill", "#172033"); title.textContent = node.short;
    const subtitle = document.createElementNS("http://www.w3.org/2000/svg", "text"); subtitle.setAttribute("x", node.x); subtitle.setAttribute("y", y + 53); subtitle.setAttribute("text-anchor", "middle"); subtitle.setAttribute("font-size", "10"); subtitle.setAttribute("fill", node.healthy ? "#166534" : "#b91c1c"); subtitle.textContent = !node.healthy ? "FAULT / BLOCKED" : id === "shield" && !state.shieldEnabled ? "OFF / BYPASSED" : "HEALTHY / VALID"; if (id === "shield" && !state.shieldEnabled && node.healthy) subtitle.setAttribute("fill", "#92400e");
    group.append(rect, dot, title, subtitle); group.addEventListener("click", () => selectItem("node", id)); group.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectItem("node", id); } }); nodesLayer.append(group);
  });
}
function updateDataInspector(type, id) {
  const traceTitle = $("traceTitle"), tracePayload = $("tracePayload"), traceStatus = $("traceStatus"), traceError = $("traceError");
  if (!traceTitle || !tracePayload || !traceStatus || !traceError) return;
  let selectedLabel = "Unknown element", sample = {}, isBad = false, reason = "";
  if (type === "node" && state.nodes[id]) {
    const node = state.nodes[id]; selectedLabel = node.label; sample = node.data; isBad = !node.healthy || !node.available || node.faulty;
    reason = node.data?.error || node.action || "Node is not healthy.";
  } else if (type === "edge") {
    const edge = state.edges.find(e => e.id === id);
    if (edge) { const status = edgeStatus(edge); selectedLabel = edge.label; isBad = status === "bad"; sample = { connection: edge.id, from: state.nodes[edge.from].label, to: state.nodes[edge.to].label, payloadStatus: isBad ? "invalid / blocked" : status === "good" ? "validated" : "standby", transferDirection: `${state.nodes[edge.from].label} → ${state.nodes[edge.to].label}` }; reason = isBad ? "Faulty data or unavailable service detected on this connection; transfer should be blocked." : "Connection is available for simulated transfer."; }
  }
  traceTitle.textContent = `Selected: ${selectedLabel}`;
  tracePayload.textContent = pretty(sample);
  traceStatus.textContent = isBad ? "ERROR / BLOCKED" : "VALID / READY";
  traceStatus.className = `rounded-full px-2.5 py-1 text-[10px] font-extrabold ${isBad ? "bg-red-100 text-red-800" : "bg-emerald-100 text-emerald-800"}`;
  traceError.classList.toggle("hidden", !isBad);
  traceError.textContent = isBad ? `⚠ Problem detected at ${selectedLabel}. ${reason}` : "";
  const clientBad = !state.nodes.client.healthy || !state.nodes.client.available;
  const appBad = !state.nodes.app.healthy || !state.nodes.shield.healthy || !state.shieldEnabled;
  const dbBad = !state.nodes[state.activeSource || "primary"]?.healthy;
  const cardStates = [
    ["traceClient", clientBad, clientBad ? "Error / blocked" : "Request / response ready"],
    ["traceServer", appBad, appBad ? "Validation / protection issue" : "Checks passed"],
    ["traceDatabase", dbBad, dbBad ? "Database unavailable" : `Active: ${sourceName(state.activeSource || "primary")}`]
  ];
  cardStates.forEach(([cardId, bad, label]) => { const card = $(cardId); if (!card) return; card.className = `rounded-lg border p-3 transition-all duration-300 ${bad ? "border-red-400 bg-red-50 shadow-md shadow-red-100" : "border-emerald-300 bg-emerald-50"}`; const p = card.querySelector("p:last-child"); if (p) { p.textContent = label; p.className = `mt-1 text-[10px] ${bad ? "font-extrabold text-red-800" : "text-emerald-800"}`; } });
  const rows = $("sqlRows");
  if (rows) {
    rows.replaceChildren();
    const rowData = { id: 1, request_id: state.lastEvent ? "req-sim-1043" : "req-1042", status: isBad ? "blocked" : "processed", payload: JSON.stringify(sample).slice(0, 90), validation: isBad ? "FAILED" : "passed" };
    const tr = document.createElement("tr"); tr.className = `border-b border-slate-200 ${isBad ? "bg-red-50" : "bg-white"}`;
    [rowData.id, rowData.request_id, rowData.status, rowData.payload, rowData.validation].forEach((value, i) => { const td = document.createElement("td"); td.className = `px-2 py-3 align-top ${i === 4 ? (isBad ? "font-extrabold text-red-700" : "font-extrabold text-emerald-700") : "text-slate-700"}`; td.textContent = String(value); td.title = String(value); tr.append(td); });
    rows.append(tr);
  }
}

function selectItem(type, id) {
  state.selected = { type, id }; const title = $("detailTitle"), subtitle = $("detailSubtitle"), status = $("detailStatus"), data = $("detailData"), action = $("detailAction");
  $("detailKind").textContent = type;
  if (type === "node") {
    const node = state.nodes[id]; title.textContent = node.label; subtitle.textContent = node.description; status.textContent = node.healthy ? "Healthy" : "Faulty / blocked"; status.className = `rounded-md px-2 py-1 text-[10px] font-bold ${node.healthy ? "bg-emerald-100 text-emerald-800" : "bg-red-100 text-red-800"}`; data.textContent = pretty(node.data); action.textContent = node.action;
  } else {
    const edge = state.edges.find(e => e.id === id); const s = edgeStatus(edge); title.textContent = edge.label; subtitle.textContent = `${state.nodes[edge.from].label} → ${state.nodes[edge.to].label}`; status.textContent = s === "good" ? "Valid flow" : s === "bad" ? "Faulty / blocked" : "Standby path"; status.className = `rounded-md px-2 py-1 text-[10px] font-bold ${s === "good" ? "bg-emerald-100 text-emerald-800" : s === "bad" ? "bg-red-100 text-red-800" : "bg-slate-200 text-slate-700"}`; data.textContent = pretty({ connection: id, from: edge.from, to: edge.to, data: s === "good" ? "validated" : s === "bad" ? "invalid or blocked" : "standby", status: s }); action.textContent = s === "good" ? "Connection is carrying validated simulated data." : s === "bad" ? "Invalid data cannot safely pass through this connection." : "Connection is a standby path or is not currently selected.";
  }
  renderGraph();
  updateDataInspector(type, id);
}
function updateDashboard() {
  const count = healthyCount(); if ($("healthyCount")) $("healthyCount").textContent = `${count} / ${Object.keys(state.nodes).length}`; if ($("incidentCount")) $("incidentCount").textContent = state.incidents.length; if ($("recoveryCount")) $("recoveryCount").textContent = state.recoveryCount;
  const noTrusted = !state.activeSource || !state.nodes[state.activeSource].healthy;
  const appHealthy = state.nodes.app.healthy && state.nodes.client.healthy;
  const overallGood = !noTrusted && appHealthy;
  if ($("systemState")) { $("systemState").textContent = overallGood ? (count === 5 ? "Operational" : "Recovered / degraded") : "Service impacted"; $("systemState").className = `mt-2 text-lg font-extrabold ${overallGood ? "text-emerald-700" : "text-red-700"}`; }
  if ($("systemStateHint")) $("systemStateHint").textContent = noTrusted ? "No trusted source selected" : state.activeSource === "backup" ? "Backup source active" : count === 5 ? "Primary source trusted" : "A fault needs attention";
  $("topologyBadge").textContent = !overallGood ? "FAULT PROPAGATION / OUTAGE" : count < 5 || state.activeSource === "backup" ? "DEGRADED / FAILOVER ACTIVE" : "ALL PATHS HEALTHY";
  $("topologyBadge").className = `rounded-full border px-2.5 py-1 text-[10px] font-extrabold ${!overallGood ? "border-red-300 bg-red-50 text-red-800" : count < 5 || state.activeSource === "backup" ? "border-amber-300 bg-amber-50 text-amber-800" : "border-emerald-300 bg-emerald-50 text-emerald-800"}`;
  $("trustedSource").textContent = state.activeSource ? sourceName(state.activeSource) : "None";
  $("validationStatus").textContent = state.activeSource && state.nodes[state.activeSource].healthy ? "Valid" : "Unavailable";
  $("validationStatus").className = `mt-1 text-sm font-bold ${state.activeSource && state.nodes[state.activeSource].healthy ? "text-emerald-700" : "text-red-700"}`;
  $("shieldModeLabel").textContent = state.shieldEnabled ? "Enabled · contains faults" : "Disabled · faults may propagate";
  $("shieldModeLabel").className = `text-[10px] ${state.shieldEnabled ? "text-emerald-700" : "text-red-700"}`;
  $("shieldToggle").setAttribute("aria-checked", String(state.shieldEnabled)); $("shieldToggle").className = `relative h-6 w-11 rounded-full p-0.5 switch-track ${state.shieldEnabled ? "bg-emerald-600" : "bg-red-600"}`; $("shieldThumb").className = `switch-thumb block h-5 w-5 rounded-full bg-white shadow ${state.shieldEnabled ? "translate-x-5" : "translate-x-0"}`;
  renderGraph(); renderIncidents();
  if (state.selected) selectItem(state.selected.type, state.selected.id);
}
function renderIncidents() {
  const root = $("incidentList"); if (!root) return; root.replaceChildren();
  if (!state.incidents.length) { const p = document.createElement("p"); p.className = "px-5 py-8 text-center text-xs text-slate-500"; p.textContent = "No incidents yet. Run a problem event to begin."; root.append(p); return; }
  state.incidents.forEach(item => {
    const row = document.createElement("div"); row.className = "grid gap-2 px-4 py-3 sm:grid-cols-[90px_minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-center sm:px-5";
    const time = document.createElement("span"); time.className = "font-mono text-[10px] text-slate-500"; time.textContent = item.time;
    const info = document.createElement("div"); const event = document.createElement("p"); event.className = "text-xs font-bold text-slate-800"; event.textContent = item.event; const source = document.createElement("p"); source.className = "mt-1 text-[10px] text-slate-500"; source.textContent = item.source; info.append(event, source);
    const action = document.createElement("p"); action.className = "text-xs text-slate-600"; action.textContent = item.decision;
    const badge = document.createElement("span"); badge.className = `w-fit rounded-md px-2 py-1 text-[10px] font-extrabold ${item.level === "good" ? "bg-emerald-100 text-emerald-800" : item.level === "bad" ? "bg-red-100 text-red-800" : "bg-amber-100 text-amber-800"}`; badge.textContent = item.outcome;
    row.append(time, info, action, badge); root.append(row);
  });
}
function markFault(source, eventType) {
  const nodeId = source === "app" ? "app" : source;
  const node = state.nodes[nodeId]; node.healthy = false; node.faulty = true;
  if (eventType === "badTimestamp") node.data = { timestamp: "2099-01-01T00:00:00Z", validation: "FAILED: timestamp too far in future", originalPreserved: true };
  else if (eventType === "sourceDown") { node.available = false; node.data = { source: nodeId, status: "unavailable", response: "timeout" }; }
  else if (eventType === "missingField") node.data = { request: "form-submit-1042", name: "Rahul", email: "MISSING", validation: "FAILED: required field missing" };
  else if (eventType === "exception") node.data = { operation: "process_request()", exception: "ValueError", status: "failed safely" };
  else node.data = { retryCount: 10, retryState: "limit exceeded", status: "circuit opened" };
  node.action = state.shieldEnabled ? "Fault detected and isolated from the trusted path." : "No shield intervention: faulty state may affect dependent services.";
  return nodeId;
}
function resetFaults() {
  for (const [id, node] of Object.entries(state.nodes)) {
    node.healthy = true; node.available = true; node.faulty = false;
    if (id === "primary") { node.data = { connection: "connected", transaction: "committed", source: "primary", timestamp: "valid" }; node.action = "Serve validated reads and writes when healthy."; }
    if (id === "backup") { node.data = { connection: "ready", mode: "standby", sync: "current" }; node.action = "Remain ready for protected failover."; }
    if (id === "shield") { node.data = { mode: state.shieldEnabled ? "enabled" : "disabled", lifecycle: "monitor → detect → contain → recover → verify" }; node.action = state.shieldEnabled ? "Ready to inspect data and prevent invalid data from spreading." : "Protection disabled by operator."; }
    if (id === "app") { node.data = { requestId: "req-1042", received: true, requiredFields: ["name", "email"], validation: "passed", processing: "complete" }; node.action = "Validate input, process the request, and return a response."; }
    if (id === "client") { node.data = { requestId: "req-1042", method: "POST", path: "/api/submit", payload: { name: "Rahul", email: "rahul@example.com" }, response: "200 OK" }; node.action = "Sends request payload and waits for a validated response."; }
  }
  state.activeSource = "primary"; state.lastEvent = null; state.selected = { type: "node", id: "primary" };
}
async function runSimulation() {
  if (state.stopped) { toast("Simulation is stopped. Press Reset to resume."); return; }
  const eventType = $("faultSelect").value; const source = $("sourceSelect").value; const sourceLabel = source === "app" ? "Application Server" : source === "client" ? "Client / Browser" : sourceName(source);
  let backendResult = null;
  try {
    const response = await fetch(`${API_BASE}/api/simulation/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        event_type: eventType,
        source,
        shield_enabled: state.shieldEnabled,
        data: { name: "Demo User", email: "demo@example.com", timestamp: new Date().toISOString() }
      })
    });
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body));
    backendResult = body;
    const message = `PostgreSQL logged ${body.request_id} · ${body.final_status} · active server: ${body.active_server || "none"}`;
    const processMessage = $("processMessage");
    if (processMessage) processMessage.textContent = message;
  } catch (error) {
    toast(`Backend unavailable: ${error.message}. Start FastAPI and PostgreSQL.`);
  }

  // A normal request must not enter the fault-simulation animation.
  if (eventType === "validRequest") {
    if (!backendResult) return;

    resetFaults();

    const successful = backendResult.final_status === "processed";
    const activeServer = backendResult.active_server || "primary";

    state.activeSource = activeServer;
    state.lastEvent = {
      eventType,
      source,
      eventLabel: "Valid request"
    };
    state.selected = { type: "node", id: "app" };

    state.nodes.client.data = {
      requestId: backendResult.request_id,
      method: "POST",
      path: "/api/simulation/run",
      payload: { name: "Demo User", email: "demo@example.com" },
      response: successful ? "200 OK" : "400 Bad Request",
      backendStatus: backendResult.final_status
    };

    state.nodes.app.data = {
      requestId: backendResult.request_id,
      requiredFields: ["name", "email"],
      validation: backendResult.validation_status,
      processing: backendResult.final_status
    };

    state.nodes[activeServer].data = {
      ...state.nodes[activeServer].data,
      backendSelected: true
    };

    setStage(
      "Monitor",
      "Normal request received from the client.",
      "neutral"
    );

    setStage(
      successful ? "Verify" : "Contain",
      successful
        ? "Required fields validated. Request processed and logged to PostgreSQL."
        : "Request rejected because required fields are missing.",
      successful ? "good" : "bad"
    );

    updateDashboard();
    selectItem("node", "app");

    await refreshPostgresRows();

    toast(
      successful
        ? `Valid request saved to PostgreSQL: ${backendResult.request_id}`
        : "Request rejected and recorded."
    );

    return;
  }

  resetFaults();
  const failedId = markFault(source, eventType);
  const eventLabels = { badTimestamp: "Invalid / future timestamp", sourceDown: "Time source unavailable", missingField: "Required field missing", exception: "Application exception", retryLoop: "Retry loop limit reached" };
  const eventLabel = eventLabels[eventType];
  setStage("Monitor", `Monitoring detected a simulated problem: ${eventLabel}.`, "neutral");
  if (!state.shieldEnabled) {
    state.nodes.shield.healthy = false; state.nodes.shield.data = { mode: "disabled", intervention: "none" }; state.nodes.shield.action = "Shield disabled: no containment or failover action taken.";
    if (failedId === "primary" || failedId === "backup") {
      state.activeSource = failedId;
      state.nodes.app.healthy = false; state.nodes.app.faulty = true; state.nodes.app.data = { payload: "untrusted data propagated", source: failedId, validation: "not protected" }; state.nodes.app.action = "Faulty source data reached the dependent application.";
    }
    state.nodes.client.healthy = false; state.nodes.client.faulty = true; state.nodes.client.data = { response: "503 Service Unavailable", cause: eventLabel, protection: "disabled" }; state.nodes.client.action = "Downstream client affected because the shield did not intervene.";
    setStage("Detect", "ChronoGuard is disabled. The injected fault was not contained and may propagate downstream.", "bad");
    addIncident(eventLabel, sourceLabel, "Shield disabled; downstream flow was not protected.", "Fault propagated", "bad");
    toast("Shield disabled: simulated fault propagated.");
  } else {
    state.nodes.shield.healthy = true; state.nodes.shield.data = { mode: "enabled", detectedEvent: eventLabel, decision: "contain" }; state.nodes.shield.action = "Detected the fault and blocked the invalid path.";
    setStage("Detect", `${eventLabel} detected in ${sourceLabel}.`, "bad");
    setStage("Contain", `Isolated ${sourceLabel}; invalid data is blocked from the application.`, "bad");
    const fallback = failedId === "primary" ? "backup" : failedId === "backup" ? "primary" : (state.nodes.primary.healthy ? "primary" : "backup");
    if (state.nodes[fallback].healthy && state.nodes[fallback].available) {
      state.activeSource = fallback; state.nodes[fallback].action = "Selected as the trusted source after validation."; state.nodes[fallback].data.validation = "passed: fallback selected";
      state.nodes.app.healthy = true; state.nodes.app.available = true; state.nodes.app.faulty = false; state.nodes.app.data = { requestId: "req-1042", payload: "accepted", validation: "passed", processing: "complete", dependency: fallback, shield: "protected" }; state.nodes.app.action = `Accepted validated data from ${sourceName(fallback)}.`;
      state.nodes.client.healthy = true; state.nodes.client.data = { requestId: "req-1042", response: "200 OK", payload: "validated", source: fallback }; state.nodes.client.action = "Service remains available using the trusted path.";
      state.recoveryCount += 1;
      setStage("Recover", `Switched to ${sourceName(fallback)} and restored the trusted data path.`, "good");
      setStage("Verify", "Recovery verified: downstream service received validated data.", "good");
      addIncident(eventLabel, sourceLabel, `Blocked fault; switched to ${sourceName(fallback)}.`, "Recovered", "good"); toast(`Recovery verified using ${sourceName(fallback)}.`);
    } else {
      state.activeSource = null; state.nodes.app.healthy = false; state.nodes.client.healthy = false;
      setStage("Recover", "No healthy fallback source is available. Dependent service remains protected but unavailable.", "bad");
      addIncident(eventLabel, sourceLabel, "Fault contained; no trusted source available.", "Contained · outage", "warn"); toast("Fault contained, but no healthy source is available.");
    }
  }
  state.lastEvent = { eventType, source, eventLabel }; state.selected = { type: "node", id: failedId };
  updateDashboard(); selectItem("node", failedId);
  if (backendResult) {
    state.nodes.client.data = { ...state.nodes.client.data, requestId: backendResult.request_id, backendStatus: backendResult.final_status };
    state.nodes.app.data = { ...state.nodes.app.data, backendStage: backendResult.processing_stage, validation: backendResult.validation_status };
    state.nodes[backendResult.active_server || "primary"].data = { ...state.nodes[backendResult.active_server || "primary"].data, backendSelected: true };
    const activeLabel = backendResult.active_server ? (backendResult.active_server === "primary" ? "Primary A" : "Backup B") : "None";
    addIncident(`${eventLabel} · ${backendResult.request_id.slice(0, 8)}`, sourceLabel, `${backendResult.validation_status} / ${backendResult.final_status}`, `PostgreSQL logged · active ${activeLabel}`, backendResult.final_status === "failed" ? "bad" : backendResult.final_status === "rejected" ? "warn" : "good");
    renderIncidents(); updateDashboard(); renderGraph();
    await refreshPostgresRows();
    toast(`Request saved to PostgreSQL: ${backendResult.final_status}`);
  }
}
async function refreshPostgresRows() {
  const root = $("sqlRows");
  if (!root) return;
  try {
    const response = await fetch(`${API_BASE}/api/requests?limit=10`);
    if (!response.ok) throw new Error("Could not read request logs");
    const result = await response.json();
    root.replaceChildren();
    if (!result.items.length) {
      const tr = document.createElement("tr"); const td = document.createElement("td");
      td.colSpan = 5; td.className = "px-2 py-5 text-center text-slate-500"; td.textContent = "No persisted requests yet."; tr.append(td); root.append(tr); return;
    }
    result.items.forEach((item) => {
      const tr = document.createElement("tr"); tr.className = "border-b border-slate-200 bg-white";
      const payloadText = JSON.stringify(item.payload || {}).slice(0, 90);
      [item.id, item.request_id, item.final_status, payloadText, item.validation_status].forEach((value, i) => {
        const td = document.createElement("td"); td.className = `px-2 py-3 align-top ${i === 4 ? "font-extrabold" : "text-slate-700"}`;
        td.textContent = String(value ?? "—"); td.title = String(value ?? "—"); tr.append(td);
      });
      root.append(tr);
    });
    const note = root.closest("div")?.querySelector("p");
    if (note) note.textContent = "Live PostgreSQL data: request_logs table · newest 10 requests.";
  } catch (error) {
    console.warn("ChronoGuard PostgreSQL request log is unavailable:", error);
  }
}

function updateClock() {
  const now = new Date(); $("clock").textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }); $("dateDisplay").textContent = now.toLocaleDateString([], { weekday: "long", year: "numeric", month: "short", day: "numeric" });
}
function applyGraphZoom() {
  const root = $("graphZoomRoot");
  if (root) root.setAttribute("transform", `translate(${450 * (1 - graphZoom)} ${215 * (1 - graphZoom)}) scale(${graphZoom})`);
  const label = $("zoomLabel"); if (label) label.textContent = `${Math.round(graphZoom * 100)}%`;
}
const zoomInButton = $("zoomIn"), zoomOutButton = $("zoomOut"), zoomResetButton = $("zoomReset");
if (zoomInButton) zoomInButton.addEventListener("click", () => { graphZoom = Math.min(1.6, +(graphZoom + 0.1).toFixed(2)); applyGraphZoom(); });
if (zoomOutButton) zoomOutButton.addEventListener("click", () => { graphZoom = Math.max(0.6, +(graphZoom - 0.1).toFixed(2)); applyGraphZoom(); });
if (zoomResetButton) zoomResetButton.addEventListener("click", () => { graphZoom = 1; applyGraphZoom(); });
$("simulateButton").addEventListener("click", runSimulation);
const stopButton = $("stopButton");
if (stopButton) stopButton.addEventListener("click", () => {
  state.stopped = true;
  document.body.classList.add("motion-paused");
  $("simulateButton").disabled = true;
  $("simulateButton").classList.add("opacity-50", "cursor-not-allowed");
  stopButton.textContent = "■ Stopped";
  stopButton.classList.add("bg-red-100", "border-red-400", "text-red-900");
  toast("Simulation stopped. Reset to start again.");
});
$("resetButton").addEventListener("click", () => {
  state.stopped = false; document.body.classList.remove("motion-paused");
  $("simulateButton").disabled = false; $("simulateButton").classList.remove("opacity-50", "cursor-not-allowed");
  if (stopButton) { stopButton.textContent = "Ⅱ Stop"; stopButton.classList.remove("bg-red-100", "border-red-400", "text-red-900"); }
  resetFaults(); state.incidents = []; state.recoveryCount = 0; state.stage = "Ready"; setStage("Ready", "Simulation reset. Both sources and dependent services are healthy.", "good"); updateDashboard(); selectItem("node", "primary"); applyGraphZoom(); refreshPostgresRows(); toast("Simulation reset.");
});
$("shieldToggle").addEventListener("click", () => { state.shieldEnabled = !state.shieldEnabled; state.nodes.shield.data.mode = state.shieldEnabled ? "enabled" : "disabled"; state.nodes.shield.action = state.shieldEnabled ? "Ready to inspect data and prevent invalid data from spreading." : "Protection disabled by operator."; updateDashboard(); selectItem("node", "shield"); toast(state.shieldEnabled ? "ChronoGuard enabled." : "ChronoGuard disabled."); });
if ($("clearLogsButton")) $("clearLogsButton").addEventListener("click", () => { state.incidents = []; renderIncidents(); updateDashboard(); toast("System logs cleared."); });
$("clearSelectionButton").addEventListener("click", () => { state.selected = null; $("detailKind").textContent = "None"; $("detailTitle").textContent = "No selection"; $("detailSubtitle").textContent = "Select a node or connector in the graph."; $("detailStatus").textContent = "—"; $("detailData").textContent = "No data selected"; $("detailAction").textContent = "Select an element to inspect its state."; renderGraph(); });
updateClock(); setInterval(updateClock, 1000); setStage("Ready", "Shield ready. Monitoring both time sources.", "good"); updateDashboard(); selectItem("node", "primary"); applyGraphZoom(); refreshPostgresRows();

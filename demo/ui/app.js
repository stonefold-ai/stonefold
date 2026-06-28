"use strict";
// Thin client for the ACP Accounts-Payable demo: live trace over WebSocket, the
// approvals inbox, the KILL switch, the audit log, and the agent runner buttons.
// Everything is same-origin against the gateway that serves this page.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function getJSON(url) { const r = await fetch(url); return r.json(); }
async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}) });
  return r.json();
}

// --- live trace ------------------------------------------------------------
const badgeClass = (d) => ({ allow: "b-allow", deny: "b-deny", hold: "b-hold",
  halt: "b-halt", bypassed: "b-bypassed" }[d] || "b-deny");

// Which pipeline stage produced this trace entry? The rule encodes it; map it to a
// readable stage + a one-line explanation so the viewer knows where it came from.
// Pipeline order: RESOLVE → KILL(pre) → AUTHORIZE → SCOPE → GATES → KILL → EXECUTE → DISPATCH.
function stageOf(ev) {
  if (ev.type === "effect") return ["DISPATCH", "the connector executed the staged effect — money moved"];
  const rule = (ev.rule || "").toLowerCase();
  const d = (ev.decision || "").toLowerCase();
  if (rule === "no-gateway") return ["BYPASS", "gateway OFF — executed directly, no checks"];
  if (rule === "unknown-action" || rule.indexOf("unknown action") === 0)
    return ["RESOLVE", "the registry doesn't know this resource/action"];
  if (rule === "unknown-principal") return ["IDENTITY", "the actor is not a recognised principal"];
  if (rule === "deny-rule") return ["AUTHORIZE", "an explicit deny rule matched (deny wins)"];
  if (rule === "default-deny") return ["AUTHORIZE", "no allow rule matched → default deny"];
  if (rule.indexOf("scope") === 0) return ["SCOPE", "tenant/ownership scope check, below the model"];
  if (rule.indexOf("gate:") === 0) return ["GATES", "deterministic gate: " + ev.rule.slice(5)];
  if (rule.indexOf("kill") === 0) return ["KILL", "the kill-switch halted this action"];
  if (rule.indexOf("rejected") === 0) return ["APPROVAL", "a human reviewer rejected the held action"];
  if (rule.indexOf("unavailable") >= 0) return ["FAIL-CLOSED", "a dependency was down → fail closed"];
  if (rule === "allow") return [d === "allow" ? "EXECUTE" : "AUTHORIZE",
    "passed authorize + scope + gates → staged/executed"];
  if (d === "hold") return ["GATES", "an approval gate held the action"];
  return ["GATEWAY", ev.rule || ""];
}

function renderEvent(ev) {
  const el = document.createElement("div");
  const [stage, why] = stageOf(ev);
  const chip = `<span class="stage" title="${esc(why)}">${esc(stage)}</span>`;
  if (ev.type === "effect") {
    const p = ev.payment || {};
    el.className = "ev eff";
    el.innerHTML = `<div class="top">${chip}<span class="badge b-effect">money moved</span>
      <strong>${esc(p.amount)} ${esc(p.currency || "USD")}</strong>
      <span class="muted">&rarr; ${esc(p.payee_name || p.payee_id)}</span></div>
      <div class="meta">${esc(why)}</div>
      <div class="meta mono">connector=${esc(ev.connector)} acct=${esc(p.account_id)} id=${esc(p.id)}</div>`;
  } else {
    el.className = "ev";
    const d = (ev.decision || "").toLowerCase();
    const data = ev.data || {};
    const amt = data.amount != null ? `${esc(data.amount)} ${esc(data.currency || "USD")}` : "";
    const tgt = data.payeeId || data.newPayee || "";
    el.innerHTML = `<div class="top">${chip}<span class="badge ${badgeClass(d)}">${esc(d)}</span>
      <strong>${esc(ev.resource)}.${esc(ev.action)}</strong>
      <span class="muted">${amt} ${tgt ? "&rarr; " + esc(tgt) : ""}</span></div>
      <div class="meta">${esc(why)}</div>
      <div class="meta mono">rule: ${esc(ev.rule)}${ev.ticket ? " &middot; ticket: " + esc(ev.ticket) : ""}</div>`;
  }
  $("trace").prepend(el);
}

// Every trace event means the gateway just decided or dispatched something, so the
// approvals inbox may have changed — refresh it automatically (debounced).
let _liveTimer = null;
function scheduleLiveRefresh() {
  clearTimeout(_liveTimer);
  _liveTimer = setTimeout(() => { refreshApprovals(); }, 250);
}

function connectTrace() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/trace`);
  ws.onmessage = (m) => {
    try { renderEvent(JSON.parse(m.data)); scheduleLiveRefresh(); } catch (e) {}
  };
  ws.onclose = () => setTimeout(connectTrace, 1500); // auto-reconnect
}

// --- agent runner ----------------------------------------------------------
function card(html) { const d = document.createElement("div"); d.className = "card"; d.innerHTML = html; return d; }

function decBadge(r) {
  if (r && r.executed) return '<span class="badge b-effect">executed &mdash; no gateway</span>';
  const dec = ((r && r.decision) || "").toLowerCase();
  return `<span class="badge ${badgeClass(dec)}">${esc(dec || "?")}</span>`;
}

// Render the FULL transcript: the raw prompt, every raw tool call (name + input),
// and the raw result of each — so nothing the agent sent is hidden.
function renderRun(res) {
  const box = $("agentout"); box.innerHTML = "";
  if (res.error) { box.appendChild(card('<span class="danger">' + esc(res.error) + "</span>")); return; }

  // 1) the raw inputs to the model
  box.appendChild(card(
    '<div class="lbl">user prompt &rarr; LLM</div><pre>' + esc(res.prompt || "") + "</pre>"
    + (res.system ? '<details><summary>system prompt (verbatim)</summary><pre>' + esc(res.system) + "</pre></details>" : "")
  ));

  // 2) each raw tool call + its raw result, in order
  let n = 0;
  for (const s of res.steps || []) {
    n++;
    const callJson = JSON.stringify({ name: s.tool, input: s.args }, null, 2);
    let head, resultBlock;
    if (s.tool === "read_inbox") {
      const inv = (s.result && s.result.invoices) || [];
      head = '<span class="badge b-bypassed">tool &middot; ungated</span> <strong>read_inbox</strong> '
           + '<span class="muted">untrusted input &middot; ' + inv.length + " invoice(s)</span>";
      resultBlock = '<details><summary>raw result (the untrusted invoices, incl. bodies)</summary><pre>'
           + esc(JSON.stringify(s.result, null, 2)) + "</pre></details>";
    } else {
      const a = s.args || {};
      head = decBadge(s.result) + " <strong>" + esc(s.tool) + "</strong> "
           + '<span class="muted">' + esc(a.resource || "") + "." + esc(a.action || "") + "</span>"
           + (s.result && s.result.rule ? ' <span class="muted">[' + esc(s.result.rule) + "]</span>" : "");
      resultBlock = '<div class="lbl" style="margin-top:6px">gateway result (raw)</div><pre>'
           + esc(JSON.stringify(s.result, null, 2)) + "</pre>";
    }
    box.appendChild(card(
      '<div class="top">' + head + "</div>"
      + '<div class="lbl" style="margin-top:6px">raw ' + (s.tool === "submit_intent" ? "intent" : "tool call") + " #" + n + " (what the LLM sent)</div>"
      + "<pre>" + esc(callJson) + "</pre>" + resultBlock
    ));
  }

  // 3) the assistant's final message
  if (res.final_text) {
    box.appendChild(card('<div class="lbl">assistant final message</div><pre>' + esc(res.final_text) + "</pre>"));
  }
}

// --- gateway on/off toggle -------------------------------------------------
function gatewayOn() {
  const el = document.querySelector('input[name="gw"]:checked');
  return !el || el.value === "on";
}
function reflectGateway() {
  const on = gatewayOn();
  document.getElementById("runner").classList.toggle("gwoff", !on);
  document.getElementById("seg-on").classList.toggle("sel-on", on);
  document.getElementById("seg-off").classList.toggle("sel-off", !on);
  $("gwhint").innerHTML = on
    ? "Gateway <b>ON</b>: payments are enforced &mdash; the $800 is paid, the $6,000 is held for "
      + "approval, and tenant scope is checked."
    : '<b class="danger">Gateway OFF &mdash; the agent’s tools hit the bank directly.</b> '
      + "Every payment executes with NO checks: the $6,000 is not held, nothing is scoped or audited.";
}

async function runAgent(body) {
  const mode = gatewayOn() ? "safe" : "unsafe";   // the toggle decides; request is otherwise identical
  $("runstatus").textContent = `running the agent (gateway ${mode === "safe" ? "ON" : "OFF"})…`;
  $("agentout").innerHTML = '<div class="muted">running…</div>';
  const res = await postJSON("/agent/run", Object.assign({}, body, { mode }));
  $("runstatus").textContent = `done via ${res.provider || "?"} — gateway ${mode === "safe" ? "ON" : "OFF"}`;
  renderRun(res);
  refreshApprovals();
}

// --- approvals -------------------------------------------------------------
async function refreshApprovals() {
  const rows = await getJSON("/admin/approvals");
  const box = $("approvals");
  box.innerHTML = rows.length ? "" : '<div class="muted">no payments awaiting approval</div>';
  for (const r of rows) {
    const data = (r.resolved && r.resolved.data) || {};
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `<div class="grid2">
        <span class="muted">action</span><span class="mono">${esc(r.resolved?.resource)}.${esc(r.resolved?.action)}</span>
        <span class="muted">amount</span><span>${esc(data.amount)} ${esc(data.currency || "USD")} &rarr; ${esc(data.payeeId || data.newPayee)}</span>
        <span class="muted">approvers</span><span>${esc((r.approval?.approvers || []).join(", "))}</span>
        <span class="muted">ticket</span><span class="mono">${esc(r.id)}</span>
      </div>
      <div class="row" style="margin-top:8px">
        <button class="primary" data-approve="${esc(r.id)}">Approve</button>
        <button data-reject="${esc(r.id)}">Reject</button>
      </div>`;
    box.appendChild(card);
  }
}

// --- wiring ----------------------------------------------------------------
document.addEventListener("click", async (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.run) return runAgent(JSON.parse(t.dataset.run));
  if (t.dataset.approve) { await postJSON(`/admin/approvals/${t.dataset.approve}/approve`,
    { approver: $("approver").value || "mgr-1" }); return refreshApprovals(); }
  if (t.dataset.reject) { await postJSON(`/admin/approvals/${t.dataset.reject}/reject`,
    { approver: $("approver").value || "mgr-1" }); return refreshApprovals(); }
});

$("refreshApprovals").onclick = refreshApprovals;
document.querySelectorAll('input[name="gw"]').forEach((r) => r.addEventListener("change", reflectGateway));

getJSON("/tool-schema").then((s) => { $("provider").textContent = "gateway online"; }).catch(() => {});
reflectGateway();
connectTrace();
refreshApprovals();

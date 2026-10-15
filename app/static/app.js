'use strict';
// Manager approval and operator summary. The page follows the latest alert from the agent service
// (GET /api/latest) and sends the manager's decisions to the mock factory (POST /api/...): see app/server.py.

const $ = (id) => document.getElementById(id);
const POLL_MS = 2000;
const STATUS = {
  planning: 'Agent is planning…',
  awaiting_approval: 'Awaiting approval',
  escalated: 'Escalated to manager',
  failed: 'Agent failed',
  approved: 'Approved by manager',
  rejected: 'Rejected by manager',
};

let result = null;
let isDemo = false;
let seen = null; // what the last poll rendered, so a poll only redraws on a change
let selectedMachine = null;
let fleetDemo = false;
let incidents = [];
const draftStates = new Map();

function machineHeader(id) {
  $('machine-header').textContent = `PITSTOP / CNC MILL ${id || '—'}`;
}

const sample = {
  status: 'awaiting_approval', machine_id: 'M-01', diagnosis: 'Cutting tool worn close to its limit.',
  repair_steps: ['Stop spindle and follow lockout procedure', 'Replace main end mill EM-1004', 'Set tool offsets and perform an authorized test cut'],
  part_number: 'EM-1004', part_name: 'Carbide end mill 10 mm 4-flute', part_in_stock: false,
  purchase_order_id: 'PO-DEMO', expected_delivery: '2026-10-15T10:00:00+00:00', booking_id: 'BK-DEMO',
  engineer_name: 'Priya Nair', booking_start: '2026-10-15T14:00:00+00:00', booking_end: '2026-10-15T14:30:00+00:00',
  operator_summary: 'The main cutting tool is nearing its wear limit. A replacement and engineer visit are planned, pending manager approval.',
  downtime: { planned_min: 30, unplanned_h: 8, saved_h: 7.5, cost_per_h: 500, currency: 'SGD', money_saved: 3750,
    assumptions: 'An unplanned tool breakdown stops the machine for about 8 h; downtime costs about 500 SGD per hour.' },
  warnings: ['DEMO ONLY: Purchase-order and booking IDs are illustrative; no requests can be sent for these IDs.'],
  trace: [
    { tool: 'check_inventory', arguments: { part_number: 'EM-1004' }, result: { quantity_on_hand: 0 } },
    { tool: 'get_supplier_quote', arguments: { part_number: 'EM-1004' }, result: { lead_time_hours: 24 } },
  ],
  backend: 'demo',
};

const value = (x) => (x === null || x === undefined || x === '' ? '—' : String(x));
// Times in UTC, the same as the agent's summary.
const date = (x) => (x
  ? new Date(x).toLocaleString('en-GB', { timeZone: 'UTC', weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) + ' UTC'
  : '—');
// A time window: the end shows only its time (browsers word dates differently, e.g. Safari's "Thu 15 Oct at 20:00").
const clock = (x) => new Date(x).toLocaleTimeString('en-GB', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit' }) + ' UTC';
const span = (start, end) => (start ? `${date(start)} – ${end ? clock(end) : '—'}` : '—');
function setText(id, v) { $(id).textContent = value(v); }

function setStatus(status) {
  $('status').textContent = STATUS[status] || status || 'Waiting for an alert';
  $('status').dataset.status = status || '';
}

// After a rejection with a reason, the plan on screen is the agent's new plan: say why there is one.
function showFeedback(feedback) {
  $('feedback').hidden = !feedback;
  $('feedback').textContent = feedback ? `New plan after the manager's feedback: "${feedback}"` : '';
}

function showAlert(alert) {
  if (!alert) {
    setText('op-failure', null);
    $('alert-text').textContent = 'No alert yet. Run the detector replay to start.';
    return;
  }
  const kind = String(alert.failure_type).replace('_', ' ');
  const wear = alert.tool_wear_min != null ? `${kind} at ${Math.round(alert.tool_wear_min)} cutting min` : kind;
  const left = alert.remaining_cutting_min != null ? `about ${Math.round(alert.remaining_cutting_min)} min left` : '';
  $('alert-text').textContent = [alert.machine_id, wear, left, `failure predicted ${date(alert.predicted_failure_at)}`]
    .filter(Boolean).join(' · ');
  setText('op-failure', date(alert.predicted_failure_at));
}

function load(data, demo = false) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('AgentResult must be a JSON object');
  for (const button of document.querySelectorAll('.buttons button')) disarm(button);
  result = data;
  isDemo = demo;
  for (const k of Object.keys(decided)) delete decided[k];
  $('op-update').hidden = true;
  setStatus(data.status);
  setText('machine', data.machine_id);
  machineHeader(selectedMachine || data.machine_id);
  setText('part', data.part_number ? [data.part_number, data.part_name].filter(Boolean).join(' · ') : 'Not selected');
  setText('planned', data.downtime?.planned_min != null ? `${data.downtime.planned_min} min` : '—');
  setText('diagnosis', data.diagnosis);
  $('steps').replaceChildren(...(data.repair_steps || []).map((step) => Object.assign(document.createElement('li'), { textContent: step })));
  setText('stock', data.part_in_stock === true ? 'Yes' : data.part_in_stock === false ? 'No' : 'Unknown');
  setText('delivery', data.expected_delivery ? date(data.expected_delivery) : 'No order needed');
  setText('engineer', data.engineer_name);
  setText('window', span(data.booking_start, data.booking_end));
  $('escalation-row').hidden = !data.escalation_reason;
  setText('escalation', data.escalation_reason);

  setText('po-id', data.purchase_order_id
    ? [data.purchase_order_id, data.part_number, data.expected_delivery && `arrives ${date(data.expected_delivery)}`].filter(Boolean).join(' · ')
    : 'No purchase order');
  setText('booking-id', data.booking_id
    ? [data.booking_id, data.engineer_name, span(data.booking_start, data.booking_end)].filter((x) => x && x !== '—').join(' · ')
    : 'No booking');
  for (const [kind, id] of [['po', data.purchase_order_id], ['booking', data.booking_id]]) {
    $(`${kind}-approve`).parentElement.hidden = false;
    $(`${kind}-state`).textContent = id && !demo ? 'pending approval' : '';
    for (const action of ['approve', 'reject']) $(`${kind}-${action}`).disabled = !id || demo;
    if (id && !demo) refreshItem(kind, id);
  }

  const warnings = data.warnings || [];
  $('warnings').replaceChildren(...(warnings.length ? warnings : ['No warnings: every check passed.']).map((w) =>
    Object.assign(document.createElement('li'), { textContent: typeof w === 'string' ? w : JSON.stringify(w) })));

  const trace = data.trace || [];
  $('trace').replaceChildren();
  if (!trace.length) $('trace').textContent = 'No tool calls reported.';
  for (const t of trace) {
    const div = Object.assign(document.createElement('div'), { className: 'trace-item' });
    const pre = Object.assign(document.createElement('pre'), { textContent: JSON.stringify({ arguments: t.arguments, result: t.result }, null, 2) });
    div.append(Object.assign(document.createElement('strong'), { textContent: t.tool || 'Tool call' }), pre);
    $('trace').append(div);
  }

  const d = data.downtime;
  setText('op-planned', d?.planned_min != null ? `${d.planned_min} min` : '—');
  setText('op-saved', d?.saved_h != null ? `${d.saved_h} hours` : '—');
  setText('op-money', d?.money_saved != null ? `${d.currency || 'SGD'} ${Number(d.money_saved).toLocaleString()}` : '—');
  setText('op-assumptions', d?.assumptions || 'No estimate: nothing is booked yet.');
  setText('op-summary', data.operator_summary);
  setText('op-status', STATUS[data.status] || data.status);
  setText('op-machine', data.machine_id);
  setText('op-engineer', data.engineer_name);
  setText('op-window', span(data.booking_start, data.booking_end));
  $('message').textContent = demo ? 'Demo data loaded. Approval buttons are disabled.' : '';
}

function clearPlan(status) {
  result = null;
  for (const button of document.querySelectorAll('.buttons button')) disarm(button);
  setStatus(status);
  setText('diagnosis', status === 'planning' ? 'The agent is reading the manual, checking stock and finding an engineer…' : 'The plan appears here when the agent has finished.');
  for (const id of ['machine', 'part', 'planned', 'stock', 'delivery', 'engineer', 'window', 'op-planned', 'op-saved', 'op-money', 'op-status', 'op-machine', 'op-engineer', 'op-window']) setText(id, null);
  $('steps').replaceChildren();
  $('escalation-row').hidden = true;
  $('trace').textContent = 'No tool calls yet.';
  $('warnings').replaceChildren(Object.assign(document.createElement('li'), { textContent: 'No plan loaded.' }));
  setText('op-summary', 'The summary appears here when the agent has finished.');
  $('op-update').hidden = true;
  for (const kind of ['po', 'booking']) {
    $(`${kind}-state`).textContent = '';
    setText(`${kind}-id`, kind === 'po' ? 'No order' : 'No booking');
    for (const action of ['approve', 'reject']) $(`${kind}-${action}`).disabled = true;
    $(`${kind}-approve`).parentElement.hidden = false;
  }
}

const API = { po: 'purchase-orders', booking: 'bookings' };

const decided = {}; // po / booking → status in the mock factory

function showDecision(kind, item) {
  const draftId = kind === 'po' ? result?.purchase_order_id : result?.booking_id;
  if (draftId) draftStates.set(`${kind}:${draftId}`, item.status);
  if (kind === 'po' && item.po_id) {
    // The order as the supplier has it: what the manager is paying for.
    setText('po-id', [item.po_id, `${item.quantity} × ${item.part_number}`, item.supplier,
      item.total_price != null && `${item.currency} ${Number(item.total_price).toLocaleString()}`,
      `arrives ${date(item.expected_delivery)}`].filter(Boolean).join(' · '));
  }
  $(`${kind}-state`).textContent = item.status.replace('_', ' ');
  $(`${kind}-state`).dataset.status = item.status;
  const open = item.status === 'pending_approval';
  for (const action of ['approve', 'reject']) {
    $(`${kind}-${action}`).disabled = !open;
    if (!open) disarm($(`${kind}-${action}`)); // a refresh while pending keeps a half-confirmed click
  }
  $(`${kind}-approve`).parentElement.hidden = !open; // a decided draft has no buttons left
  if (!open) $(`${kind}-reason`).hidden = true;
  decided[kind] = item.status;
  // Once the manager has decided every draft of an awaiting plan, the status says so.
  const drafts = [['po', result?.purchase_order_id], ['booking', result?.booking_id]].filter(([, id]) => id).map(([k]) => decided[k]);
  if (result?.status === 'awaiting_approval' && drafts.length && drafts.every((s) => s && s !== 'pending_approval')) {
    const outcome = drafts.includes('rejected') ? 'rejected' : 'approved';
    setStatus(outcome);
    setText('op-status', STATUS[outcome]);
    // The agent wrote the summary before the manager decided: say what changed since.
    $('op-update').hidden = false;
    $('op-update').textContent = outcome === 'approved'
      ? `Update: the manager has approved the plan. ${result.engineer_name || 'The engineer'} will do the repair `
        + `${span(result.booking_start, result.booking_end)}${result.purchase_order_id ? ', after the new part arrives' : ''}.`
      : 'Update: the manager rejected part of this plan, so the repair is not scheduled yet. The manager will arrange a new plan.';
  }
}

// Two clicks instead of a browser confirm() dialog, which automated browsers (and demo recordings) suppress.
// A reject also asks for a reason: with one, the agent makes a new plan (replan); it stays armed while typing.
function arm(button, action) {
  button.dataset.armed = '1';
  button.dataset.label = button.textContent;
  button.textContent = action === 'approve' ? 'Click to confirm' : 'Confirm reject';
  button.classList.add('armed');
  if (action === 'approve') button.timer = setTimeout(() => disarm(button), 5000);
  else {
    const reason = $(button.id.replace('-reject', '-reason'));
    reason.hidden = false;
    reason.focus();
    const label = () => { button.textContent = reason.value.trim() ? 'Reject and replan' : 'Reject (no new plan)'; };
    reason.oninput = label;
    label();
  }
}

function disarm(button) {
  if (!button.dataset.armed) return;
  clearTimeout(button.timer);
  delete button.dataset.armed;
  button.textContent = button.dataset.label;
  button.classList.remove('armed');
  if (button.id.endsWith('-reject')) $(button.id.replace('-reject', '-reason')).hidden = true;
}

async function refreshItem(kind, id) {
  try {
    const response = await fetch(`/api/${API[kind]}/${encodeURIComponent(id)}`);
    if (response.ok) {
      const item = await response.json();
      draftStates.set(`${kind}:${id}`, item.status);
      if (!isDemo && id === (kind === 'po' ? result?.purchase_order_id : result?.booking_id)) showDecision(kind, item);
    }
  } catch { /* the next poll tries again */ }
}

async function decision(kind, action) {
  const activePlan = result;
  const id = kind === 'po' ? result?.purchase_order_id : result?.booking_id;
  if (!id || isDemo) return;
  const label = kind === 'po' ? 'purchase order' : 'booking';
  const button = $(`${kind}-${action}`);
  if (!button.dataset.armed) {
    for (const other of document.querySelectorAll('.buttons button')) disarm(other);
    arm(button, action);
    return;
  }
  const reason = action === 'reject' ? $(`${kind}-reason`).value.trim() : '';
  disarm(button);
  $(`${kind}-approve`).disabled = $(`${kind}-reject`).disabled = true;
  try {
    const response = await fetch(`/api/${API[kind]}/${encodeURIComponent(id)}/${action}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    draftStates.set(`${kind}:${id}`, body.status);
    if (result !== activePlan || isDemo) return;
    showDecision(kind, body);
    $(`${kind}-reason`).value = '';
    $('message').textContent = reason
      ? `${label[0].toUpperCase()}${label.slice(1)} ${id} rejected. The agent is making a new plan with your reason…`
      : action === 'reject'
        ? `${label[0].toUpperCase()}${label.slice(1)} ${id} rejected. No reason given, so the agent won't make a new plan.`
        : `${label[0].toUpperCase()}${label.slice(1)} ${id}: ${body.status}.`;
  } catch (e) {
    if (result !== activePlan || isDemo) return;
    $('message').textContent = `Request failed: ${e.message}`;
    refreshItem(kind, id);
  }
}

async function poll() {
  if (isDemo) return;
  const machine = selectedMachine;
  try {
    const response = await fetch(machine ? `/api/incidents/${encodeURIComponent(machine)}` : '/api/latest');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const latest = await response.json();
    if (machine !== selectedMachine || isDemo) return;
    $('live').textContent = 'Live';
    const key = JSON.stringify(latest);
    if (key === seen) {
      // Decisions made in another window show up here too.
      if (result && !isDemo) {
        for (const [kind, id] of [['po', result.purchase_order_id], ['booking', result.booking_id]]) {
          if (id && (!decided[kind] || decided[kind] === 'pending_approval')) refreshItem(kind, id);
        }
      }
      return;
    }
    seen = key;
    showAlert(latest.alert);
    showFeedback(latest.feedback);
    machineHeader(machine || latest.alert?.machine_id || latest.result?.machine_id);
    if (latest.result) load(latest.result);
    else clearPlan(latest.alert ? 'planning' : null);
  } catch {
    if (machine !== selectedMachine || isDemo) return;
    $('live').textContent = 'Agent service not reachable';
  }
}

function renderFleet() {
  const focused = document.activeElement?.dataset.machine;
  $('fleet-rows').replaceChildren();
  for (const incident of incidents) {
    const alert = incident.alert || {};
    const plan = incident.result || {};
    const id = alert.machine_id || plan.machine_id;
    if (!id) continue;
    const drafts = [['po', plan.purchase_order_id], ['booking', plan.booking_id]].filter(([, draft]) => draft);
    const states = drafts.map(([kind, draft]) => draftStates.get(`${kind}:${draft}`) ||
      (['approved', 'rejected'].includes(plan.status) ? plan.status : 'pending_approval'));
    const pending = states.filter((status) => status === 'pending_approval').length;
    let status = plan.status || 'planning';
    if (status === 'awaiting_approval' && states.length && !pending) status = states.includes('rejected') ? 'rejected' : 'approved';
    const row = document.createElement('tr');
    row.classList.toggle('selected', id === selectedMachine);
    const cells = Array.from({ length: 7 }, () => document.createElement('td'));
    const button = Object.assign(document.createElement('button'), { textContent: id, className: 'machine-select' });
    button.dataset.machine = id;
    button.setAttribute('aria-label', `Review machine ${id}`);
    button.addEventListener('click', () => selectMachine(incident));
    cells[0].append(button);
    cells[1].textContent = value(alert.failure_type).replace(/_/g, ' ');
    if (alert.simulated) cells[1].append(Object.assign(document.createElement('span'), { className: 'simulated-tag', textContent: 'Simulated alert' }));
    cells[2].textContent = date(alert.predicted_failure_at);
    const pill = Object.assign(document.createElement('span'), { className: 'pill', textContent: STATUS[status] || status });
    pill.dataset.status = status;
    cells[3].append(pill);
    cells[4].textContent = [plan.part_number, plan.part_name, plan.purchase_order_id || (plan.part_in_stock ? 'From stock' : 'No purchase order')].filter(Boolean).join(' · ');
    cells[5].textContent = [plan.engineer_name || 'No engineer booked', span(plan.booking_start, plan.booking_end)].join(' · ');
    cells[6].textContent = String(pending);
    row.append(...cells);
    row.addEventListener('click', (event) => { if (!event.target.closest('button')) selectMachine(incident); });
    $('fleet-rows').append(row);
    if (focused === id) button.focus();
  }
}

function selectMachine(incident) {
  selectedMachine = incident.alert?.machine_id || incident.result?.machine_id;
  isDemo = fleetDemo;
  seen = null;
  clearPlan(incident.alert ? 'planning' : null);
  showAlert(incident.alert);
  showFeedback(incident.feedback);
  machineHeader(selectedMachine);
  if (incident.result) load(incident.result, fleetDemo);
  document.querySelector('[data-page="approval"]').click();
  poll();
}

async function pollFleet() {
  if (fleetDemo) return;
  try {
    const response = await fetch('/api/incidents');
    if (fleetDemo) return;
    if (response.status === 404) {
      $('fleet-message').textContent = 'Fleet view needs the fleet backend';
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const body = await response.json();
    if (fleetDemo) return;
    if (!Array.isArray(body.incidents)) throw new Error('Invalid fleet response');
    incidents = body.incidents;
    // The incident result contains draft IDs; the factory owns their current approval states.
    await Promise.all(incidents.flatMap(({ result: plan }) =>
      [['po', plan?.purchase_order_id], ['booking', plan?.booking_id]].filter(([, id]) => id).map(([kind, id]) => refreshItem(kind, id))));
    if (fleetDemo) return;
    $('fleet-message').textContent = incidents.length ? 'Live fleet' : 'No fleet alerts yet.';
    renderFleet();
  } catch {
    if (!fleetDemo) $('fleet-message').textContent = 'Fleet service not reachable. Retrying…';
  }
}

$('load-fleet').addEventListener('click', () => {
  fleetDemo = true;
  draftStates.clear();
  incidents = [1, 2, 3, 4].map((n) => {
    const id = `M-0${n}`;
    const escalated = n === 4;
    return {
      alert: { machine_id: id, failure_type: escalated ? 'coolant_pump' : 'tool_wear', simulated: escalated,
        predicted_failure_at: `2026-10-${escalated ? '17' : '16'}T18:00:00Z` },
      result: { ...sample, machine_id: id, status: escalated ? 'escalated' : 'awaiting_approval',
        part_number: escalated ? 'CP-2001' : sample.part_number, part_name: escalated ? 'Coolant pump' : sample.part_name,
        part_in_stock: n === 1, purchase_order_id: n === 2 || n === 3 ? `PO-DEMO-${n}` : null,
        expected_delivery: n === 2 || n === 3 ? sample.expected_delivery : null,
        booking_id: escalated ? null : `BK-DEMO-${n}`, engineer_name: escalated ? null : n === 3 ? 'Wei Ling' : 'Priya Nair',
        booking_start: escalated ? null : `2026-10-15T${n === 1 ? '07' : n === 2 ? '14' : '15'}:00:00Z`,
        booking_end: escalated ? null : `2026-10-15T${n === 1 ? '07' : n === 2 ? '14' : '15'}:30:00Z`,
        diagnosis: escalated ? 'Simulated coolant-pump failure: replacement cannot arrive before predicted failure.' : sample.diagnosis,
        escalation_reason: escalated ? 'Pump lead time is 72 hours; failure is predicted in 40 hours.' : null,
        repair_steps: escalated ? ['Manager to arrange an urgent coolant-pump repair.'] : sample.repair_steps,
        operator_summary: escalated ? 'Simulated coolant-pump alert escalated to the manager. No repair is booked.' : sample.operator_summary,
        downtime: escalated ? null : sample.downtime },
      received_at: null, feedback: null, previous: null,
    };
  });
  $('fleet-message').textContent = 'Demo fleet loaded. Approval buttons are disabled for demo machines.';
  renderFleet();
});

$('follow-latest').addEventListener('click', () => {
  selectedMachine = null;
  fleetDemo = false;
  isDemo = false;
  seen = null;
  draftStates.clear();
  clearPlan(null);
  showAlert(null);
  showFeedback(null);
  machineHeader(null);
  document.querySelector('[data-page="approval"]').click();
  poll();
  pollFleet();
});

$('load-sample').addEventListener('click', () => load(sample, true));
$('load-json').addEventListener('click', () => {
  try { load(JSON.parse($('result-json').value)); } catch (e) { $('message').textContent = `Invalid input: ${e.message}`; }
});
for (const kind of ['po', 'booking']) for (const action of ['approve', 'reject']) $(`${kind}-${action}`).addEventListener('click', () => decision(kind, action));
for (const button of document.querySelectorAll('.nav-btn')) {
  button.addEventListener('click', () => {
    const page = button.dataset.page;
    document.querySelectorAll('.nav-btn').forEach((b) => b.classList.toggle('active', b === button));
    $('approval-page').hidden = page !== 'approval';
    $('operator-page').hidden = page !== 'operator';
    $('fleet-page').hidden = page !== 'fleet';
    if (page === 'fleet') {
      $('page-title').textContent = 'Fleet';
      $('page-subtitle').textContent = 'Monitor every machine and review its maintenance plan.';
      return;
    }
    $('page-title').textContent = page === 'approval' ? 'Maintenance approval' : 'Operator summary';
    $('page-subtitle').textContent = page === 'approval'
      ? 'Review the AI maintenance plan before any action is authorized.'
      : 'Clear instructions and downtime estimates for the machine operator.';
  });
}

poll();
setInterval(poll, POLL_MS);
pollFleet();
setInterval(pollFleet, POLL_MS);

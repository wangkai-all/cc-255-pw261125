/* 设备端：localStorage 模拟本地库；断网时操作进 outbox，恢复后按序回传。 */
const RESPONSE_ID = "RSP-DEMO-01";
const params = new URLSearchParams(location.search);
const DEVICE_ID = params.get("device") || "A";
const STORE_KEY = `offline-survey-device-${DEVICE_ID}`;
const PEER = DEVICE_ID === "A" ? "B" : "A";

let state = null;

function loadState() {
  const raw = localStorage.getItem(STORE_KEY);
  if (raw) { try { return JSON.parse(raw); } catch { /* fall through */ } }
  return {
    deviceId: DEVICE_ID,
    online: true,
    baseRev: 0,
    baseFingerprint: null,       // 首次同步时从服务端取
    baseFields: {},              // 同步时的基准字段快照，用于计算 delta
    fields: {},                  // 本地字段值
    attachments: [],             // [{attachment_id, slot, file_name, content_base64, status}]
    outbox: [],                  // 待发操作
    receipts: [],                // 收到的收据
  };
}
function saveState() { localStorage.setItem(STORE_KEY, JSON.stringify(state)); }

function toast(msg, kind = "info") {
  const box = document.getElementById("toast");
  const el = document.createElement("div");
  el.className = `t ${kind}`;
  el.innerHTML = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

async function init() {
  document.getElementById("deviceName").textContent = DEVICE_ID;
  document.getElementById("peerLink").innerHTML =
    `另一台：<a href="/device.html?device=${PEER}">设备 ${PEER}</a>`;
  state = loadState();
  document.getElementById("netToggle").checked = state.online;
  document.getElementById("netToggle").addEventListener("change", e => {
    state.online = e.target.checked; saveState(); renderNet();
    toast(state.online ? "网络恢复，可发送 outbox" : "已断网：编辑将保存在本地 outbox", "info");
    if (state.online) flushOutbox();
  });

  document.getElementById("btnSubmit").onclick = submitAnswers;
  document.getElementById("btnWithdraw").onclick = submitWithdraw;
  document.getElementById("btnTamper").onclick = tamperFingerprint;
  document.getElementById("btnFlush").onclick = flushOutbox;
  document.getElementById("btnSync").onclick = pullServer;

  await ensureBootstrap();
  await refreshServerState();
  if (state.online) await flushOutbox();
  renderAll();
}

/* 首次进入或本地无数据：从服务端拉取 rev=0 空白卷作为共同祖先 */
async function ensureBootstrap() {
  if (state.baseFingerprint) return;
  const snap = await api(`/api/snapshot/${RESPONSE_ID}`);
  state.fields = Object.fromEntries(snap.fields.map(f => [f.field_key, f.value]));
  state.baseFields = JSON.parse(JSON.stringify(state.fields));
  state.baseRev = snap.response.head_rev;
  state.baseFingerprint = snap.head_fingerprint;
  saveState();
}

function renderAll() {
  renderNet();
  renderForm();
  renderAttachments();
  renderOutbox();
  renderReceipts();
  renderMeta();
}

function renderNet() {
  document.getElementById("netDot").className = "dot " + (state.online ? "on" : "off");
}

function renderMeta() {
  document.getElementById("respId").textContent = RESPONSE_ID;
  document.getElementById("baseRev").textContent = "rev=" + state.baseRev;
  document.getElementById("baseFp").textContent = state.baseFingerprint
    ? state.baseFingerprint.slice(0, 24) + "…" : "—";
  fingerprintOf(state.fields).then(fp => {
    document.getElementById("localFp").textContent = fp.slice(0, 24) + "…";
  });
  document.getElementById("outboxCount").textContent =
    String(state.outbox.length) + (state.online ? "" : "（断网积压中）");
}

async function renderForm() {
  const snap = await api(`/api/snapshot/${RESPONSE_ID}`);
  const box = document.getElementById("fieldForm");
  box.innerHTML = "";
  for (const f of snap.survey.fields) {
    const lbl = document.createElement("label");
    lbl.className = "field";
    const isArea = f.type === "textarea";
    lbl.innerHTML = `<span class="lbl">${esc(f.label)} <span class="tag">${esc(f.key)}</span></span>`;
    const input = document.createElement(isArea ? "textarea" : "input");
    if (!isArea) input.type = f.type === "number" ? "text" : "text";
    input.value = state.fields[f.key] ?? "";
    input.addEventListener("input", () => {
      state.fields[f.key] = input.value;
      saveState(); renderMeta();
    });
    lbl.appendChild(input);
    box.appendChild(lbl);
  }
}

async function renderAttachments() {
  const snap = await api(`/api/snapshot/${RESPONSE_ID}`);
  const box = document.getElementById("attachSlots");
  box.innerHTML = "";
  for (const slot of snap.survey.attachments) {
    const wrap = document.createElement("div");
    wrap.className = "attach-row";
    const local = state.attachments.filter(a => a.slot === slot.slot && a.status === "present");
    wrap.innerHTML =
      `<div style="flex:1">
         <b>${esc(slot.label)}</b> <span class="tag">${esc(slot.slot)}</span>
         ${slot.required ? '<span class="tag" style="color:var(--amber)">必交</span>' : ""}
         <div class="small muted">本地：${local.length ? esc(local.map(a => a.file_name).join(", ")) : "未选择文件"}</div>
       </div>`;
    const file = document.createElement("input");
    file.type = "file";
    file.onchange = () => addAttachment(slot.slot, file.files[0]);
    const del = document.createElement("button");
    del.className = "danger pill-btn";
    del.textContent = "删除并回传";
    del.disabled = local.length === 0;
    del.onclick = () => deleteAttachment(slot.slot);
    wrap.appendChild(file); wrap.appendChild(del);
    box.appendChild(wrap);
  }
}

function readFileB64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1]);
    r.onerror = reject;
    r.readAsDataURL(file);
  });
}

async function addAttachment(slot, file) {
  if (!file) return;
  const content_base64 = await readFileB64(file);
  const att = {
    attachment_id: genId("ATT"),       // 设备端稳定 ID
    slot, file_name: file.name,
    content_base64, status: "present",
  };
  state.attachments.push(att);
  saveState();
  enqueue({
    kind: "attach",
    op_id: genId("OP"),
    response_id: RESPONSE_ID, device_id: DEVICE_ID,
    attachment: att,
  });
  renderAttachments(); renderOutbox(); renderMeta();
}

function deleteAttachment(slot) {
  const target = state.attachments.find(a => a.slot === slot && a.status === "present");
  if (!target) return;
  target.status = "deleted";
  saveState();
  enqueue({
    kind: "delete_attach",
    op_id: genId("OP"),
    response_id: RESPONSE_ID, device_id: DEVICE_ID,
    attachment_id: target.attachment_id,
  });
  renderAttachments(); renderOutbox();
}

/* ---------------- outbox 与回传 ---------------- */
function enqueue(envelope) {
  envelope.submission_id = envelope.submission_id || genId("SUB");
  state.outbox.push(envelope);
  saveState();
  if (state.online) flushOutbox();
  else toast(`断网：操作已存入本地 outbox（${envelope.kind}），恢复网络后回传`, "info");
}

function submitAnswers() {
  // 只回传相对本地基准发生变化的字段（delta 协议），未改字段不参与合并
  const delta = {};
  for (const [k, v] of Object.entries(state.fields)) {
    if ((state.baseFields[k] ?? "") !== (v ?? "")) delta[k] = v;
  }
  if (!Object.keys(delta).length) {
    toast("没有字段变化，无需回传", "info");
    return;
  }
  enqueue({
    kind: "answers",
    op_id: genId("OP"),
    submission_id: genId("SUB"),
    response_id: RESPONSE_ID, device_id: DEVICE_ID,
    base_rev: state.baseRev,
    base_fingerprint: state.baseFingerprint,
    fields: delta,
  });
  renderOutbox(); renderMeta();
}

function submitWithdraw() {
  if (!confirm("确认撤回答卷？撤回后双方后续编辑/附件任务将被接收端阻断。")) return;
  enqueue({
    kind: "withdraw",
    op_id: genId("OP"),
    submission_id: genId("SUB"),
    response_id: RESPONSE_ID, device_id: DEVICE_ID,
    reason: `设备 ${DEVICE_ID} 现场撤回`,
  });
  renderOutbox();
}

function tamperFingerprint() {
  // 只改本地基准指纹，不改字段：服务端核验必然失败，触发"指纹不符→整份分支保留"
  state.baseFingerprint = "0".repeat(64);
  saveState();
  toast("已篡改本地基准指纹。下次提交将被服务端判定指纹不符。", "err");
  renderMeta();
}

async function flushOutbox() {
  if (!state.online || state.outbox.length === 0) { renderAll(); return; }
  const remaining = [];
  for (const env of state.outbox) {
    try {
      const result = await api("/api/receive", { method: "POST", body: env });
      state.receipts.unshift({ envelope: env, result, at: new Date().toISOString() });
      saveState();
      renderReceipts();
      if (result.integrity_status === "duplicate") {
        toast(`重复回传被幂等识别：收据 ${result.receipt_id} → 原始 ${result.duplicate_of}`, "info");
      } else if (result.process_result?.state === "blocked") {
        toast(`任务被阻断：${esc(result.process_result.reason)}`, "err");
      } else if (result.process_result?.state === "conflicted") {
        toast("已存证并入队，但产生冲突，等待工作台人工裁决", "err");
      } else if (result.process_result?.state === "applied") {
        toast(`回传成功并自动合并：收据 ${result.receipt_id}`, "ok");
        // 合并成功后推进本地基准（用服务端最新位点校正）
        await pullServer(true);
      } else if (result.integrity_status === "fingerprint_mismatch") {
        toast("指纹不符：整份回传已作为分支保留，需人工裁决", "err");
      }
    } catch (err) {
      toast(`发送失败，保留在 outbox：${err}`, "err");
      remaining.push(env);
    }
  }
  state.outbox = remaining;
  saveState();
  renderOutbox(); renderMeta();
  await refreshServerState();
}

/* 拉取服务端最新：本地没有未发送编辑时直接对齐；有积压时只看不动，避免覆盖本地修改 */
async function pullServer(silent = false) {
  const snap = await api(`/api/snapshot/${RESPONSE_ID}`);
  const hasPendingEdits = state.outbox.some(o => o.kind === "answers");
  if (!hasPendingEdits) {
    state.fields = Object.fromEntries(snap.fields.map(f => [f.field_key, f.value]));
    state.baseFields = JSON.parse(JSON.stringify(state.fields));
    state.baseRev = snap.response.head_rev;
    state.baseFingerprint = snap.head_fingerprint;
    saveState();
    if (!silent) toast("已对齐服务端最新修订", "ok");
  } else if (!silent) {
    toast("本地有未回传编辑，暂不覆盖本地内容；冲突将在回传时由服务端裁决", "info");
  }
  renderForm(); renderMeta();
}

function renderOutbox() {
  const body = document.getElementById("outboxBody");
  if (!state.outbox.length) {
    body.innerHTML = `<tr><td colspan="5" class="muted">outbox 为空</td></tr>`;
    return;
  }
  body.innerHTML = state.outbox.map(o => {
    let summary = "";
    if (o.kind === "answers") summary = `rev ${o.base_rev} → ${Object.keys(o.fields).length} 字段`;
    else if (o.kind === "withdraw") summary = esc(o.reason || "撤回");
    else if (o.kind === "attach") summary = esc(o.attachment.file_name);
    else if (o.kind === "delete_attach") summary = "删除 " + esc(o.attachment_id);
    return `<tr>
      <td class="mono">${esc(o.op_id.slice(-6))}</td>
      <td>${esc(o.kind)}</td><td>${summary}</td>
      <td><span class="badge pending">${state.online ? "待发送" : "断网积压"}</span></td>
      <td><button class="ghost pill-btn" onclick="resendOne('${esc(o.submission_id)}')">重复回传</button></td>
    </tr>`;
  }).join("");
}

window.resendOne = function (submissionId) {
  const env = state.outbox.find(o => o.submission_id === submissionId);
  if (!env) {
    // 已发送的也支持重复回传：从收据历史里找
    const hist = state.receipts.find(r => r.envelope.submission_id === submissionId);
    if (hist) { api("/api/receive", { method: "POST", body: hist.envelope }).then(r => {
      state.receipts.unshift({ envelope: hist.envelope, result: r, at: new Date().toISOString() });
      saveState(); renderReceipts();
      toast(`重复回传完成：${r.integrity_status}`, "info");
    }); }
    return;
  }
  // 同一 submission_id 再发一次，服务端应识别为 duplicate
  api("/api/receive", { method: "POST", body: env }).then(r => {
    state.receipts.unshift({ envelope: env, result: r, at: new Date().toISOString() });
    saveState(); renderReceipts();
    toast(`重复回传完成：${r.integrity_status}`, "info");
  });
};

function renderReceipts() {
  const box = document.getElementById("receipts");
  if (!state.receipts.length) { box.innerHTML = "暂无回传"; return; }
  box.innerHTML = state.receipts.map(r => {
    const p = r.result.process_result || {};
    return `<div class="receipt-box">
      <div><b>收据 ${esc(r.result.receipt_id)}</b>
        <span class="badge ${esc(r.result.integrity_status === "valid" ? "applied" :
          r.result.integrity_status === "duplicate" ? "idempotent" : "conflicted")}">
          ${esc(r.result.integrity_status)}</span>
        ${p.state ? `<span class="badge ${esc(p.state)}">${esc(p.state)}</span>` : ""}
      </div>
      <div class="muted small">类型 ${esc(r.envelope.kind)} · 提交号 ${esc(r.envelope.submission_id)} · ${fmtTime(r.at)}</div>
      ${p.reason ? `<div class="small" style="color:var(--amber)">${esc(p.reason)}</div>` : ""}
      <div class="small muted">${esc(r.result.note || "收据仅证明原始回传可靠保存，发布另受附件齐套与冲突裁决约束")}</div>
    </div>`;
  }).join("");
}

async function refreshServerState() {
  const snap = await api(`/api/snapshot/${RESPONSE_ID}`);
  const rows = snap.fields.map(f =>
    `<tr><td>${esc(f.field_key)}</td><td>${esc(f.value)}</td><td>rev${f.rev}</td><td>${esc(f.origin_device || "")}</td></tr>`
  ).join("");
  const att = snap.attachments.filter(a => a.status === "present").map(a =>
    `<tr><td>${esc(a.slot)}</td><td>${esc(a.file_name)}</td><td class="mono">${esc(a.content_hash.slice(0, 12))}…</td><td>${esc(a.origin_device || "")}</td></tr>`
  ).join("");
  document.getElementById("serverState").innerHTML = `
    <div>服务端状态：<span class="badge ${snap.response.status}">${esc(STATUS_LABELS[snap.response.status])}</span>
      head_rev=${snap.response.head_rev}</div>
    <table style="margin-top:8px"><thead><tr><th>字段</th><th>值</th><th>修订</th><th>来源</th></tr></thead>
      <tbody>${rows || '<tr><td colspan=4 class=muted>无</td></tr>'}</tbody></table>
    <table style="margin-top:8px"><thead><tr><th>槽位</th><th>文件名</th><th>哈希</th><th>来源</th></tr></thead>
      <tbody>${att || '<tr><td colspan=4 class=muted>无附件</td></tr>'}</tbody></table>`;
}

init();

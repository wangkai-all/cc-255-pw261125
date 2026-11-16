/* 接收工作台：展示修订关系、自动裁决依据、冲突人工处理、发布门禁。 */
const RESPONSE_ID = "RSP-DEMO-01";

function toast(msg, kind = "info") {
  const box = document.getElementById("toast");
  const el = document.createElement("div");
  el.className = `t ${kind}`;
  el.innerHTML = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

async function refresh() {
  const [snap, receipts, queue, audit] = await Promise.all([
    api(`/api/snapshot/${RESPONSE_ID}`),
    api(`/api/receipts/${RESPONSE_ID}`),
    api(`/api/queue/${RESPONSE_ID}`),
    api("/api/audit"),
  ]);
  renderHeader(snap);
  renderConflicts(snap);
  renderFields(snap);
  renderBranches(snap);
  renderReceipts(receipts);
  renderQueue(queue);
  renderAudit(audit);
}

function renderHeader(snap) {
  document.getElementById("wbRespId").textContent = snap.response.id;
  document.getElementById("wbStatus").innerHTML =
    `<span class="badge ${snap.response.status}">${esc(STATUS_LABELS[snap.response.status])}</span>`;
  document.getElementById("wbHeadRev").textContent = "rev=" + snap.response.head_rev;
  document.getElementById("wbHeadFp").textContent =
    (snap.head_fingerprint || "").slice(0, 28) + "…";

  const c = snap.completeness;
  document.getElementById("wbCompleteness").innerHTML = `
    <div class="small">必交槽位：${c.required.map(esc).join("、") || "无"}</div>
    <div class="small">已有：${c.present_slots.map(esc).join("、") || "无"}</div>
    ${c.missing.length
      ? `<div class="small" style="color:var(--red)">缺失：${c.missing.map(esc).join("、")}</div>`
      : '<div class="small" style="color:var(--green)">附件齐套 ✓</div>'}`;

  const open = snap.conflicts.filter(x => x.status === "open");
  const blockers = [];
  if (snap.response.status === "withdrawn") blockers.push("答卷已撤回");
  if (open.length) blockers.push(`${open.length} 条未裁决冲突`);
  if (c.missing.length) blockers.push("附件不齐套：" + c.missing.join("、"));
  document.getElementById("wbGate").innerHTML = blockers.length
    ? blockers.map(b => `<div style="color:var(--red)">✗ ${esc(b)}</div>`).join("")
    : '<div style="color:var(--green)">✓ 无未决冲突、附件齐套、未撤回，允许发布</div>';
  document.getElementById("btnPublish").disabled = blockers.length > 0;
}

function renderConflicts(snap) {
  const box = document.getElementById("wbConflicts");
  if (!snap.conflicts.length) {
    box.innerHTML = '<div class="muted small">暂无冲突记录</div>';
    return;
  }
  box.innerHTML = snap.conflicts.map(c => {
    const d = c.detail;
    let body = "";
    if (c.conflict_type === "field") {
      body = `<div class="diff">
        <div class="col base"><div class="muted small">基准 base (rev ${d.base_rev})</div>${esc(d.base)}</div>
        <div class="col head"><div class="muted small">服务端当前（他人已提交）</div>${esc(d.head)}</div>
        <div class="col in"><div class="muted small">本次回传（设备 ${esc(d.device_id)}）</div>${esc(d.incoming)}</div>
      </div>
      <div style="margin-top:8px">
        <input type="text" id="cv-${c.id}" value="${esc(d.incoming)}" placeholder="人工裁定值">
        <button class="pill-btn" onclick="resolveField('${c.id}')">按输入值裁决</button>
        <button class="ghost pill-btn" onclick="resolveField('${c.id}', ${JSON.stringify(d.head)})">取服务端值</button>
        <button class="ghost pill-btn" onclick="resolveField('${c.id}', ${JSON.stringify(d.incoming)})">取回传值</button>
      </div>`;
    } else if (c.conflict_type === "fingerprint") {
      body = `<div class="small muted">设备 ${esc(d.device_id)} 基于 rev ${d.base_rev} 的回传指纹核验失败，
        整份内容已保留为分支 <code>${esc(d.branch_id)}</code>，服务端数据未被改动。</div>
      <button class="purple pill-btn" onclick="resolveC('${c.id}', {resolution:'apply_branch'})">保留整份分支并应用</button>
      <button class="ghost pill-btn" onclick="resolveC('${c.id}', {resolution:'discard_branch'})">丢弃分支（维持服务端数据）</button>`;
    } else if (c.conflict_type === "attachment_version") {
      body = `<div class="small muted">旧版本 <code>${esc(d.old_hash.slice(0,16))}…</code>
        vs 新版本 <code>${esc(d.new_hash.slice(0,16))}…</code>（暂存 ID <code>${esc(d.staged_attachment_id)}</code>）</div>
      <button class="pill-btn" onclick="resolveC('${c.id}', {resolution:'manual', chosen_attachment_id:${JSON.stringify(c.attachment_id)}})">保留旧版本</button>
      <button class="ghost pill-btn" onclick="resolveC('${c.id}', {resolution:'manual', chosen_attachment_id:${JSON.stringify(d.staged_attachment_id)}})">采用新版本</button>`;
    } else if (c.conflict_type === "attachment_name") {
      body = `<div class="small muted">槽位 ${esc(d.slot)}：已挂 <code>${esc(d.existing_id)}</code>，
        新来 <code>${esc(d.incoming_id)}</code>，两份同名文件均已暂存。</div>
      <button class="pill-btn" onclick="resolveC('${c.id}', {resolution:'keep_incoming'})">确认新附件归属（删旧）</button>
      <button class="ghost pill-btn" onclick="resolveC('${c.id}', {resolution:'keep_existing'})">维持原附件（删新）</button>`;
    } else if (c.conflict_type === "attachment_delete") {
      body = `<div class="small muted">删除发生时该附件尚有未决版本冲突，当前为软删除（blob 仍保留）。</div>
      <button class="danger pill-btn" onclick="resolveC('${c.id}', {resolution:'confirm_delete'})">确认删除</button>
      <button class="ghost pill-btn" onclick="resolveC('${c.id}', {resolution:'restore'})">恢复附件</button>`;
    } else if (c.conflict_type === "withdrawal") {
      body = `<div class="small muted">撤回原因：${esc(d.reason || "—")}。撤回之后入队的编辑/附件任务全部 blocked。</div>
      <button class="pill-btn" onclick="resolveC('${c.id}', {resolution:'restore', restore_response:true})">解除撤回、恢复编辑</button>
      <button class="ghost pill-btn" onclick="resolveC('${c.id}', {resolution:'terminate'})">维持撤回（终结答卷）</button>`;
    }
    return `<div class="card" style="background:var(--panel2);margin-bottom:10px;">
      <div><b>${esc(TYPE_LABELS[c.conflict_type] || c.conflict_type)}</b>
        <span class="badge ${c.status}">${c.status === "open" ? "待裁决" : "已裁决"}</span>
        <span class="tag mono">${esc(c.id)}</span>
        ${c.field_key ? `<span class="tag">字段 ${esc(c.field_key)}</span>` : ""}
        ${c.attachment_id ? `<span class="tag">附件 ${esc(c.attachment_id)}</span>` : ""}
      </div>
      <div class="basis">⚖️ 自动裁决依据：${esc(c.basis)}</div>
      ${c.status === "open" ? body : `<div class="small muted">裁决结果：${esc(c.resolution || "")}</div>`}
    </div>`;
  }).join("");
}

window.resolveField = async function (conflictId, fixedValue) {
  const value = fixedValue !== undefined
    ? fixedValue
    : document.getElementById("cv-" + conflictId).value;
  await resolveC(conflictId, { resolution: "manual", chosen_value: value });
};

window.resolveC = async function (conflictId, body) {
  const r = await api(`/api/conflicts/${conflictId}/resolve`, { method: "POST", body });
  if (r.ok) { toast("裁决已落库：" + JSON.stringify(r), "ok"); refresh(); }
  else toast("裁决失败：" + (r.reason || "未知错误"), "err");
};

function renderFields(snap) {
  const labels = Object.fromEntries(snap.survey.fields.map(f => [f.key, f.label]));
  document.getElementById("wbFields").innerHTML =
    `<table><thead><tr><th>字段</th><th>当前值</th><th>修订</th><th>来源</th></tr></thead><tbody>` +
    snap.fields.map(f => `<tr>
      <td>${esc(labels[f.field_key] || f.field_key)}<div class="tag mono">${esc(f.field_key)}</div></td>
      <td>${esc(f.value)}</td><td>rev${f.rev}</td><td>${esc(f.origin_device || "")}</td>
    </tr>`).join("") + "</tbody></table>";

  document.getElementById("wbRevisions").innerHTML =
    `<table><thead><tr><th>时间</th><th>字段</th><th>rev</th><th>值</th><th>原因</th><th>来源</th></tr></thead><tbody>` +
    snap.revisions.map(r => `<tr>
      <td class="small">${fmtTime(r.changed_at)}</td><td>${esc(r.field_key)}</td>
      <td>${r.rev}</td><td>${esc(r.value)}</td>
      <td class="small">${esc(r.reason)}</td><td>${esc(r.origin_device || "")}</td>
    </tr>`).join("") + "</tbody></table>";
}

function renderBranches(snap) {
  const box = document.getElementById("wbBranches");
  if (!snap.branches.length) { box.innerHTML = "无"; return; }
  box.innerHTML = snap.branches.map(b => `
    <div class="receipt-box">
      <div><code>${esc(b.id)}</code> · ${esc(b.name)}</div>
      <div class="muted small">来源收据 ${esc(b.source_receipt)} · 设备 ${esc(b.device_id)} · ${fmtTime(b.created_at)}</div>
      <pre class="mono" style="white-space:pre-wrap;max-height:120px;overflow:auto;">${esc(b.fields_json)}</pre>
    </div>`).join("");
}

function renderReceipts(receipts) {
  document.getElementById("wbReceipts").innerHTML =
    `<table><thead><tr><th>收据</th><th>时间</th><th>类型</th><th>设备</th><th>完整性</th><th>重复指向/备注</th></tr></thead><tbody>` +
    receipts.map(r => `<tr>
      <td class="mono">${esc(r.receipt_id)}</td>
      <td class="small">${fmtTime(r.received_at)}</td>
      <td>${esc(r.kind)}</td><td>${esc(r.device_id || "")}</td>
      <td><span class="badge ${r.integrity_status === "valid" ? "applied" :
        r.integrity_status === "duplicate" ? "idempotent" : "conflicted"}">${esc(r.integrity_status)}</span></td>
      <td class="small">${esc(r.duplicate_of ? "重复→" + r.duplicate_of : (r.note || ""))}</td>
    </tr>`).join("") + "</tbody></table>" +
    '<div class="small muted" style="margin-top:6px">收据仅证明原始回传可靠保存；合并、冲突处理与发布是后续独立环节。</div>';
}

function renderQueue(queue) {
  document.getElementById("wbQueue").innerHTML =
    `<table><thead><tr><th>seq</th><th>操作</th><th>设备</th><th>状态</th><th>处理依据/阻断原因</th></tr></thead><tbody>` +
    queue.map(q => `<tr>
      <td>${q.seq}</td><td>${esc(q.op_type)}</td><td>${esc(q.device_id || "")}</td>
      <td><span class="badge ${esc(q.state)}">${esc(q.state)}</span></td>
      <td class="small">${esc(q.result?.reason ||
        (q.result?.state ? JSON.stringify(q.result.state) : "—"))}</td>
    </tr>`).join("") + "</tbody></table>";
}

function renderAudit(audit) {
  document.getElementById("wbAudit").innerHTML =
    `<table><thead><tr><th>时间</th><th>动作</th><th>详情</th><th>收据/seq</th></tr></thead><tbody>` +
    audit.slice().reverse().map(a => `<tr>
      <td class="small">${fmtTime(a.at)}</td><td>${esc(a.action)}</td>
      <td class="small">${esc(a.detail)}</td>
      <td class="small mono">${esc([a.receipt_id, a.seq].filter(Boolean).join(" / ") || "")}</td>
    </tr>`).join("") + "</tbody></table>";
}

document.getElementById("btnPublish").onclick = async () => {
  const r = await api(`/api/publish/${RESPONSE_ID}`, { method: "POST", body: {} });
  if (r.ok) toast("发布成功 ✓ head_rev=" + r.head_rev, "ok");
  else toast("发布被门禁拦截：" + r.blockers.join("；"), "err");
  refresh();
};

document.getElementById("btnReset").onclick = async () => {
  if (!confirm("重置全部演示数据（含本地数据库与附件 blob 索引）？")) return;
  await api("/api/reset", { method: "POST", body: {} });
  toast("演示数据已重置；请在两个设备页面重新加载以重建本地库", "ok");
  refresh();
};

refresh();
setInterval(refresh, 4000);

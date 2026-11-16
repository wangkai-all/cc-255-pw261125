/* 公共工具：指纹算法与服务端 engine.fingerprint_of 严格一致
 * canonical = JSON.stringify(fields) 键排序、无空白；指纹 = SHA-256 十六进制。 */

async function sha256Hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, "0")).join("");
}

function canonicalFields(fields) {
  const keys = Object.keys(fields).sort();
  const parts = keys.map(k => JSON.stringify(k) + ":" + JSON.stringify(fields[k]));
  return "{" + parts.join(",") + "}";
}

async function fingerprintOf(fields) {
  return sha256Hex(canonicalFields(fields));
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  return resp.json();
}

function genId(prefix) {
  return prefix + "-" + Math.random().toString(16).slice(2, 10).toUpperCase();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTime(iso) {
  return iso ? iso.replace("T", " ").replace("+00:00", " UTC") : "—";
}

const STATUS_LABELS = {
  editing: "编辑中", conflict: "待裁决", ready: "可发布",
  published: "已发布", withdrawn: "已撤回",
};

const TYPE_LABELS = {
  field: "字段分叉", fingerprint: "指纹不符", attachment_name: "同名附件",
  attachment_version: "附件版本", attachment_delete: "附件删除冲突",
  withdrawal: "撤回事件",
};

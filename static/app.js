/* MC Boss 计时器 - 前端逻辑：倒计时渲染、轮询同步、增删改交互 */
"use strict";

const $ = (sel) => document.querySelector(sel);

let bosses = [];            // 最近一次从服务器拿到的 Boss 列表
let serverOffset = 0;       // 服务器时间 - 本地时间（秒），用于校准时钟
let editingId = null;       // 当前编辑的 Boss id；null 表示新增
let killTargetId = null;    // 击杀弹窗针对的 Boss id

/* ---------- 工具函数 ---------- */

function esc(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function now() { return Date.now() / 1000 + serverOffset; }

function fmtCountdown(seconds) {
  if (seconds == null) return "--:--:--";
  const neg = seconds < 0;
  let s = Math.floor(Math.abs(seconds));
  const d = Math.floor(s / 86400); s %= 86400;
  const h = Math.floor(s / 3600); s %= 3600;
  const m = Math.floor(s / 60); s %= 60;
  const hh = String(h).padStart(2, "0"), mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  const core = d > 0 ? `${d}天 ${hh}:${mm}:${ss}` : `${hh}:${mm}:${ss}`;
  return neg ? `+${core}` : core;
}

function fmtClock(unix) {
  if (unix == null) return "--";
  const d = new Date(unix * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/* ---------- 数据同步 ---------- */

async function fetchState() {
  try {
    const res = await fetch("/api/state");
    const data = await res.json();
    serverOffset = data.server_time - Date.now() / 1000;
    bosses = data.bosses;
    $("#conn-status").textContent = "已连接";
    $("#conn-status").className = "conn online";
    const ips = (data.lan_ips && data.lan_ips.length ? data.lan_ips : ["本机IP"]);
    $("#lan-hint").textContent = `队友访问: ${ips.map((ip) => `http://${ip}:${data.port}`).join("　或　")}`;
    render();
  } catch (e) {
    $("#conn-status").textContent = "连接断开，重试中…";
    $("#conn-status").className = "conn offline";
  }
}

/* ---------- 渲染 ---------- */

function render() {
  const list = $("#boss-list");
  $("#empty-tip").classList.toggle("hidden", bosses.length > 0);

  list.innerHTML = bosses.map((b) => {
    const meta = [];
    if (b.mode === "range") meta.push(`<div>复活区间：<b>${b.respawn_min} ~ ${b.respawn_max} 分钟</b></div>`);
    else meta.push(`<div>复活间隔：<b>${b.respawn_minutes} 分钟</b></div>`);
    if (b.location) meta.push(`<div>位置：<b>${esc(b.location)}</b></div>`);
    if (b.drops) meta.push(`<div>掉落：<b>${esc(b.drops)}</b></div>`);
    if (b.notes) meta.push(`<div>备注：<b>${esc(b.notes)}</b></div>`);
    if (b.last_kill_at) meta.push(`<div>击杀时间：${fmtClock(b.last_kill_at)}</div>`);

    return `
    <div class="boss-card status-${b.status}" data-id="${b.id}">
      <div class="card-head">
        <span class="boss-name">${esc(b.name)}</span>
        <span class="status-badge status-${b.status}">${esc(b.status_label)}</span>
      </div>
      <div class="cd-area" data-cd="${b.id}"></div>
      <div class="boss-meta">${meta.join("")}</div>
      <div class="card-actions">
        <button class="btn small primary" data-act="kill">🗡 击杀</button>
        <button class="btn small" data-act="edit">编辑</button>
        <button class="btn small" data-act="reset">重置</button>
        <button class="btn small danger" data-act="del">删除</button>
      </div>
    </div>`;
  }).join("");

  tickCountdowns();
}

function tickCountdowns() {
  const t = now();
  for (const b of bosses) {
    const area = document.querySelector(`[data-cd="${b.id}"]`);
    if (!area) continue;

    if (b.status === "idle") {
      area.innerHTML = `<div class="countdown sub">尚未标记击杀，等待首杀开启计时</div>`;
      continue;
    }

    if (b.mode === "range") {
      const minLeft = b.next_at - t;
      const maxLeft = b.next_at_max - t;
      let main, cls = "";
      if (b.status === "respawned") { main = "应已复活"; cls = "done"; }
      else if (b.status === "possible") { main = `最早 ${fmtCountdown(minLeft)}`; cls = "warn"; }
      else main = fmtCountdown(minLeft);
      area.innerHTML = `
        <div class="countdown ${cls}">${main}</div>
        <div class="countdown sub">最早 ${fmtCountdown(minLeft)} ｜ 最晚 ${fmtCountdown(maxLeft)}
          ｜ 最早出现 ${fmtClock(b.next_at)} · 最晚 ${fmtClock(b.next_at_max)}</div>`;
    } else {
      const left = b.next_at - t;
      if (b.status === "respawned") {
        area.innerHTML = `<div class="countdown done">已复活（${fmtCountdown(left)}）</div>
          <div class="countdown sub">复活时刻 ${fmtClock(b.next_at)}</div>`;
      } else {
        area.innerHTML = `<div class="countdown">${fmtCountdown(left)}</div>
          <div class="countdown sub">预计复活 ${fmtClock(b.next_at)}</div>`;
      }
    }
  }
}

/* ---------- API 调用 ---------- */

async function api(url, method = "GET", body = null) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(url, opts);
  if (!res.ok) {
    let msg = `请求失败 (${res.status})`;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    // FastAPI 的校验错误是数组，取第一条
    if (Array.isArray(msg) && msg[0]) msg = msg[0].msg || msg[0];
    throw new Error(msg);
  }
  return res.json();
}

/* ---------- Boss 弹窗（新增/编辑） ---------- */

function openBossModal(boss = null) {
  editingId = boss ? boss.id : null;
  $("#boss-modal-title").textContent = boss ? "编辑 Boss" : "添加 Boss";
  $("#f-name").value = boss ? boss.name : "";
  document.querySelector(`input[name="f-mode"][value="${boss ? boss.mode : "fixed"}"]`).checked = true;
  $("#f-minutes").value = boss && boss.respawn_minutes != null ? boss.respawn_minutes : "";
  $("#f-range-min").value = boss && boss.respawn_min != null ? boss.respawn_min : "";
  $("#f-range-max").value = boss && boss.respawn_max != null ? boss.respawn_max : "";
  $("#f-location").value = boss ? boss.location : "";
  $("#f-drops").value = boss ? boss.drops : "";
  $("#f-notes").value = boss ? boss.notes : "";
  syncModeUI();
  $("#boss-modal").classList.remove("hidden");
  $("#f-name").focus();
}

function syncModeUI() {
  const mode = document.querySelector('input[name="f-mode"]:checked').value;
  $("#mode-fixed").classList.toggle("hidden", mode !== "fixed");
  $("#mode-range").classList.toggle("hidden", mode !== "range");
}

async function submitBossForm(e) {
  e.preventDefault();
  const mode = document.querySelector('input[name="f-mode"]:checked').value;
  const body = {
    name: $("#f-name").value.trim(),
    mode,
    location: $("#f-location").value.trim(),
    drops: $("#f-drops").value.trim(),
    notes: $("#f-notes").value.trim(),
  };
  if (mode === "fixed") {
    body.respawn_minutes = parseFloat($("#f-minutes").value);
    if (!body.respawn_minutes || body.respawn_minutes <= 0) { alert("请填写复活间隔（分钟）"); return; }
  } else {
    body.respawn_min = parseFloat($("#f-range-min").value);
    body.respawn_max = parseFloat($("#f-range-max").value);
    if (!body.respawn_min || !body.respawn_max || body.respawn_min > body.respawn_max) {
      alert("请填写正确的复活区间（最早 ≤ 最晚）"); return;
    }
  }
  try {
    if (editingId) await api(`/api/bosses/${editingId}`, "PUT", body);
    else await api("/api/bosses", "POST", body);
    closeModals();
    fetchState();
  } catch (err) { alert(err.message); }
}

/* ---------- 击杀弹窗 ---------- */

function openKillModal(boss) {
  killTargetId = boss.id;
  $("#kill-boss-name").textContent = boss.name;
  document.querySelector('input[name="kill-time"][value="now"]').checked = true;
  const dt = $("#kill-custom-time");
  dt.classList.add("hidden");
  // 默认补录时间 = 当前本地时间，精确到分钟
  const d = new Date(Date.now() + serverOffset * 1000);
  d.setSeconds(0, 0);
  const pad = (n) => String(n).padStart(2, "0");
  dt.value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  $("#kill-modal").classList.remove("hidden");
}

async function confirmKill() {
  const mode = document.querySelector('input[name="kill-time"]:checked').value;
  let killAt = null;
  if (mode === "custom") {
    const v = $("#kill-custom-time").value;
    if (!v) { alert("请选择补录的击杀时间"); return; }
    killAt = new Date(v).getTime() / 1000;
  }
  try {
    await api(`/api/bosses/${killTargetId}/kill`, "POST", killAt ? { kill_at: killAt } : null);
    closeModals();
    fetchState();
  } catch (err) { alert(err.message); }
}

/* ---------- 其他操作 ---------- */

async function resetBoss(boss) {
  if (!confirm(`清除「${boss.name}」的击杀记录？计时将回到未开始状态。`)) return;
  try { await api(`/api/bosses/${boss.id}/reset`, "POST"); fetchState(); }
  catch (err) { alert(err.message); }
}

async function deleteBoss(boss) {
  if (!confirm(`确定删除「${boss.name}」？此操作不可恢复。`)) return;
  try { await api(`/api/bosses/${boss.id}`, "DELETE"); fetchState(); }
  catch (err) { alert(err.message); }
}

function closeModals() {
  document.querySelectorAll(".modal").forEach((m) => m.classList.add("hidden"));
  editingId = null;
  killTargetId = null;
}

/* ---------- 事件绑定 ---------- */

$("#btn-add").addEventListener("click", () => openBossModal());
$("#boss-form").addEventListener("submit", submitBossForm);
$("#btn-kill-confirm").addEventListener("click", confirmKill);
document.querySelectorAll('input[name="f-mode"]').forEach((r) => r.addEventListener("change", syncModeUI));
document.querySelectorAll('input[name="kill-time"]').forEach((r) =>
  r.addEventListener("change", () => $("#kill-custom-time").classList.toggle("hidden", r.value !== "custom" || !r.checked)));
document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeModals));
document.querySelectorAll(".modal").forEach((m) =>
  m.addEventListener("mousedown", (e) => { if (e.target === m) closeModals(); }));

// 事件委托：卡片按钮
$("#boss-list").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const card = btn.closest(".boss-card");
  const boss = bosses.find((b) => b.id === card.dataset.id);
  if (!boss) return;
  ({ kill: openKillModal, edit: () => openBossModal(boss), reset: resetBoss, del: deleteBoss })[btn.dataset.act](boss);
});

// 本地每秒刷新倒计时；每 2 秒向服务器同步一次
setInterval(tickCountdowns, 1000);
setInterval(fetchState, 2000);
fetchState();

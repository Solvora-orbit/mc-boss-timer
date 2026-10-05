/* MC Boss 计时器 - 前端逻辑
 *
 * 数据同步：每 2 秒轮询 /api/state（以服务器时间为准，校准本地时钟）；
 * 倒计时/进度条本地每秒刷新。
 * 渲染：卡片 DOM 按 Boss id 复用（不整块重绘），列表刷新不闪烁、动画不重放。
 */
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

/* ---------- 渲染（DOM 复用，避免整块重绘导致的闪烁与动画重放） ---------- */

// 卡片骨架只创建一次；文本/状态每次刷新时原位更新
function buildCard(b) {
  const card = document.createElement("div");
  card.className = "boss-card";
  card.innerHTML = `
    <div class="card-head">
      <span class="boss-name"></span>
      <span class="status-badge"></span>
    </div>
    <div class="cd-area"></div>
    <div class="progress-wrap"><div class="progress-bar"></div><div class="progress-range"></div></div>
    <div class="boss-meta"></div>
    <div class="card-actions">
      <button class="btn small primary" data-act="kill">🗡 击杀</button>
      <button class="btn small" data-act="edit">编辑</button>
      <button class="btn small" data-act="reset">重置</button>
      <button class="btn small danger" data-act="del">删除</button>
    </div>`;
  return card;
}

function buildMetaHTML(b) {
  const meta = [];
  if (b.mode === "range") meta.push(`<div>复活区间：<b>${b.respawn_min} ~ ${b.respawn_max} 分钟</b></div>`);
  else meta.push(`<div>复活间隔：<b>${b.respawn_minutes} 分钟</b></div>`);
  if (b.location) meta.push(`<div>位置：<b>${esc(b.location)}</b></div>`);
  if (b.drops) meta.push(`<div>掉落：<b>${esc(b.drops)}</b></div>`);
  if (b.notes) meta.push(`<div>备注：<b>${esc(b.notes)}</b></div>`);
  if (b.ocr_keywords) meta.push(`<div>OCR关键词：<span class="keyword-tag">${esc(b.ocr_keywords)}</span></div>`);
  if (b.last_kill_at) meta.push(`<div>击杀时间：${fmtClock(b.last_kill_at)}</div>`);
  return meta.join("");
}

// 只更新有变化的节点（meta 用 HTML 缓存比对），避免打断 hover/选中状态
function updateCard(card, b) {
  if (card.dataset.id !== b.id) card.dataset.id = b.id;
  card.className = `boss-card status-${b.status}`;

  const nameEl = card.querySelector(".boss-name");
  if (nameEl.textContent !== b.name) nameEl.textContent = b.name;

  const badge = card.querySelector(".status-badge");
  if (badge.textContent !== b.status_label) badge.textContent = b.status_label;
  badge.className = `status-badge status-${b.status}`;

  const metaHTML = buildMetaHTML(b);
  const metaEl = card.querySelector(".boss-meta");
  if (metaEl.dataset.cache !== metaHTML) {
    metaEl.innerHTML = metaHTML;
    metaEl.dataset.cache = metaHTML;
  }
}

function render() {
  const list = $("#boss-list");
  $("#empty-tip").classList.toggle("hidden", bosses.length > 0);

  const existing = new Map([...list.children].map((el) => [el.dataset.id, el]));
  const used = new Set();
  for (const b of bosses) {           // 依次 append 自动完成重排
    const card = existing.get(b.id) || buildCard(b);
    updateCard(card, b);
    list.appendChild(card);
    used.add(b.id);
  }
  existing.forEach((el, id) => { if (!used.has(id)) el.remove(); });

  tickCountdowns();
}

// 每秒刷新：倒计时文本 + 进度条 + 临界状态样式
function tickCountdowns() {
  const t = now();
  for (const b of bosses) {
    const card = document.querySelector(`.boss-card[data-id="${b.id}"]`);
    if (!card) continue;
    const area = card.querySelector(".cd-area");
    const bar = card.querySelector(".progress-bar");
    const rangeMark = card.querySelector(".progress-range");

    // ---- 倒计时区 ----
    if (b.status === "idle") {
      area.innerHTML = `<div class="countdown sub">尚未标记击杀，等待首杀开启计时</div>`;
      bar.style.width = "0%";
    } else if (b.mode === "range") {
      const minLeft = b.next_at - t;
      const maxLeft = b.next_at_max - t;
      let main, cls = "";
      if (b.status === "respawned") { main = "应已复活"; cls = "done"; }
      else if (b.status === "possible") { main = `最早 ${fmtCountdown(minLeft)}`; cls = "warn"; }
      else main = fmtCountdown(minLeft);
      // 距最早复活不足 60 秒：数字变红脉冲
      if (b.status === "waiting" && minLeft <= 60) cls = "warn-last";
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
        const cls = left <= 60 ? "warn-last" : "";
        area.innerHTML = `<div class="countdown ${cls}">${fmtCountdown(left)}</div>
          <div class="countdown sub">预计复活 ${fmtClock(b.next_at)}</div>`;
      }
    }

    // ---- 复活进度条 ----
    if (b.last_kill_at && b.next_at != null) {
      const total = (b.mode === "range" ? b.next_at_max : b.next_at) - b.last_kill_at;
      const done = Math.min(1, Math.max(0, (t - b.last_kill_at) / total));
      bar.style.width = `${(done * 100).toFixed(2)}%`;
      if (b.mode === "range") {
        // 区间模式：进度条上标出最早~最晚的复活窗口段
        const startPct = ((b.next_at - b.last_kill_at) / total * 100).toFixed(2);
        const widthPct = ((b.next_at_max - b.next_at) / total * 100).toFixed(2);
        rangeMark.style.left = `${startPct}%`;
        rangeMark.style.width = `${widthPct}%`;
        rangeMark.style.display = "block";
      } else {
        rangeMark.style.display = "none";
      }
      card.querySelector(".progress-wrap").style.visibility = "visible";
    } else {
      card.querySelector(".progress-wrap").style.visibility = "hidden";
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
  $("#f-ocr-keywords").value = boss ? (boss.ocr_keywords || "") : "";
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
    ocr_keywords: $("#f-ocr-keywords").value.trim(),
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

/* ---------- 设置弹窗 ---------- */

async function openSettingsModal() {
  try {
    const s = await api("/api/settings");
    $("#s-ocr-enabled").checked = s.ocr.enabled;
    $("#s-ocr-runmode").value = s.ocr.run_mode || "always";
    $("#s-ocr-engine").value = s.ocr.engine;
    $("#s-ocr-interval").value = s.ocr.interval;
    $("#s-ocr-mode").value = s.ocr.mode;
    $("#s-ocr-cooldown").value = s.ocr.cooldown;
    $("#s-ocr-foreground").checked = !!s.ocr.only_game_foreground;
    $("#s-ocr-gamekw").value = s.ocr.game_window_keyword || "Minecraft";
    const r = s.ocr.region;
    $("#s-ocr-region").textContent = r ? `x=${r.x}, y=${r.y}, ${r.w}×${r.h}` : "全屏（在悬浮窗⚙里框选）";
    $("#s-notify-enabled").checked = s.notify.enabled;
    $("#s-notify-sound").checked = s.notify.sound;
    $("#s-notify-toast").checked = s.notify.toast;
    $("#s-notify-banner").checked = s.notify.banner;
    $("#s-notify-range").value = s.notify.range_mode;
    $("#ocr-test-result").textContent = "";
    renderKeywordManager();
    $("#settings-modal").classList.remove("hidden");
  } catch (err) { alert(err.message); }
}

// 关键词集中管理：一处查看/批量修改所有 Boss 的识别关键词
function renderKeywordManager() {
  const wrap = $("#s-kw-list");
  if (!bosses.length) {
    wrap.innerHTML = `<span class="settings-note">还没有 Boss</span>`;
    return;
  }
  wrap.innerHTML = bosses.map((b) => `
    <div class="kw-row" data-id="${b.id}">
      <span class="kw-name" title="${esc(b.name)}">${esc(b.name)}</span>
      <input type="text" class="kw-input" maxlength="200"
             placeholder="逗号分隔，如：${esc(b.name)},击杀" value="${esc(b.ocr_keywords || "")}">
    </div>`).join("");
}

// 只提交有改动的行，返回改动数量
async function saveKeywordManager() {
  const rows = [...document.querySelectorAll("#s-kw-list .kw-row")];
  let saved = 0;
  for (const row of rows) {
    const boss = bosses.find((b) => b.id === row.dataset.id);
    if (!boss) continue;
    const val = row.querySelector(".kw-input").value.trim();
    if (val !== (boss.ocr_keywords || "")) {
      await api(`/api/bosses/${boss.id}`, "PUT", { ...boss, ocr_keywords: val });
      saved++;
    }
  }
  fetchState();
  return saved;
}

async function saveSettings() {
  let kwMsg = "";
  try {
    const n = await saveKeywordManager();
    if (n) kwMsg = `（已更新 ${n} 个 Boss 的关键词）`;
  } catch (err) { alert(`关键词保存失败：${err.message}`); }
  const patch = {
    ocr: {
      enabled: $("#s-ocr-enabled").checked,
      run_mode: $("#s-ocr-runmode").value,
      engine: $("#s-ocr-engine").value,
      interval: Math.max(1, parseInt($("#s-ocr-interval").value) || 3),
      mode: $("#s-ocr-mode").value,
      cooldown: Math.max(5, parseInt($("#s-ocr-cooldown").value) || 60),
      only_game_foreground: $("#s-ocr-foreground").checked,
      game_window_keyword: $("#s-ocr-gamekw").value.trim() || "Minecraft",
    },
    notify: {
      enabled: $("#s-notify-enabled").checked,
      sound: $("#s-notify-sound").checked,
      toast: $("#s-notify-toast").checked,
      banner: $("#s-notify-banner").checked,
      range_mode: $("#s-notify-range").value,
    },
  };
  try {
    await api("/api/settings", "PUT", patch);
    closeModals();
    if (kwMsg) alert("已保存" + kwMsg);
  } catch (err) { alert(err.message); }
}

async function ocrTestOnce() {
  const el = $("#ocr-test-result");
  el.textContent = "识别中…";
  try {
    const res = await api("/api/watcher/test", "POST");
    if (res.error) { el.textContent = `未执行：${res.error}`; return; }
    el.textContent = res.hits && res.hits.length
      ? `识别：${res.text.slice(0, 60)}　命中：${res.hits.join("、")}`
      : `识别：${(res.text || "（无文字）").slice(0, 60)}`;
  } catch (err) { el.textContent = `失败：${err.message}`; }
}

/* ---------- 配置导出/导入（换机/重装一键迁移） ---------- */

async function exportConfig() {
  try {
    const data = await api("/api/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `mc-boss-timer-配置-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (err) { alert(err.message); }
}

async function importConfig(file) {
  try {
    const data = JSON.parse(await file.text());
    const res = await api("/api/import", "POST", data);
    alert(`导入完成：新增 ${res.added} 个，更新 ${res.updated} 个，跳过 ${res.skipped} 条无效记录`);
    fetchState();
  } catch (err) { alert(`导入失败：${err.message}`); }
}

/* ---------- 历史弹窗 ---------- */

const SOURCE_LABEL = { manual: "手动", hotkey: "热键", ocr: "OCR" };

async function openHistoryModal() {
  try {
    const data = await api("/api/history?limit=200");
    const stats = $("#history-stats");
    const statItems = Object.entries(data.stats).map(([id, st]) => {
      const boss = bosses.find((b) => b.id === id);
      const avg = st.avg_interval != null ? `平均 ${Math.round(st.avg_interval / 60)} 分一轮` : "";
      return `<span class="stat"><b>${esc(boss ? boss.name : "?")}</b> ${st.count} 次 ${avg}</span>`;
    });
    stats.innerHTML = statItems.join("") || `<span class="stat">暂无击杀记录</span>`;

    $("#history-body").innerHTML = data.entries.map((e) => {
      const boss = bosses.find((b) => b.id === e.boss_id);
      return `<tr>
        <td>${fmtClock(e.kill_at)}</td>
        <td>${esc(boss ? boss.name : e.boss_name)}</td>
        <td><span class="source-badge source-${e.source}">${SOURCE_LABEL[e.source] || e.source}</span></td>
      </tr>`;
    }).join("") || `<tr><td colspan="3" style="color:var(--text-dim)">还没有击杀记录，标记一次击杀试试</td></tr>`;
    $("#history-modal").classList.remove("hidden");
  } catch (err) { alert(err.message); }
}

async function clearHistory() {
  if (!confirm("确定清空全部击杀历史？")) return;
  try { await api("/api/history", "DELETE"); openHistoryModal(); }
  catch (err) { alert(err.message); }
}

/* ---------- 事件绑定 ---------- */

$("#btn-add").addEventListener("click", () => openBossModal());
$("#btn-settings").addEventListener("click", openSettingsModal);
$("#btn-settings-save").addEventListener("click", saveSettings);
$("#btn-ocr-test").addEventListener("click", ocrTestOnce);
$("#btn-history").addEventListener("click", openHistoryModal);
$("#btn-history-clear").addEventListener("click", clearHistory);
$("#btn-export").addEventListener("click", exportConfig);
$("#btn-import").addEventListener("click", () => $("#import-file").click());
$("#import-file").addEventListener("change", (e) => {
  if (e.target.files[0]) importConfig(e.target.files[0]);
  e.target.value = "";
});
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

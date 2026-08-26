const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

let accounts = [];          // 当前编辑中的账号列表（含 targets）
let toastTimer = null;
let addingTarget = -1;      // 正在添加目标的账号下标；-1 表示不显示表单
let confirmResolve = null;  // 自绘确认弹窗的 Promise 回调

function toast(msg) {
  const el = $("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function numVal(id, def) {
  const v = parseInt($(id)?.value, 10);
  return Number.isFinite(v) ? v : def;
}
function boolVal(id) { return !!$(id)?.checked; }
function strVal(id) { return ($(id)?.value ?? "").trim(); }

// ---------- 配置 -> 表单 ----------
function fillForm(cfg) {
  $("enabled").checked = !!cfg.enabled;
  $("poll_interval_seconds").value = cfg.poll_interval_seconds ?? 300;
  $("poll_jitter_seconds").value = cfg.poll_jitter_seconds ?? 10;

  $("render_backend").value = cfg.render?.backend ?? "html";
  $("aspect_ratio").value = (cfg.image?.aspect_ratio) ?? "1:1";
  $("corner_radius").value = cfg.image?.corner_radius ?? 24;
  $("emoji_mode").value = cfg.render?.emoji_mode ?? "strip";
  $("font_path").value = cfg.render?.font_path ?? "";

  $("caption_format").value = cfg.send?.caption_format ?? "{platform} · {author} · {time}";
  $("send_caption").checked = !!cfg.send?.send_caption;
  $("link_to_post").checked = cfg.send?.link_to_post !== false;
  $("max_jobs_per_account").value = cfg.send?.max_jobs_per_account ?? 10;

  $("proxy").value = cfg.network?.proxy ?? "";
  $("proxy_enabled").checked = !!cfg.network?.proxy_enabled;
  $("timeout_seconds").value = cfg.network?.timeout_seconds ?? 15;
  $("max_concurrency").value = cfg.network?.max_concurrency ?? 3;

  $("bili_cookie").value = cfg.credentials?.bilibili?.cookie ?? "";
  $("x_bearer").value = cfg.credentials?.x?.bearer_token ?? "";
  $("x_mode").value = cfg.credentials?.x?.mode ?? "api";
  $("x_rss_base").value = cfg.credentials?.x?.rss_base ?? "https://rsshub.app";

  accounts = (cfg.accounts || []).map((a) => ({
    platform: a.platform, account_id: a.account_id, name: a.display_name || "",
    enabled: a.enabled !== false, targets: (a.targets || []).map((t) => ({ type: t.type, id: t.id })),
  }));
  renderAccounts();
}

// ---------- 表单 -> 配置 ----------
function collect() {
  return {
    enabled: boolVal("enabled"),
    poll_interval_seconds: numVal("poll_interval_seconds", 300),
    poll_jitter_seconds: numVal("poll_jitter_seconds", 10),
    image: { aspect_ratio: strVal("aspect_ratio"), corner_radius: numVal("corner_radius", 24) },
    render: { backend: strVal("render_backend"), emoji_mode: strVal("emoji_mode"), font_path: strVal("font_path") },
    send: {
      caption_format: strVal("caption_format"),
      send_caption: boolVal("send_caption"),
      link_to_post: boolVal("link_to_post"),
      max_jobs_per_account: numVal("max_jobs_per_account", 10),
    },
    network: {
      proxy: strVal("proxy"), proxy_enabled: boolVal("proxy_enabled"),
      timeout_seconds: numVal("timeout_seconds", 15), max_concurrency: numVal("max_concurrency", 3),
    },
    credentials: {
      bilibili: { cookie: strVal("bili_cookie") },
      x: { bearer_token: strVal("x_bearer"), mode: strVal("x_mode"), rss_base: strVal("x_rss_base") },
    },
    accounts,
  };
}

// ---------- 账号管理 ----------
function targetsText(a) {
  if (!a.targets.length) return "（无）";
  const label = { group: "群聊", private: "私聊", umo: "UMO" };
  return a.targets.map((t) => `${label[t.type] || t.type} ${t.id}`).join("、");
}

function renderAccounts() {
  const box = $("accounts-box");
  if (!box) return;
  let html = "";
  if (accounts.length) {
    html += '<div class="accounts-grid">' + accounts.map((a, i) => {
      const card = `
        <div class="account-card">
          <div class="ac-head">
            <span class="ac-title">${esc(a.name || a.account_id)}</span>
            <span class="ac-badge">${esc(a.platform)}</span>
          </div>
          <div class="ac-targets">ID：${esc(a.account_id)}<br>投递：${esc(targetsText(a))}</div>
          <div class="ac-actions">
            <button class="btn ghost small" data-act="toggle" data-i="${i}">${a.enabled ? "停用" : "启用"}</button>
            <button class="btn ghost small" data-act="target" data-i="${i}">加目标</button>
            <button class="btn danger small" data-act="remove" data-i="${i}">删除</button>
          </div>
        </div>`;
      return card + (addingTarget === i ? renderTargetForm(i) : "");
    }).join("") + "</div>";
  } else {
    html += '<div class="empty">尚未订阅账号，可在此添加</div>';
  }
  html += `
    <div class="add-account" style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <select id="new_platform" style="padding:8px;border:1px solid var(--border);border-radius:9px"><option value="bilibili">bilibili</option><option value="x">x</option></select>
      <input id="new_account_id" placeholder="account_id / UID / screen_name" style="flex:1;padding:8px;border:1px solid var(--border);border-radius:9px"/>
      <input id="new_display_name" placeholder="可选 display_name" style="flex:1;padding:8px;border:1px solid var(--border);border-radius:9px"/>
      <button class="btn primary" id="btn-add-account">添加账号</button>
    </div>`;
  box.innerHTML = html;

  box.querySelector("#btn-add-account")?.addEventListener("click", addAccount);
  box.querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      const i = +btn.dataset.i;
      const act = btn.dataset.act;
      if (act === "remove") {
        accounts.splice(i, 1);
        toast("已删除该账号（记得点保存生效）");
        if (addingTarget === i) addingTarget = -1;
      } else if (act === "toggle") {
        accounts[i].enabled = !accounts[i].enabled;
        toast(accounts[i].enabled ? "已启用该账号" : "已停用该账号");
      } else if (act === "target") {
        addingTarget = addingTarget === i ? -1 : i;
      }
      renderAccounts();
    });
  });
  box.querySelectorAll(".target-form button[data-tf]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      const i = +btn.dataset.i;
      if (btn.dataset.tf === "ok") confirmAddTarget(i);
      else { addingTarget = -1; renderAccounts(); }
    });
  });
}

function renderTargetForm(i) {
  return `
    <div class="target-form">
      <div class="tf-row">
        <select id="tf_type_${i}">
          <option value="group">群聊</option>
          <option value="private">私聊</option>
          <option value="umo">UMO（直接填 unified_msg_origin）</option>
        </select>
        <input id="tf_id_${i}" placeholder="群号 / QQ号 / UMO 字符串"/>
      </div>
      <div class="tf-row" style="justify-content:flex-end">
        <button class="btn ghost small" data-tf="cancel" data-i="${i}">取消</button>
        <button class="btn primary small" data-tf="ok" data-i="${i}">添加</button>
      </div>
    </div>`;
}

function addAccount() {
  const platform = $("new_platform").value;
  const account_id = $("new_account_id").value.trim();
  const display_name = $("new_display_name").value.trim();
  if (!account_id) { toast("请填写 account_id"); return; }
  accounts.push({ platform, account_id, name: display_name, enabled: true, targets: [] });
  toast(`已添加 ${platform}:${account_id}（点「保存配置」生效）`);
  renderAccounts();
}

function confirmAddTarget(i) {
  const type = $("tf_type_" + i).value;
  const id = $("tf_id_" + i).value.trim();
  if (!id) { toast("请填写目标 ID"); return; }
  accounts[i].targets.push({ type, id });
  addingTarget = -1;
  toast("已添加投递目标（点「保存配置」生效）");
  renderAccounts();
}

// ---------- 自绘确认弹窗（原生 confirm 在 AstrBot iframe 中被禁用） ----------
function confirmAction(message, mark = "!") {
  return new Promise((resolve) => {
    confirmResolve = resolve;
    $("confirm-message").textContent = message;
    $("confirm-mark").textContent = mark;
    $("confirm-modal").classList.add("show");
  });
}
function closeConfirm(val) {
  $("confirm-modal").classList.remove("show");
  if (confirmResolve) { confirmResolve(val); confirmResolve = null; }
}

// ---------- 加载/保存 ----------
async function loadConfig() {
  try {
    const cfg = await bridge.apiGet("config");
    fillForm(cfg);
    $("save-tip").textContent = "修改完成后，请保存配置";
  } catch (e) {
    toast("加载配置失败：" + e);
  }
}
async function saveConfig() {
  try {
    const payload = collect();
    const r = await bridge.apiPost("config/save", payload);
    if (r && r.saved) toast("配置已保存，正在生效");
    else toast("保存失败（请重试）");
  } catch (e) {
    toast("保存失败：" + e);
  }
}

// ---------- 状态 ----------
async function loadStatus() {
  try {
    const s = await bridge.apiGet("status");
    const dot = $("stat-dot");
    if (dot) dot.classList.toggle("on", !!s.running && s.enabled);
    $("stat-text").textContent = `${s.enabled ? "已启用" : "已暂停"} · 账号 ${s.account_count}`;
    const last = s.last_run ? new Date(s.last_run * 1000).toLocaleString() : "从未";
    $("status-detail").innerHTML = `
      <div class="info-grid">
        <div class="info-item"><span class="info-label">插件启用</span><b>${s.enabled ? "是" : "否"}</b></div>
        <div class="info-item"><span class="info-label">轮询运行中</span><b>${s.running ? "是" : "否"}</b></div>
        <div class="info-item"><span class="info-label">轮询间隔</span><b>${s.poll_interval}s</b></div>
        <div class="info-item"><span class="info-label">上次检查</span><b>${last}</b></div>
        <div class="info-item"><span class="info-label">自启动投递</span><b>${s.processed_since_start}</b></div>
        <div class="info-item"><span class="info-label">已去重记录</span><b>${s.storage_count}</b></div>
        <div class="info-item"><span class="info-label">订阅账号</span><b>${s.account_count}（启用 ${s.enabled_accounts}）</b></div>
      </div>`;
  } catch (e) { $("status-detail").textContent = "加载状态失败：" + e; }
}
async function doCheck() {
  toast("正在触发检查…");
  try {
    const r = await bridge.apiPost("check", {});
    const txt = r.skipped ? `跳过：${r.skipped}` : `抓到 ${r.fetched}，新帖 ${r.new}，投递成功 ${r.sent}，失败 ${r.failed}，异常 ${r.errored}`;
    toast("检查完成：" + txt);
    loadStatus();
  } catch (e) { toast("触发失败：" + e); }
}
async function doClear() {
  const ok = await confirmAction("确认清空全部已处理记录？此操作不可撤销。");
  if (!ok) return;
  const r = await bridge.apiPost("history/clear", {});
  toast(`已清空 ${r.cleared} 条记录`);
  loadStatus();
}
async function loadCards() {
  try {
    const r = await bridge.apiGet("cards");
    const files = r.files || [];
    $("cards-list").innerHTML = files.length
      ? files.map((f) => `<div class="info-item"><span class="info-label">${esc(f.name)}</span><b>${(f.size / 1024).toFixed(0)}KB</b></div>`).join("")
      : '<div class="empty">暂无卡片</div>';
  } catch (e) { $("cards-list").innerHTML = '<div class="empty">加载失败：' + esc(e) + "</div>"; }
}

// ---------- 平滑滚动（复刻 eidolon 页面的动效） ----------
function initSmoothWheelScroll() {
  const content = document.querySelector(".content");
  if (!content) return;
  let target = content.scrollTop;
  let frame = 0;
  const tick = () => {
    const distance = target - content.scrollTop;
    if (Math.abs(distance) < 0.5) {
      content.scrollTop = target;
      frame = 0;
      return;
    }
    content.scrollTop += distance * 0.16;
    frame = requestAnimationFrame(tick);
  };
  content.addEventListener("scroll", () => {
    if (!frame) target = content.scrollTop;
  }, { passive: true });
  content.addEventListener("wheel", (e) => {
    // 只放行可滚动文本域；普通输入框/下拉滚轮交给页面平滑滚动
    if (e.ctrlKey || e.target.closest("textarea")) return;
    const max = content.scrollHeight - content.clientHeight;
    if (max <= 0) return;
    const delta = e.deltaMode === 2 ? e.deltaY * content.clientHeight : e.deltaY;
    target = Math.max(0, Math.min(max, target + delta));
    e.preventDefault();
    if (!frame) frame = requestAnimationFrame(tick);
  }, { passive: false });
}

function scrollContentToTop() {
  const content = document.querySelector(".content");
  if (!content) return;
  let frame = 0;
  const tick = () => {
    const distance = 0 - content.scrollTop;
    if (Math.abs(distance) < 0.5) { content.scrollTop = 0; frame = 0; return; }
    content.scrollTop += distance * 0.16;
    frame = requestAnimationFrame(tick);
  };
  frame = requestAnimationFrame(tick);
}

// ---------- 导航 ----------
function setupNav() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
      const view = document.getElementById("view-" + btn.dataset.view);
      if (view) view.classList.add("active");
      if (btn.dataset.view === "status") { loadStatus(); loadCards(); }
      scrollContentToTop();
    });
  });
}

async function init() {
  const ctx = await bridge.ready();
  setupNav();
  initSmoothWheelScroll();
  $("btn-save").addEventListener("click", saveConfig);
  $("btn-check").addEventListener("click", doCheck);
  $("btn-clear").addEventListener("click", doClear);
  $("confirm-ok").addEventListener("click", () => closeConfirm(true));
  $("confirm-cancel").addEventListener("click", () => closeConfirm(false));
  $("about-info").innerHTML = `
    <div class="info-item"><span class="info-label">名称</span><b>astrbot_plugin_phantasm</b></div>
    <div class="info-item"><span class="info-label">显示名</span><b>Phantasm</b></div>
    <div class="info-item"><span class="info-label">版本</span><b>1.0.0</b></div>
    <div class="info-item"><span class="info-label">能力</span><b>Bilibili / X 发帖监听 · 卡片投递</b></div>`;
  await loadConfig();
  if (ctx?.isDark) document.documentElement.classList.add("dark");
}

init();

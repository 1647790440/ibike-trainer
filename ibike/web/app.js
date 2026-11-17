/* iBike 控制台前端（Hallmark · Formwork · app screen）
 * 只做三件事：把状态画出来、把操作发出去、把这一场的曲线留下来。
 * 从零写的，旧的 app.js 不再引用。
 */
'use strict';

const $ = (id) => document.getElementById(id);
const WS_PATH = '/ws';
const MAX_LIVE_POINTS = 7200;

const app = {
  snapshot: null,
  view: 'train',
  mode: 'constant',
  templates: [],
  tests: [],
  courses: [],
  params: {},          // 间歇模板的参数值
  templateId: '',
  editingCourse: null,
  plan: null,
  planTimer: 0,
  live: [],            // 实时曲线：[{t, p, target}]
  liveStart: 0,
  reports: [],
  detail: null,
  logLines: 0,
  startPending: false,
};

/* ------------------------------------------------------------------ 基础 */

async function api(path, body, method = 'POST') {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* 空响应 */ }
  if (!res.ok || data.ok === false) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}

function toast(message, kind = 'info') {
  const box = $('toasts');
  if (!box) return;
  const el = document.createElement('p');
  el.className = 'toast';
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => el.remove(), kind === 'error' ? 6000 : 3200);
}

function logLine(kind, message) {
  const list = $('logList');
  if (!list) return;
  if (app.logLines === 0) list.textContent = '';
  const li = document.createElement('li');
  li.className = 'log-line' + (kind === 'error' ? ' log-line--error' : kind === 'ok' ? ' log-line--ok' : '');
  li.textContent = message;
  list.appendChild(li);
  app.logLines += 1;
  while (list.children.length > 120) list.removeChild(list.firstChild);
  list.scrollTop = list.scrollHeight;
}

function num(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const v = Number(value);
  return digits > 0 ? v.toFixed(digits) : String(Math.round(v));
}

function clock(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const total = Math.max(0, Math.round(Number(seconds)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

function when(epoch) {
  if (!epoch) return '—';
  const d = new Date(Number(epoch) * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

/* ------------------------------------------------------------------ 路由 */

function route() {
  const hash = (location.hash || '#/train').replace(/^#\/?/, '');
  const [name, arg] = hash.split('/');
  const view = ['train', 'devices', 'reports'].includes(name) ? name : 'train';
  app.view = view;
  document.querySelectorAll('.view').forEach((el) => { el.hidden = el.id !== `v-${view}`; });
  document.querySelectorAll('.tabs a').forEach((a) => {
    if (a.getAttribute('href') === `#/${view}`) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  if (view === 'reports') {
    loadReports().then(() => { if (arg) openReport(arg); });
  }
}

window.addEventListener('hashchange', route);

/* ------------------------------------------------------------------ websocket */

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws;
  try { ws = new WebSocket(`${proto}://${location.host}${WS_PATH}`); } catch (e) { return; }
  ws.addEventListener('message', (event) => {
    try { render(JSON.parse(event.data)); } catch (e) { console.error('状态解析失败', e); }
  });
  ws.addEventListener('close', () => setTimeout(connect, 1500));
  ws.addEventListener('error', () => { try { ws.close(); } catch (e) { /* 忽略 */ } });
}

/* ------------------------------------------------------------------ 渲染 */

function render(s) {
  app.snapshot = s;
  renderShell(s);
  if (app.view === 'train') renderTrain(s);
  if (app.view === 'devices') renderDevices(s);
}

function renderShell(s) {
  const trainer = s.trainer || {};
  const hr = s.hr_client || {};
  const tChip = $('trainerChip');
  if (tChip) {
    tChip.textContent = trainer.connected ? (trainer.name || '骑行台已连接') : '骑行台未连接';
    tChip.className = 'chip' + (trainer.connected ? ' chip--on' : '');
  }
  const hChip = $('hrChip');
  if (hChip) {
    const on = !!hr.connected;
    hChip.textContent = on ? (hr.name || '心率带已连接') : '心率带未连接';
    hChip.className = 'chip' + (on ? ' chip--on' : '');
  }
  const running = !!s.overlay_running;
  document.querySelectorAll('#btnOverlay, #btnOverlay2').forEach((b) => {
    b.textContent = running ? '关闭悬浮窗' : '悬浮显示';
    b.setAttribute('aria-pressed', running ? 'true' : 'false');
  });
}

function renderTrain(s) {
  const state = s.state || 'idle';
  const live = state === 'running' || state === 'paused';
  const finished = state === 'finished';

  // 标题与状态
  const title = live
    ? (s.step_name || s.plan_name || '训练中')
    : finished ? '训练结束' : state === 'error' ? '出错了' : '准备开始';
  setText('castTitle', title);

  let note = '';
  if (s.trainer_lost && s.auto_reconnecting) note = '骑行台掉线了，正在后台自动重连，踩起来就会自动继续';
  else if (s.trainer_lost) note = '骑行台掉线了，训练已挂起';
  else if (s.auto_paused) note = '检测到没在踩，已自动暂停';
  else if (state === 'paused') note = '已暂停，点「继续」接着骑';
  else if (state === 'running') note = s.is_test ? `测试进行中 · 第 ${s.test_level || 1} 档`
    : s.is_interval ? `第 ${(s.step_index || 0) + 1} / ${s.step_count || '?'} 段 · 本段还剩 ${clock(s.step_remaining_s)}`
      : '控制中';
  else if (finished) note = (s.summary && s.summary.completed) ? '目标完成，报告已保存' : '训练结束，报告已保存';
  else if (state === 'error') note = s.error || '看看运行日志';
  else if (!s.trainer || !s.trainer.connected) note = '先连接骑行台，再设定目标';
  else note = '设定目标功率和时长，然后开始';
  setText('castState', note);

  // 主图 + strip
  const power = s.power;
  const powerInput = Number(($('inPower') || {}).value) || null;
  const planTarget = (s.plan && s.plan.length)
    ? Number((s.plan[s.step_index || 0] || {}).target_power || s.plan[0].target_power || s.plan[0].watts) || null
    : null;
  const idleTarget = s.is_interval || s.is_test ? (planTarget || powerInput) : powerInput;
  const target = s.target_power || (live || finished ? null : idleTarget);
  // 空闲时大数字显示"等会儿要骑多少"，开始后才是实时功率
  const showValue = (power === null || power === undefined) && !live && !finished ? target : power;
  const powerEl = $('power');
  if (powerEl) {
    powerEl.textContent = '';
    powerEl.className = 'power' + (showValue === null || showValue === undefined ? ' power--off' : '');
    powerEl.setAttribute('aria-label', live || finished ? `当前功率 ${num(showValue)} 瓦` : `目标功率 ${num(showValue)} 瓦`);
    powerEl.appendChild(document.createTextNode(num(showValue)));
    const unit = document.createElement('span');
    unit.className = 'power-unit';
    unit.textContent = 'W';
    powerEl.appendChild(unit);
  }
  const scaleMax = target ? Math.max(target * 1.6, target + 40) : 200;
  const ratio = (power !== null && power !== undefined && scaleMax) ? Math.min(1, Math.max(0, power / scaleMax)) : 0;
  const fill = $('stripFill');
  if (fill) fill.style.transform = `scaleX(${ratio.toFixed(4)})`;
  const mark = $('stripMark');
  if (mark && scaleMax && target) mark.style.left = `calc(${(Math.min(1, target / scaleMax) * 100).toFixed(2)}% - 1px)`;
  const zone = $('stripZone');
  if (zone && target && scaleMax) {
    const lo = Math.max(0, (target * 0.95) / scaleMax);
    const hi = Math.min(1, (target * 1.05) / scaleMax);
    zone.style.left = `${(lo * 100).toFixed(2)}%`;
    zone.style.width = `${((hi - lo) * 100).toFixed(2)}%`;
  }
  setText('scaleMid', target ? `目标 ${num(target)} W` : '目标');
  setText('scaleMax', target ? `${num(scaleMax)} W` : '');
  const dev = (power !== null && power !== undefined && target) ? power - target : null;
  setText('stripCaption', dev === null ? '功率 / 目标'
    : `功率 ${num(power)} W，比目标 ${dev >= 0 ? '高' : '低'} ${num(Math.abs(dev))} W`);

  // 数字
  setText('mTarget', target ? `${num(target)} W` : '—');
  const idleDuration = Number(($('inDuration') || {}).value) * 60
    || (s.plan && s.plan.length ? s.plan.reduce((a, st2) => a + (Number(st2.duration_s) || 0), 0) : 0)
    || (s.duration_s || 0);
  setText('mRemain', live || finished ? clock(s.remaining_s) : (idleDuration ? clock(idleDuration) : '—'));
  setText('mElapsed', live || finished ? clock(s.elapsed_s) : '—');
  const hr = s.heart_rate;
  const zoneCode = (s.hr_zone || {}).code;
  setText('mHr', hr === null || hr === undefined ? '—' : `${num(hr)}${zoneCode ? ' ' + zoneCode : ''}`);
  setText('mCadence', s.cadence === null || s.cadence === undefined ? '—' : `${num(s.cadence)} rpm`);
  setText('mResistance', s.resistance_raw === null || s.resistance_raw === undefined ? '—'
    : `${Math.round(Number(s.resistance_raw) / 10)}`);

  // 控件
  const idle = !live && !finished && !app.startPending;
  show('btnStart', idle);
  show('btnPause', state === 'running');
  show('btnResume', state === 'paused');
  show('btnStop', live);
  show('btnSkip', live && !!s.is_interval);
  show('adjustRow', live);
  const startBtn = $('btnStart');
  if (startBtn) {
    startBtn.disabled = app.startPending;
    startBtn.textContent = app.startPending ? '正在开始…' : '开始训练';
  }
  const paused = $('btnPause');
  if (paused) paused.disabled = !!s.trainer_lost;
  const resume = $('btnResume');
  if (resume) resume.disabled = !!s.trainer_lost || !!s.auto_reconnecting;

  // 实时曲线
  sampleLive(s, live);
  drawLiveChart(s, live);

  // 课表进度
  renderTimeline(s);
  renderPlanPreviewIntoSteps(s);

  // 结束之后把总结画在报告页
  if (finished && s.summary) {
    app.detail = s.summary;
    renderSummary(s.summary);
  }
}

function show(id, visible) {
  const el = $(id);
  if (el) el.hidden = !visible;
}

function sampleLive(s, live) {
  if (live && s.power !== null && s.power !== undefined) {
    const t = Number(s.elapsed_s || 0);
    const last = app.live[app.live.length - 1];
    if (!last || Math.abs(last.t - t) >= 0.9) {
      app.live.push({ t, p: Number(s.power), target: s.target_power ? Number(s.target_power) : null });
      if (app.live.length > MAX_LIVE_POINTS) app.live.shift();
    } else if (s.power !== undefined) {
      last.p = Number(s.power);
    }
  }
}

function renderTimeline(s) {
  const plan = s.plan || [];
  const box = $('timeline');
  if (!box) return;
  if (!plan.length || (!s.is_interval && !s.is_test)) { box.hidden = true; return; }
  box.hidden = false;
  setText('timelineHead', s.is_test ? `测试：${plan.length} 档`
    : `课表 ${plan.length} 段，现在第 ${(s.step_index || 0) + 1} 段`);
  const track = $('timelineTrack');
  if (!track) return;
  const now = Number(s.step_index || 0);
  if (track.children.length !== plan.length) {
    track.textContent = '';
    plan.forEach((step) => {
      const seg = document.createElement('span');
      seg.className = 'timeline-seg';
      seg.title = `${step.name || ''} ${num(step.target_power || step.watts)}W`;
      track.appendChild(seg);
    });
  }
  Array.from(track.children).forEach((seg, i) => {
    seg.className = 'timeline-seg' + (i < now ? ' timeline-seg--done' : i === now ? ' timeline-seg--now' : '');
  });
}

/* ------------------------------------------------------------------ 图表 */

function setupCanvas(canvas) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const rect = canvas.getBoundingClientRect ? canvas.getBoundingClientRect() : { width: 800, height: 220 };
  const w = Math.max(320, Math.round(rect.width || 800));
  const h = Math.round((rect.height || 220) || 220);
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

function css(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name);
  return (v || '').trim() || fallback;
}

function drawSeries(canvas, points, opts) {
  const box = setupCanvas(canvas);
  if (!box) return;
  const { ctx, w, h } = box;
  const padL = 40;
  const padR = 8;
  const padT = 10;
  const padB = 18;
  ctx.clearRect(0, 0, w, h);
  const ink = css('--color-ink', '#1b1c22');
  const rule = css('--color-rule', '#a6a49e');
  const muted = css('--color-muted', '#565450');
  const accent = css('--color-accent', '#d4571f');

  const vals = points.filter((p) => Number.isFinite(p.y));
  if (!vals.length) {
    ctx.fillStyle = muted;
    ctx.font = '12px Archivo, sans-serif';
    ctx.fillText(opts.empty || '还没有数据', padL, h / 2);
    return;
  }
  let maxY = opts.maxY || 0;
  vals.forEach((p) => { maxY = Math.max(maxY, p.y); });
  if (opts.target) maxY = Math.max(maxY, opts.target * 1.15);
  maxY = Math.ceil(maxY / 50) * 50 || 100;
  const maxX = Math.max(opts.minX || 1, vals[vals.length - 1].x);
  const mapX = (x) => padL + (x / maxX) * (w - padL - padR);
  const mapY = (y) => h - padB - (y / maxY) * (h - padT - padB);

  // 3 条横网格，1px 实线
  ctx.strokeStyle = rule;
  ctx.lineWidth = 1;
  ctx.fillStyle = muted;
  ctx.font = '11px "Archivo Narrow", Archivo, sans-serif';
  for (let i = 0; i <= 3; i += 1) {
    const y = padT + (i / 3) * (h - padT - padB);
    const value = Math.round(maxY * (1 - i / 3));
    ctx.beginPath();
    ctx.moveTo(padL, y + 0.5);
    ctx.lineTo(w - padR, y + 0.5);
    ctx.stroke();
    ctx.fillText(String(value), 4, y + 4);
  }

  if (opts.target) {
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = muted;
    ctx.beginPath();
    ctx.moveTo(padL, mapY(opts.target));
    ctx.lineTo(w - padR, mapY(opts.target));
    ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.strokeStyle = opts.color || ink;
  ctx.lineWidth = 2;
  ctx.beginPath();
  vals.forEach((p, i) => {
    const x = mapX(p.x);
    const y = mapY(p.y);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();

  if (opts.top) {
    ctx.fillStyle = accent;
    ctx.fillRect(w - padR - 2, padT, 2, h - padT - padB);
  }
}

function drawLiveChart(s, live) {
  const canvas = $('liveChart');
  if (!canvas) return;
  drawSeries(canvas, app.live.map((p) => ({ x: p.t, y: p.p })), {
    target: s.target_power || null,
    empty: live ? '等待第一帧数据' : '开始训练后出现曲线',
  });
  const caption = $('liveCaption');
  if (caption) {
    caption.textContent = live
      ? `功率曲线，虚线是目标 ${num(s.target_power)} W，共 ${app.live.length} 个采样点`
      : app.live.length ? '这一场的功率曲线（结束后可在报告里看完整曲线）' : '开始训练后这里会画出功率曲线';
  }
}

function drawTraceChart(canvas, rows, key, color) {
  drawSeries(canvas, rows.map((r) => ({ x: Number(r.t) || 0, y: Number(r[key]) || 0 })), { color });
}

/* ------------------------------------------------------------------ 训练计划 */

function readFtp() {
  const el = $('inFtp');
  return Number(el && el.value) || 200;
}

function currentMode() {
  const checked = document.querySelector('input[name="mode"]:checked');
  return checked ? checked.value : 'constant';
}

function switchMode(mode) {
  app.mode = mode;
  document.querySelectorAll('[data-mode]').forEach((el) => { el.hidden = el.dataset.mode !== mode; });
  schedulePlanRefresh();
}

function fillSelect(select, items, value, label) {
  if (!select) return;
  select.textContent = '';
  items.forEach((item) => {
    const opt = document.createElement('option');
    opt.value = value(item);
    opt.textContent = label(item);
    select.appendChild(opt);
  });
}

function renderTemplateParams() {
  const box = $('templateParams');
  if (!box) return;
  box.textContent = '';
  const tpl = app.templates.find((t) => t.id === app.templateId);
  if (!tpl) return;
  (tpl.params || []).forEach((p) => {
    const label = document.createElement('label');
    label.className = 'field';
    const name = document.createElement('span');
    name.textContent = `${p.label || p.key}${p.unit ? '（' + p.unit + '）' : ''}`;
    const input = document.createElement('input');
    input.type = 'number';
    input.min = String(p.min);
    input.max = String(p.max);
    input.step = String(p.step || 1);
    input.value = String(app.params[p.key] !== undefined ? app.params[p.key] : p.default);
    input.addEventListener('input', () => {
      app.params[p.key] = Number(input.value);
      schedulePlanRefresh();
    });
    label.append(name, input);
    box.appendChild(label);
  });
}

function schedulePlanRefresh() {
  clearTimeout(app.planTimer);
  app.planTimer = setTimeout(refreshPlan, 300);
}

async function refreshPlan() {
  const mode = currentMode();
  app.plan = null;
  if (mode === 'interval') {
    const tpl = app.templates.find((t) => t.id === app.templateId);
    if (!tpl) return;
    try {
      const res = await api('/api/plan', { template_id: app.templateId, ftp: readFtp(), params: app.params });
      app.plan = res;
      logLine('info', `课表已更新：${res.plan_name || ''} ${res.stats ? res.stats.total_s : ''}`);
    } catch (err) {
      logLine('error', `生成课表失败：${err.message}`);
    }
  } else if (mode === 'custom') {
    const course = app.courses.find((c) => c.id === ($('inCourse') || {}).value);
    if (course) app.plan = planFromCourse(course);
  }
  renderPlanPreview();
}

function planFromCourse(course) {
  const steps = (course.steps || []).map((st) => ({
    name: st.name,
    kind: st.kind,
    duration_s: Number(st.duration_s) || 0,
    target_power: st.power_mode === 'pct' ? Math.round((readFtp() * Number(st.pct || 0)) / 100) : Number(st.watts) || 0,
  }));
  const total = steps.reduce((a, s) => a + s.duration_s, 0);
  return { plan_name: course.name, steps, stats: { total_s: total, step_count: steps.length } };
}

function renderPlanPreview() {
  const statsBox = $('planStats');
  const list = $('planSteps');
  if (!statsBox || !list) return;
  const mode = currentMode();
  const plan = app.plan;
  if (mode === 'constant') {
    const power = Number(($('inPower') || {}).value) || 0;
    const mins = Number(($('inDuration') || {}).value) || 0;
    statsBox.textContent = `${mins} 分钟 @ ${power} W`;
    list.textContent = '';
    return;
  }
  if (mode === 'test') {
    const t = app.tests.find((x) => x.id === ($('inTest') || {}).value);
    statsBox.textContent = t ? (t.subtitle || t.name || '') : '';
    list.textContent = '';
    const li = document.createElement('li');
    li.textContent = t ? (t.desc || '') : '';
    li.style.gridTemplateColumns = '1fr';
    if (t) list.appendChild(li);
    return;
  }
  if (!plan || !plan.steps) {
    statsBox.textContent = mode === 'custom' ? '选一个课程，或新建一节' : '正在生成课表…';
    list.textContent = '';
    return;
  }
  const total = (plan.stats && plan.stats.total_s) || plan.steps.reduce((a, s) => a + (s.duration_s || 0), 0);
  statsBox.textContent = `${plan.plan_name || ''}　共 ${Math.round(total / 60)} 分钟，${plan.steps.length} 段`;
  list.textContent = '';
  const s = app.snapshot || {};
  const now = s.state === 'running' ? Number(s.step_index || 0) : -1;
  plan.steps.forEach((step, i) => {
    const li = document.createElement('li');
    if (i === now) li.className = 'is-now';
    const idx = document.createElement('span');
    idx.className = 'num';
    idx.textContent = `${(step.duration_s || 0) >= 60 ? Math.round((step.duration_s || 0) / 60) + ' 分' : (step.duration_s || 0) + ' 秒'}`;
    const name = document.createElement('span');
    name.className = 'step-name';
    name.textContent = step.name || step.kind || '';
    const power = document.createElement('span');
    power.className = 'step-power';
    power.textContent = `${num(step.target_power || step.watts)} W`;
    li.append(idx, name, power);
    list.appendChild(li);
  });
}

function renderPlanPreviewIntoSteps(s) {
  if (s && s.plan && s.plan.length && (s.is_interval || s.is_test)) {
    app.plan = { plan_name: s.plan_name, steps: s.plan, stats: { total_s: s.duration_s } };
    renderPlanPreview();
  }
}

function startBody() {
  const mode = currentMode();
  const erg = ($('inErg') || {}).value || 'auto';
  if (mode === 'constant') {
    return {
      mode: 'constant',
      target_power: Number(($('inPower') || {}).value) || 100,
      duration_min: Number(($('inDuration') || {}).value) || 60,
      erg_mode: erg,
    };
  }
  if (mode === 'interval') {
    return {
      mode: 'interval', template_id: app.templateId, ftp: readFtp(),
      params: app.params, erg_mode: erg,
    };
  }
  if (mode === 'custom') {
    const course = app.editingCourse || app.courses.find((c) => c.id === ($('inCourse') || {}).value);
    if (!course || !(course.steps || []).length) throw new Error('先选一个课程，或新建一节');
    return { mode: 'custom', custom_id: course.id, ftp: readFtp(), erg_mode: erg };
  }
  const testId = ($('inTest') || {}).value;
  if (!testId) throw new Error('先选一个测试方案');
  return { mode: 'test', test_id: testId, ftp: readFtp(), erg_mode: erg };
}

async function startRide() {
  if (app.startPending) return;
  let body;
  try { body = startBody(); } catch (err) { toast(err.message, 'error'); return; }
  app.startPending = true;
  app.live = [];
  const btn = $('btnStart');
  if (btn) { btn.disabled = true; btn.setAttribute('aria-busy', 'true'); }
  try {
    await api('/api/start', body);
    logLine('ok', '训练已开始');
  } catch (err) {
    toast(err.message, 'error');
    logLine('error', `开始失败：${err.message}`);
  } finally {
    app.startPending = false;
    if (btn) { btn.disabled = false; btn.removeAttribute('aria-busy'); }
  }
}

/* ------------------------------------------------------------------ 设备 */

function renderDevices(s) {
  renderShell(s);
  renderDeviceList($('trainerList'), s.scan_results, (d) => d.address, (d) => ({
    name: d.name || '(无名骑行台)',
    meta: `${d.rssi !== undefined && d.rssi !== null ? d.rssi + ' dBm' : ''} ${d.has_ftms ? 'FTMS' : ''}`.trim(),
    connected: !!(s.trainer && s.trainer.connected && String(d.address).toLowerCase() === String(s.trainer.address || '').toLowerCase()),
    onClick: () => connectTrainer({ address: d.address }),
  }), '还没扫描');
  renderDeviceList($('hrList'), s.hr_devices, (d) => d.address, (d) => ({
    name: d.name || '(无名心率带)',
    meta: d.rssi !== undefined && d.rssi !== null ? `${d.rssi} dBm` : '心率带',
    connected: !!(s.hr_client && s.hr_client.connected && String(d.address).toLowerCase() === String(s.hr_client.address || '').toLowerCase()),
    onClick: () => connectHr(d.address),
  }), '还没扫描');

  setText('cTrainer', s.trainer && s.trainer.connected ? (s.trainer.name || '已连接') : '未连接');
  setText('cHr', s.hr_client && s.hr_client.connected
    ? `${s.hr_client.name || '已连接'}${s.hr_client.battery != null ? ' · ' + s.hr_client.battery + '%' : ''}`
    : '未连接');
  setText('cOverlay', s.overlay_running ? '已开启' : '未开启');
  const hrHint = $('hrHint');
  if (hrHint) {
    const hr = s.hr_client || {};
    if (!hr.connected) hrHint.textContent = '心率带是可选的：只连骑行台也能正常训练。';
    else if (hr.stale) hrHint.textContent = '已连接，但还没收到心率数据。检查带子有没有戴好、电极有没有沾水。';
    else if (s.hr_source === 'trainer') hrHint.textContent = '心率来自骑行台自带的传感器，数值可能不准。';
    else hrHint.textContent = '心率正常。';
  }
  const btnDisc = $('btnDisconnect');
  if (btnDisc) btnDisc.disabled = !(s.trainer && s.trainer.connected);
  const btnHrDisc = $('btnHrDisconnect');
  if (btnHrDisc) btnHrDisc.disabled = !(s.hr_client && s.hr_client.connected);
}

function renderDeviceList(list, devices, valueOf, viewOf, emptyText) {
  if (!list) return;
  const items = devices || [];
  list.textContent = '';
  if (!items.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = emptyText;
    list.appendChild(li);
    return;
  }
  items.forEach((d) => {
    const view = viewOf(d);
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'device' + (view.connected ? ' device-on' : '');
    btn.disabled = view.connected;
    const name = document.createElement('span');
    name.className = 'device-name';
    name.textContent = view.name;
    const meta = document.createElement('span');
    meta.className = 'device-meta';
    meta.textContent = view.connected ? '已连接' : view.meta;
    btn.append(name, meta);
    btn.addEventListener('click', () => view.onClick(valueOf(d)));
    li.appendChild(btn);
    list.appendChild(li);
  });
}

async function connectTrainer(payload) {
  try {
    await api('/api/connect', payload);
    logLine('ok', payload.simulator ? '已连接模拟设备' : '已连接骑行台');
    toast('骑行台已连接');
  } catch (err) { toast(err.message, 'error'); }
}

async function connectHr(address) {
  try {
    await api('/api/hr/connect', address ? { address } : {});
    logLine('ok', '心率带已连接');
  } catch (err) { toast(err.message, 'error'); }
}

async function toggleOverlay() {
  try {
    const res = await api('/api/overlay', { action: 'toggle' });
    toast(res.overlay_running ? '悬浮窗已开启' : '悬浮窗已关闭');
    logLine('info', res.overlay_running ? '悬浮窗已开启' : '悬浮窗已关闭');
  } catch (err) { toast(err.message, 'error'); }
}

/* ------------------------------------------------------------------ 自定义课程 */

function startNewCourse() {
  app.editingCourse = {
    id: '', name: '我的课程', power_mode: 'abs',
    steps: [{ name: '热身', kind: 'warmup', duration_s: 300, watts: 80 }],
  };
  const editor = $('courseEditor');
  if (editor) editor.hidden = false;
  const nameInput = $('inCourseName');
  if (nameInput) nameInput.value = app.editingCourse.name;
  renderCourseEditor();
}

function renderCourseEditor() {
  const body = document.querySelector('#courseSteps tbody');
  const course = app.editingCourse;
  if (!body || !course) return;
  body.textContent = '';
  course.steps.forEach((step, i) => {
    const tr = document.createElement('tr');
    const idx = document.createElement('td');
    idx.className = 'num';
    idx.textContent = String(i + 1);
    const nameTd = document.createElement('td');
    const nameInput = document.createElement('input');
    nameInput.value = step.name || '';
    nameInput.setAttribute('aria-label', `第 ${i + 1} 节名称`);
    nameInput.addEventListener('input', () => { step.name = nameInput.value; });
    nameTd.appendChild(nameInput);
    const durTd = document.createElement('td');
    const durInput = document.createElement('input');
    durInput.type = 'number';
    durInput.min = '10';
    durInput.step = '15';
    durInput.value = String(Math.round((step.duration_s || 0)));
    durInput.setAttribute('aria-label', `第 ${i + 1} 节时长（秒）`);
    durInput.addEventListener('input', () => {
      step.duration_s = Number(durInput.value) || 0;
      updateCourseTotal();
    });
    durTd.appendChild(durInput);
    const powTd = document.createElement('td');
    const powInput = document.createElement('input');
    powInput.type = 'number';
    powInput.min = '20';
    powInput.max = '600';
    powInput.step = '5';
    powInput.value = String(Math.round(step.watts || 80));
    powInput.setAttribute('aria-label', `第 ${i + 1} 节功率（瓦）`);
    powInput.addEventListener('input', () => { step.watts = Number(powInput.value) || 0; });
    powTd.appendChild(powInput);
    const actTd = document.createElement('td');
    actTd.className = 'row-act';
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn btn--tiny';
    del.textContent = '删';
    del.setAttribute('aria-label', `删除第 ${i + 1} 节`);
    del.addEventListener('click', () => {
      course.steps.splice(i, 1);
      renderCourseEditor();
    });
    actTd.appendChild(del);
    tr.append(idx, nameTd, durTd, powTd, actTd);
    body.appendChild(tr);
  });
  updateCourseTotal();
}

function updateCourseTotal() {
  const course = app.editingCourse;
  const total = course ? course.steps.reduce((a, s) => a + (Number(s.duration_s) || 0), 0) : 0;
  setText('courseTotal', `合计 ${Math.round(total / 60)} 分钟，${course ? course.steps.length : 0} 节`);
}

async function saveCourse() {
  const course = app.editingCourse;
  if (!course) return null;
  if (!course.steps.length) { toast('课程至少要有一节', 'error'); return null; }
  const nameInput = $('inCourseName');
  const body = {
    id: course.id || undefined,
    name: (nameInput && nameInput.value.trim()) || course.name || '我的课程',
    power_mode: 'abs',
    steps: course.steps.map((s) => ({
      name: s.name, kind: s.kind || 'work',
      duration_s: Number(s.duration_s) || 60,
      watts: Number(s.watts) || 100,
    })),
  };
  try {
    const res = await api('/api/custom', body);
    toast('课程已保存');
    await loadCourses();
    if (res.course && res.course.id) {
      const sel = $('inCourse');
      if (sel) sel.value = res.course.id;
      app.editingCourse = null;
      const editor = $('courseEditor');
      if (editor) editor.hidden = true;
    }
    await refreshPlan();
    return res.course || null;
  } catch (err) {
    toast(err.message, 'error');
    return null;
  }
}

/* ------------------------------------------------------------------ 报告 */

async function loadReports() {
  try {
    const res = await api('/api/reports', undefined, 'GET');
    app.reports = res.reports || [];
    renderReports();
  } catch (err) {
    logLine('error', `读取报告失败：${err.message}`);
    const body = $('reportsBody');
    if (body) {
      body.textContent = '';
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 7;
      td.className = 'empty';
      td.textContent = `读取失败：${err.message}`;
      tr.appendChild(td);
      body.appendChild(tr);
    }
  }
}

function renderReports() {
  const body = $('reportsBody');
  if (!body) return;
  setText('reportsNote', `共 ${app.reports.length} 份，每次训练结束会自动存一份。`);
  body.textContent = '';
  if (!app.reports.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 7;
    td.className = 'empty';
    td.textContent = '还没有记录。连上骑行台骑一场，结束时会自动保存。';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  app.reports.forEach((r) => {
    const tr = document.createElement('tr');
    const cells = [
      { text: when(r.started_at), num: false },
      { text: r.plan_name || '训练', num: false },
      { text: clock(r.actual_s), num: true },
      { text: `${num(r.avg_power)} W`, num: true },
      { text: r.heart_rate && r.heart_rate.avg_bpm ? `${num(r.heart_rate.avg_bpm)} bpm` : '—', num: true },
      { text: r.completed ? '完成' : (r.reason || '提前结束'), badge: !r.completed },
    ];
    cells.forEach((cell) => {
      const td = document.createElement('td');
      if (cell.num) td.className = 'num';
      td.textContent = cell.text;
      if (cell.badge) {
        const span = document.createElement('span');
        span.className = 'badge badge--warn';
        span.textContent = cell.text;
        td.textContent = '';
        td.appendChild(span);
      }
      tr.appendChild(td);
    });
    const act = document.createElement('td');
    act.className = 'row-act';
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'btn btn--tiny';
    open.textContent = '查看';
    open.addEventListener('click', () => openReport(r.id));
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn btn--tiny';
    del.textContent = '删除';
    del.addEventListener('click', () => deleteReport(r));
    act.append(open, del);
    tr.appendChild(act);
    body.appendChild(tr);
  });
}

async function openReport(id) {
  try {
    const res = await api(`/api/reports/${encodeURIComponent(id)}`, undefined, 'GET');
    const summary = res.report && res.report.summary ? res.report.summary : res.summary;
    app.detail = summary || null;
    renderSummary(summary, res.report ? res.report.id : id);
    if (location.hash !== `#/reports/${id}`) history.replaceState(null, '', `#/reports/${id}`);
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function deleteReport(report) {
  if (!window.confirm(`删除「${report.plan_name || report.id}」这份报告？删了就找不回来了。`)) return;
  try {
    await api('/api/reports/delete', { id: report.id });
    toast('报告已删除');
    if (app.detail === report.id) closeDetail();
    await loadReports();
  } catch (err) { toast(err.message, 'error'); }
}

function closeDetail() {
  const plate = $('reportDetail');
  if (plate) plate.hidden = true;
  app.detail = null;
  if (location.hash.startsWith('#/reports/')) history.replaceState(null, '', '#/reports');
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function renderSummary(summary, id) {
  const plate = $('reportDetail');
  const body = $('detailBody');
  if (!plate || !body) return;
  if (!summary) { plate.hidden = true; return; }
  plate.hidden = false;
  body.textContent = '';
  setText('h-detail', summary.plan_name ? `总结 · ${summary.plan_name}` : '总结');

  const wrap = el('div', 'sum');

  // 关键数字
  const figures = el('dl', 'sum-figures');
  const addFigure = (label, value, unit) => {
    const box = el('div', 'sum-figure');
    box.appendChild(el('span', null, label));
    const dd = el('dd', null, null);
    dd.textContent = unit ? `${value} ${unit}` : String(value);
    box.appendChild(dd);
    figures.appendChild(box);
  };
  addFigure('骑行时间', clock(summary.actual_s), '');
  addFigure('平均功率', num(summary.avg_power), 'W');
  addFigure('标准化功率', num(summary.normalized_power), 'W');
  addFigure('最大功率', num(summary.max_power), 'W');
  addFigure('距离', num(summary.distance_m ? summary.distance_m / 1000 : null, 1), 'km');
  addFigure('做功', num(summary.energy_kj), 'kJ');
  if (summary.heart_rate && summary.heart_rate.avg_bpm) {
    addFigure('平均心率', num(summary.heart_rate.avg_bpm), 'bpm');
    addFigure('最大心率', num(summary.heart_rate.max_bpm), 'bpm');
  }
  wrap.appendChild(figures);

  // 达成
  if (summary.in_zone_pct !== undefined && summary.in_zone_pct !== null) {
    const block = el('div', 'sum-block');
    block.appendChild(el('h3', null, '达成'));
    const bits = [];
    bits.push(`全程落在目标 ±${num(summary.in_zone_tolerance_w)} W 内 ${num(summary.in_zone_pct)}%`);
    if (summary.work_in_zone_pct !== null && summary.work_in_zone_pct !== undefined) {
      bits.push(`高强度段 ${num(summary.work_in_zone_pct)}%`);
    }
    if (summary.mean_abs_deviation_w !== null && summary.mean_abs_deviation_w !== undefined) {
      bits.push(`平均偏差 ${num(summary.mean_abs_deviation_w)} W`);
    }
    block.appendChild(el('p', 'note', bits.join('，')));
    wrap.appendChild(block);
  }

  // 分段
  if ((summary.intervals || []).length) {
    const block = el('div', 'sum-block');
    block.appendChild(el('h3', null, '分段'));
    const scroll = el('div', 'table-scroll');
    const table = el('table', 'table');
    const thead = document.createElement('thead');
    const hr = document.createElement('tr');
    ['#', '小节', '目标', '实际', '达标', '时长'].forEach((t) => hr.appendChild(el('th', null, t)));
    thead.appendChild(hr);
    const tbody = document.createElement('tbody');
    summary.intervals.forEach((st, i) => {
      const tr = document.createElement('tr');
      tr.appendChild(el('td', 'num', i + 1));
      tr.appendChild(el('td', null, st.name || ''));
      tr.appendChild(el('td', 'num', `${num(st.target_power)} W`));
      tr.appendChild(el('td', 'num', `${num(st.avg_power)} W`));
      tr.appendChild(el('td', 'num', st.in_zone_pct === null || st.in_zone_pct === undefined ? '—' : `${num(st.in_zone_pct)}%`));
      tr.appendChild(el('td', 'num', clock(st.actual_s)));
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    scroll.appendChild(table);
    block.appendChild(scroll);
    wrap.appendChild(block);
  }

  // 心率区
  const hrBlock = summary.heart_rate || {};
  if ((hrBlock.zones || []).length) {
    const block = el('div', 'sum-block');
    block.appendChild(el('h3', null, `心率区间（最大心率 ${num(hrBlock.max_hr)}）`));
    const zones = el('ul', 'hr-zones');
    const top = Math.max(...hrBlock.zones.map((z) => Number(z.pct) || 0));
    hrBlock.zones.forEach((z) => {
      const li = document.createElement('li');
      li.appendChild(el('span', null, z.label || z.code));
      const bar = el('span', 'hr-zone-bar');
      const fill = el('span', (Number(z.pct) === top && top > 0) ? 'is-top' : null);
      fill.style.transform = `scaleX(${((Number(z.pct) || 0) / 100).toFixed(3)})`;
      bar.appendChild(fill);
      li.appendChild(bar);
      li.appendChild(el('span', null, `${num(z.pct)}%`));
      zones.appendChild(li);
    });
    block.appendChild(zones);
    wrap.appendChild(block);
  }

  // 休息段
  if ((summary.rests || []).length) {
    const block = el('div', 'sum-block');
    block.appendChild(el('h3', null, '休息段（暂停期间的心率，不计入训练统计）'));
    const scroll = el('div', 'table-scroll');
    const table = el('table', 'table');
    const thead = document.createElement('thead');
    const hr = document.createElement('tr');
    ['骑到', '时长', '原因', '心率', '60 秒回落'].forEach((t) => hr.appendChild(el('th', null, t)));
    thead.appendChild(hr);
    const tbody = document.createElement('tbody');
    summary.rests.forEach((rest) => {
      const tr = document.createElement('tr');
      tr.appendChild(el('td', 'num', clock(rest.started_at)));
      tr.appendChild(el('td', 'num', clock(rest.duration_s)));
      tr.appendChild(el('td', null, rest.reason || '休息'));
      tr.appendChild(el('td', 'num', rest.hr_start !== null && rest.hr_end !== null
        ? `${num(rest.hr_start)} → ${num(rest.hr_end)}` : '—'));
      tr.appendChild(el('td', 'num', rest.hr_drop_60s === null || rest.hr_drop_60s === undefined
        ? '—' : `${num(rest.hr_drop_60s)} bpm`));
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    scroll.appendChild(table);
    block.appendChild(scroll);
    wrap.appendChild(block);
  }

  // 控功率方式切换
  if ((summary.mode_changes || []).length) {
    const block = el('div', 'sum-block');
    block.appendChild(el('h3', null, '控功率方式'));
    const list = el('ol', 'steps');
    summary.mode_changes.forEach((c) => {
      const li = document.createElement('li');
      li.style.gridTemplateColumns = '4rem 1fr';
      li.appendChild(el('span', 'num', clock(c.t)));
      li.appendChild(el('span', 'step-name', c.kind === 'probe' ? '目标功率探测'
        : c.kind === 'probe-ok' ? '探测通过' : `${c.from || ''} → ${c.to || ''}${c.reason ? '，' + c.reason : ''}`));
      list.appendChild(li);
    });
    block.appendChild(list);
    wrap.appendChild(block);
  }

  // 曲线
  const trace = summary.trace || [];
  if (trace.length) {
    const block = el('div', 'sum-block');
    block.appendChild(el('h3', null, '功率曲线'));
    const fig1 = el('figure', 'chart');
    const c1 = document.createElement('canvas');
    c1.width = 800; c1.height = 200;
    c1.setAttribute('role', 'img');
    c1.setAttribute('aria-label', '本场训练的功率曲线');
    fig1.appendChild(c1);
    block.appendChild(fig1);
    wrap.appendChild(block);
    const hasHr = trace.some((row) => row.hr !== null && row.hr !== undefined);
    if (hasHr) {
      const block2 = el('div', 'sum-block');
      block2.appendChild(el('h3', null, '心率曲线'));
      const fig2 = el('figure', 'chart');
      const c2 = document.createElement('canvas');
      c2.width = 800; c2.height = 160;
      c2.setAttribute('role', 'img');
      c2.setAttribute('aria-label', '本场训练的心率曲线');
      fig2.appendChild(c2);
      block2.appendChild(fig2);
      wrap.appendChild(block2);
      requestAnimationFrame(() => drawTraceChart(c2, trace, 'hr'));
    }
    requestAnimationFrame(() => drawTraceChart(c1, trace, 'p'));
  } else {
    wrap.appendChild(el('p', 'note', '这份报告没有曲线数据（老版本保存的报告）。'));
  }

  body.appendChild(wrap);
}

/* ------------------------------------------------------------------ 设置 */

function fillSettings(settings) {
  if (!settings) return;
  const ftp = $('inFtp');
  if (ftp && settings.ftp) ftp.value = String(settings.ftp);
  const map = {
    setHrMax: settings.hr_max, setHrRest: settings.hr_rest,
    setHrLimitBpm: settings.hr_limit_bpm, setFree: settings.free_resistance,
  };
  Object.entries(map).forEach(([id, value]) => {
    const el = $(id);
    if (el && value !== undefined && value !== null) el.value = String(value);
  });
  const limit = $('setHrLimit');
  if (limit) limit.checked = !!settings.hr_limit_enabled;
}

async function loadSettings() {
  try {
    const res = await api('/api/settings', undefined, 'GET');
    fillSettings(res.settings);
    if (res.settings && res.settings.ftp) schedulePlanRefresh();
  } catch (err) { logLine('error', `读取设置失败：${err.message}`); }
}

async function saveSettings() {
  const body = {
    ftp: Number(($('inFtp') || {}).value) || 200,
    hr_max: Number(($('setHrMax') || {}).value) || 190,
    hr_rest: Number(($('setHrRest') || {}).value) || 60,
    hr_limit_enabled: !!($('setHrLimit') || {}).checked,
    hr_limit_bpm: Number(($('setHrLimitBpm') || {}).value) || 165,
    free_resistance: Number(($('setFree') || {}).value) || 90,
  };
  try {
    await api('/api/settings', body);
    toast('设置已保存');
    await refreshPlan();
  } catch (err) { toast(err.message, 'error'); }
}

/* ------------------------------------------------------------------ 事件绑定 */

function bind() {
  document.querySelectorAll('input[name="mode"]').forEach((radio) => {
    radio.addEventListener('change', () => switchMode(radio.value));
  });
  const tpl = $('inTemplate');
  if (tpl) tpl.addEventListener('change', () => {
    app.templateId = tpl.value;
    const found = app.templates.find((t) => t.id === app.templateId);
    app.params = {};
    (found && found.params || []).forEach((p) => { app.params[p.key] = p.default; });
    renderTemplateParams();
    schedulePlanRefresh();
  });
  const course = $('inCourse');
  if (course) course.addEventListener('change', () => { app.editingCourse = null; schedulePlanRefresh(); });
  const test = $('inTest');
  if (test) test.addEventListener('change', () => schedulePlanRefresh());
  ['inPower', 'inDuration', 'inFtp'].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener('input', schedulePlanRefresh);
  });
  const btnCourseNew = $('btnCourseNew');
  if (btnCourseNew) btnCourseNew.addEventListener('click', startNewCourse);
  const btnStepAdd = $('btnStepAdd');
  if (btnStepAdd) btnStepAdd.addEventListener('click', () => {
    if (!app.editingCourse) startNewCourse();
    if (!app.editingCourse) return;
    app.editingCourse.steps.push({ name: '高强度', kind: 'work', duration_s: 300, watts: 130 });
    renderCourseEditor();
  });
  const btnCourseSave = $('btnCourseSave');
  if (btnCourseSave) btnCourseSave.addEventListener('click', saveCourse);

  const on = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
  on('btnStart', startRide);
  on('btnPause', async () => {
    try { await api('/api/pause', {}); logLine('info', '已暂停'); }
    catch (err) { toast(err.message, 'error'); }
  });
  on('btnResume', async () => {
    try { await api('/api/resume', {}); logLine('info', '已继续'); }
    catch (err) { toast(err.message, 'error'); }
  });
  on('btnStop', async () => {
    try { await api('/api/stop', {}); logLine('info', '训练已结束'); }
    catch (err) { toast(err.message, 'error'); }
  });
  on('btnSkip', async () => {
    try { await api('/api/interval/skip', { delta: 1 }); }
    catch (err) { toast(err.message, 'error'); }
  });
  on('btnTargetUp', () => adjustTarget(5));
  on('btnTargetDown', () => adjustTarget(-5));
  on('btnResUp', () => adjustResistance(1));
  on('btnResDown', () => adjustResistance(-1));
  on('btnScan', async () => {
    const btn = $('btnScan');
    if (btn) btn.disabled = true;
    try {
      const res = await api('/api/scan', {});
      logLine('info', `扫描完成：骑行台 ${(res.devices || []).length} 台，心率带 ${(res.hr_devices || []).length} 根`);
      if (app.snapshot) renderDevices(Object.assign({}, app.snapshot, {
        scan_results: res.devices || [], hr_devices: res.hr_devices || [],
      }));
    } catch (err) { toast(err.message, 'error'); }
    finally { if (btn) btn.disabled = false; }
  });
  on('btnSim', () => connectTrainer({ simulator: true }));
  on('btnDisconnect', async () => {
    try { await api('/api/disconnect', {}); toast('已断开骑行台'); }
    catch (err) { toast(err.message, 'error'); }
  });
  on('btnHrDisconnect', async () => {
    try { await api('/api/hr/disconnect', {}); toast('已断开心率带'); }
    catch (err) { toast(err.message, 'error'); }
  });
  on('btnOverlay', toggleOverlay);
  on('btnOverlay2', toggleOverlay);
  on('btnSettingsSave', saveSettings);
  on('btnReportsRefresh', loadReports);
  on('btnDetailClose', closeDetail);

  const freeTouched = $('setFree');
  if (freeTouched) freeTouched.addEventListener('change', async () => {
    try { await api('/api/free-resistance', { raw: Number(freeTouched.value) }); }
    catch (err) { toast(err.message, 'error'); }
  });
}

async function adjustTarget(delta) {
  const s = app.snapshot || {};
  const next = Math.max(20, Math.round((Number(s.target_power) || 100) + delta));
  try {
    await api('/api/target', { target_power: next });
    logLine('info', `目标功率调到 ${next} W`);
  } catch (err) { toast(err.message, 'error'); }
}

async function adjustResistance(delta) {
  const s = app.snapshot || {};
  const current = Number(s.resistance_raw);
  const raw = Math.max(0, Math.min(1000, (Number.isFinite(current) ? current : 150) + delta * 10));
  try { await api('/api/resistance', { raw }); }
  catch (err) { toast(err.message, 'error'); }
}

async function loadCourses() {
  try {
    const res = await api('/api/custom', undefined, 'GET');
    app.courses = res.courses || [];
    fillSelect($('inCourse'), app.courses, (c) => c.id, (c) => c.name || '(未命名)');
  } catch (err) { logLine('error', `读取课程失败：${err.message}`); }
}

async function init() {
  bind();
  try {
    const [tplRes, testRes] = await Promise.all([
      api('/api/templates', undefined, 'GET'),
      api('/api/tests', undefined, 'GET'),
    ]);
    app.templates = tplRes.templates || [];
    app.tests = testRes.tests || [];
  } catch (err) {
    logLine('error', `读取课程模板失败：${err.message}`);
  }
  fillSelect($('inTemplate'), app.templates, (t) => t.id, (t) => `${t.name}${t.subtitle ? '：' + t.subtitle : ''}`);
  fillSelect($('inTest'), app.tests, (t) => t.id, (t) => t.name || t.id);
  if (app.templates.length) {
    app.templateId = app.templates[0].id;
    const found = app.templates[0];
    app.params = {};
    (found.params || []).forEach((p) => { app.params[p.key] = p.default; });
    const sel = $('inTemplate');
    if (sel) sel.value = app.templateId;
    renderTemplateParams();
  }
  await loadCourses();
  await loadSettings();
  await loadReports();
  route();
  switchMode('constant');
  renderPlanPreview();
  connect();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

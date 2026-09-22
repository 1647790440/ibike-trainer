'use strict';

/* ------------------------------------------------------------------ *
 * iBike 智能骑行控制台 —— 前端逻辑（零依赖）
 * ------------------------------------------------------------------ */

const $ = (id) => document.getElementById(id);

const els = {
  banner: $('banner'), bannerText: $('bannerText'), bannerClose: $('bannerClose'),
  btnScan: $('btnScan'), btnSim: $('btnSim'), btnDisconnect: $('btnDisconnect'),
  scanHint: $('scanHint'), deviceList: $('deviceList'),
  hrPill: $('hrPill'), hrName: $('hrName'), hrBattery: $('hrBattery'),
  hrHint: $('hrHint'), hrList: $('hrList'), hrEmpty: $('hrEmpty'),
  devHr: $('devHr'), devHrZone: $('devHrZone'), devHrContact: $('devHrContact'),
  foundBlock: $('foundBlock'), deviceEmpty: $('deviceEmpty'),
  trainerPill: $('trainerPill'), trainerName: $('trainerName'),
  trainerHint: $('trainerHint'),
  devPower: $('devPower'), devCadence: $('devCadence'),
  devSpeed: $('devSpeed'), devRes: $('devRes'),
  btnHrDisconnect: $('btnHrDisconnect'),
  devicesView: $('devicesView'), trainingView: $('trainingView'),
  btnViewDevices: $('btnViewDevices'), btnViewTraining: $('btnViewTraining'),
  connChips: $('connChips'),
  hrMaxInput: $('hrMaxInput'), hrRestInput: $('hrRestInput'),
  hrZoneMode: $('hrZoneMode'), hrLimitEnabled: $('hrLimitEnabled'),
  hrLimitInput: $('hrLimitInput'),
  mHrZone: $('mHrZone'),
  targetPower: $('targetPower'), powerSlider: $('powerSlider'),
  duration: $('duration'), ergMode: $('ergMode'), modeHint: $('modeHint'),
  btnStart: $('btnStart'), startHint: $('startHint'),
  constantFields: $('constantFields'), intervalFields: $('intervalFields'),
  customFields: $('customFields'), ftpBlock: $('ftpBlock'), ftpHint: $('ftpHint'),
  ftpInput: $('ftpInput'), templateList: $('templateList'), templateDesc: $('templateDesc'),
  templateParams: $('templateParams'), planChart: $('planChart'),
  planStats: $('planStats'), planList: $('planList'),
  customList: $('customList'), customEmpty: $('customEmpty'),
  btnNewCourse: $('btnNewCourse'), courseEditor: $('courseEditor'),
  courseName: $('courseName'), pmodeHint: $('pmodeHint'),
  stepRows: $('stepRows'), stepCount: $('stepCount'), stepTotal: $('stepTotal'),
  btnAddStep: $('btnAddStep'), customPlanChart: $('customPlanChart'),
  customPlanStats: $('customPlanStats'), customPlanList: $('customPlanList'),
  btnSaveCourse: $('btnSaveCourse'), btnDeleteCourse: $('btnDeleteCourse'),
  saveHint: $('saveHint'),
  testFields: $('testFields'), testList: $('testList'), testDesc: $('testDesc'),
  testParams: $('testParams'), testPlanChart: $('testPlanChart'),
  testPlanStats: $('testPlanStats'), testPlanList: $('testPlanList'),
  testBar: $('testBar'), testName: $('testName'), testLevel: $('testLevel'),
  testLive: $('testLive'), testChart: $('testChart'), testNote: $('testNote'),
  testFreeCtl: $('testFreeCtl'), btnResDown: $('btnResDown'), btnResUp: $('btnResUp'),
  sumTestSection: $('sumTestSection'), trFtp: $('trFtp'), trSource: $('trSource'),
  trFormula: $('trFormula'), trDetail: $('trDetail'), trInvalid: $('trInvalid'),
  btnUseFtp: $('btnUseFtp'),
  reportsPanel: $('reportsPanel'), reportsList: $('reportsList'),
  reportsEmpty: $('reportsEmpty'), btnClearReports: $('btnClearReports'),
  btnDeleteReport: $('btnDeleteReport'),
  planBadge: $('planBadge'), intervalBar: $('intervalBar'),
  ivName: $('ivName'), ivZone: $('ivZone'), ivRep: $('ivRep'),
  ivChart: $('ivChart'), ivNext: $('ivNext'),
  btnSkip: $('btnSkip'), btnSkipBack: $('btnSkipBack'),
  timeLabel: $('timeLabel'), totalTime: $('totalTime'),
  sumIntervalSection: $('sumIntervalSection'), sumIntervals: $('sumIntervals'),
  sumModeSection: $('sumModeSection'), sumModeChanges: $('sumModeChanges'),
  sumHrSection: $('sumHrSection'), sumHrStats: $('sumHrStats'),
  sumHrZones: $('sumHrZones'), sumHrHint: $('sumHrHint'),
  setupPanel: $('setupPanel'), livePanel: $('livePanel'),
  stateBadge: $('stateBadge'), modeBadge: $('modeBadge'),
  btnPause: $('btnPause'), btnStop: $('btnStop'),
  ringFg: $('ringFg'), timeLeft: $('timeLeft'),
  powerNow: $('powerNow'), powerTarget: $('powerTarget'), powerDelta: $('powerDelta'),
  chart: $('chart'),
  mCadence: $('mCadence'), mAvg: $('mAvg'), mMax: $('mMax'), mNp: $('mNp'),
  mDist: $('mDist'), mKj: $('mKj'), mRes: $('mRes'), mHr: $('mHr'),
  note: $('note'), log: $('log'), btnClearLog: $('btnClearLog'),
  summaryPanel: $('summaryPanel'),
  sumTitle: $('sumTitle'), sumSub: $('sumSub'), sumBadge: $('sumBadge'),
  sumAvg: $('sumAvg'), sumTarget: $('sumTarget'), sumDelta: $('sumDelta'),
  sumMode: $('sumMode'), sumInZone: $('sumInZone'), sumInZoneBar: $('sumInZoneBar'),
  sumQualityLabel: $('sumQualityLabel'), sumQualityNote: $('sumQualityNote'),
  sumChart: $('sumChart'),
  sumMetrics: $('sumMetrics'), sumDist: $('sumDist'),
  btnAgain: $('btnAgain'), btnDismiss: $('btnDismiss'),
};

const RING_C = 2 * Math.PI * 88;

/* "是否踩在目标上"的容差：目标 ±5%，且不小于 5W。
   必须和服务端 session.py 里算达标率用的公式完全一致，否则会出现
   实时显示绿色、总结里却算作不达标的矛盾。 */
function targetTolerance(target) {
  return Math.max(5, Math.abs(target || 0) * 0.05);
}

/* 每个画布的 CSS 高度。这里是唯一来源，绘制前会用行内样式把它钉死。
   （tests/test_frontend_smoke.js 会校验它与 CSS 里的高度一致。）

   为什么非要在 JS 里再钉一次：canvas 如果没有 CSS 尺寸，它的布局尺寸就由
   width/height 属性决定。绘制函数读到 clientWidth 后乘以 devicePixelRatio 写回
   canvas.width，布局尺寸跟着变大，下次读到的更大——于是每画一次图就大一圈。
   自定义课程预览的柱状图踩过这个坑：它当时漏写了 CSS 规则，来回切换功率表示
   方式时被反复重绘，就无限膨胀了。钉死 CSS 尺寸后，位图尺寸再也不会反过来
   影响布局。 */
const CANVAS_HEIGHT = {
  chart: 180,
  sumChart: 180,
  planChart: 100,
  customPlanChart: 100,
  testPlanChart: 100,
  testChart: 64,
  ivChart: 64,
};

/** 按 CSS 尺寸适配画布位图（考虑高分屏），返回已清空、可以直接开始画的上下文。 */
function fitCanvas(canvas, fallbackHeight) {
  const dpr = window.devicePixelRatio || 1;
  const cssH = CANVAS_HEIGHT[canvas.id] || fallbackHeight || 120;

  // 顺序很重要：先把 CSS 尺寸钉死，再去读它
  if (canvas.style.width !== '100%') canvas.style.width = '100%';
  const heightStyle = cssH + 'px';
  if (canvas.style.height !== heightStyle) canvas.style.height = heightStyle;

  const cssW = Math.max(120, Math.round(canvas.clientWidth) || 600);
  const cssHpx = Math.max(40, Math.round(canvas.clientHeight) || cssH);

  const bitmapW = Math.round(cssW * dpr);
  const bitmapH = Math.round(cssHpx * dpr);
  if (canvas.width !== bitmapW || canvas.height !== bitmapH) {
    canvas.width = bitmapW;
    canvas.height = bitmapH;
  }

  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssHpx);
  return { ctx: ctx, w: cssW, h: cssHpx };
}

/* 不同段用不同颜色：高强度偏暖，恢复偏冷，一眼能看出当前处在什么状态 */
const KIND_COLORS = {
  warmup: '#4da3ff',
  work: '#ff7a5f',
  recovery: '#4da3ff',
  cooldown: '#34e3a4',
};

let state = { state: 'idle', trainer: {} };
let chartData = [];
let lastChartPush = 0;
let ws = null;
let wsRetry = 0;
let lastWsMessage = 0;

/* ------------------------------------------------------------------ *
 * 工具
 * ------------------------------------------------------------------ */

function fmtClock(seconds) {
  if (seconds == null || isNaN(seconds)) return '--:--';
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function setText(el, value) {
  const next = value == null ? '--' : String(value);
  if (el.textContent !== next) el.textContent = next;
}

async function api(path, body, method = 'POST') {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* 忽略 */ }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `请求失败 (${res.status})`);
  }
  return data;
}

function logLine(kind, message) {
  const li = document.createElement('li');
  const time = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  // kind 只用来拼 class 名。虽然目前都是内部常量，但它是这里唯一被插进 HTML 的
  // 变量，过滤一下不留隐患；正文一律走 textContent，不会被当成标签解析。
  const safeKind = String(kind || 'info').replace(/[^a-z0-9_-]/gi, '');
  li.innerHTML = `<span class="t">${time}</span><span class="k k-${safeKind}">●</span><span></span>`;
  li.lastChild.textContent = message;
  els.log.prepend(li);
  while (els.log.children.length > 80) els.log.lastChild.remove();
}

/* 页面顶部的消息条。连接失败这类错误以前被写进 #livePanel 里的隐藏元素，
   用户完全看不到，只能干瞪着灰掉的按钮——所以改成全局可见。 */
function showBanner(message, kind = 'error') {
  els.bannerText.textContent = message;
  els.banner.className = 'banner banner-' + kind;
}

function hideBanner() {
  els.banner.className = 'banner hidden';
}

function toast(message) {
  logLine('error', message);
  showBanner(message, 'error');
}

/* ------------------------------------------------------------------ *
 * WebSocket
 * ------------------------------------------------------------------ */

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => { wsRetry = 0; };
  ws.onclose = () => {
    wsRetry = Math.min(wsRetry + 1, 10);
    setTimeout(connectWS, 500 * wsRetry);
  };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    lastWsMessage = Date.now();
    if (msg.type === 'state') {
      render(msg.data);
    } else if (msg.type === 'event') {
      onServerEvent(msg.data);
    } else if (msg.type === 'events') {
      els.log.innerHTML = '';
      // 不要 reverse：logLine 是往前插的，服务端给的历史本来就是旧→新，
      // 再倒一遍会让整段日志次序颠倒（重连、清空日志之后特别明显）
      msg.data.forEach((e) => logLine(e.kind, e.message));
    }
  };
}

function onServerEvent(e) {
  if (!e) return;
  logLine(e.kind, e.message);
  if (e.kind === 'disconnected') {
    showBanner(e.message + '。骑行台可能休眠了、被手机抢走了连接，或者超出了蓝牙范围。', 'warn');
  } else if (e.kind === 'error') {
    showBanner(e.message, 'error');
  }
}

/* WebSocket 万一在你的浏览器/网络环境里连不上，页面就永远停在初始状态——
   按钮会一直是灰的。所以这里加一个兜底轮询：只要最近没收到 WS 消息，就主动
   拉一次状态。 */
async function pollState() {
  if (Date.now() - lastWsMessage < 4000) return;   // WS 活着，不用多此一举
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    if (!res.ok) return;
    render(await res.json());
  } catch (e) { /* 服务没起来，等下一次 */ }
}

/* ------------------------------------------------------------------ *
 * 渲染
 * ------------------------------------------------------------------ */

function render(s) {
  if (!s) return;
  const prevState = state ? state.state : null;
  state = s;

  const trainer = s.trainer || {};
  const connected = !!trainer.connected;

  // 一场新训练开始时清空实时曲线。正常情况下"开始训练/再来一次"的处理器会清，
  // 但换个标签页、或者从另一台设备上开训练时走不到那儿，于是上一场的曲线会被
  // 接着画进这一场的图里（指标却只算这一场，图上和图下对不上）。
  const inRide = s.state === 'running' || s.state === 'paused';
  const wasInRide = prevState === 'running' || prevState === 'paused';
  if (inRide && !wasInRide) {
    chartData = [];
    lastChartPush = 0;
  }

  // 设备状态（标题行不再放连接标识了：那一枚只显示骑行台，而心率带的状态
  // 它显示不了，容易让人以为"连上了"——现在下面两块各自说清楚）
  renderTrainerBlock(s);
  renderHr(s);
  renderFoundLists(false);

  const running = s.state === 'running' || s.state === 'paused';
  const showLive = running || s.state === 'error';
  const hasSummary = !!s.summary;
  // 刚结束的训练总结，和用户在报告列表里点开的历史报告，共用同一块面板渲染
  const showingSummary = hasSummary || !!viewingReport;

  renderConnChips(s);

  // 训练一开始（或从别的设备/标签页开起来）就切到训练页：骑行中用户要看的是
  // 功率和倒计时，不是设备列表。手动切回去也允许，这里只在"进入骑行"那一刻切一次。
  if (inRide && !wasInRide && currentView !== 'training') {
    switchView('training');
  }

  els.setupPanel.classList.toggle('hidden', running || showingSummary);
  els.livePanel.classList.toggle('hidden', !showLive);
  els.summaryPanel.classList.toggle('hidden', !showingSummary);
  els.reportsPanel.classList.toggle('hidden', running || showingSummary);
  els.btnStart.disabled = !connected || running || startPending;

  // 新的训练报告落盘后刷新列表
  if (hasSummary && s.summary.finished_at !== lastReportSyncKey) {
    lastReportSyncKey = s.summary.finished_at;
    loadReports();
  }

  // 按钮为什么点不了，直接写在按钮下面，别让人猜
  if (!connected) {
    els.startHint.textContent =
      '按钮是灰的：还没连上骑行台。请在上面点「扫描蓝牙设备」，再选中你的骑行台。';
  } else if (!running && trainer.capabilities
             && trainer.capabilities.has_control_point === false) {
    els.startHint.textContent =
      '⚠ 这台设备没有 FTMS 控制点（0x2AD9），只能读取数据，无法控制阻力。';
  } else {
    els.startHint.textContent = '';
  }

  if (showingSummary) renderSummary(viewingReport ? viewingReport.summary : s.summary);
  else renderedSummaryKey = null;

  if (!showLive) return;

  // 状态徽标
  const badges = {
    running: ['进行中', ''], paused: ['已暂停', 'badge-paused'],
    finished: ['已完成', ''], error: ['出错', 'badge-paused'],
  };
  const [label, cls] = badges[s.state] || ['—', ''];
  els.stateBadge.textContent = label;
  els.stateBadge.className = 'badge ' + cls;
  setText(els.modeBadge, s.erg_mode_label || '—');

  // 圆圈进度 + 剩余时间。
  // 间歇训练时圆环显示的是「本段还剩多久」而不是总剩余——HIIT 里你真正关心的
  // 是这一组还要撑多久，总进度放到下面用一行小字给出。
  // 测试课表段数也很多，但那是测试，不该套用间歇训练的界面
  const interval = !!s.is_interval && !s.is_test;
  let ringProgress, ringTime, ringLabel;
  if (interval) {
    const stepDur = s.step_duration_s || 1;
    ringProgress = Math.max(0, Math.min(1, (s.step_elapsed_s || 0) / stepDur));
    ringTime = s.step_remaining_s;
    ringLabel = '本段剩余';
  } else {
    ringProgress = Math.max(0, Math.min(1, s.progress || 0));
    ringTime = s.remaining_s;
    ringLabel = '剩余时间';
  }
  els.ringFg.style.strokeDashoffset = String(RING_C * (1 - ringProgress));
  els.ringFg.style.stroke = s.state === 'paused' ? 'var(--warn)'
    : s.state === 'finished' ? 'var(--blue)'
      : (interval ? KIND_COLORS[s.step_kind] || 'var(--accent)' : 'var(--accent)');
  setText(els.timeLeft, fmtClock(ringTime));
  setText(els.timeLabel, ringLabel);
  setText(els.totalTime, interval
    ? '总计 ' + fmtClock(s.elapsed_s) + ' / ' + fmtClock(s.duration_s) : '');

  // FTP 测试专用面板
  const isTest = !!s.is_test;
  els.testBar.classList.toggle('hidden', !isTest);
  if (isTest) {
    setText(els.testName, s.plan_name || 'FTP 测试');
    const lvl = s.test_level;
    els.testLevel.classList.toggle('hidden', lvl == null);
    setText(els.testLevel, lvl ? '第 ' + lvl + ' / ' + s.test_level_count + ' 级' : '');

    const live = s.live_test;
    if (live && live.kind === 'projected_ftp') {
      setText(els.testLive, live.label + ' ' + live.value + 'W');
    } else if (live) {
      setText(els.testLive,
        live.label + ' ' + live.value + 'W · 推算 FTP ' + live.projected_ftp + 'W');
    } else {
      // 推算 FTP 需要满 1 分钟的功率窗口。这之前给一句话，别让人以为界面坏了
      setText(els.testLive, s.test_id === 'ramp' ? '踩满 1 分钟后开始推算 FTP' : '');
    }
    setText(els.testNote, s.test_self_paced
      ? '自由骑行：程序不控制功率，自己配速' : '');
    // 自由骑行时给出阻力微调——骑行台上只有阻力可调，配速靠它和踏频
    els.testFreeCtl.classList.toggle('hidden', s.free_resistance == null);
    drawPlanChart(els.testChart, s.plan || [], {
      currentIndex: s.step_index,
      progress: s.step_duration_s ? (s.step_elapsed_s || 0) / s.step_duration_s : 0,
    });
  } else {
    els.testFreeCtl.classList.add('hidden');
  }

  // 间歇训练专用面板
  els.intervalBar.classList.toggle('hidden', !interval);
  els.planBadge.classList.toggle('hidden', !interval);
  if (interval) {
    setText(els.planBadge, s.plan_name || '间歇训练');
    setText(els.ivName, s.step_name || '—');
    setText(els.ivZone, s.step_zone || '');
    els.ivZone.classList.toggle('hidden', !s.step_zone);
    setText(els.ivRep, s.work_index
      ? '第 ' + s.work_index + '/' + s.work_count + ' 组'
      : '第 ' + (s.step_index + 1) + '/' + s.step_count + ' 段');
    setText(els.ivNext, s.next_step_name
      ? '下一段：' + s.next_step_name + ' ' + Math.round(s.next_step_power) + 'W'
      : '这是最后一段');
    drawPlanChart(els.ivChart, s.plan || [], {
      currentIndex: s.step_index,
      progress: s.step_duration_s ? (s.step_elapsed_s || 0) / s.step_duration_s : 0,
    });
  }

  // 功率
  setText(els.powerNow, s.power == null ? '--' : Math.round(s.power));
  setText(els.powerTarget, Math.round(s.target_power));
  // 同步恒定功率的输入框。两个条件：
  //  - 间歇训练时不同步，否则当前小节的瓦数会被写进恒定功率的目标里，
  //    跑完间歇切回恒定模式就直接用上了最后那一段（往往是恢复段）的瓦数；
  //    间歇中的 ± 按钮改为以 state.target_power 为基准，不依赖这个输入框。
  //  - 用户正在编辑（含拖滑块）时不同步，否则输入框会跟人的操作打架。
  const powerBusy = document.activeElement === els.targetPower
                 || document.activeElement === els.powerSlider;
  if (!powerBusy && !s.is_interval) {
    els.targetPower.value = Math.round(s.target_power);
    els.powerSlider.value = Math.min(500, Math.round(s.target_power));
  }

  if (s.power != null) {
    const delta = s.power - s.target_power;
    const tol = targetTolerance(s.target_power);
    els.powerDelta.textContent = (delta >= 0 ? '+' : '') + Math.round(delta) + 'W';
    els.powerDelta.className = 'delta ' + (Math.abs(delta) <= tol ? 'good' : 'bad');
    els.powerNow.style.color = Math.abs(delta) <= tol ? 'var(--accent)' : 'var(--text)';
  } else {
    els.powerDelta.textContent = '';
    els.powerNow.style.color = 'var(--text)';
  }

  // 指标
  setText(els.mCadence, s.cadence == null ? '--' : Math.round(s.cadence));
  setText(els.mAvg, s.power_avg == null ? '--' : Math.round(s.power_avg));
  setText(els.mMax, s.power_max == null ? '--' : Math.round(s.power_max));
  // 总结里 NP 要 active_s >= 60 才给（30 秒滑动窗口刚起步时会系统性偏高），
  // 实时这里也按同一门槛，免得同一个数字在实时有、进报告又消失。
  setText(els.mNp, (s.normalized_power == null || (s.active_s || 0) < 60)
    ? '--' : Math.round(s.normalized_power));
  setText(els.mDist, s.distance_m == null ? '--' : (s.distance_m / 1000).toFixed(2));
  setText(els.mKj, s.energy_kj == null ? '--' : Math.round(s.energy_kj));
  setText(els.mRes, s.resistance_raw == null ? '--' : (s.resistance_raw / 10).toFixed(1));
  const hrZone = s.hr_zone || null;
  const hrLimit = s.hr_limit || {};
  setText(els.mHr, s.heart_rate == null ? '--' : Math.round(s.heart_rate));
  setText(els.mHrZone, hrZone ? hrZone.label : '');
  // 心率带没数据、但确实连着 —— 说清楚是"没读数"而不是"没连"
  const hrOver = !!(hrLimit.enabled && hrLimit.bpm && s.heart_rate != null
    && s.heart_rate >= hrLimit.bpm);
  els.mHr.className = 'm-value' + (hrOver ? ' m-hr-over' : '');
  if (s.heart_rate == null && s.hr_stale) {
    setText(els.mHrZone, '未收到数据');
  } else if (hrOver) {
    setText(els.mHrZone, (hrZone ? hrZone.label + ' · ' : '') + '超过上限');
  }

  els.btnPause.textContent = s.state === 'paused' ? '继续' : '暂停';
  els.btnPause.disabled = s.state === 'finished' || s.state === 'error';

  // 提示行
  let note = s.command_note || '';
  if (s.error) note = '⚠ ' + s.error;
  else if (s.stale_data) note = '⚠ 已经有一阵子没收到骑行台数据了，检查一下它是否还在连接状态';
  setText(els.note, note || '—');
  els.note.classList.toggle('warn', !!(s.error || s.stale_data));

  // 图表
  const now = Date.now();
  if (s.power != null && now - lastChartPush > 500) {
    lastChartPush = now;
    chartData.push({ t: now, power: s.power, target: s.target_power,
                     hr: s.heart_rate });
    if (chartData.length > 24000) chartData.shift();
  }
  drawChart();
}

/* ------------------------------------------------------------------ *
 * 训练总结
 * ------------------------------------------------------------------ */

let lastSummary = null;
let renderedSummaryKey = null;
let startPending = false;
/* 正在查看的历史报告（null 表示没有在看历史，展示的是刚结束的那次训练） */
let viewingReport = null;
let reportList = [];
let lastReportSyncKey = null;

function qualityVerdict(pct, modeLabel) {
  if (pct == null) return '';
  if (pct >= 90) return '功率压得非常稳，几乎全程贴着目标走。';
  if (pct >= 75) return '整体控制得不错，偶有波动。';
  if (pct >= 50) return '能跟住目标，但波动偏大。';
  if (modeLabel && modeLabel.indexOf('闭环') >= 0) {
    return '闭环阻力模式下波动偏大是正常的——阻力档位是分步调整的，响应比固件内部闭环慢。';
  }
  return '波动较大。如果骑行台支持原生 ERG，可以试试把控功率方式改成「原生 ERG」。';
}

function renderSummary(sum) {
  if (!sum) return;

  // 训练结束后服务端仍会周期性推送状态，但总结内容是不变的。
  // 不做这个判断就会每 0.5 秒重建一次 DOM 并重画曲线，白白闪烁。
  const key = [sum.finished_at, sum.actual_s, sum.target_power, sum.reason].join('|');
  if (key === renderedSummaryKey) return;
  renderedSummaryKey = key;
  lastSummary = sum;

  // 查看历史报告时，操作按钮换成"删除这份报告 / 返回列表"
  const historical = !!viewingReport;
  els.btnDeleteReport.classList.toggle('hidden', !historical);
  els.btnDismiss.textContent = historical ? '返回报告列表' : '关闭';
  // 看历史报告时"再来一次"会去重放服务端记住的"上一次训练"，和屏幕上这份
  // 报告未必是同一套课表（服务重启后还会直接报"还没有可重复的训练"）。
  els.btnAgain.classList.toggle('hidden', historical);

  const completed = !!sum.completed;
  els.sumTitle.textContent = completed ? '训练完成' : '训练结束';
  els.sumBadge.textContent = completed ? '已完成' : '提前结束';
  els.sumBadge.className = 'badge' + (completed ? '' : ' badge-paused');

  // 副标题：时间范围 · 实际/计划时长 · 结束原因 · 骑行台
  const timeOf = (ts) => ts
    ? new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
    : null;
  const parts = [];
  if (sum.is_interval && sum.plan_name) parts.push(sum.plan_name);
  if (sum.started_at && sum.finished_at) {
    parts.push(timeOf(sum.started_at) + ' — ' + timeOf(sum.finished_at));
  }
  parts.push(fmtClock(sum.actual_s) + ' / ' + fmtClock(sum.planned_s));
  if (sum.skipped_s > 5) parts.push('跳过 ' + fmtClock(sum.skipped_s));
  if (sum.reason) parts.push(sum.reason);
  if (sum.trainer_name) parts.push(sum.trainer_name);
  els.sumSub.textContent = parts.join('  ·  ');

  setText(els.sumAvg, sum.avg_power == null ? '--' : Math.round(sum.avg_power));
  setText(els.sumTarget, Math.round(sum.target_power));

  const dev = sum.target_deviation_w;
  if (dev == null) {
    els.sumDelta.textContent = '';
  } else {
    const tol = targetTolerance(sum.target_power);
    els.sumDelta.textContent = '实际比目标 ' + (dev >= 0 ? '+' : '') + dev.toFixed(1) + ' W';
    els.sumDelta.className = 'sum-delta ' + (Math.abs(dev) <= tol ? 'good' : 'bad');
  }
  setText(els.sumMode, sum.erg_mode_label ? '控功率方式：' + sum.erg_mode_label : '');

  // 控功率质量。间歇训练看的是「高强度段达标率」——把恢复段混进来算整体达标率
  // 没有意义（恢复段目标是降速，物理上几秒内降不到），那个数字会很难看却说明不了
  // 你高强度段踩得好不好。
  const qualityPct = sum.is_interval ? sum.work_in_zone_pct : sum.in_zone_pct;
  setText(els.sumQualityLabel, sum.is_interval ? '高强度段达标率' : '控功率达标率');
  setText(els.sumInZone, qualityPct == null ? '--' : qualityPct.toFixed(1) + '%');
  els.sumInZoneBar.style.width = (qualityPct == null ? 0
    : Math.max(2, Math.min(100, qualityPct))) + '%';
  const notes = [];
  if (sum.in_zone_tolerance_w != null) {
    notes.push('目标 ±' + Math.round(sum.in_zone_tolerance_w) + ' W 内的时间占比');
  }
  if (sum.is_interval) {
    notes.push('只统计高强度段');
  }
  if (sum.mean_abs_deviation_w != null) {
    notes.push('全程平均偏差 ' + sum.mean_abs_deviation_w.toFixed(1) + ' W');
  }
  const verdict = qualityVerdict(qualityPct, sum.erg_mode_label);
  if (verdict) notes.push(verdict);
  els.sumQualityNote.textContent = notes.join('；');

  // 其余指标
  const metrics = [
    ['最大功率', sum.max_power, 'W', (v) => Math.round(v)],
    ['标准化功率', sum.normalized_power, 'W', (v) => Math.round(v)],
    ['平均踏频', sum.avg_cadence, 'rpm', (v) => Math.round(v)],
    ['最高踏频', sum.max_cadence, 'rpm', (v) => Math.round(v)],
    ['做功', sum.energy_kj, 'kJ', (v) => Math.round(v)],
    ['估算消耗', sum.energy_kcal_est, 'kcal', (v) => Math.round(v)],
    ['距离', sum.distance_m == null ? null : sum.distance_m / 1000, 'km', (v) => v.toFixed(2)],
    ['实际时长', sum.actual_s, '', (v) => fmtClock(v)],
  ];
  els.sumMetrics.innerHTML = '';
  metrics.forEach(([label, value, unit, fmt]) => {
    const box = document.createElement('div');
    box.className = 'metric';
    const l = document.createElement('span');
    l.className = 'm-label'; l.textContent = label;
    const v = document.createElement('span');
    v.className = 'm-value';
    v.textContent = (value == null || isNaN(value)) ? '--' : fmt(value);
    const u = document.createElement('span');
    u.className = 'm-unit'; u.textContent = unit;
    box.append(l, v, u);
    els.sumMetrics.appendChild(box);
  });

  // 功率区间分布
  els.sumDist.innerHTML = '';
  const dist = sum.distribution || [];
  if (!dist.length) {
    const empty = document.createElement('p');
    empty.className = 'hint';
    empty.textContent = '这次训练没有采集到功率数据。';
    els.sumDist.appendChild(empty);
  } else {
    const peak = Math.max(...dist.map((b) => b.pct), 1);
    dist.forEach((band) => {
      const row = document.createElement('div');
      row.className = 'dist-row';

      const label = document.createElement('span');
      label.className = 'dist-label';
      label.textContent = band.label;

      const track = document.createElement('div');
      track.className = 'dist-track';
      const fill = document.createElement('div');
      fill.className = 'dist-fill' + (band.label === '97–103%' ? ' core' : '');
      fill.style.width = Math.max(0, (band.pct / peak) * 100) + '%';
      track.appendChild(fill);

      const value = document.createElement('span');
      value.className = 'dist-value';
      value.textContent = band.pct.toFixed(1) + '% · ' + fmtClock(band.seconds);

      row.append(label, track, value);
      els.sumDist.appendChild(row);
    });
  }

  // 整段的功率曲线
  const points = (sum.trace || []).map((d) => ({
    t: d.t * 1000, p: d.p, target: d.target, hr: d.hr,
  }));
  drawPowerChart(els.sumChart, points, {
    emptyText: '这次训练没有采集到功率数据',
    hrMax: (sum.heart_rate || {}).max_bpm,
  });

  // FTP 测试结果
  const tr = sum.test_result;
  els.sumTestSection.classList.toggle('hidden', !tr);
  if (tr) {
    setText(els.trFtp, tr.ftp);
    els.trFtp.style.color = tr.valid ? 'var(--accent)' : 'var(--muted)';
    setText(els.trSource, (tr.source_label || '依据') + '：' + tr.source_power + ' W');
    setText(els.trFormula,
      '× ' + Math.round((tr.multiplier || 0) * 100) + '%  →  ' + tr.ftp + ' W');
    setText(els.trDetail, tr.detail || '');
    els.btnUseFtp.classList.toggle('hidden', !tr.valid);
    els.trInvalid.textContent = tr.valid ? '' : ('这次结果不能用：' + (tr.detail || ''));
    els.trInvalid.classList.toggle('warn', !tr.valid);
  }

  // 心率区块。没接心率带的那次训练就整块不显示——空着比显示一堆 "--" 诚实。
  const hrStats = sum.heart_rate || null;
  els.sumHrSection.classList.toggle('hidden', !hrStats);
  if (hrStats) {
    els.sumHrStats.innerHTML = '';
    const cells = [
      ['平均心率', hrStats.avg_bpm == null ? '--' : Math.round(hrStats.avg_bpm), 'bpm'],
      ['最大心率', hrStats.max_bpm == null ? '--' : Math.round(hrStats.max_bpm), 'bpm'],
      ['心率来源', hrStats.source === 'strap' ? '心率带' : '骑行台', ''],
    ];
    cells.forEach(([label, value, unit]) => {
      const box = document.createElement('div');
      box.className = 'sum-cell';
      const lb = document.createElement('span'); lb.className = 'sc-label';
      lb.textContent = label;
      const val = document.createElement('span'); val.className = 'sc-value';
      val.textContent = value;
      box.append(lb, val);
      if (unit) {
        const u = document.createElement('span'); u.className = 'sc-unit';
        u.textContent = unit;
        box.appendChild(u);
      }
      els.sumHrStats.appendChild(box);
    });

    const zones = hrStats.zones || [];
    els.sumHrZones.innerHTML = '';
    zones.forEach((z) => {
      const row = document.createElement('div');
      row.className = 'hr-zone-row';
      const name = document.createElement('span');
      name.textContent = z.label;
      const range = document.createElement('span');
      range.className = 'hr-range';
      range.textContent = z.min_bpm + '~' + (z.max_bpm == null ? '∞' : z.max_bpm);
      const bar = document.createElement('div'); bar.className = 'hr-zone-bar';
      const fill = document.createElement('div'); fill.className = 'hr-zone-fill';
      fill.style.width = Math.max(0, Math.min(100, z.pct || 0)) + '%';
      bar.appendChild(fill);
      const pct = document.createElement('span');
      pct.className = 'hr-pct';
      pct.textContent = (z.pct || 0).toFixed(0) + '%';
      row.append(name, range, bar, pct);
      els.sumHrZones.appendChild(row);
    });

    const bits = [];
    bits.push(hrStats.zone_mode === 'reserve'
      ? '区间按储备心率（Karvonen）算，最大心率 '
        + Math.round(hrStats.max_hr || 0) + '、静息 ' + Math.round(hrStats.rest_hr || 0)
      : '区间按最大心率百分比算，最大心率 ' + Math.round(hrStats.max_hr || 0));
    if (hrStats.cap_active) {
      bits.push('心率上限保护已启用（' + Math.round(hrStats.cap_bpm || 0) + ' bpm）'
        + (hrStats.cap_note ? '：' + hrStats.cap_note : ''));
    }
    if (hrStats.rr_seen) bits.push('这根带子会发 RR 间期（以后可以做静息 HRV）');
    els.sumHrHint.textContent = bits.join('；');
  }

  // 控功率方式切换记录。这一段的存在是为了回答"阻力为什么突然变了"——
  // 只看最终模式是查不出来的，必须知道什么时候切的、为什么切。
  const modeChanges = sum.mode_changes || [];
  const MODE_NAMES = { ftms: '原生 ERG', resistance: '闭环阻力', free: '自由骑行' };
  els.sumModeSection.classList.toggle('hidden', modeChanges.length === 0);
  els.sumModeChanges.innerHTML = '';
  modeChanges.forEach((c) => {
    const row = document.createElement('div');
    row.className = 'iv-row';
    const idx = document.createElement('span');
    idx.className = 'r-idx';
    idx.textContent = fmtClock(c.t);
    const name = document.createElement('span');
    name.className = 'r-name';
    // 除了真正的切换，这里还会有降级前的"阶跃探测"记录——它不是切换，
    // 但探测期间那 20 秒功率会故意低一截，不说清楚就成了无源之水。
    name.textContent = c.kind === 'probe' ? '目标功率探测'
      : c.kind === 'probe-ok' ? '探测通过，保留原生 ERG'
        : (MODE_NAMES[c.from] || c.from) + ' → ' + (MODE_NAMES[c.to] || c.to);
    const why = document.createElement('span');
    why.className = 'r-num';
    why.style.gridColumn = '2 / -1';
    why.style.textAlign = 'left';
    why.style.color = 'var(--muted)';
    why.textContent = c.reason || '';
    row.append(idx, name, why);
    els.sumModeChanges.appendChild(row);
  });

  // 分段明细：间歇训练最有价值的反馈粒度就是"每组"，整体平均会把踩崩的一组
  // 和踩得漂亮的一组平均掉，看不出问题在哪。
  const rows = sum.intervals || [];
  els.sumIntervalSection.classList.toggle('hidden', rows.length === 0);
  els.sumIntervals.innerHTML = '';
  if (rows.length) {
    const head = document.createElement('div');
    head.className = 'iv-head-row';
    ['#', '分段', '目标', '实际', '达标'].forEach((text) => {
      const span = document.createElement('span');
      span.textContent = text;
      head.appendChild(span);
    });
    els.sumIntervals.appendChild(head);

    rows.forEach((row, i) => {
      const el = document.createElement('div');
      el.className = 'iv-row ' + (row.kind || '');

      const idx = document.createElement('span');
      idx.className = 'r-idx'; idx.textContent = i + 1;

      const name = document.createElement('span');
      name.className = 'r-name';
      name.textContent = row.name + (row.zone ? ' · ' + row.zone : '');
      name.title = row.name + (row.zone ? ' · ' + row.zone : '');

      const target = document.createElement('span');
      target.className = 'r-num';
      target.textContent = Math.round(row.target_power) + 'W';

      const avg = document.createElement('span');
      avg.className = 'r-num';
      avg.textContent = row.avg_power == null ? '--' : Math.round(row.avg_power) + 'W';

      const pct = document.createElement('span');
      const good = row.in_zone_pct != null && row.in_zone_pct >= 80;
      pct.className = 'r-num ' + (row.in_zone_pct == null ? '' : (good ? 'r-good' : 'r-bad'));
      pct.textContent = row.in_zone_pct == null ? '--' : row.in_zone_pct.toFixed(0) + '%';

      el.append(idx, name, target, avg, pct);
      els.sumIntervals.appendChild(el);
    });
  }
}

/* ------------------------------------------------------------------ *
 * 训练方案（间歇训练）
 * ------------------------------------------------------------------ */

let templates = [];
let selectedTemplateId = null;
let paramValues = {};
let currentPlan = null;          // {steps, stats}
let setupMode = 'constant';      // 'constant' | 'interval'
let planTimer = null;

async function loadTemplates() {
  try {
    const res = await fetch('/api/templates', { cache: 'no-store' });
    const data = await res.json();
    templates = data.templates || [];
    renderTemplateCards();
    if (templates.length) selectTemplate(templates[0].id);
  } catch (e) {
    logLine('error', '读取训练方案失败：' + e.message);
  }
}

function renderTemplateCards() {
  els.templateList.innerHTML = '';
  templates.forEach((t) => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'template-card' + (t.id === selectedTemplateId ? ' on' : '');
    const name = document.createElement('span');
    name.className = 'tc-name';
    name.textContent = t.name;
    const sub = document.createElement('span');
    sub.className = 'tc-sub';
    sub.textContent = t.subtitle || '';
    card.append(name, sub);
    card.addEventListener('click', () => selectTemplate(t.id));
    els.templateList.appendChild(card);
  });
}

function selectTemplate(id) {
  selectedTemplateId = id;
  const t = templates.find((x) => x.id === id);
  paramValues = {};
  (t && t.params ? t.params : []).forEach((p) => { paramValues[p.key] = p.default; });
  renderTemplateCards();
  els.templateDesc.textContent = (t && t.desc) || '';
  renderParamEditor();
  refreshPlan();
}

/* 通用参数编辑器：间歇方案和 FTP 测试方案共用同一套渲染。
   两边唯一的差别只是"值存在哪个对象里"和"刷新哪个预览"。 */
function renderParamGrid(container, params, values, onChanged, resolve) {
  container.innerHTML = '';
  (params || []).forEach((p) => {
    const cell = document.createElement('div');
    cell.className = 'param';

    const label = document.createElement('span');
    label.className = 'param-label';
    label.textContent = p.label;

    const wrap = document.createElement('div');
    wrap.className = 'param-input';
    const input = document.createElement('input');
    input.type = 'number';
    input.min = p.min; input.max = p.max; input.step = p.step;
    input.value = values[p.key];
    input.addEventListener('input', () => {
      values[p.key] = Number(input.value);
      onChanged();
    });
    input.addEventListener('blur', () => {
      const typed = Number(input.value);
      // 只在用户填的值真的越界时才用服务端的夹取结果纠正。
      // 无条件回填是错的：预览还停留在上一次请求的结果上（刷新有防抖），
      // 改完参数马上按 Tab 或点「开始训练」（mousedown 会先触发 blur）
      // 会把刚输入的值悄悄退回旧值，然后用旧参数开跑。
      const outOfRange = isNaN(typed) || typed < p.min || typed > p.max;
      if (!outOfRange) return;
      resolve().then((resolved) => {
        if (!resolved) return;
        const v = resolved[p.key];
        if (v != null) { input.value = v; values[p.key] = v; }
      });
    });
    const unit = document.createElement('span');
    unit.className = 'param-unit';
    unit.textContent = p.unit || '';

    wrap.append(input, unit);
    cell.append(label, wrap);
    container.appendChild(cell);
  });
}

function renderParamEditor() {
  const t = templates.find((x) => x.id === selectedTemplateId);
  if (!t) { els.templateParams.innerHTML = ''; return; }
  renderParamGrid(els.templateParams, t.params, paramValues, schedulePlanRefresh,
    () => {
      clearTimeout(planTimer);
      return refreshPlan().then(() => (currentPlan || {}).params || null);
    });
}

function renderTestParamEditor() {
  const t = ftpTests.find((x) => x.id === selectedTestId);
  if (!t) { els.testParams.innerHTML = ''; return; }
  renderParamGrid(els.testParams, t.params, testParamValues, scheduleTestPlan,
    () => {
      clearTimeout(testPlanTimer);
      return refreshTestPlan().then(() => (currentTestPlan || {}).params || null);
    });
}

function schedulePlanRefresh() {
  clearTimeout(planTimer);
  planTimer = setTimeout(refreshPlan, 350);
}

async function refreshPlan() {
  if (!selectedTemplateId) return;
  try {
    const res = await api('/api/plan', {
      template_id: selectedTemplateId,
      ftp: Number(els.ftpInput.value) || 200,
      params: paramValues,
    });
    currentPlan = res;
    renderPlanPreview();
  } catch (err) {
    els.planStats.innerHTML = '';
    els.planList.innerHTML = '';
    logLine('error', '生成课表失败：' + err.message);
  }
}

function renderPlanPreview() {
  if (!currentPlan) return;
  renderPlanInto(currentPlan, els.planStats, els.planList, els.planChart, false);
}

/* ------------------------------------------------------------------ *
 * FTP 测试方案
 * ------------------------------------------------------------------ */

let ftpTests = [];
let selectedTestId = null;
let testParamValues = {};
let currentTestPlan = null;
let testPlanTimer = null;

async function loadFtpTests() {
  try {
    const res = await fetch('/api/tests', { cache: 'no-store' });
    const data = await res.json();
    ftpTests = data.tests || [];
    // 这个函数每次切到「FTP 测试」标签页都会跑（顺带当重试），所以不能无条件
    // selectTest(第一个方案)——那样用户选好「8 分钟测试」、填完参数，切走再切回来
    // 就被悄悄重置成坡道测试，一点开始跑的是另一套协议。
    if (ftpTests.length && !ftpTests.some((t) => t.id === selectedTestId)) {
      selectTest(ftpTests[0].id);
    } else {
      renderTestCards();
    }
  } catch (e) {
    logLine('error', '读取 FTP 测试方案失败：' + e.message);
  }
}

function renderTestCards() {
  els.testList.innerHTML = '';
  ftpTests.forEach((t) => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'template-card' + (t.id === selectedTestId ? ' on' : '');
    const name = document.createElement('span');
    name.className = 'tc-name';
    name.textContent = t.name;
    const sub = document.createElement('span');
    sub.className = 'tc-sub';
    sub.textContent = t.subtitle || '';
    card.append(name, sub);
    card.addEventListener('click', () => selectTest(t.id));
    els.testList.appendChild(card);
  });
}

function selectTest(id) {
  selectedTestId = id;
  const t = ftpTests.find((x) => x.id === id);
  testParamValues = {};
  (t && t.params ? t.params : []).forEach((p) => { testParamValues[p.key] = p.default; });
  renderTestCards();
  els.testDesc.textContent = (t && t.desc) || '';
  renderTestParamEditor();
  refreshTestPlan();
}

function scheduleTestPlan() {
  clearTimeout(testPlanTimer);
  testPlanTimer = setTimeout(refreshTestPlan, 350);
}

async function refreshTestPlan() {
  if (!selectedTestId) return null;
  try {
    const res = await api('/api/plan', {
      test_id: selectedTestId,
      ftp: Number(els.ftpInput.value) || 200,
      params: testParamValues,
    });
    currentTestPlan = res;
    renderTestPlanPreview();
    return res;
  } catch (err) {
    logLine('error', '生成测试课表失败：' + err.message);
    return null;
  }
}

/* ------------------------------------------------------------------ *
 * 视图切换：连接设备 / 训练
 *
 * 分成两页是因为它们对应两件不同的事：一页只负责"把设备接上"（骑行台 + 心率带），
 * 另一页是训练本身（目标、心率参数、实时、总结、报告）。以前全堆在一页里，
 * 连接的时候要往下滚过一堆参数才找得到设备列表。
 * ------------------------------------------------------------------ */

let currentView = 'devices';

function applyView(view) {
  currentView = view === 'training' ? 'training' : 'devices';
  els.devicesView.classList.toggle('hidden', currentView !== 'devices');
  els.trainingView.classList.toggle('hidden', currentView !== 'training');
  // 按 id 取而不是按 class 选择器：按钮就两个，写死更简单，测试也好断言
  els.btnViewDevices.classList.toggle('on', currentView === 'devices');
  els.btnViewTraining.classList.toggle('on', currentView === 'training');
  // 状态胶囊只在训练页显示：设备页本身就有一块"骑行台"和一块"心率带"，
  // 各自带着状态和数据，再在顶上重复一遍纯属噪音。
  els.connChips.classList.toggle('hidden', currentView !== 'training');
  try { window.localStorage.setItem('ibike.view', currentView); } catch (e) { /* 无所谓 */ }
}

function switchView(view) {
  applyView(view);
}

/* 顶部那两枚状态胶囊：不管在哪一页都能看到设备接上没有 */
function renderConnChips(s) {
  const trainer = (s && s.trainer) || {};
  const hr = (s && s.hr_client) || null;
  const items = [
    ['骑行台', !!trainer.connected],
    ['心率带', !!(hr && hr.connected)],
  ];
  els.connChips.innerHTML = '';
  items.forEach(([label, on]) => {
    const chip = document.createElement('span');
    chip.className = 'conn-chip ' + (on ? 'on' : 'off');
    chip.textContent = label + (on ? ' ✓' : ' —');
    els.connChips.appendChild(chip);
  });
}

/* ------------------------------------------------------------------ *
 * FTP 设置（跨重启保留）
 * ------------------------------------------------------------------ */

let ftpSaveTimer = null;

async function loadSettings() {
  try {
    const res = await fetch('/api/settings', { cache: 'no-store' });
    const data = await res.json();
    const st = data.settings || {};
    if (st.ftp) els.ftpInput.value = st.ftp;
    if (st.hr_max) els.hrMaxInput.value = st.hr_max;
    if (st.hr_rest) els.hrRestInput.value = st.hr_rest;
    if (st.hr_zone_mode) els.hrZoneMode.value = st.hr_zone_mode;
    els.hrLimitEnabled.checked = !!st.hr_limit_enabled;
    if (st.hr_limit_bpm) els.hrLimitInput.value = st.hr_limit_bpm;
    syncHrLimitDisabled();
  } catch (e) { /* 用默认值就行 */ }
}

/* 心率上限那个输入框只有在勾了"启用"之后才有意义 */
function syncHrLimitDisabled() {
  els.hrLimitInput.disabled = !els.hrLimitEnabled.checked;
}

function hrSettingsBody() {
  return {
    hr_max: Number(els.hrMaxInput.value) || 180,
    hr_rest: Number(els.hrRestInput.value) || 60,
    hr_zone_mode: els.hrZoneMode.value,
    hr_limit_enabled: els.hrLimitEnabled.checked,
    hr_limit_bpm: Number(els.hrLimitInput.value) || 165,
  };
}

let hrSettingsTimer = null;
function scheduleHrSettingsSave() {
  clearTimeout(hrSettingsTimer);
  hrSettingsTimer = setTimeout(async () => {
    try { await api('/api/settings', hrSettingsBody()); }
    catch (e) { toast('心率设置没存上：' + e.message); }
  }, 800);
}

function scheduleFtpSave() {
  clearTimeout(ftpSaveTimer);
  ftpSaveTimer = setTimeout(async () => {
    const ftp = Number(els.ftpInput.value);
    if (!(ftp >= FTP_MIN && ftp <= FTP_MAX)) return;
    try { await api('/api/settings', { ftp: ftp }); } catch (e) { /* 存不上就算了 */ }
  }, 800);
}

/* 课表柱状图。同一个函数也用在训练中的进度显示上：
   currentIndex 之前的柱子变暗表示已完成，当前柱子高亮并带一条进度线。 */
function drawPlanChart(canvas, steps, opts = {}) {
  const fitted = fitCanvas(canvas, 100);
  const ctx = fitted.ctx;
  const cssW = fitted.w;
  const cssH = fitted.h;

  const padT = 7, padB = 7;
  const w = cssW, h = cssH - padT - padB;

  if (!steps || !steps.length) {
    ctx.fillStyle = '#3d4c5f';
    ctx.font = '11px -apple-system, sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('暂无课表', cssW / 2, cssH / 2);
    return;
  }

  const total = steps.reduce((a, s) => a + (s.duration_s || 0), 0) || 1;
  const maxP = Math.max(...steps.map((s) => s.target_power || 0), 1);
  const COLORS = {
    warmup: '#4da3ff', work: '#ff5f6d', recovery: '#43536b', cooldown: '#34e3a4',
  };
  const current = (opts.currentIndex == null) ? -1 : opts.currentIndex;

  let x = 0;
  let currentX = 0, currentW = 0;
  steps.forEach((s, i) => {
    const bw = Math.max(1, (s.duration_s / total) * w);
    const bh = Math.max(2, ((s.target_power || 0) / maxP) * h);
    const y = padT + h - bh;
    const base = COLORS[s.kind] || '#8b9bb0';
    if (current >= 0) {
      ctx.globalAlpha = i < current ? 0.28 : (i === current ? 1 : 0.65);
    } else {
      ctx.globalAlpha = 0.85;
    }
    ctx.fillStyle = base;
    ctx.fillRect(x, y, Math.max(1, bw - 0.7), bh);
    if (i === current) { currentX = x; currentW = bw; }
    x += bw;
  });
  ctx.globalAlpha = 1;

  // 当前段内部进度线：一眼看出这一段还剩多久
  if (current >= 0 && opts.progress != null) {
    const px = currentX + currentW * Math.max(0, Math.min(1, opts.progress));
    ctx.strokeStyle = '#e8eef6';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(px, padT - 2);
    ctx.lineTo(px, padT + h + 2);
    ctx.stroke();
  }
}

/* ------------------------------------------------------------------ *
 * 自定义课程
 * ------------------------------------------------------------------ */

const KIND_OPTIONS = [
  ['warmup', '热身'], ['work', '高强度'], ['recovery', '恢复'], ['cooldown', '冷身'],
];
/* 服务端 custom.py 里的同一上限。前端也要拦，否则超出的部分会被静默丢掉。 */
const MAX_STEPS = 80;
/* FTP 的合法范围，与服务端校验保持一致 */
const FTP_MIN = 50, FTP_MAX = 600;
const KIND_LABELS = {};
KIND_OPTIONS.forEach(([value, label]) => { KIND_LABELS[value] = label; });

let customCourses = [];
let editingCourse = null;
let customPlanTimer = null;
let customDirty = false;

function currentFtp() {
  return Math.max(50, Math.min(600, Number(els.ftpInput.value) || 200));
}

/* 两份功率值始终在客户端同步，切换表示方式时不需要往返服务器，
   也不会出现"切过去还是旧值"的问题。服务端保存时会再校验一次。 */
function setStepPct(step, pct) {
  step.pct = pct;
  step.watts = Math.max(20, Math.round(pct * currentFtp() / 100));
}
function setStepWatts(step, watts) {
  step.watts = Math.max(20, Math.round(watts));
  step.pct = Math.round(Math.max(20, watts) / currentFtp() * 1000) / 10;
}

async function loadCustomCourses() {
  try {
    const res = await fetch('/api/custom?ftp=' + currentFtp(), { cache: 'no-store' });
    const data = await res.json();
    customCourses = data.courses || [];
    renderCustomList();
  } catch (e) {
    logLine('error', '读取自定义课程失败：' + e.message);
  }
}

function renderCustomList() {
  els.customList.innerHTML = '';
  els.customEmpty.classList.toggle('hidden', customCourses.length > 0);
  customCourses.forEach((course) => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'template-card'
      + (editingCourse && editingCourse.id === course.id ? ' on' : '');

    const name = document.createElement('span');
    name.className = 'tc-name';
    name.textContent = course.name;

    const sub = document.createElement('span');
    sub.className = 'tc-sub';
    const stats = course.stats || {};
    sub.textContent = (stats.step_count || course.steps.length) + ' 节 · '
      + fmtClock(stats.total_s || 0) + ' · 峰值 ' + (stats.peak_power || 0) + 'W';

    card.append(name, sub);
    card.addEventListener('click', () => {
      // 同一门课再点一次不算切换；换课程会丢掉未保存的改动，先问一声
      if (editingCourse && editingCourse.id === course.id) return;
      if (!confirmDiscardChanges()) return;
      openCourse(course);
    });
    els.customList.appendChild(card);
  });
}

/* 有未保存改动时切换课程会丢掉它们，而 customDirty 只体现在一行小字提示里，
   容易看不见。删除课程都有确认，切换也应当有。 */
function confirmDiscardChanges() {
  if (!customDirty) return true;
  return window.confirm('当前课程有未保存的改动，确定要放弃吗？');
}

function openCourse(course) {
  editingCourse = JSON.parse(JSON.stringify(course));
  if (!editingCourse.steps || !editingCourse.steps.length) {
    editingCourse.steps = [];
  }
  customDirty = false;
  // 清掉上一门课排队中的课表刷新，否则它的响应回来会覆盖新课程的预览
  clearTimeout(customPlanTimer);
  lastCustomPlan = null;
  els.courseEditor.classList.remove('hidden');
  els.courseName.value = editingCourse.name || '';
  els.btnDeleteCourse.classList.toggle('hidden', !editingCourse.id);
  document.querySelectorAll('[data-pmode]').forEach((tab) => {
    tab.classList.toggle('on', tab.dataset.pmode === editingCourse.power_mode);
  });
  updatePmodeHint();
  updateSaveHint();
  renderStepRows();
  renderCustomList();
  refreshCustomPlan();
}

async function newCourse() {
  if (!confirmDiscardChanges()) return;
  try {
    const res = await fetch('/api/custom/new', { cache: 'no-store' });
    const data = await res.json();
    const course = data.course || {};
    course.id = '';                       // 空 id 表示新建，保存时才分配
    course.name = '我的课程';
    openCourse(course);
    els.courseName.focus();
    els.courseName.select();
  } catch (e) {
    toast('新建课程失败：' + e.message);
  }
}

function updatePmodeHint() {
  if (!editingCourse) return;
  els.pmodeHint.textContent = editingCourse.power_mode === 'abs'
    ? '每个小节的瓦数固定不变，换 FTP 也不会影响这门课。适合"我就想稳稳踩 150W"。'
    : '每个小节的强度按 FTP 的百分比算，改 FTP 整门课跟着缩放。适合"我想练阈值"。';
}

function updateSaveHint() {
  if (!editingCourse) return;
  if (customDirty) {
    els.saveHint.textContent = '有未保存的改动。点「开始训练」会自动先保存。';
  } else if (editingCourse.id) {
    els.saveHint.textContent = '已保存。';
  } else {
    els.saveHint.textContent = '新课程尚未保存。';
  }
}

function markCustomDirty() {
  customDirty = true;
  updateSaveHint();
}

function updateStepSummary() {
  if (!editingCourse) return;
  const total = editingCourse.steps.reduce((a, s) => a + (s.duration_s || 0), 0);
  setText(els.stepCount, editingCourse.steps.length);
  setText(els.stepTotal, fmtClock(total));
}

function renderStepRows() {
  els.stepRows.innerHTML = '';
  if (!editingCourse) return;
  editingCourse.steps.forEach((step, index) => {
    els.stepRows.appendChild(buildStepRow(step, index));
  });
  updateStepSummary();
}

function buildStepRow(step, index) {
  const row = document.createElement('div');
  row.className = 'step-row ' + (step.kind || '');

  const idx = document.createElement('span');
  idx.className = 'sr-idx';
  idx.textContent = index + 1;

  // 类型：决定总结里这一段算不算"高强度"（只有高强度段才统计达标率）
  const kind = document.createElement('select');
  kind.className = 'sr-kind';
  KIND_OPTIONS.forEach(([value, label]) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    if (value === step.kind) option.selected = true;
    kind.appendChild(option);
  });

  const nameInput = document.createElement('input');
  nameInput.className = 'sr-name';
  nameInput.type = 'text';
  nameInput.maxLength = 20;
  nameInput.value = step.name || '';
  nameInput.placeholder = KIND_LABELS[step.kind] || '小节';

  kind.addEventListener('change', () => {
    step.kind = kind.value;
    row.className = 'step-row ' + step.kind;
    nameInput.placeholder = KIND_LABELS[step.kind] || '小节';
    // 名字还是默认值时跟着类型走，省得每次都要手打
    const defaults = Object.values(KIND_LABELS);
    if (!step.name || defaults.indexOf(step.name) >= 0) {
      step.name = KIND_LABELS[step.kind];
      nameInput.value = step.name;
    }
    markCustomDirty();
    scheduleCustomPlan();
  });

  nameInput.addEventListener('input', () => {
    step.name = nameInput.value;
    markCustomDirty();
    scheduleCustomPlan();
  });

  // 时长：内部一律存秒，界面按"分钟/秒"折算显示
  const durWrap = document.createElement('div');
  durWrap.className = 'sr-dur';
  const durInput = document.createElement('input');
  durInput.type = 'number';
  durInput.min = '1';
  durInput.inputMode = 'numeric';
  const unitSelect = document.createElement('select');
  [['60', '分钟'], ['1', '秒']].forEach(([value, label]) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    unitSelect.appendChild(option);
  });
  let unit = (step.duration_s >= 60 && step.duration_s % 60 === 0) ? 60 : 1;
  unitSelect.value = String(unit);

  /* 把秒数换算成当前单位下要显示的数字。
     这里必须保留小数：90 秒显示成"2 分钟"而模型还是 90 秒的话，屏幕上写的和
     实际存的就不是一回事（下面预览会立刻显示 01:30 打脸）。 */
  function showDuration() {
    const shown = step.duration_s / unit;
    return String(Number(shown.toFixed(2)));
  }
  durInput.value = showDuration();

  durInput.addEventListener('input', () => {
    const value = Number(durInput.value);
    // 允许中途清空输入框，不要打断正在打字的人
    if (!value || value <= 0) { markCustomDirty(); return; }
    step.duration_s = Math.max(5, Math.round(value * unit));
    updateStepSummary();
    markCustomDirty();
    scheduleCustomPlan();
  });
  // 失焦/回车时把夹取后的真实值写回输入框（填 1 秒会变成 5 秒）。
  // 只在 change 里做、不在 input 里做，否则"打 12"会被 5 秒的夹取结果打断。
  durInput.addEventListener('change', () => { durInput.value = showDuration(); });
  unitSelect.addEventListener('change', () => {
    unit = Number(unitSelect.value);
    durInput.value = showDuration();
  });
  durWrap.append(durInput, unitSelect);

  // 功率：单位由课程级的表示方式决定
  const absMode = editingCourse.power_mode === 'abs';
  const powWrap = document.createElement('div');
  powWrap.className = 'sr-pow';
  const powInput = document.createElement('input');
  powInput.type = 'number';
  powInput.step = '1';
  powInput.inputMode = 'numeric';
  powInput.value = String(absMode ? step.watts : step.pct);
  const powUnit = document.createElement('span');
  powUnit.className = 'sr-powunit';
  powUnit.textContent = absMode ? 'W' : '%FTP';
  powInput.addEventListener('input', () => {
    const value = Number(powInput.value);
    if (!value || value <= 0) { markCustomDirty(); return; }
    if (absMode) setStepWatts(step, value); else setStepPct(step, value);
    markCustomDirty();
    scheduleCustomPlan();
  });
  powWrap.append(powInput, powUnit);

  const buttons = document.createElement('div');
  buttons.className = 'sr-btns';
  const mk = (label, title, handler, extra) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.title = title;
    if (extra) button.className = extra;
    button.addEventListener('click', handler);
    return button;
  };
  buttons.append(
    mk('↑', '上移', () => moveStep(index, -1)),
    mk('↓', '下移', () => moveStep(index, 1)),
    mk('×', '删除这一节', () => removeStep(index), 'del'),
  );

  row.append(idx, kind, nameInput, durWrap, powWrap, buttons);
  return row;
}

function addStep() {
  if (!editingCourse) return;
  if (editingCourse.steps.length >= MAX_STEPS) {
    // 服务端也是这个上限，而且是静默取前 N 个。不在这里拦住的话，用户会看到
    // 自己刚加的小节在保存后当场消失。
    toast('一节课程最多 ' + MAX_STEPS + ' 个小节');
    return;
  }
  const last = editingCourse.steps[editingCourse.steps.length - 1];
  editingCourse.steps.push({
    name: '高强度',
    kind: 'work',
    duration_s: 300,
    pct: last ? last.pct : 90,
    watts: last ? last.watts : Math.round(currentFtp() * 0.9),
  });
  markCustomDirty();
  renderStepRows();
  scheduleCustomPlan();
}

function removeStep(index) {
  if (!editingCourse) return;
  if (editingCourse.steps.length <= 1) {
    toast('课程至少要有一个小节');
    return;
  }
  editingCourse.steps.splice(index, 1);
  markCustomDirty();
  renderStepRows();
  scheduleCustomPlan();
}

function moveStep(index, delta) {
  if (!editingCourse) return;
  const target = index + delta;
  if (target < 0 || target >= editingCourse.steps.length) return;
  const steps = editingCourse.steps;
  const tmp = steps[index];
  steps[index] = steps[target];
  steps[target] = tmp;
  markCustomDirty();
  renderStepRows();
  scheduleCustomPlan();
}

function scheduleCustomPlan() {
  clearTimeout(customPlanTimer);
  customPlanTimer = setTimeout(refreshCustomPlan, 350);
}

let scheduleCourseListTimer = null;

/* FTP 变了以后课程卡片上的峰值功率就不准了，重新拉一次列表（带防抖，
   因为用户可能连续敲好几个数字） */
function scheduleCourseListRefresh() {
  clearTimeout(scheduleCourseListTimer);
  scheduleCourseListTimer = setTimeout(loadCustomCourses, 600);
}

let lastCustomPlan = null;

function renderCustomPlanPreview() {
  if (!lastCustomPlan) return;
  renderPlanInto(lastCustomPlan, els.customPlanStats, els.customPlanList,
                 els.customPlanChart, editingCourse && editingCourse.power_mode === 'abs');
}

async function refreshCustomPlan() {
  if (!editingCourse) return;
  if (!editingCourse.steps.length) {
    lastCustomPlan = null;
    els.customPlanStats.innerHTML = '';
    els.customPlanList.innerHTML = '';
    drawPlanChart(els.customPlanChart, [], {});
    return;
  }
  try {
    const res = await api('/api/plan', {
      course: {
        id: editingCourse.id || "",
        name: editingCourse.name,
        desc: editingCourse.desc || "",
        power_mode: editingCourse.power_mode,
        steps: editingCourse.steps,
      },
      ftp: currentFtp(),
    });
    lastCustomPlan = res;
    renderCustomPlanPreview();
  } catch (err) {
    logLine('error', '生成课表失败：' + err.message);
  }
}

/* 把服务端返回的课表渲染到指定的一组元素里（间歇预览和自定义预览共用） */
function renderPlanInto(plan, statsEl, listEl, chartEl, showPct) {
  const steps = plan.steps || [];
  const stats = plan.stats || {};

  statsEl.innerHTML = '';
  const items = [
    ['总时长', (stats.total_s / 60).toFixed(0) + ' 分钟'],
    ['高强度', (stats.work_s / 60).toFixed(0) + ' 分钟'],
    ['节数', stats.step_count + ' 节'],
    ['峰值', stats.peak_power + ' W'],
    ['均值', stats.avg_power + ' W'],
    ['预计做功', stats.est_kj + ' kJ'],
  ];
  items.forEach(([label, value]) => {
    const span = document.createElement('span');
    span.className = 'plan-stat';
    span.textContent = label + ' ';
    const strong = document.createElement('strong');
    strong.textContent = value;
    span.appendChild(strong);
    statsEl.appendChild(span);
  });

  listEl.innerHTML = '';
  steps.forEach((s, i) => {
    const li = document.createElement('li');
    li.className = s.kind || '';
    const idx = document.createElement('span');
    idx.className = 'pl-idx'; idx.textContent = i + 1;
    const name = document.createElement('span');
    name.className = 'pl-name';
    name.textContent = s.name + (showPct && s.pct_ftp != null
      ? '  (' + Math.round(s.pct_ftp * 100) + '%)' : '');
    const dur = document.createElement('span');
    dur.className = 'pl-dur'; dur.textContent = fmtClock(s.duration_s);
    const pow = document.createElement('span');
    pow.className = 'pl-pow'; pow.textContent = s.target_power + 'W';
    li.append(idx, name, dur, pow);
    listEl.appendChild(li);
  });

  drawPlanChart(chartEl, steps, {});
}

async function saveCourse(silent) {
  if (!editingCourse) return null;
  // 只提交课程本身需要的字段，别把 stats / updated_at 这些只读内容一起发过去
  const body = {
    id: editingCourse.id || "",
    name: editingCourse.name,
    desc: editingCourse.desc || "",
    power_mode: editingCourse.power_mode,
    steps: editingCourse.steps,
    ftp: currentFtp(),
  };
  try {
    const res = await api('/api/custom', body);
    // 服务端会夹取越界值（比如 500% 会被夹到 300%），保存后按它的结果回填界面，
    // 否则输入框里显示的和真正存下来的对不上
    editingCourse = JSON.parse(JSON.stringify(res.course));
    customDirty = false;
    els.courseName.value = editingCourse.name || '';
    renderStepRows();
    await loadCustomCourses();
    renderCustomList();
    els.btnDeleteCourse.classList.remove('hidden');
    updateSaveHint();
    if (!silent) logLine('info', '已保存课程「' + editingCourse.name + '」');
    return editingCourse;
  } catch (err) {
    toast('保存失败：' + err.message);
    return null;
  }
}

async function deleteCourse() {
  if (!editingCourse) return;
  if (!editingCourse.id) {
    // 还没保存过，直接丢掉编辑状态就行
    editingCourse = null;
    els.courseEditor.classList.add('hidden');
    renderCustomList();
    return;
  }
  if (!window.confirm('删除课程「' + editingCourse.name + '」？这个操作不能撤销。')) return;
  try {
    await api('/api/custom/delete', { id: editingCourse.id });
    logLine('info', '已删除课程「' + editingCourse.name + '」');
    editingCourse = null;
    els.courseEditor.classList.add('hidden');
    await loadCustomCourses();
  } catch (err) {
    toast('删除失败：' + err.message);
  }
}

/* ------------------------------------------------------------------ *
 * 功率曲线
 * ------------------------------------------------------------------ */

/* 通用功率曲线绘制。points 里每项是 {t(毫秒), p(瓦), target(瓦)}。
   目标功率画成阶梯虚线，这样训练中途改过目标也能如实反映出来。 */
function drawPowerChart(canvas, points, opts = {}) {
  const fitted = fitCanvas(canvas, 180);
  const ctx = fitted.ctx;
  const cssW = fitted.w;
  const cssH = fitted.h;

  const padT = 12, padB = 20, padL = 38, padR = 10;
  const w = cssW - padL - padR;
  const h = cssH - padT - padB;

  // 只认有限数：NaN/Infinity 混进来会让 maxP 变成 NaN，整块图的刻度全成 "NaN"。
  // （Python 的 json.dumps 会输出裸 NaN，前端 JSON.parse 会直接失败——所以这条
  // 主要是防御，真出现时宁可退化成空图，也不能画出一张全是 NaN 的图。）
  const usable = (points || []).filter(
    (d) => Number.isFinite(d.p) && Number.isFinite(d.t));
  if (usable.length < 2) {
    ctx.fillStyle = '#3d4c5f';
    ctx.font = '12px -apple-system, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(opts.emptyText || '暂无数据', cssW / 2, cssH / 2);
    return;
  }

  let maxP = 0;
  usable.forEach((d) => {
    maxP = Math.max(maxP, d.p, d.target || 0);
  });
  maxP = Math.max(120, Math.ceil((maxP * 1.15) / 50) * 50);

  const t0 = usable[0].t;
  const t1 = usable[usable.length - 1].t;
  const span = Math.max(1000, t1 - t0);
  const X = (t) => padL + ((t - t0) / span) * w;
  const Y = (p) => padT + h - (Math.max(0, Math.min(maxP, p)) / maxP) * h;

  // 网格 + 纵轴刻度
  ctx.strokeStyle = '#1c2735';
  ctx.fillStyle = '#4a5a6e';
  ctx.lineWidth = 1;
  ctx.font = '10px -apple-system, sans-serif';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 3; i++) {
    const p = (maxP / 3) * i;
    const y = Y(p);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + w, y); ctx.stroke();
    ctx.fillText(String(Math.round(p)), padL - 6, y);
  }

  // 目标功率阶梯虚线
  ctx.save();
  ctx.setLineDash([5, 5]);
  ctx.strokeStyle = 'rgba(77,163,255,.75)';
  ctx.beginPath();
  let started = false;
  usable.forEach((d) => {
    if (d.target == null) return;
    const y = Y(d.target);
    if (!started) { ctx.moveTo(X(d.t), y); started = true; }
    else { ctx.lineTo(X(d.t), y); }
  });
  if (started) ctx.stroke();
  ctx.restore();

  // 功率曲线 + 渐变填充
  const grad = ctx.createLinearGradient(0, padT, 0, padT + h);
  grad.addColorStop(0, 'rgba(52,227,164,.30)');
  grad.addColorStop(1, 'rgba(52,227,164,0)');

  ctx.beginPath();
  ctx.moveTo(X(usable[0].t), Y(usable[0].p));
  for (let i = 1; i < usable.length; i++) {
    ctx.lineTo(X(usable[i].t), Y(usable[i].p));
  }
  ctx.strokeStyle = '#34e3a4';
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.stroke();

  ctx.lineTo(X(usable[usable.length - 1].t), padT + h);
  ctx.lineTo(X(usable[0].t), padT + h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // 心率曲线：右侧独立刻度。和功率画在一起是因为"同样的功率、心率一路往上爬"
  // 这件事只有两条线叠着看才看得出来（那就是有氧漂移）。
  const hrPoints = usable.filter((d) => Number.isFinite(d.hr));
  if (hrPoints.length >= 2) {
    // 心率的纵轴范围取数据本身的上下各留 10 的余量，不和功率共用刻度
    let lo = Infinity, hi = -Infinity;
    hrPoints.forEach((d) => { lo = Math.min(lo, d.hr); hi = Math.max(hi, d.hr); });
    const pad = Math.max(6, (hi - lo) * 0.15);
    lo = Math.max(30, lo - pad);
    hi = hi + pad;
    const spanHr = Math.max(1, hi - lo);
    const Yhr = (v) => padT + h - ((Math.max(lo, Math.min(hi, v)) - lo) / spanHr) * h;

    ctx.save();
    ctx.beginPath();
    ctx.moveTo(X(hrPoints[0].t), Yhr(hrPoints[0].hr));
    for (let i = 1; i < hrPoints.length; i++) {
      ctx.lineTo(X(hrPoints[i].t), Yhr(hrPoints[i].hr));
    }
    ctx.strokeStyle = 'rgba(229,72,77,.9)';
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.stroke();
    ctx.restore();

    // 右侧刻度：只标最高/最低两个值，避免和左侧的功率刻度挤在一起
    ctx.fillStyle = 'rgba(229,72,77,.85)';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.font = '10px -apple-system, sans-serif';
    ctx.fillText(Math.round(hi) + ' bpm', padL + w - 46, padT + 6);
    ctx.fillText(Math.round(lo) + '', padL + w - 30, padT + h - 6);
  }

  // 时间轴
  ctx.fillStyle = '#4a5a6e';
  ctx.textBaseline = 'top';
  ctx.textAlign = 'left';
  ctx.font = '10px -apple-system, sans-serif';
  const minutes = span / 60000;
  ctx.fillText(minutes < 1 ? `${Math.round(span / 1000)} 秒` : `${minutes.toFixed(0)} 分钟`,
    padL, padT + h + 5);
  ctx.textAlign = 'right';
  ctx.fillText(hrPoints.length >= 2 ? '功率 W ／ 心率 bpm' : '功率 W',
    padL + w, padT + h + 5);
}

function drawChart() {
  drawPowerChart(els.chart, chartData.map((d) => ({
    t: d.t, p: d.power, target: d.target, hr: d.hr,
  })), { emptyText: '开始骑行后这里会显示功率曲线' });
}

/* ------------------------------------------------------------------ *
 * 训练报告
 * ------------------------------------------------------------------ */

async function loadReports() {
  try {
    const res = await fetch('/api/reports', { cache: 'no-store' });
    const data = await res.json();
    reportList = data.reports || [];
    renderReports();
  } catch (e) {
    logLine('error', '读取训练报告失败：' + e.message);
  }
}

function fmtDateTime(ts) {
  if (!ts) return '--';
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return (d.getMonth() + 1) + '月' + d.getDate() + '日 '
    + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

function renderReports() {
  els.reportsList.innerHTML = '';
  els.reportsEmpty.classList.toggle('hidden', reportList.length > 0);
  els.btnClearReports.classList.toggle('hidden', reportList.length === 0);

  reportList.forEach((r) => {
    const li = document.createElement('li');
    li.className = 'report-item';

    const main = document.createElement('div');
    main.className = 'rp-main';

    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'rp-open';

    const title = document.createElement('span');
    title.className = 'rp-title';
    const name = document.createElement('span');
    name.textContent = r.plan_name || '训练';
    const when = document.createElement('span');
    when.className = 'rp-when';
    // 显示开始时间：认出"这是哪一次训练"靠的是什么时候骑的，不是什么时候结束的
    when.textContent = fmtDateTime(r.started_at || r.finished_at || r.saved_at);
    const badge = document.createElement('span');
    badge.className = 'rp-badge' + (r.completed ? '' : ' partial');
    badge.textContent = r.completed ? '已完成' : '提前结束';
    title.append(name, when, badge);
    if (r.sample) {
      // 合成数据明确标出来，免得过几天看到一条自己没骑过的记录以为程序出错
      const sampleTag = document.createElement('span');
      sampleTag.className = 'rp-badge sample';
      sampleTag.textContent = '示例';
      sampleTag.title = '这是生成的示例数据，不是真实训练记录';
      title.appendChild(sampleTag);
    }

    // 一行关键数字，够用来认出"这是哪一次"
    const stats = document.createElement('span');
    stats.className = 'rp-stats';
    const items = [
      ['时长', fmtClock(r.actual_s)],
      ['均值', r.avg_power == null ? '--' : Math.round(r.avg_power) + 'W'],
      ['目标', r.target_power == null ? '--' : Math.round(r.target_power) + 'W'],
      ['NP', r.normalized_power == null ? '--' : Math.round(r.normalized_power) + 'W'],
      ['做功', r.energy_kj == null ? '--' : Math.round(r.energy_kj) + 'kJ'],
    ];
    items.forEach(([label, value]) => {
      const span = document.createElement('span');
      span.append(label + ' ');
      const b = document.createElement('b');
      b.textContent = value;
      span.appendChild(b);
      stats.appendChild(span);
    });

    open.append(title, stats);
    open.addEventListener('click', () => openReport(r.id));

    main.appendChild(open);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'rp-del';
    del.textContent = '×';
    del.title = '删除这份报告';
    del.setAttribute('aria-label', '删除 ' + (r.plan_name || '这份报告'));
    del.addEventListener('click', (ev) => {
      ev.stopPropagation();       // 别把外层的"打开报告"也触发了
      deleteReport(r.id, r.plan_name || '这份报告');
    });

    li.append(main, del);
    els.reportsList.appendChild(li);
  });
}

async function openReport(id) {
  try {
    const res = await fetch('/api/reports/' + encodeURIComponent(id), { cache: 'no-store' });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || '读取失败');
    viewingReport = data.report;
    renderedSummaryKey = null;      // 强制重画，避免复用上一次的签名
    render(state);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (e) {
    toast('打开报告失败：' + e.message);
  }
}

function closeReport() {
  viewingReport = null;
  renderedSummaryKey = null;
  render(state);
}

async function deleteReport(id, label) {
  if (!window.confirm('删除「' + label + '」这份报告？这个操作不能撤销。')) return;
  try {
    const data = await api('/api/reports/delete', { id: id });
    reportList = data.reports || [];
    renderReports();
    logLine('info', '已删除训练报告');
    // 正在看的就是被删掉的那份，就退回列表
    if (viewingReport && viewingReport.id === id) closeReport();
  } catch (e) {
    toast('删除失败：' + e.message);
  }
}

async function clearReports() {
  if (!window.confirm('删除全部 ' + reportList.length + ' 份训练报告？这个操作不能撤销。')) return;
  try {
    const data = await api('/api/reports/clear', {});
    reportList = [];
    renderReports();
    logLine('info', '已清空训练报告（' + (data.removed || 0) + ' 份）');
    if (viewingReport) closeReport();
  } catch (e) {
    toast('清空失败：' + e.message);
  }
}

/* ------------------------------------------------------------------ *
 * 交互
 * ------------------------------------------------------------------ */

els.btnScan.addEventListener('click', async () => {
  els.btnScan.disabled = true;
  els.scanHint.textContent = '正在扫描，大约需要 8 秒…';
  els.deviceList.innerHTML = '';
  els.hrList.innerHTML = '';
  try {
    // 一次广播扫描就把骑行台和心率带都找出来（服务端只扫一次，见 ibike/ble.py）。
    // 它们本来就是同一批广播里的两类设备，没必要让用户点两次、等两遍。
    const data = await api('/api/scan', { timeout: 8 });
    lastScan = { trainers: data.devices || [], straps: data.hr_devices || [] };
    els.foundBlock.classList.remove('hidden');
    renderFoundLists(true);
    const devices = lastScan.trainers;
    const straps = lastScan.straps;
    const count = '（骑行台 ' + devices.length + ' 台 · 心率带 ' + straps.length + ' 根）';
    if (!devices.length && !straps.length) {
      els.scanHint.textContent = '没搜到设备。确认骑行台已通电、没被手机上的其他 App 占用，'
        + '心率带也戴上（电极沾点水），然后重试。';
    } else if (!devices.length) {
      els.scanHint.textContent = '只搜到心率带' + count + '，没搜到骑行台。';
    } else {
      els.scanHint.textContent = '点一下设备就能连接' + count + '。';
    }
  } catch (err) {
    toast('扫描失败：' + err.message);
    els.scanHint.textContent = '扫描失败。若是第一次运行，请在「系统设置 → 隐私与安全性 → 蓝牙」里允许终端访问蓝牙。';
  } finally {
    els.btnScan.disabled = false;
  }
});

/* 心率带的候选列表（和骑行台共用上面那一次扫描的结果） */
function renderHrList(devices, connectedAddress) {
  els.hrList.innerHTML = '';
  els.hrEmpty.classList.toggle('hidden', (devices || []).length > 0);
  (devices || []).forEach((d) => {
    const isConnected = !!connectedAddress
      && String(d.address).toLowerCase() === String(connectedAddress).toLowerCase();
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'device' + (isConnected ? ' device-on' : '');
    const info = document.createElement('span');
    info.className = 'device-info';
    const name = document.createElement('span');
    name.className = 'device-name';
    name.textContent = d.name || '(无名)';
    const meta = document.createElement('span');
    meta.className = 'device-meta';
    meta.textContent = (d.rssi != null ? d.rssi + ' dBm' : '') + '  ' + d.address;
    info.append(name, meta);
    const tag = document.createElement('span');
    tag.className = isConnected ? 'tag tag-on' : 'tag tag-dim';
    tag.textContent = isConnected ? '已连接' : (d.rssi != null ? d.rssi + ' dBm' : '心率带');
    btn.append(info, tag);
    if (!isConnected) {
      btn.addEventListener('click', () => connectHr(d.address));
    } else {
      btn.disabled = true;
    }
    li.appendChild(btn);
    els.hrList.appendChild(li);
  });
}

/* 上次扫描到的设备。留着是为了在连接状态变化时重新渲染，
   把"已连接"那一台标出来——不然用户点完不知道到底连上没有。 */
let lastScan = { trainers: [], straps: [] };
let lastScanConn = { trainer: '', strap: '' };

/* 把扫描结果画成两个列表。everyRender=false 时只在连接状态真的变了才重画——
   render() 被状态推送按 4Hz 调用，每次都重建列表会把点击吃掉。 */
function renderFoundLists(force) {
  const conn = {
    trainer: ((state && state.trainer) || {}).address || '',
    strap: ((state && state.hr_client) || {}).address || '',
  };
  if (!force && conn.trainer === lastScanConn.trainer
      && conn.strap === lastScanConn.strap) {
    return;
  }
  lastScanConn = conn;
  renderDevices(lastScan.trainers, conn.trainer, 'trainer');
  renderHrList(lastScan.straps, conn.strap);
}

function renderDevices(devices, connectedAddress) {
  els.deviceList.innerHTML = '';
  els.deviceEmpty.classList.toggle('hidden', devices.length > 0);
  devices.forEach((d) => {
    const isConnected = !!connectedAddress
      && String(d.address).toLowerCase() === String(connectedAddress).toLowerCase();
    const li = document.createElement('li');
    li.className = 'device' + (isConnected ? ' device-on' : '');

    const info = document.createElement('div');
    info.className = 'device-info';
    const name = document.createElement('span');
    name.className = 'device-name';
    name.textContent = d.name;
    const meta = document.createElement('span');
    meta.className = 'device-meta';
    meta.textContent = `${d.address}  ·  信号 ${d.rssi ?? '?'} dBm`;
    info.append(name, meta);

    const tag = document.createElement('span');
    if (isConnected) {
      // 已经连上的那台明确标出来：连接结果在这里就能看到，
      // 不用去下面那块状态里反着找
      tag.className = 'tag tag-on';
      tag.textContent = '已连接';
    } else if (d.has_ftms) {
      tag.className = 'tag';
      tag.textContent = 'FTMS';
    } else if (d.has_cps) {
      tag.className = 'tag';
      tag.textContent = '功率计';
    } else {
      tag.className = 'tag tag-dim';
      tag.textContent = '未知';
    }

    li.append(info, tag);
    if (!isConnected) {
      // 设备列表是唯一的连接入口，只能用鼠标点的话键盘用户就没法连骑行台了
      li.setAttribute('role', 'button');
      li.setAttribute('tabindex', '0');
      li.setAttribute('aria-label', '连接 ' + d.name);
      const connect = () => doConnect({ address: d.address });
      li.addEventListener('click', connect);
      li.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          connect();
        }
      });
    }
    els.deviceList.appendChild(li);
  });
}

async function doConnect(payload) {
  els.scanHint.textContent = '正在连接…';
  try {
    const res = await api('/api/connect', payload);
    // 直接用连接接口返回的状态刷新界面，不等 WebSocket——这样即使 WS 不通，
    // 按钮也能立刻变成可点状态
    if (res.state) render(res.state);
    els.deviceList.innerHTML = '';
    els.scanHint.textContent = '已连接。设定目标功率和时长，然后开始。';
    showBanner('已连接骑行台，可以设定目标并开始训练了。', 'info');
    logLine('connected', '已连接 ' + ((res.trainer || {}).name || payload.address || '模拟设备'));
  } catch (err) {
    toast('连接失败：' + err.message);
    els.scanHint.textContent = '连接失败，请重试或换一台设备。';
  }
}

els.btnSim.addEventListener('click', () => {
  // 选了闭环阻力就给一台"不理会目标功率"的模拟台，好把闭环这条路径真跑起来
  const mode = els.ergMode.value;
  doConnect({ simulator: true, simulator_responds: mode !== 'resistance' });
});

/* scanHint 的初始文案（断开后要还原成它，否则页面上会一直挂着"已连接"） */
const SCAN_HINT_IDLE = '骑行台通上电、踩两圈唤醒它，再点扫描。';

els.btnDisconnect.addEventListener('click', async () => {
  try {
    await api('/api/disconnect', {});
    logLine('info', '已断开');
    // 断开之前这里只写了一条日志，于是左边写着"未连接"，右边提示还挂着
    // "已连接。设定目标功率和时长，然后开始。"，横幅也还是那条绿色的"已连接"。
    els.scanHint.textContent = SCAN_HINT_IDLE;
    hideBanner();
  } catch (err) { toast(err.message); }
});

els.btnStart.addEventListener('click', async () => {
  let body;
  if (setupMode === 'interval' || setupMode === 'custom') {
    // FTP 原样交给服务端校验。以前在 currentFtp() 里静默夹到 50-600，用户填了
    // 2（想打 250 打了一半）就会以 FTP=50 开跑，强度差好几倍而毫无提示。
    const ftp = Number(els.ftpInput.value);
    if (!(ftp >= FTP_MIN && ftp <= FTP_MAX)) {
      toast('FTP 请填 ' + FTP_MIN + '-' + FTP_MAX + 'W 之间的值（当前填的是 ' + els.ftpInput.value + '）');
      els.ftpInput.focus();
      return;
    }
  }
  if (setupMode === 'interval') {
    body = {
      mode: 'interval',
      template_id: selectedTemplateId,
      ftp: Number(els.ftpInput.value),
      params: paramValues,
      erg_mode: els.ergMode.value,
    };
  } else if (setupMode === 'test') {
    if (!selectedTestId) { toast('请先选一个测试方案'); return; }
    // FTP 原样交给服务端校验
    const ftp = Number(els.ftpInput.value);
    if (!(ftp >= FTP_MIN && ftp <= FTP_MAX)) {
      toast('FTP 请填 ' + FTP_MIN + '-' + FTP_MAX + 'W 之间的值');
      els.ftpInput.focus();
      return;
    }
    // 控制方式由服务端按测试方案决定，这里不传 erg_mode——
    // 20 分钟测试必须自由骑行，用 ERG 跑出来的结果没有意义
    body = {
      mode: 'test',
      test_id: selectedTestId,
      ftp: ftp,
      params: testParamValues,
    };
  } else if (setupMode === 'custom') {
    if (!editingCourse) { toast('请先新建或选择一个自定义课程'); return; }
    if (!editingCourse.steps.length) { toast('课程至少要有一个小节'); return; }
    if (editingCourse.steps.length > MAX_STEPS) {
      toast('一节课程最多 ' + MAX_STEPS + ' 个小节');
      return;
    }
    // 先把当前编辑保存下来，别让辛苦排的课表因为忘了点保存而丢掉
    const saved = await saveCourse(true);
    if (!saved) return;
    body = {
      mode: 'custom',
      custom_id: saved.id,
      ftp: Number(els.ftpInput.value),
      erg_mode: els.ergMode.value,
    };
  } else {
    body = {
      mode: 'constant',
      target_power: Number(els.targetPower.value),
      duration_min: Number(els.duration.value),
      erg_mode: els.ergMode.value,
    };
  }
  // startPending 让 render 不要在请求飞行途中把按钮又点亮（空闲时每 2 秒有一次
  // 状态心跳，正好落在窗口里的话，用户会以为按钮能点，再点一次就收到一条
  // "训练已经在进行中" 的假报错）
  startPending = true;
  els.btnStart.disabled = true;
  chartData = [];
  lastSummary = null;
  viewingReport = null;
  renderedSummaryKey = null;
  try {
    await api('/api/start', body);
    if (window.innerWidth > 700) {
      // 桌面端把实时区滚进视野
      setTimeout(() => els.livePanel.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100);
    }
  } catch (err) {
    toast('无法开始：' + err.message);
  } finally {
    startPending = false;
  }
});

/* ---- 模式切换 ---- */

/* 只匹配顶层的模式标签；自定义编辑器里那组"功率表示方式"也用了 .tab 类，
   不加这个限定会被一起选中，点一下就跳到错误的模式。 */
const MODE_TAB_SELECTOR = '.tabs > .tab[data-mode]';

function applySetupMode(mode) {
  setupMode = mode;
  document.querySelectorAll(MODE_TAB_SELECTOR).forEach((tab) => {
    tab.classList.toggle('on', tab.dataset.mode === mode);
  });
  els.constantFields.classList.toggle('hidden', mode !== 'constant');
  els.intervalFields.classList.toggle('hidden', mode !== 'interval');
  els.customFields.classList.toggle('hidden', mode !== 'custom');
  els.testFields.classList.toggle('hidden', mode !== 'test');
  els.ftpBlock.classList.toggle('hidden', mode === 'constant');

  if (mode === 'interval') {
    // 启动时那一次 /api/templates 万一失败，这里要能重试，否则间歇标签页
    // 永远是空的，"开始训练"只会回一句"未知的训练方案"，只能刷新页面
    if (!templates.length) loadTemplates();
    els.ftpHint.textContent =
      '强度都按这个值的百分比计算——乳酸阈值因人而异，只有表达成阈值百分比才算科学。'
      + '不确定的话：FTP ≈ 你能全力骑 20 分钟的平均功率 × 0.95，或者先用 200W 试一次。';
  } else if (mode === 'custom') {
    els.ftpHint.textContent =
      '课程里的小节按 %FTP 表示时会用这个值折算成瓦数；'
      + '按绝对瓦数表示的小节不受它影响。';
    loadCustomCourses();
  } else if (mode === 'test') {
    els.ftpHint.textContent =
      '这个值影响测试课表里"轻松骑"这类陪衬段的强度，以及方案预览。'
      + '测试的结论不依赖它——测出来的 FTP 会覆盖它。';
    loadFtpTests();
  }
}

document.querySelectorAll(MODE_TAB_SELECTOR).forEach((tab) => {
  tab.addEventListener('click', () => {
    applySetupMode(tab.dataset.mode);
    if (setupMode === 'interval' && !currentPlan) refreshPlan();
    if (setupMode === 'custom') refreshCustomPlan();
    if (setupMode === 'test' && !currentTestPlan) refreshTestPlan();
  });
});

/* ---- 心率带 ---- */

/* 「2 骑行台」这一块：连接状态 + 实时数据。
   列表里点一下就连接，这里立刻能看到功率/踏频在动——一眼就知道连没连上。 */
function renderTrainerBlock(s) {
  const trainer = (s && s.trainer) || {};
  const connected = !!trainer.connected;
  const sim = trainer.kind === 'simulator';
  els.trainerPill.textContent = connected ? (sim ? '模拟器' : '已连接') : '未连接';
  els.trainerPill.className = 'pill ' + (connected ? 'pill-on' : 'pill-off');
  els.trainerName.textContent = connected ? (trainer.name || '') : '—';
  els.btnDisconnect.classList.toggle('hidden', !connected);

  const idle = !connected;
  setText(els.devPower, idle || s.power == null ? '--' : Math.round(s.power));
  setText(els.devCadence, idle || s.cadence == null ? '--' : Math.round(s.cadence));
  setText(els.devSpeed, idle || s.speed == null ? '--' : s.speed.toFixed(1));
  setText(els.devRes, idle || s.resistance_raw == null
    ? '--' : (s.resistance_raw / 10).toFixed(1));

  if (!connected) {
    els.trainerHint.textContent = '还没连接。在上面扫描，然后点列表里的骑行台。';
  } else if (s.stale_data) {
    els.trainerHint.textContent = '已连接，但有一阵子没收到数据了——检查它是不是休眠了、'
      + '或者被手机抢走了连接。';
  } else if (s.power == null) {
    els.trainerHint.textContent = '已连接。踩两圈就会开始出数据。';
  } else {
    els.trainerHint.textContent = connected && sim
      ? '模拟设备，数据是程序生成的，用来先跑通流程。'
      : '数据正常。可以切到「2 训练」设定目标了。';
  }
}

function renderHr(stateObj) {
  const hr = (stateObj && stateObj.hr_client) || null;
  const connected = !!(hr && hr.connected);
  els.hrPill.textContent = connected ? '已连接' : '未连接';
  els.hrPill.className = 'pill ' + (connected ? 'pill-on' : 'pill-off');
  // 名字和电量只在**真的连着**的时候显示。断开之后对象还留着（界面要能区分
  // "没配过"和"配过但没连上"），但把上一根的名字和电量继续挂在那儿会被读成
  // "还连着"——状态写着未连接、右边却写着"模拟心率带 88%"，自相矛盾。
  els.hrName.textContent = (connected && hr && hr.name) ? hr.name : '—';
  els.hrBattery.textContent = (connected && hr && hr.battery != null)
    ? (hr.battery + '%') : '';
  els.btnHrDisconnect.classList.toggle('hidden', !connected);

  // 实时读数：和骑行台那块一样，连上就该立刻看到数字在动
  const bpm = (stateObj && stateObj.heart_rate) || null;
  const zone = (stateObj && stateObj.hr_zone) || null;
  const contact = (stateObj && stateObj.hr_contact) || null;
  setText(els.devHr, connected && bpm != null ? Math.round(bpm) : '--');
  setText(els.devHrZone, connected && zone ? zone.code : '--');
  setText(els.devHrContact, connected ? (contact || '—') : '--');
  els.devHr.className = 'm-value' + (connected && bpm != null
    && (stateObj.hr_limit || {}).enabled && stateObj.hr_limit.bpm
    && bpm >= stateObj.hr_limit.bpm ? ' m-hr-over' : '');

  if (!connected) {
    els.hrHint.textContent = '还没连接。心率带是可选的——只连骑行台也能正常训练。'
      + '在上面的「扫描与连接」里点它就能连上。';
    return;
  }
  // 带子有没有 RR 间期，第一次连上就能看出来（决定以后能不能做 HRV）
  const rr = hr.rr_seen ? '支持 RR 间期' : '不带 RR 间期（做不了静息 HRV）';
  if (hr.stale || bpm == null) {
    els.hrHint.textContent = '已连接，但还没收到心率数据——检查带子有没有戴好、'
      + '电极有没有沾水（干的电极读数会不稳）。' + rr + '。';
  } else {
    els.hrHint.textContent = '数据正常，' + rr + '。';
  }
}

async function connectHr(address) {
  els.hrHint.textContent = '正在连接…';
  try {
    await api('/api/hr/connect', address ? { address: address } : {});
    els.hrList.innerHTML = '';
    els.hrHint.textContent = '已连接。';
  } catch (err) {
    els.hrHint.textContent = '连接失败：' + err.message;
  }
}

els.btnHrDisconnect.addEventListener('click', async () => {
  try {
    await api('/api/hr/disconnect', {});
    els.hrHint.textContent = '已断开。连过一次之后，下次启动会自动重连。';
  } catch (err) { toast(err.message); }
});

els.hrLimitEnabled.addEventListener('change', () => {
  syncHrLimitDisabled();
  scheduleHrSettingsSave();
});
[els.hrMaxInput, els.hrRestInput, els.hrLimitInput].forEach((el) => {
  el.addEventListener('input', scheduleHrSettingsSave);
});
els.hrZoneMode.addEventListener('change', scheduleHrSettingsSave);

els.ftpInput.addEventListener('input', () => {
  schedulePlanRefresh();
  scheduleCustomPlan();
  scheduleTestPlan();
  scheduleFtpSave();
  // 课程卡片上显示的是按 FTP 折算后的峰值功率，FTP 变了要重新拉一次列表
  if (setupMode === 'custom' && editingCourse) scheduleCourseListRefresh();
});

/* ---- 自定义课程编辑 ---- */

els.btnNewCourse.addEventListener('click', newCourse);
els.btnAddStep.addEventListener('click', addStep);
els.btnSaveCourse.addEventListener('click', () => saveCourse(false));
els.btnDeleteCourse.addEventListener('click', deleteCourse);

els.courseName.addEventListener('input', () => {
  if (!editingCourse) return;
  editingCourse.name = els.courseName.value;
  markCustomDirty();
});

document.querySelectorAll('[data-pmode]').forEach((tab) => {
  tab.addEventListener('click', () => {
    if (!editingCourse) return;
    const next = tab.dataset.pmode;
    const prev = editingCourse.power_mode;
    if (next === prev) return;

    // 按"切换前那个权威值"在当前 FTP 下重新折算。
    // 不能直接显示另一份缓存值——那可能是上次用别的 FTP 存下来的，
    // 直接用了功率就会悄悄变掉（按 FTP 250 存的 150W/60%，缓存里
    // 可能是 75%，一切换就变成 187W）。
    editingCourse.steps.forEach((step) => {
      if (prev === 'abs') setStepWatts(step, step.watts);
      else setStepPct(step, step.pct);
    });

    editingCourse.power_mode = next;
    document.querySelectorAll('[data-pmode]').forEach((t) => {
      t.classList.toggle('on', t === tab);
    });
    updatePmodeHint();
    markCustomDirty();
    renderStepRows();          // 值来自另一份字段，重新渲染一次
    scheduleCustomPlan();
  });
});

/* ---- 间歇训练中跳过 / 回退 ---- */

async function sendSkip(delta) {
  try { await api('/api/interval/skip', { delta }); }
  catch (err) { toast(err.message); }
}
els.btnSkip.addEventListener('click', () => sendSkip(1));
els.btnSkipBack.addEventListener('click', () => sendSkip(-1));

els.btnPause.addEventListener('click', async () => {
  const path = state.state === 'paused' ? '/api/resume' : '/api/pause';
  try { await api(path, {}); } catch (err) { toast(err.message); }
});

els.btnStop.addEventListener('click', async () => {
  try { await api('/api/stop', {}); } catch (err) { toast(err.message); }
});

/* 总结面板上的两个按钮 */
els.btnDismiss.addEventListener('click', async () => {
  // 正在看历史报告时，这个按钮是"返回报告列表"，不要动服务端的会话状态
  if (viewingReport) {
    closeReport();
    return;
  }
  try {
    await api('/api/dismiss', {});
    lastSummary = null;
    chartData = [];
    hideBanner();
    loadReports();
  } catch (err) { toast(err.message); }
});

els.btnDeleteReport.addEventListener('click', () => {
  if (!viewingReport) return;
  deleteReport(viewingReport.id, viewingReport.summary
    ? (viewingReport.summary.plan_name || '这份报告') : '这份报告');
});

els.btnClearReports.addEventListener('click', clearReports);

els.btnUseFtp.addEventListener('click', async () => {
  const tr = lastSummary && lastSummary.test_result;
  if (!tr || !tr.valid) return;
  els.ftpInput.value = tr.ftp;
  try {
    await api('/api/settings', { ftp: tr.ftp });
    showBanner('FTP 已更新为 ' + tr.ftp + 'W，后面的间歇课表和自定义课程都会按它折算。', 'info');
    logLine('info', 'FTP 已更新为 ' + tr.ftp + 'W');
    schedulePlanRefresh();
    scheduleCustomPlan();
    scheduleTestPlan();
  } catch (err) {
    toast('保存 FTP 失败：' + err.message);
  }
});

els.btnAgain.addEventListener('click', async () => {
  // 配置存在服务端，原样复现上一次的训练（间歇方案的模板和参数也一并复用）
  chartData = [];
  try {
    await api('/api/repeat', {});
    lastSummary = null;
    viewingReport = null;
    renderedSummaryKey = null;
    hideBanner();
  } catch (err) {
    toast('无法开始：' + err.message);
  }
});

/* 自由骑行中微调阻力档位 */
async function nudgeResistance(delta) {
  const base = (state && state.free_resistance != null) ? state.free_resistance : 90;
  const next = Math.max(0, Math.min(255, Math.round(base + delta)));
  try { await api('/api/free-resistance', { raw: next }); }
  catch (err) { toast(err.message); }
}
els.btnResDown.addEventListener('click', () => nudgeResistance(-6));
els.btnResUp.addEventListener('click', () => nudgeResistance(6));

document.querySelectorAll('[data-delta]').forEach((btn) => {
  btn.addEventListener('click', async () => {
    // 自由骑行模式下没有"目标功率"可控，± 改为调阻力档位
    if (state && state.free_resistance != null) {
      await nudgeResistance(Number(btn.dataset.delta) / 2);
      return;
    }
    // 基准取服务端当前的目标功率，而不是输入框里的值。间歇训练时输入框不会跟着
    // 当前小节同步（免得污染恒定功率设置），拿它当基准会把目标跳到不相干的值。
    const base = (state && state.target_power != null)
      ? state.target_power : Number(els.targetPower.value);
    const next = Math.max(1, Math.min(2000, Math.round(base) + Number(btn.dataset.delta)));
    // 先把输入框更新掉，否则连点两次会基于同一个旧值计算
    els.targetPower.value = next;
    els.powerSlider.value = Math.min(500, next);
    try { await api('/api/target', { target_power: next }); } catch (err) { toast(err.message); }
  });
});

document.querySelectorAll('.chip[data-power]').forEach((btn) => {
  btn.addEventListener('click', () => {
    els.targetPower.value = btn.dataset.power;
    els.powerSlider.value = Math.min(500, Number(btn.dataset.power));
    if (state.state === 'running' || state.state === 'paused') {
      api('/api/target', { target_power: Number(btn.dataset.power) }).catch((err) => toast(err.message));
    }
  });
});

document.querySelectorAll('.chip[data-min]').forEach((btn) => {
  btn.addEventListener('click', () => { els.duration.value = btn.dataset.min; });
});

els.powerSlider.addEventListener('input', () => {
  els.targetPower.value = els.powerSlider.value;
});
els.powerSlider.addEventListener('change', () => {
  if (state.state === 'running' || state.state === 'paused') {
    api('/api/target', { target_power: Number(els.powerSlider.value) })
      .catch((err) => toast(err.message));
  }
});
els.targetPower.addEventListener('change', () => {
  if (state.state === 'running' || state.state === 'paused') {
    api('/api/target', { target_power: Number(els.targetPower.value) })
      .catch((err) => toast(err.message));
  }
});

els.ergMode.addEventListener('change', () => {
  const hints = {
    auto: '自动模式会优先用原生 ERG；只有在做一次目标功率阶跃探测、实证骑行台确实不响应之后，才会改用闭环阻力控制。',
    ftms: '由骑行台固件自己把功率稳在目标值，响应最快、手感最像 Zwift。选它就不会被自动换掉。',
    resistance: '本程序读回实际功率后不断修正阻力档位。适合只支持设阻力的骑行台。',
  };
  els.modeHint.textContent = hints[els.ergMode.value] || '';
});

els.btnClearLog.addEventListener('click', () => { els.log.innerHTML = ''; });
els.bannerClose.addEventListener('click', hideBanner);

window.addEventListener('resize', () => {
  // 所有画布都是 width:100%，位图尺寸只在绘制时才重算，所以尺寸变化后必须重画，
  // 否则会被拉伸变形。以前只重画了两块实时曲线，三块课表预览会一直糊着。
  drawChart();
  if (lastSummary && !els.summaryPanel.classList.contains('hidden')) {
    const points = (lastSummary.trace || []).map((d) => ({
      t: d.t * 1000, p: d.p, target: d.target, hr: d.hr,
    }));
    drawPowerChart(els.sumChart, points, { hrMax: (lastSummary.heart_rate || {}).max_bpm });
  }
  if (currentPlan) renderPlanPreview();
  if (editingCourse) renderCustomPlanPreview();
  // FTP 测试的课表预览也是一块 canvas，漏了它就一直是旧的位图尺寸被拉伸
  if (currentTestPlan) renderTestPlanPreview();
});

function renderTestPlanPreview() {
  if (!currentTestPlan) return;
  renderPlanInto(currentTestPlan, els.testPlanStats, els.testPlanList,
                 els.testPlanChart, false);
}

/* ------------------------------------------------------------------ *
 * 启动
 * ------------------------------------------------------------------ */

els.btnViewDevices.addEventListener('click', () => switchView('devices'));
els.btnViewTraining.addEventListener('click', () => switchView('training'));

(async function boot() {
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    const data = await res.json();
    // 同上：历史日志按服务端给的旧→新顺序喂给 logLine 即可
    if (data.events) data.events.forEach((e) => logLine(e.kind, e.message));
    render(data);
  } catch (e) { /* WebSocket 和轮询会补上 */ }
  // 恢复上次停留的那一页（刷新页面不至于跳回设备页）
  let savedView = 'devices';
  try { savedView = window.localStorage.getItem('ibike.view') || 'devices'; } catch (e) { /* 无所谓 */ }
  applyView(savedView);
  connectWS();
  setInterval(pollState, 3000);
  drawChart();
  loadSettings().then(loadTemplates);
  loadReports();
})();

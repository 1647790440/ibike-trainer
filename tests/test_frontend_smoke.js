#!/usr/bin/env node
/**
 * 前端冒烟测试：在 Node 里用一层 DOM 桩把 app.js 真正跑起来。
 *
 * 光做语法检查（node --check）只能发现拼写错误；这个测试会真的加载脚本、
 * 触发模块级的绑定、再用几组"服务端可能推过来的状态"调用 render()，
 * 从而抓出运行时才会暴露的问题（空引用、字段拼错、函数没定义……）。
 *
 *     node tests/test_frontend_smoke.js
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'ibike/web/index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(ROOT, 'ibike/web/app.js'), 'utf8');

const htmlIds = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));

let failures = 0;
function check(cond, label, detail) {
  const mark = cond ? '\x1b[32m✔\x1b[0m' : '\x1b[31m✘\x1b[0m';
  console.log(`  ${mark} ${label}${detail ? '  → ' + detail : ''}`);
  if (!cond) failures += 1;
  return cond;
}

/* ------------------------------------------------------------------ *
 * DOM 桩
 * ------------------------------------------------------------------ */

const ctx = {
  setTransform() {}, clearRect() {}, fillRect() {}, fillText() {},
  beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fill() {},
  save() {}, restore() {}, setLineDash() {}, closePath() {},
  createLinearGradient() { return { addColorStop() {} }; },
  measureText() { return { width: 10 }; },
  set font(v) {}, set fillStyle(v) {}, set strokeStyle(v) {},
  set lineWidth(v) {}, set lineJoin(v) {}, set textAlign(v) {},
  set textBaseline(v) {}, set globalAlpha(v) {},
};

const elCache = new Map();
const elementListeners = [];

/* 每个 canvas 的初始位图尺寸，按 HTML 里的 height 属性和 canvas 默认宽度来 */
const canvasAttrs = {};
for (const m of html.matchAll(/<canvas\s+id="([^"]+)"([^>]*)>/g)) {
  const attrs = m[2] || '';
  const h = /height="(\d+)"/.exec(attrs);
  canvasAttrs[m[1]] = { width: 300, height: h ? Number(h[1]) : 150 };
}
const PARENT_CONTENT_WIDTH = 600;

function makeEl(id) {
  const attrs = canvasAttrs[id];
  const el = {
    id,
    _text: '', _html: '', _className: '', _value: '', _attrs: {},
    style: {},
    dataset: {},
    children: [],
    // 真实 canvas 的位图尺寸（等价于 width/height 属性）
    width: attrs ? attrs.width : 600,
    height: attrs ? attrs.height : 180,
    checked: false,
    disabled: false,
    _classes: new Set(),
    appendChild(child) { this.children.push(child); return child; },
    append(...kids) { this.children.push(...kids); },
    prepend(child) { this.children.unshift(child); },
    remove() {},
    insertBefore() {},
    addEventListener(type, fn) { elementListeners.push({ id, type, fn }); },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    getAttribute(name) { return this._attrs[name]; },
    removeAttribute(name) { delete this._attrs[name]; },
    removeEventListener() {},
    querySelector() { return makeEl('q'); },
    querySelectorAll() { return []; },
    getContext() { return ctx; },
    focus() {}, select() {}, click() {}, scrollIntoView() {},
    getBoundingClientRect() {
      return { width: this.clientWidth, height: this.clientHeight, top: 0, left: 0 };
    },
  };

  /* 关键的忠实模拟：canvas 没有 CSS 尺寸时，它的**布局尺寸由位图尺寸决定**。
     这正是"每画一次图就大一圈"那个 bug 的成因，桩里必须还原出来，
     否则这类 bug 在测试里永远暴露不了。 */
  Object.defineProperty(el, 'clientWidth', {
    get() {
      if (this.style.width) {
        if (this.style.width === '100%') return PARENT_CONTENT_WIDTH;
        return parseFloat(this.style.width) || PARENT_CONTENT_WIDTH;
      }
      return this.width;
    },
  });
  Object.defineProperty(el, 'clientHeight', {
    get() {
      if (this.style.height) return parseFloat(this.style.height) || this.height;
      return this.height;
    },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return this._text; }, set(v) { this._text = v == null ? '' : String(v); },
  });
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) {
      this._html = v == null ? '' : String(v);
      // 粗略模拟：按顶层 <span> 的数量建子节点，这样 lastChild 之类能用
      this.children = [];
      const count = (this._html.match(/<span/g) || []).length;
      for (let i = 0; i < count; i += 1) this.children.push(makeEl('span'));
    },
  });
  Object.defineProperty(el, 'lastChild', {
    get() { return this.children[this.children.length - 1]; },
  });
  Object.defineProperty(el, 'firstChild', {
    get() { return this.children[0]; },
  });

  // classList 用真实实现：这样测试才能断言某块面板是不是 hidden
  el.classList = {
    add(c) { el._classes.add(c); },
    remove(c) { el._classes.delete(c); },
    contains(c) { return el._classes.has(c); },
    toggle(c, force) {
      const on = force === undefined ? !el._classes.has(c) : !!force;
      if (on) el._classes.add(c); else el._classes.delete(c);
      return on;
    },
  };
  Object.defineProperty(el, 'className', {
    get() { return [...el._classes].join(' '); },
    set(v) {
      el._classes.clear();
      String(v == null ? '' : v).split(/\s+/).forEach((c) => { if (c) el._classes.add(c); });
    },
  });
  Object.defineProperty(el, 'value', {
    get() { return this._value; }, set(v) { this._value = v == null ? '' : String(v); },
  });
  return el;
}

function getEl(id) {
  if (!elCache.has(id)) elCache.set(id, makeEl(id));
  return elCache.get(id);
}

const documentStub = {
  getElementById: getEl,
  querySelector() { return makeEl('q'); },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl('new-' + tag); },
  addEventListener() {},
  activeElement: null,
  body: makeEl('body'),
};

/* ------------------------------------------------------------------ *
 * 其他浏览器 API 桩
 * ------------------------------------------------------------------ */

const fetchCalls = [];
function fakeJson(payload) {
  return { ok: true, status: 200, json: async () => payload, text: async () => '' };
}

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    FakeWebSocket.last = this;
    setTimeout(() => { if (this.onopen) this.onopen(); }, 0);
  }
  send() {}
  close() { if (this.onclose) this.onclose(); }
}

const sandbox = {
  console,
  document: documentStub,
  window: {
    devicePixelRatio: 2,
    innerWidth: 1200,
    addEventListener() {},
    confirm() { return true; },
    location: { protocol: 'http:', host: '127.0.0.1:8765' },
    // localStorage 用真桩（不是让它抛异常走 catch）："刷新后停在原来那一页"
    // 这条行为必须真的被验证到
    localStorage: (() => {
      const store = new Map();
      return {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
        _dump: () => Object.fromEntries(store),
      };
    })(),
  },
  location: { protocol: 'http:', host: '127.0.0.1:8765' },
  navigator: { userAgent: 'node' },
  WebSocket: FakeWebSocket,
  fetch: async (url, opts) => {
    fetchCalls.push({ url, opts });
    if (url.indexOf('/api/templates') >= 0) {
      return fakeJson({ ok: true, templates: [
        { id: 'hiit_4x4', name: '4×4 分钟', subtitle: 'Helgerod',
          desc: '说明', params: [
            { key: 'reps', label: '组数', default: 4, min: 2, max: 10, step: 1, unit: '组' },
            { key: 'work_pct', label: '强度', default: 95, min: 85, max: 120, step: 1, unit: '% FTP' },
          ] },
      ] });
    }
    if (url.indexOf('/api/custom?') >= 0) {
      return fakeJson({ ok: true, courses: [
        { id: 'c1', name: '我的课', power_mode: 'abs',
          steps: [{ name: '热身', kind: 'warmup', duration_s: 600, pct: 55, watts: 110 }],
          stats: { step_count: 1, total_s: 600, peak_power: 110 } },
      ] });
    }
    if (url.indexOf('/api/custom/new') >= 0) {
      return fakeJson({ ok: true, course: {
        id: '', name: '我的课程', power_mode: 'pct',
        steps: [{ name: '热身', kind: 'warmup', duration_s: 600, pct: 60, watts: 120 }],
      } });
    }
    if (url.indexOf('/api/plan') >= 0) {
      return fakeJson({ ok: true, ftp: 200, stats: {
        total_s: 2400, work_s: 960, step_count: 3, peak_power: 238,
        avg_power: 179, est_kj: 430,
      }, steps: [
        { name: '热身', kind: 'warmup', duration_s: 600, target_power: 120, pct_ftp: 0.6, zone: 'Z2 耐力' },
        { name: '高强度', kind: 'work', duration_s: 1200, target_power: 238, pct_ftp: 0.95, zone: 'Z4 乳酸阈值' },
        { name: '冷身', kind: 'cooldown', duration_s: 600, target_power: 100, pct_ftp: 0.5, zone: 'Z1 主动恢复' },
      ] });
    }
    if (/\/api\/reports\/[^/]+$/.test(url)) {
      return fakeJson({ ok: true, report: {
        id: 'r1', saved_at: 1700003600,
        summary: {
          reason: '完成目标时长', completed: true, plan_name: '4×4 分钟',
          is_interval: true, trainer_name: 'MOK iBike', erg_mode_label: '原生 ERG',
          target_power: 238, planned_s: 2400, actual_s: 2400, skipped_s: 0,
          started_at: 1700001200, finished_at: 1700003600,
          avg_power: 236, max_power: 260, normalized_power: 240,
          avg_cadence: 88, max_cadence: 96, energy_kj: 560, energy_kcal_est: 582,
          distance_m: 22000, in_zone_pct: 62.0, work_in_zone_pct: 88.5,
          in_zone_tolerance_w: 11.9, mean_abs_deviation_w: 6.1,
          target_deviation_w: -2.0,
          distribution: [{ label: '97–103%', seconds: 1400, pct: 58.3 }],
          intervals: [{ index: 1, name: '高强度', kind: 'work', planned_s: 240,
                        actual_s: 240, settle_s: 5, target_power: 238,
                        avg_power: 236, in_zone_pct: 88, valid_s: 235,
                        in_zone_s: 207, work_position: 1 }],
          trace: [{ t: 0, p: 120, target: 120 }, { t: 1, p: 238, target: 238 }],
        },
      } });
    }
    if (url.indexOf('/api/tests') >= 0) {
      return fakeJson({ ok: true, tests: [
        { id: 'ramp', name: '坡道测试', subtitle: '室内最常用 · 全程 ERG 自动加载',
          desc: '说明文字', erg_mode: 'ftms', self_paced: false,
          result: { kind: 'ramp', multiplier: 0.75 },
          params: [{ key: 'start_w', label: '起始功率', default: 100,
                     min: 60, max: 250, step: 5, unit: 'W' }] },
        { id: 'twenty', name: '20 分钟测试', subtitle: 'Allen-Coggan 标准方案',
          desc: '说明文字', erg_mode: 'free', self_paced: true,
          result: { kind: 'best_segment', multiplier: 0.95 },
          params: [{ key: 'test_min', label: '计时时长', default: 20,
                     min: 8, max: 40, step: 1, unit: '分钟' }] },
        { id: 'eight', name: '8 分钟测试', subtitle: '两次全力',
          desc: '说明文字', erg_mode: 'free', self_paced: true,
          result: { kind: 'best_segment', multiplier: 0.90 },
          params: [{ key: 'test_min', label: '每次时长', default: 8,
                     min: 5, max: 15, step: 1, unit: '分钟' }] },
      ] });
    }
    if (url.indexOf('/api/settings') >= 0) {
      return fakeJson({ ok: true, settings: { ftp: 248, free_resistance: 90,
        hr_max: 190, hr_rest: 55, hr_zone_mode: 'reserve',
        hr_limit_enabled: true, hr_limit_bpm: 165 } });
    }
    if (url.indexOf('/api/scan') >= 0) {
      // 一次扫描返回两份列表：骑行台 + 心率带
      return fakeJson({ ok: true,
        devices: [{ address: 'SIM', name: '模拟骑行台', rssi: -40, has_ftms: true }],
        hr_devices: [
          { address: 'AA:BB:CC:DD:EE:01', name: 'Magene H603', rssi: -52 },
          { address: 'AA:BB:CC:DD:EE:02', name: 'Polar H10', rssi: -70 },
        ] });
    }
    if (url.indexOf('/api/hr/connect') >= 0) {
      return fakeJson({ ok: true, hr: { connected: true, name: 'Magene H603' } });
    }
    if (url.indexOf('/api/hr/disconnect') >= 0) {
      return fakeJson({ ok: true });
    }
    if (url.indexOf('/api/reports') >= 0) {
      return fakeJson({ ok: true, reports: [
        { id: 'r1', saved_at: 1700003600, plan_name: '4×4 分钟', completed: true,
          finished_at: 1700003600, actual_s: 2400, avg_power: 236,
          target_power: 238, normalized_power: 240, energy_kj: 560 },
        { id: 'r2', saved_at: 1699900000, plan_name: '恒定功率', completed: false,
          finished_at: 1699900000, actual_s: 600, avg_power: 100,
          target_power: 100, normalized_power: null, energy_kj: 16 },
      ] });
    }
    if (url.indexOf('/api/state') >= 0) {
      return fakeJson({ state: 'idle', trainer: {} });
    }
    return fakeJson({ ok: true });
  },
  setInterval() { return 0; },
  setTimeout(fn, ms) { return 0; },
  clearTimeout() {},
  requestAnimationFrame() { return 0; },
  JSON,
  Math,
  Date,
  Object,
  Array,
  Number,
  String,
  Boolean,
  Promise,
  Error,
  isNaN,
  parseFloat,
  parseInt,
};
sandbox.window.document = documentStub;
sandbox.globalThis = sandbox;

/* ------------------------------------------------------------------ *
 * 执行
 * ------------------------------------------------------------------ */

console.log('='.repeat(66));
console.log('前端冒烟测试（Node + DOM 桩）');
console.log('='.repeat(66));

console.log('\n[1] 加载 app.js');
let loadError = null;
const context = vm.createContext(sandbox);
try {
  vm.runInContext(appJs, context, { filename: 'app.js' });
} catch (err) {
  loadError = err;
}
const loaded = check(!loadError, '脚本加载与模块级绑定没有抛异常',
                     loadError ? String(loadError && loadError.stack || loadError).split('\n')[0] : '');
if (!loaded) {
  // 加载失败就没必要继续了
  console.log('\n加载阶段就出错，后续检查跳过');
  process.exit(1);
}

check(typeof context.render === 'function', 'render 是全局可调用的函数');
check(typeof context.renderSummary === 'function', 'renderSummary 是全局可调用的函数');
check(typeof context.drawPlanChart === 'function', 'drawPlanChart 是全局可调用的函数');
check(fetchCalls.length > 0, '启动时发起了初始请求',
      fetchCalls.map((c) => c.url).join(', ').slice(0, 80));

/* ------------------------------------------------------------------ *
 * 渲染各种服务端状态
 * ------------------------------------------------------------------ */

function tryRender(label, stateObj) {
  try {
    context.render(stateObj);
    return check(true, label);
  } catch (err) {
    return check(false, label, String(err && err.message || err));
  }
}

console.log('\n[2] 渲染各种服务端状态');

tryRender('未连接的空状态', { state: 'idle', trainer: {} });

tryRender('恒定功率运行中', {
  state: 'running', trainer: { connected: true, kind: 'ble', name: 'MOK iBike', capabilities: {} },
  target_power: 100, duration_s: 3600, elapsed_s: 600, remaining_s: 3000, progress: 0.17,
  power: 101, power_avg: 99.8, power_max: 120, normalized_power: 102,
  cadence: 87, speed: 31.2, heart_rate: 140, distance_m: 5200, energy_kj: 100,
  resistance_raw: 45, is_interval: false, plan: [], summary: null,
});

tryRender('间歇训练运行中', {
  state: 'running', trainer: { connected: true, kind: 'ble', name: 'MOK iBike', capabilities: {} },
  target_power: 238, duration_s: 2400, elapsed_s: 900, remaining_s: 1500, progress: 0.375,
  power: 240, power_avg: 210, power_max: 260, cadence: 88, distance_m: 8000, energy_kj: 200,
  is_interval: true, plan_name: '4×4 分钟', step_index: 1, step_count: 9,
  step_name: '高强度', step_kind: 'work', step_zone: 'Z4 乳酸阈值',
  step_duration_s: 240, step_elapsed_s: 100, step_remaining_s: 140,
  work_index: 2, work_count: 4, next_step_name: '恢复', next_step_power: 138,
  plan: [
    { name: '热身', kind: 'warmup', duration_s: 600, target_power: 120 },
    { name: '高强度', kind: 'work', duration_s: 240, target_power: 238 },
    { name: '恢复', kind: 'recovery', duration_s: 180, target_power: 138 },
  ],
  summary: null,
});

tryRender('暂停中', {
  state: 'paused', trainer: { connected: true, kind: 'ble', name: 'x', capabilities: {} },
  target_power: 100, power: 0, is_interval: false, plan: [], summary: null,
});

tryRender('出错状态', {
  state: 'error', trainer: { connected: false, capabilities: {} },
  error: '与骑行台的连接断开了', is_interval: false, plan: [], summary: null,
});

/* 总结面板：恒定功率 */
const constantSummary = {
  reason: '完成目标时长', completed: true, plan_name: '恒定功率', is_interval: false,
  trainer_name: 'MOK iBike', erg_mode_label: '原生 ERG', requested_erg_mode: 'auto',
  target_power: 100, planned_s: 3600, actual_s: 3600, skipped_s: 0,
  started_at: 1700000000, finished_at: 1700003600,
  avg_power: 100.2, max_power: 130, normalized_power: 103, avg_cadence: 88, max_cadence: 95,
  energy_kj: 360, energy_kcal_est: 374, distance_m: 30000,
  in_zone_pct: 92.5, work_in_zone_pct: null, in_zone_tolerance_w: 5,
  mean_abs_deviation_w: 3.2, target_deviation_w: 0.2, intervals: [],
  distribution: [
    { label: '低于 90%', seconds: 100, pct: 2.8 },
    { label: '90–97%', seconds: 200, pct: 5.6 },
    { label: '97–103%', seconds: 3200, pct: 88.8 },
    { label: '103–110%', seconds: 80, pct: 2.2 },
    { label: '高于 110%', seconds: 20, pct: 0.6 },
  ],
  trace: [{ t: 0, p: 100, target: 100 }, { t: 1, p: 105, target: 100 }],
};
tryRender('总结（恒定功率）', {
  state: 'finished', trainer: { connected: true, kind: 'ble', name: 'MOK iBike', capabilities: {} },
  summary: constantSummary, is_interval: false, plan: [],
});

/* 总结面板：间歇训练，含不合格与缺数据的分段 */
const intervalSummary = Object.assign({}, constantSummary, {
  plan_name: '4×4 分钟', is_interval: true, in_zone_pct: 40.0, work_in_zone_pct: 86.4,
  skipped_s: 65,
  intervals: [
    { index: 0, name: '热身', kind: 'warmup', planned_s: 600, actual_s: 600, settle_s: 5,
      target_power: 120, avg_power: 121, in_zone_pct: null, valid_s: 595, in_zone_s: 500 },
    { index: 1, name: '高强度', kind: 'work', planned_s: 240, actual_s: 240, settle_s: 5,
      target_power: 238, avg_power: 236, in_zone_pct: 88.0, valid_s: 235, in_zone_s: 207,
      work_position: 1 },
    { index: 2, name: '恢复', kind: 'recovery', planned_s: 180, actual_s: 115, settle_s: 5,
      target_power: 138, avg_power: null, in_zone_pct: null, valid_s: 0, in_zone_s: 0 },
  ],
});
tryRender('总结（间歇，含数据不足的分段）', {
  state: 'finished', trainer: { connected: true, kind: 'ble', name: 'MOK iBike', capabilities: {} },
  summary: intervalSummary, is_interval: true, plan: [],
});

/* ------------------------------------------------------------------ *
 * 课表绘制
 * ------------------------------------------------------------------ */

console.log('\n[3] 课表柱状图');

const planSteps = [
  { name: '热身', kind: 'warmup', duration_s: 600, target_power: 120 },
  { name: '高强度', kind: 'work', duration_s: 240, target_power: 238 },
  { name: '恢复', kind: 'recovery', duration_s: 180, target_power: 138 },
];
try {
  context.drawPlanChart(getEl('planChart'), planSteps, { currentIndex: 1, progress: 0.5 });
  check(true, '带进度的课表绘制');
} catch (err) {
  check(false, '带进度的课表绘制', String(err && err.message || err));
}
try {
  context.drawPlanChart(getEl('planChart'), [], {});
  check(true, '空课表不会崩');
} catch (err) {
  check(false, '空课表不会崩', String(err && err.message || err));
}

/* ------------------------------------------------------------------ *
 * 画布尺寸（回归：曾经因为漏写 CSS 规则导致越画越大）
 * ------------------------------------------------------------------ */

console.log('\n[4] 画布尺寸不会被反复绘制撑大');

/* 用一个"没有 CSS 尺寸"的 canvas 模拟漏写规则的情况。桩里 clientWidth 会跟着
   位图尺寸走，所以只要绘制函数先把 CSS 尺寸钉死，尺寸就应当稳定下来。 */
const bare = makeEl('bareCanvas');
const bareSizes = [];
for (let i = 0; i < 6; i++) {
  context.drawPlanChart(bare, planSteps, {});
  bareSizes.push(bare.width + 'x' + bare.height);
}
check(new Set(bareSizes).size === 1,
      '反复绘制 6 次，画布尺寸保持不变', bareSizes.join(' → '));
check(bare.style.width === '100%' && !!bare.style.height,
      '绘制时把 CSS 尺寸钉死了（这是防膨胀的关键）',
      'style.width=' + bare.style.width + ' style.height=' + bare.style.height);

const barePower = makeEl('barePowerCanvas');
const powerSizes = [];
for (let i = 0; i < 6; i++) {
  context.drawPowerChart(barePower, [{ t: 0, p: 100, target: 100 }, { t: 1000, p: 110, target: 100 }], {});
  powerSizes.push(barePower.width + 'x' + barePower.height);
}
check(new Set(powerSizes).size === 1,
      '功率曲线同样不会越画越大', powerSizes.join(' → '));

/* 每个 canvas 都必须有 CSS 高度规则，且与 JS 里的 CANVAS_HEIGHT 一致。
   这是最初那个 bug 的直接防线。 */
const jsHeights = {};
const heightBlock = /const CANVAS_HEIGHT = \{([\s\S]*?)\};/.exec(appJs);
if (heightBlock) {
  for (const m of heightBlock[1].matchAll(/(\w+)\s*:\s*(\d+)/g)) {
    jsHeights[m[1]] = Number(m[2]);
  }
}
const cssHeights = {};
const cssText = fs.readFileSync(path.join(ROOT, 'ibike/web/style.css'), 'utf8');
for (const m of cssText.matchAll(/canvas#(\w+)\s*\{([^}]*)\}/g)) {
  const h = /height:\s*(\d+)px/.exec(m[2]);
  cssHeights[m[1]] = h ? Number(h[1]) : null;
}

const canvasIds = Object.keys(canvasAttrs);
check(canvasIds.length >= 5, 'HTML 里的 canvas 都识别到了', canvasIds.join(', '));
canvasIds.forEach((id) => {
  check(jsHeights[id] != null, `CANVAS_HEIGHT 里有 ${id}`);
  check(cssHeights[id] != null, `CSS 里为 #${id} 写了高度规则`,
        cssHeights[id] == null ? '缺失——位图尺寸会撑大布局，导致越画越大' : '');
  if (jsHeights[id] != null && cssHeights[id] != null) {
    check(jsHeights[id] === cssHeights[id],
          `#${id} 的 CSS 高度与 JS 一致`, `${cssHeights[id]} vs ${jsHeights[id]}`);
  }
});

/* ------------------------------------------------------------------ *
 * 移动端布局
 * ------------------------------------------------------------------ */

console.log('\n[5] 移动端小节行的网格定义自洽');

const mobileQuery = /@media \(max-width: 700px\)\s*\{([\s\S]*?)\n\}/.exec(cssText);
if (check(!!mobileQuery, '找到手机端媒体查询')) {
  const block = mobileQuery[1];
  const cols = /grid-template-columns:\s*([^;]+);/.exec(block);
  const areas = /grid-template-areas:\s*([\s\S]*?);/.exec(block);

  const colCount = cols ? cols[1].trim().split(/\s+/).length : 0;
  const rows = areas
    ? [...areas[1].matchAll(/"([^"]+)"/g)].map((m) => m[1].trim().split(/\s+/))
    : [];

  check(colCount > 0 && rows.length > 0, '解析到网格的列与行',
        colCount + ' 列 / ' + rows.length + ' 行');
  check(rows.every((r) => r.length === colCount),
        '每一行的格子数与列数一致',
        rows.map((r) => r.join(' ')).join(' | '));

  /* 这条是那个真实 bug 的直接防线：命名区域如果只落在索引列（很窄，只放序号），
     里面的输入框会被压没。曾经写成 "dur pow pow"，dur 就只占了第 1 列。
     所以判定条件是"从第 1 列开始、且没有跨到第 2 列"。 */
  const indexColumn = cols ? cols[1].trim().split(/\s+/)[0] : '';
  const narrowPx = parseFloat(indexColumn) || 0;
  const trapped = (narrowPx > 0 && narrowPx < 60)
    ? rows.filter((r) => r[0] === 'dur' && r[1] !== 'dur')
    : [];
  check(trapped.length === 0,
        '「时长」跨出了最窄的序号列（原来就是这个 bug）',
        trapped.length ? '第 1 列只有 ' + narrowPx + 'px，dur 只占了它' : 'dur 跨了两列');

  const usedAreas = new Set(rows.flat());
  ['idx', 'kind', 'name', 'dur', 'pow', 'btns'].forEach((name) => {
    check(usedAreas.has(name), '网格里安排了 ' + name);
  });
}

/* ------------------------------------------------------------------ *
 * 交互回调
 * ------------------------------------------------------------------ */

console.log('\n[6] 关键交互回调已绑定');
const boundIds = new Set(elementListeners.map((l) => l.id));
['btnStart', 'btnScan', 'btnSim', 'btnStop', 'btnPause', 'btnAgain', 'btnDismiss',
 'btnNewCourse', 'btnAddStep', 'btnSaveCourse', 'btnDeleteCourse',
 'btnSkip', 'btnSkipBack', 'ftpInput', 'courseName', 'bannerClose',
].forEach((id) => {
  check(boundIds.has(id), (htmlIds.has(id) ? '' : '（HTML 里没有这个 id！）') + id + ' 绑定了事件');
});

/* ------------------------------------------------------------------ *
 * 训练报告
 * ------------------------------------------------------------------ */

(async function reportSection() {
  console.log('\n[7] 训练报告列表与查看');

  const idle = {
    state: 'idle', trainer: { connected: true, kind: 'ble', capabilities: {} },
    summary: null, is_interval: false, plan: [],
  };
  context.render(idle);
  check(!getEl('reportsPanel').classList.contains('hidden'),
        '空闲时显示报告面板');
  check(getEl('summaryPanel').classList.contains('hidden'),
        '空闲时总结面板是收起的');

  await context.loadReports();
  const list = getEl('reportsList');
  check(list.children.length === 2, '报告列表渲染出两条记录',
        String(list.children.length) + ' 条');

  // 打开其中一份
  await context.openReport('r1');
  check(!getEl('summaryPanel').classList.contains('hidden'),
        '打开报告后总结面板显示出来');
  check(getEl('reportsPanel').classList.contains('hidden'),
        '打开报告后列表收起，不跟报告抢地方');
  check(!getEl('btnDeleteReport').classList.contains('hidden'),
        '查看历史报告时出现「删除这份报告」');
  check(getEl('btnDismiss').textContent === '返回报告列表',
        '关闭按钮变成「返回报告列表」',
        getEl('btnDismiss').textContent);

  // 返回列表
  context.closeReport();
  check(getEl('summaryPanel').classList.contains('hidden'),
        '返回后总结面板收起');
  check(!getEl('reportsPanel').classList.contains('hidden'),
        '返回后重新显示报告列表');

  // 看历史报告时不应该显示"删除"按钮
  context.render(Object.assign({}, idle, {
    state: 'finished',
    summary: {
      reason: '完成目标时长', completed: true, plan_name: '恒定功率',
      is_interval: false, target_power: 100, planned_s: 600, actual_s: 600,
      skipped_s: 0, started_at: 1700000000, finished_at: 1700000600,
      avg_power: 100, max_power: 110, avg_cadence: 88, energy_kj: 60,
      in_zone_pct: 95, in_zone_tolerance_w: 5, mean_abs_deviation_w: 2,
      intervals: [], distribution: [], trace: [],
    },
  }));
  check(getEl('btnDeleteReport').classList.contains('hidden'),
        '刚结束的训练不显示「删除这份报告」');
  check(getEl('btnDismiss').textContent === '关闭',
        '刚结束的训练按钮是「关闭」', getEl('btnDismiss').textContent);

  /* ---- 切标签页不能重置已选的测试方案 ---- */
  console.log('\n[8b] 切走再切回「FTP 测试」不该丢掉已选的方案和参数');
  await context.loadFtpTests();
  context.selectTest('eight');
  vm.runInContext('testParamValues.test_min = 12;', context);
  context.applySetupMode('interval');
  context.applySetupMode('test');          // 相当于重新点一次这个标签页
  // applySetupMode 里是 loadFtpTests() 不带 await 的，这里必须把微任务跑完，
  // 否则断言跑在重置之前——那样这条用例即使 bug 回来了也照样通过
  await new Promise((r) => setImmediate(r));
  check(vm.runInContext('selectedTestId', context) === 'eight',
        '仍选中「8 分钟测试」（修复前会被重置成第一个方案）',
        String(vm.runInContext('selectedTestId', context)));
  check(vm.runInContext('testParamValues.test_min', context) === 12,
        '填过的参数还在',
        String(vm.runInContext('testParamValues.test_min', context)));
  // 但服务端真的没有这个方案时（换了版本/被删掉），仍然要退回第一个
  vm.runInContext('selectedTestId = "no-such-test";', context);
  await context.loadFtpTests();
  check(vm.runInContext('selectedTestId', context) === 'ramp',
        '已选方案不存在时退回第一个',
        String(vm.runInContext('selectedTestId', context)));

  /* ---- 断开之后不能还挂着「已连接」的提示 ---- */
  console.log('\n[8c] 断开连接后清掉「已连接」的提示和横幅');
  context.render({ state: 'idle', trainer: { connected: true, kind: 'ble', capabilities: {} },
                   plan: [], is_interval: false, is_test: false, summary: null });
  vm.runInContext('els.scanHint.textContent = "已连接。设定目标功率和时长，然后开始。";'
    + ' showBanner("已连接骑行台，可以设定目标并开始训练了。", "info");', context);
  const disc = elementListeners.find((l) => l.id === 'btnDisconnect' && l.type === 'click');
  if (check(!!disc, '找到了「断开」按钮的处理器')) {
    await disc.fn();
    check(getEl('scanHint').textContent.indexOf('已连接') < 0,
          '提示不再声称已连接', getEl('scanHint').textContent);
    // 注意用 className 这个 getter，不是内部的 _className —— 桩里 className
    // 是走 _classes 集合的，读 _className 永远是空串（那样断言必然失败/必然通过）
    check(getEl('banner').className.indexOf('hidden') >= 0,
          '绿色「已连接」横幅已收起', getEl('banner').className);
  }

  /* ---- FTP 测试 ---- */
  console.log('\n[8] FTP 测试界面');

  await context.loadFtpTests();
  check(getEl('testList').children.length === 3, '三个测试方案渲染成卡片',
        String(getEl('testList').children.length) + ' 个');

  // 坡道测试进行中
  context.render({
    state: 'running', trainer: { connected: true, kind: 'ble', capabilities: {} },
    target_power: 260, power: 258, cadence: 88, power_avg: 255, power_max: 300,
    distance_m: 4000, energy_kj: 90, duration_s: 2000, elapsed_s: 700,
    remaining_s: 1300, progress: 0.35,
    is_interval: true, is_test: true, plan_name: '坡道测试',
    step_index: 8, step_count: 35, step_name: '第 9 级 260W', step_kind: 'test',
    step_duration_s: 60, step_elapsed_s: 20, step_remaining_s: 40,
    step_zone: null, work_index: null, work_count: 0,
    test_level: 9, test_level_count: 25, test_self_paced: false,
    free_resistance: null, test_multiplier: 0.75,
    live_test: { kind: 'projected_ftp', label: '推算 FTP', value: 194, source: 259 },
    plan: [
      { name: '热身', kind: 'warmup', duration_s: 300, target_power: 110 },
      { name: '第 9 级 260W', kind: 'test', duration_s: 60, target_power: 260 },
    ],
    summary: null,
  });
  check(!getEl('testBar').classList.contains('hidden'), '测试中显示测试条');
  check(getEl('intervalBar').classList.contains('hidden'),
        '测试时不套用间歇训练的界面（虽然课表段数也很多）');
  check(getEl('testLive').textContent.indexOf('推算 FTP 194W') >= 0,
        '实时显示推算 FTP', getEl('testLive').textContent);
  check(getEl('testLevel').textContent.indexOf('第 9') >= 0, '显示当前级数',
        getEl('testLevel').textContent);
  check(getEl('testFreeCtl').classList.contains('hidden'),
        'ERG 模式下不显示阻力微调');

  // 自由骑行的测试：显示阻力微调
  context.render({
    state: 'running', trainer: { connected: true, kind: 'ble', capabilities: {} },
    target_power: 240, power: 245, cadence: 90, power_avg: 243, power_max: 280,
    distance_m: 9000, energy_kj: 200, duration_s: 3000, elapsed_s: 1500,
    remaining_s: 1500, progress: 0.5,
    is_interval: true, is_test: true, plan_name: '20 分钟测试',
    step_index: 5, step_count: 11, step_name: '20 分钟计时', step_kind: 'test',
    step_duration_s: 1200, step_elapsed_s: 600, step_remaining_s: 600,
    test_level: 1, test_level_count: 1, test_self_paced: true,
    free_resistance: 90, test_multiplier: 0.95,
    live_test: { kind: 'segment_avg', label: '本段实时平均', value: 243,
                 projected_ftp: 231 },
    plan: [{ name: '20 分钟计时', kind: 'test', duration_s: 1200, target_power: 240 }],
    summary: null,
  });
  check(!getEl('testFreeCtl').classList.contains('hidden'),
        '自由骑行时显示阻力微调（骑行台上只有阻力可调）');

  // 总结里的测试结果
  context.render({
    state: 'finished', trainer: { connected: true, kind: 'ble', capabilities: {} },
    is_interval: false, is_test: true, plan: [],
    summary: {
      reason: '力竭，测试结束', completed: true, plan_name: '坡道测试',
      is_interval: false, is_test: true, target_power: 100,
      planned_s: 1500, actual_s: 1320, skipped_s: 0,
      started_at: 1700000000, finished_at: 1700001320,
      avg_power: 210, max_power: 380, avg_cadence: 88, energy_kj: 270,
      in_zone_pct: 40, in_zone_tolerance_w: 5, mean_abs_deviation_w: 60,
      intervals: [], distribution: [], trace: [],
      test_result: {
        protocol: '坡道测试', test_id: 'ramp', result_kind: 'ramp',
        source_label: '最好 1 分钟功率', source_power: 248.0,
        multiplier: 0.75, ftp: 186, valid: true,
        detail: '踩到第 11 级 / 共 25 级就力竭了', note: '',
      },
    },
  });
  check(!getEl('sumTestSection').classList.contains('hidden'),
        '总结里显示 FTP 测试结果');
  check(getEl('trFtp').textContent === '186', '显示算出来的 FTP',
        getEl('trFtp').textContent);
  check(getEl('trSource').textContent.indexOf('248') >= 0, '显示计算依据',
        getEl('trSource').textContent);
  check(getEl('trFormula').textContent.indexOf('75%') >= 0, '显示折算系数',
        getEl('trFormula').textContent);
  check(!getEl('btnUseFtp').classList.contains('hidden'), '结果有效时给出「用这个 FTP」');

  // 无效结果不该给出"用这个"
  const invalidSummary = JSON.parse(JSON.stringify(
    getEl('sumTestSection') ? {} : {}));
  context.render({
    state: 'finished', trainer: { connected: true, kind: 'ble', capabilities: {} },
    is_interval: false, is_test: true, plan: [],
    summary: Object.assign({}, {
      reason: '完成目标时长', completed: false, plan_name: '坡道测试',
      is_interval: false, is_test: true, target_power: 100,
      planned_s: 300, actual_s: 300, skipped_s: 0,
      started_at: 1700000000, finished_at: 1700000300,
      avg_power: 118, max_power: 140, avg_cadence: 88, energy_kj: 35,
      in_zone_pct: 50, in_zone_tolerance_w: 5, mean_abs_deviation_w: 20,
      intervals: [], distribution: [], trace: [],
      test_result: {
        protocol: '坡道测试', test_id: 'ramp', result_kind: 'ramp',
        source_label: '最好 1 分钟功率', source_power: 118.1,
        multiplier: 0.75, ftp: 89, valid: false,
        detail: '跑完了全部 3 级还没力竭——坡道太短。', note: '',
      },
    }),
  });
  check(getEl('btnUseFtp').classList.contains('hidden'),
        '结果无效时不给出「用这个 FTP」');
  check(getEl('trInvalid').textContent.indexOf('坡道太短') >= 0,
        '无效时说明原因', getEl('trInvalid').textContent);

  /* ---- 控功率方式切换记录 ---- */
  console.log('\n[9] 控功率方式切换记录');

  const baseSum = {
    reason: '完成目标时长', completed: true, plan_name: '恒定功率',
    is_interval: false, is_test: false, target_power: 130,
    planned_s: 3600, actual_s: 3600, skipped_s: 0,
    started_at: 1700000000, finished_at: 1700003600,
    avg_power: 127, max_power: 160, avg_cadence: 85, energy_kj: 460,
    in_zone_pct: 80, in_zone_tolerance_w: 6.5, mean_abs_deviation_w: 4,
    intervals: [], distribution: [], trace: [],
  };
  context.render({
    state: 'finished', trainer: { connected: true, kind: 'ble', capabilities: {} },
    is_interval: false, is_test: false, plan: [],
    summary: Object.assign({}, baseSum, { mode_changes: [] }),
  });
  check(getEl('sumModeSection').classList.contains('hidden'),
        '没有切换时，不显示切换记录这一段');

  context.render({
    state: 'finished', trainer: { connected: true, kind: 'ble', capabilities: {} },
    is_interval: false, is_test: false, plan: [],
    summary: Object.assign({}, baseSum, {
      // 换一个 finished_at：renderSummary 用签名避免重复渲染（总结生成后就不变了），
      // 两次用同样的签名会被判成"没变化"而跳过
      finished_at: 1700009999,
      mode_changes: [
        { t: 911.0, kind: 'probe', from: 'ftms', to: 'ftms',
          reason: '怀疑固件没在跟目标功率，做一次阶跃探测：目标 130W → 105W' },
        { t: 931.0, from: 'ftms', to: 'resistance', reason: '自动降级：目标功率阶跃探测无响应' },
        { t: 4000.0, from: 'resistance', to: 'ftms', reason: '重新用回原生 ERG' },
      ],
    }),
  });
  check(!getEl('sumModeSection').classList.contains('hidden'),
        '有切换时显示切换记录');
  check(getEl('sumModeChanges').children.length === 3, '三条记录都渲染出来了',
        String(getEl('sumModeChanges').children.length) + ' 条');
  // 行内结构是 [时间, 名称, 说明]，名称那格才是模式名/探测标签
  const probeRow = getEl('sumModeChanges').children[0].children;
  check(probeRow[1].textContent === '目标功率探测',
        '阶跃探测单独成一行，不会被渲染成 undefined → undefined', probeRow[1].textContent);
  check(probeRow[2].textContent.includes('阶跃探测'), '探测行也带上了原因',
        probeRow[2].textContent.slice(0, 40));

  /* ---- 心率带 ---- */
  console.log('\n[10] 心率带界面');

  context.render({
    state: 'idle', trainer: { connected: true, kind: 'ble', capabilities: {} },
    plan: [], is_interval: false, is_test: false, summary: null,
    hr_client: { connected: true, name: 'Magene H603', battery: 88,
                 heart_rate: 132, rr_seen: true, stale: false,
                 saved: { address: '', name: '' } },
  });
  check(getEl('hrPill').textContent === '已连接', '心率带状态显示已连接',
        getEl('hrPill').textContent);
  check(getEl('hrName').textContent === 'Magene H603', '显示心率带名字',
        getEl('hrName').textContent);
  check(getEl('hrBattery').textContent === '88%', '显示电量',
        getEl('hrBattery').textContent);
  check(!getEl('btnHrDisconnect').classList.contains('hidden'),
        '已连接时显示「断开」');
  check(getEl('hrHint').textContent.indexOf('RR') >= 0,
        '告诉用户这根带子支不支持 RR 间期', getEl('hrHint').textContent);

  // 一次扫描同时填两份列表（心率带不再单独扫描）
  check(!elementListeners.some((l) => l.id === 'btnHrScan'),
        '界面上不再有单独的「扫描心率带」按钮');
  const scanHandler = elementListeners.find((l) => l.id === 'btnScan' && l.type === 'click');
  if (check(!!scanHandler, '找到了「扫描蓝牙设备」的处理器')) {
    await scanHandler.fn();
    check(getEl('hrList').children.length === 2, '一次扫描就把心率带列出来了',
          String(getEl('hrList').children.length) + ' 条');
    check(getEl('scanHint').textContent.indexOf('心率带 2 根') >= 0,
          '提示里同时报了骑行台和心率带的数量',
          getEl('scanHint').textContent);
  }

  /* ---- 设备页拆成三块 ---- */
  console.log('\n[10b] 设备页三块：扫描/连接、骑行台状态、心率带状态');

  // 扫出来的设备列表要点得动；这里只验证结构上分了两组
  const html = require('fs').readFileSync(
    require('path').join(__dirname, '..', 'ibike', 'web', 'index.html'), 'utf8');
  check(html.indexOf('<title>iBike 智能骑行控制台</title>') >= 0,
        '页面标题是「iBike 智能骑行控制台」');
  check(html.indexOf('智能骑行台控制台') < 0, '标题里不再出现「骑行台控制台」');
  // 标题行右侧那枚只显示骑行台的连接标识已删掉（心率带状态它显示不了，容易误导）
  check(html.indexOf('id="connPill"') < 0, '标题行不再放过时的连接标识');
  check(html.indexOf('id="connName"') < 0, '标题行也不再放设备名');
  ['1. 扫描与连接', '2. 骑行台', '3. 心率带'].forEach((h) => {
    check(html.indexOf('<h2>' + h + '</h2>') >= 0, '设备页有「' + h + '」这一块');
  });

  // 三块必须在 devicesView **里面**：放在外面的话切到训练页它们照样显示
  // （上次就是这么错的——骑行台/心率带两块在训练页也挂着）。
  function sectionSpan(source, id) {
    const anchor = source.indexOf('id="' + id + '"');
    if (anchor < 0) return null;
    const start = source.lastIndexOf('<section', anchor);
    const re = /<section\b|\/section>/g;
    re.lastIndex = start;
    let depth = 0, m;
    while ((m = re.exec(source))) {
      if (m[0] === '<section') depth += 1;
      else {
        depth -= 1;
        if (depth === 0) return source.slice(start, re.lastIndex);
      }
    }
    return null;
  }
  const span = sectionSpan(html, 'devicesView');
  if (check(!!span, '找得到 devicesView 的范围',
            span ? ('长度 ' + span.length) : '没找到')) {
    ['1. 扫描与连接', '2. 骑行台', '3. 心率带'].forEach((h) => {
      check(span.indexOf('<h2>' + h + '</h2>') >= 0,
            '「' + h + '」在 devicesView 里面（切到训练页会被一起藏起来）');
    });
  }
  check(html.indexOf('id="btnHrConnect"') < 0,
        '心率带块里不再放多余的「连接」按钮（连接入口统一在扫描结果里）');

  // 骑行台块：连着的时候显示实时数据，断开时是 --
  context.render({
    state: 'idle', trainer: { connected: false, kind: 'ble', capabilities: {} },
    plan: [], is_interval: false, is_test: false, summary: null,
    hr_client: null, power: null, cadence: null,
  });
  check(getEl('trainerPill').textContent === '未连接', '骑行台块显示未连接',
        getEl('trainerPill').textContent);
  check(getEl('devPower').textContent === '--' && getEl('devCadence').textContent === '--',
        '没连接时实时读数是 --（不显示上一次的残留值）',
        getEl('devPower').textContent + '/' + getEl('devCadence').textContent);
  check(getEl('btnDisconnect').classList.contains('hidden'), '没连接时不显示「断开」');

  context.render({
    state: 'idle', trainer: { connected: true, kind: 'ble', name: 'FitShow',
                             capabilities: {} },
    plan: [], is_interval: false, is_test: false, summary: null,
    power: 173.4, cadence: 87.6, speed: 31.24, resistance_raw: 168.0,
    hr_client: { connected: true, name: 'Magene H603', battery: 91, stale: false,
                 rr_seen: true },
    heart_rate: 141, hr_contact: '已接触',
    hr_zone: { code: 'Z3', name: '节奏', label: 'Z3 节奏' },
    hr_limit: { enabled: false, bpm: null },
  });
  check(getEl('trainerPill').textContent === '已连接', '骑行台块显示已连接');
  check(getEl('trainerName').textContent === 'FitShow', '骑行台块显示设备名',
        getEl('trainerName').textContent);
  check(getEl('devPower').textContent === '173', '骑行台块显示实时功率',
        getEl('devPower').textContent);
  check(getEl('devCadence').textContent === '88', '显示实时踏频',
        getEl('devCadence').textContent);
  check(getEl('devSpeed').textContent === '31.2', '显示实时速度',
        getEl('devSpeed').textContent);
  check(getEl('devRes').textContent === '16.8', '显示实时阻力档位',
        getEl('devRes').textContent);
  check(!getEl('btnDisconnect').classList.contains('hidden'), '连上后出现「断开」');

  // 心率带块：实时心率、区间、电极状态
  check(getEl('hrPill').textContent === '已连接', '心率带块显示已连接');
  check(getEl('devHr').textContent === '141', '心率带块显示实时心率',
        getEl('devHr').textContent);
  check(getEl('devHrZone').textContent === 'Z3', '显示当前心率区间',
        getEl('devHrZone').textContent);
  check(getEl('devHrContact').textContent === '已接触', '显示电极接触状态',
        getEl('devHrContact').textContent);
  check(getEl('hrBattery').textContent === '91%', '显示带子电量',
        getEl('hrBattery').textContent);

  // 断开之后不该还挂着上一根的名字和电量（状态写着未连接、右边写着
  // "模拟心率带 88%"，读起来像还连着）
  context.render({
    state: 'idle', trainer: { connected: false, capabilities: {} },
    plan: [], is_interval: false, is_test: false, summary: null,
    hr_client: { connected: false, name: '模拟心率带', battery: 88, stale: true,
                 saved: { address: 'AA:BB', name: '模拟心率带' } },
    heart_rate: null, hr_zone: null, hr_contact: null,
    hr_limit: { enabled: false },
  });
  check(getEl('hrPill').textContent === '未连接', '断开后状态是未连接');
  check(getEl('hrName').textContent === '—', '断开后不再显示设备名',
        getEl('hrName').textContent);
  check(getEl('hrBattery').textContent === '', '断开后不再显示电量',
        JSON.stringify(getEl('hrBattery').textContent));
  check(getEl('devHr').textContent === '--' && getEl('devHrContact').textContent === '--',
        '断开后实时读数也是 --',
        getEl('devHr').textContent + '/' + getEl('devHrContact').textContent);
  check(!getEl('btnHrDisconnect').classList.contains('hidden') === false,
        '断开后不显示「断开」按钮');

  // 心率带连着但没读数：要提示可能是电极没湿，而不是显示一个冻结值
  context.render({
    state: 'idle', trainer: { connected: true, kind: 'ble', capabilities: {} },
    plan: [], is_interval: false, is_test: false, summary: null,
    hr_client: { connected: true, name: 'Magene H603', stale: true, rr_seen: false },
    heart_rate: null, hr_zone: null, hr_limit: { enabled: false },
  });
  check(getEl('devHr').textContent === '--', '没读数时显示 --');
  check(getEl('hrHint').textContent.indexOf('电极') >= 0,
        '提示检查带子有没有戴好、电极有没有湿', getEl('hrHint').textContent);

  /* ---- 两个视图 ---- */
  console.log('\n[11] 设备页 / 训练页分开');

  // 桩里的 fetch 对 /api/scan 返回了两个心率带，但骑行台列表要有内容才看得出区别，
  // 这里直接用两个视图的 hidden 状态判断切换是否生效
  context.switchView('devices');
  check(!getEl('devicesView').classList.contains('hidden'), '设备页显示');
  check(getEl('trainingView').classList.contains('hidden'), '训练页隐藏');
  check(getEl('btnViewDevices').classList.contains('on')
        && !getEl('btnViewTraining').classList.contains('on'),
        '「1 连接设备」高亮');

  context.switchView('training');
  check(getEl('devicesView').classList.contains('hidden'), '切到训练页后设备页隐藏');
  check(!getEl('trainingView').classList.contains('hidden'), '训练页显示');
  check(!getEl('btnViewDevices').classList.contains('on')
        && getEl('btnViewTraining').classList.contains('on'),
        '「2 训练」高亮');

  // 训练开始时自动切到训练页（哪怕当前停在设备页）
  context.switchView('devices');
  context.render({
    state: 'running', trainer: { connected: true, kind: 'ble', capabilities: {} },
    target_power: 200, power: 198, duration_s: 3600, elapsed_s: 10,
    is_interval: false, is_test: false, plan: [], summary: null,
    hr_client: { connected: false, stale: false },
  });
  check(!getEl('trainingView').classList.contains('hidden'),
        '训练一开始自动切到训练页');

  // 点导航按钮也能切页
  const tabHandler = elementListeners.find((l) => l.id === 'btnViewTraining' && l.type === 'click');
  if (check(!!tabHandler, '找到了「2 训练」导航按钮的处理器')) {
    context.switchView('devices');
    tabHandler.fn();
    check(!getEl('trainingView').classList.contains('hidden'), '点导航按钮切到训练页');
  }

  // 顶部状态胶囊：两页都能看到设备接上没有
  context.render({
    state: 'idle',
    trainer: { connected: true, kind: 'ble', capabilities: {} },
    plan: [], is_interval: false, is_test: false, summary: null,
    hr_client: { connected: false, name: 'Magene H603', stale: false },
  });
  const chips = getEl('connChips').children;
  check(chips.length === 2, '训练页顶部有骑行台和心率带两枚状态胶囊',
        String(chips.length) + ' 枚');
  // 设备页自己有两块状态，胶囊在那儿是重复信息，所以只挂在训练页
  context.switchView('devices');
  check(getEl('connChips').classList.contains('hidden'),
        '设备页不显示那两枚胶囊（下面两块已经写清楚了）');
  context.switchView('training');
  check(!getEl('connChips').classList.contains('hidden'),
        '切回训练页时胶囊又出现');
  check(chips[0].textContent.indexOf('✓') >= 0 && chips[1].textContent.indexOf('—') >= 0,
        '骑行台已连接、心率带未连接（各自显示对）',
        chips[0].textContent + ' / ' + chips[1].textContent);

  check(context.window.localStorage.getItem('ibike.view') === 'training'
        || context.window.localStorage.getItem('ibike.view') === 'devices',
        '停留的页面被记住了',
        String(context.window.localStorage.getItem('ibike.view')));

  // 实时心率 + 区间；超过上限要变红
  context.render({
    state: 'running', trainer: { connected: true, kind: 'ble', capabilities: {} },
    target_power: 200, power: 198, cadence: 88, duration_s: 3600, elapsed_s: 600,
    remaining_s: 3000, progress: 0.17, is_interval: false, is_test: false, plan: [],
    summary: null, heart_rate: 132, hr_source: 'strap', hr_stale: false,
    hr_zone: { code: 'Z3', name: '节奏', label: 'Z3 节奏', pct: 0.72 },
    hr_limit: { enabled: true, bpm: 165, note: '' },
    hr_client: { connected: true, name: 'Magene H603', battery: 88, stale: false },
  });
  check(getEl('mHr').textContent === '132', '实时心率显示出来了',
        getEl('mHr').textContent);
  check(getEl('mHrZone').textContent === 'Z3 节奏', '心率下面显示当前区间',
        getEl('mHrZone').textContent);
  check(getEl('mHr').className.indexOf('m-hr-over') < 0, '没超上限时不标红');

  context.render({
    state: 'running', trainer: { connected: true, kind: 'ble', capabilities: {} },
    target_power: 200, power: 198, cadence: 88, duration_s: 3600, elapsed_s: 610,
    remaining_s: 2990, progress: 0.17, is_interval: false, is_test: false, plan: [],
    summary: null, heart_rate: 172, hr_source: 'strap', hr_stale: false,
    hr_zone: { code: 'Z4', name: '阈值', label: 'Z4 阈值', pct: 0.86 },
    hr_limit: { enabled: true, bpm: 165, note: '心率超过上限，目标已下调到 190W' },
    hr_client: { connected: true, name: 'Magene H603', battery: 88, stale: false },
  });
  check(getEl('mHr').className.indexOf('m-hr-over') >= 0, '超过上限时数字标红');
  check(getEl('mHrZone').textContent.indexOf('超过上限') >= 0,
        '并且写清楚是超上限', getEl('mHrZone').textContent);

  // 心率带连着但没有读数
  context.render({
    state: 'running', trainer: { connected: true, kind: 'ble', capabilities: {} },
    target_power: 200, power: 198, duration_s: 3600, elapsed_s: 620,
    is_interval: false, is_test: false, plan: [], summary: null,
    heart_rate: null, hr_source: null, hr_stale: true, hr_zone: null,
    hr_limit: { enabled: false, bpm: null, note: '' },
    hr_client: { connected: true, name: 'Magene H603', stale: true },
  });
  check(getEl('mHr').textContent === '--' && getEl('mHrZone').textContent === '未收到数据',
        '连着但没数据时说「未收到数据」，而不是显示一个冻结值',
        getEl('mHrZone').textContent);

  // 报告里的心率区块
  context.render({
    state: 'finished', trainer: { connected: true, kind: 'ble', capabilities: {} },
    is_interval: false, is_test: false, plan: [],
    hr_client: { connected: true, name: 'Magene H603', stale: false },
    summary: Object.assign({}, baseSum, {
      finished_at: 1700009998,
      heart_rate: {
        avg_bpm: 148.3, max_bpm: 176, sample_count: 599, source: 'strap',
        rr_seen: true, zone_mode: 'reserve', max_hr: 190, rest_hr: 55,
        cap_active: true, cap_bpm: 165, cap_note: '心率超过上限，目标已下调到 190W',
        zones: [
          { code: 'Z1', name: '恢复', label: 'Z1 恢复', min_bpm: 55, max_bpm: 136, seconds: 120, pct: 20.0 },
          { code: 'Z2', name: '有氧', label: 'Z2 有氧', min_bpm: 136, max_bpm: 149.5, seconds: 180, pct: 30.0 },
          { code: 'Z3', name: '节奏', label: 'Z3 节奏', min_bpm: 149.5, max_bpm: 163, seconds: 240, pct: 40.0 },
          { code: 'Z4', name: '阈值', label: 'Z4 阈值', min_bpm: 163, max_bpm: 176.5, seconds: 60, pct: 10.0 },
          { code: 'Z5', name: '最大', label: 'Z5 最大', min_bpm: 176.5, max_bpm: null, seconds: 0, pct: 0.0 },
        ],
      },
    }),
  });
  check(!getEl('sumHrSection').classList.contains('hidden'), '报告里显示心率区块');
  check(getEl('sumHrStats').children.length === 3, '心率区块有三格（平均/最大/来源）',
        String(getEl('sumHrStats').children.length) + ' 格');
  check(getEl('sumHrZones').children.length === 5, '5 个心率区间都画出来了',
        String(getEl('sumHrZones').children.length) + ' 行');
  const firstZone = getEl('sumHrZones').children[0].children;
  check(firstZone[0].textContent === 'Z1 恢复', '区间行有名称',
        firstZone[0].textContent);
  check(firstZone[1].textContent === '55~136', '区间行有 bpm 范围',
        firstZone[1].textContent);
  check(getEl('sumHrHint').textContent.indexOf('储备心率') >= 0,
        '说明用的是哪种区间算法', getEl('sumHrHint').textContent.slice(0, 30));
  check(getEl('sumHrHint').textContent.indexOf('上限保护') >= 0,
        '报告里带上了上限保护的下调记录');

  // 没有心率数据的报告：整块不显示（空着比一堆 "--" 诚实）
  context.render({
    state: 'finished', trainer: { connected: true, kind: 'ble', capabilities: {} },
    is_interval: false, is_test: false, plan: [],
    summary: Object.assign({}, baseSum, { finished_at: 1700009997, heart_rate: null }),
  });
  check(getEl('sumHrSection').classList.contains('hidden'),
        '没接心率带的那次训练不显示心率区块');

  console.log('\n' + '='.repeat(66));
  if (failures) {
    console.log(`\x1b[31m✘\x1b[0m 失败 ${failures} 项`);
    process.exit(1);
  }
  console.log('\x1b[32m✔\x1b[0m 全部通过');
})();

#!/usr/bin/env node
/* 新前端（Hallmark · Formwork · app screen）的冒烟测试。
 *
 * 重写前端的代价是旧的那份测试全部作废（它检查的是旧 DOM），这份是给新结构
 * 重新写的：加载真实的 index.html + app.js，喂几份状态，看它画得对不对。
 * 覆盖：空闲/骑行中/暂停/结束总结/报告列表，以及"按钮点了会打到哪个接口"。
 *
 *   node tests/test_frontend_smoke.js
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const WEB = path.join(ROOT, 'ibike', 'web');

const failures = [];
function check(cond, label, detail) {
  const mark = cond ? '\u001b[32m✔\u001b[0m' : '\u001b[31m✘\u001b[0m';
  console.log(`  ${mark} ${label}${detail ? '  → ' + detail : ''}`);
  if (!cond) failures.push(label);
}

/* ---------------- 一个够用的 DOM 桩 ---------------- */

function makeEl(tag = 'div', id = '') {
  const el = {
    tagName: String(tag).toUpperCase(),
    id,
    children: [],
    _text: '',
    className: '',
    hidden: false,
    disabled: false,
    checked: false,
    value: '',
    style: {},
    _attrs: {},
    _listeners: {},
    dataset: {},
    scrollTop: 0,
    scrollHeight: 0,
    appendChild(child) { this.children.push(child); return child; },
    append(...kids) { kids.forEach((k) => this.children.push(k)); },
    removeChild(child) { this.children = this.children.filter((c) => c !== child); },
    remove() {},
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return this._attrs[k]; },
    removeAttribute(k) { delete this._attrs[k]; },
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    removeEventListener() {},
    focus() {},
    getContext() {
      return new Proxy({}, { get: () => () => {} });
    },
    getBoundingClientRect() { return { width: 800, height: 220, top: 0, left: 0 }; },
  };
  Object.defineProperty(el, 'textContent', {
    get() {
      return this.children.length
        ? this._text + this.children.map((c) => c.textContent || '').join('')
        : this._text;
    },
    set(v) { this._text = String(v); this.children = []; },
  });
  Object.defineProperty(el, 'innerHTML', {
    get() { return ''; },
    set() { this.children = []; this._text = ''; },
  });
  Object.defineProperty(el, 'firstChild', { get() { return this.children[0] || null; } });
  return el;
}

/* 从 index.html 里收集 id 和标签名，建一棵够用的树 */
const html = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
const ids = new Map();
const byTag = new Map();
for (const m of html.matchAll(/<([a-z0-9]+)([^>]*?)\bid="([^"]+)"([^>]*)>/g)) {
  const tag = m[1];
  const el = makeEl(tag, m[3]);
  if (/\bhidden\b/.test(m[2] + m[4])) el.hidden = true;
  const cls = /class="([^"]*)"/.exec(m[2] + m[4]);
  if (cls) el.className = cls[1];
  const val = /value="([^"]*)"/.exec(m[2] + m[4]);
  if (val) el.value = val[1];
  ids.set(m[3], el);
  byTag.set(tag, (byTag.get(tag) || []).concat([el]));
}

const tally = { fetch: [], listeners: [] };
function $(id) { return ids.get(id) || null; }

const documentStub = {
  readyState: 'complete',
  documentElement: makeEl('html'),
  getElementById: (id) => ids.get(id) || null,
  querySelectorAll: (sel) => {
    if (sel === '.view') return Array.from(ids.values()).filter((e) => /^v-/.test(e.id));
    if (sel === '.tabs a') return Array.from(ids.values()).filter((e) => e.tagName === 'A');
    if (sel === 'input[name="mode"]:checked') return [{ value: 'constant' }];
    if (sel === 'input[name="mode"]') return [];
    if (sel === '[data-mode]') return [];
    if (sel === '#courseSteps tbody') return [];
    if (sel === '.tabs a[aria-current="page"]') return [];
    return [];
  },
  querySelector: (sel) => (documentStub.querySelectorAll(sel) || [])[0] || null,
  createElement: (tag) => makeEl(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
  addEventListener: () => {},
};
documentStub.body = makeEl('body');

const sandbox = {
  document: documentStub,
  console,
  location: { hash: '#/train', protocol: 'http:', host: '127.0.0.1:8700' },
  history: { replaceState: () => {}, pushState: () => {} },
  window: { addEventListener: () => {}, devicePixelRatio: 1, confirm: () => true, innerWidth: 1440 },
  getComputedStyle: () => ({ getPropertyValue: () => '#111111' }),
  requestAnimationFrame: (fn) => { try { fn(); } catch (e) { /* canvas 桩 */ } },
  setTimeout: (fn) => { try { fn(); } catch (e) { /* 定时器桩 */ } return 0; },
  clearTimeout: () => {},
  fetch: async (url, opts) => {
    tally.fetch.push({ url, body: opts && opts.body });
    if (url.indexOf('/api/templates') >= 0) return fake({ ok: true, templates: [{ id: 't1', name: '4×4', params: [] }] });
    if (url.indexOf('/api/tests') >= 0) return fake({ ok: true, tests: [{ id: 'ramp', name: '坡道测试' }] });
    if (url.indexOf('/api/custom') >= 0) return fake({ ok: true, courses: [{ id: 'c1', name: '我的课', steps: [] }] });
    if (url.indexOf('/api/settings') >= 0) return fake({ ok: true, settings: { ftp: 160, hr_max: 195, hr_rest: 60 } });
    if (url.indexOf('/api/reports') >= 0) return fake({ ok: true, reports: [{
      id: 'r1', started_at: 1794723820, plan_name: '二区有氧', actual_s: 3960, avg_power: 106.8,
      heart_rate: { avg_bpm: 130 }, completed: true,
    }] });
    return fake({ ok: true });
  },
  WebSocket: class { constructor() {} addEventListener() {} close() {} },
};
sandbox.window.document = documentStub;
sandbox.window.location = sandbox.location;
sandbox.window.history = sandbox.history;
sandbox.globalThis = sandbox;

async function fake(obj) { return { ok: true, status: 200, json: async () => obj }; }

vm.createContext(sandbox);
// 前台 JS 是经典脚本（非模块），函数都落在全局，直接跑
vm.runInContext(fs.readFileSync(path.join(WEB, 'app.js'), 'utf8'), sandbox, { filename: 'app.js' });

/* ---------------- 断言 ---------------- */

const baseState = {
  state: 'running', power: 132, target_power: 130, remaining_s: 2400, elapsed_s: 1200,
  heart_rate: 148, hr_zone: { code: 'Z4' }, cadence: 86, resistance_raw: 160,
  trainer: { connected: true, name: '拟合骑行台' }, hr_client: { connected: true, name: 'H603', battery: 88 },
  overlay_running: false, is_interval: false, is_test: false, plan: [], step_count: 0, step_index: 0,
  hr_limit: { enabled: false }, summary: null, error: null, stale_data: false,
};

(async () => {
  console.log('=' .repeat(66));
  console.log('新前端冒烟测试（Hallmark · Formwork）');
  console.log('='.repeat(66));

  console.log('\n[1] 页面结构');
  const need = ['trainerChip', 'hrChip', 'btnOverlay', 'power', 'stripFill', 'stripMark',
    'mTarget', 'mRemain', 'mElapsed', 'mHr', 'mCadence', 'mResistance', 'btnStart', 'btnPause',
    'btnResume', 'btnStop', 'liveChart', 'timeline', 'logList', 'reportsBody', 'detailBody'];
  const missing = need.filter((id) => !$(id));
  check(missing.length === 0, '关键元素齐全（' + need.length + ' 个）', missing.join(', '));
  check(/data-mode="constant"/.test(html) && /data-mode="interval"/.test(html)
    && /data-mode="custom"/.test(html) && /data-mode="test"/.test(html), '四种训练模式都在');
  check(/<meta name="description"/.test(html), '有 meta description');
  check(!/\u2014/.test(html), '静态文案里没有破折号（gate 19）');

  console.log('\n[2] 骑行中的渲染');
  sandbox.render(baseState);
  check($('power').textContent === '132W', '大数字是实时功率', $('power').textContent);
  check($('mTarget').textContent === '130 W', '目标功率', $('mTarget').textContent);
  check($('mRemain').textContent === '40:00', '剩余时间', $('mRemain').textContent);
  check($('mHr').textContent.indexOf('148') === 0, '心率', $('mHr').textContent);
  check($('mCadence').textContent === '86 rpm', '踏频', $('mCadence').textContent);
  check($('btnStart').hidden === true && $('btnPause').hidden === false,
    '骑行中显示暂停、隐藏开始');
  check($('stripFill').style.transform.startsWith('scaleX('), '功率条用 scaleX 画',
    $('stripFill').style.transform);
  check(($('stripMark').style.left || '').indexOf('%') > 0, '目标刻度用 left 定位',
    $('stripMark').style.left);
  check($('trainerChip').textContent.indexOf('拟合骑行台') >= 0, '顶栏显示骑行台状态',
    $('trainerChip').textContent);

  console.log('\n[3] 暂停 / 掉线');
  sandbox.render(Object.assign({}, baseState, { state: 'paused', power: null }));
  check($('btnResume').hidden === false && $('btnPause').hidden === true, '暂停时显示「继续」');
  sandbox.render(Object.assign({}, baseState, { state: 'paused', trainer_lost: true, auto_reconnecting: true }));
  check($('castState').textContent.indexOf('自动重连') >= 0, '掉线时说明在自动重连',
    $('castState').textContent);
  check($('btnResume').disabled === true, '掉线时「继续」是灰的');

  console.log('\n[4] 空闲');
  sandbox.render(Object.assign({}, baseState, {
    state: 'idle', power: null, target_power: null, heart_rate: null, cadence: null,
    resistance_raw: null, remaining_s: 3600,
  }));
  check($('power').textContent.indexOf('—') < 0 && /\d/.test($('power').textContent),
    '空闲时大数字显示表单里的目标而不是破折号', $('power').textContent);
  check($('btnStart').hidden === false, '空闲时显示「开始训练」');

  console.log('\n[5] 结束总结');
  sandbox.render(Object.assign({}, baseState, {
    state: 'finished', power: null,
    summary: {
      plan_name: '二区有氧', actual_s: 3960, avg_power: 106.8, normalized_power: 108.3,
      max_power: 158, distance_m: 25274, energy_kj: 423, in_zone_pct: 57.5,
      work_in_zone_pct: 57.5, in_zone_tolerance_w: 5, mean_abs_deviation_w: 6.6,
      heart_rate: { avg_bpm: 130, max_bpm: 144, max_hr: 195, zones: [
        { code: 'Z1', label: 'Z1 恢复', pct: 6.5 }, { code: 'Z2', label: 'Z2 有氧', pct: 86.6 },
        { code: 'Z3', label: 'Z3 节奏', pct: 6.9 }] },
      intervals: [{ name: '高强度', target_power: 110, avg_power: 109.6, in_zone_pct: 57.5, actual_s: 3602 }],
      rests: [{ started_at: 1191, duration_s: 103, reason: '自动暂停', hr_start: 116, hr_end: 95, hr_drop_60s: 21 }],
      mode_changes: [{ t: 665, kind: 'probe', reason: '怀疑固件没跟' }],
      trace: [{ t: 0, p: 100, hr: 90 }, { t: 1, p: 120, hr: 95 }],
    },
  }));
  check($('reportDetail').hidden === false, '结束后展开总结');
  const detail = $('detailBody').textContent;
  ['107', '骑行', 'Z2', '自动暂停', '目标功率探测'].forEach((needle) => {
    check(detail.indexOf(needle) >= 0, '总结里有「' + needle + '」');
  });
  check($('detailBody').children.length > 0, '总结渲染出了内容');

  console.log('\n[6] 报告列表');
  await sandbox.loadReports();
  check($('reportsBody').children.length === 1, '一行报告', String($('reportsBody').children.length));
  check($('reportsBody').textContent.indexOf('二区有氧') >= 0, '显示课程名');

  console.log('\n[7] 操作会打到哪个接口');
  tally.fetch.length = 0;
  sandbox.render(baseState);
  const click = (id) => {
    const el = $(id);
    (el._listeners.click || []).forEach((fn) => fn());
  };
  click('btnPause');
  click('btnStop');
  click('btnOverlay');
  await new Promise((r) => setImmediate(r));
  const urls = tally.fetch.map((f) => f.url);
  check(urls.some((u) => u.indexOf('/api/pause') >= 0), '暂停 → /api/pause');
  check(urls.some((u) => u.indexOf('/api/stop') >= 0), '结束 → /api/stop');
  check(urls.some((u) => u.indexOf('/api/overlay') >= 0), '悬浮窗 → /api/overlay');

  console.log('\n' + '='.repeat(66));
  if (failures.length) {
    console.log(`失败 ${failures.length} 项：`);
    failures.forEach((f) => console.log('   - ' + f));
    process.exit(1);
  }
  console.log('全部通过');
})();

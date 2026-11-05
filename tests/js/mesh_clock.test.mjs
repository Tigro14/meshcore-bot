import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = fs.readFileSync(new URL('../../modules/web_viewer/templates/mesh.html', import.meta.url), 'utf8');
const start = html.indexOf('    function nodeClockNumber');
const end = html.indexOf('    function getNodeFillColor', start);
assert.ok(start >= 0 && end > start);
class Element {
    constructor() { this.textContent = ''; this.children = []; this.style = {}; this.attrs = {}; this.listeners = {}; }
    appendChild(child) { this.children.push(child); }
    setAttribute(key, value) { this.attrs[key] = value; }
    addEventListener(key, handler) { this.listeners[key] = handler; }
}
const labels = { checked: false };
const ctx = vm.createContext({ document: { getElementById: () => labels, createElement: () => new Element() },
    L: { DomEvent: { disableClickPropagation: () => {} } },
    getNodeShortLabel: node => node.name,
    escapeHtml: value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
});
vm.runInContext(html.slice(start, end), ctx);
const badClock = { name: 'Relay', clock_drift_exceeded: true, clock_drift_seconds: 601,
    clock_signed_drift_seconds: -601, clock_observed_at: 1793890000, clock_drift_source: 'advert' };
function marker() {
    return { clicks: 0, fire(event) { assert.equal(event, 'click'); this.clicks++; },
        bindTooltip(label, options) { this.label = label; this.options = options; } };
}

test('clock warnings remain visible when node labels are disabled', () => {
    labels.checked = false;
    const m = marker();
    ctx.bindNodeLabel(m, badClock);
    assert.equal(m.label.textContent, '');
    assert.equal(m.label.children.length, 1);
    assert.match(m.label.children[0].innerHTML, /mesh-clock-outline/);
    assert.doesNotMatch(m.label.children[0].innerHTML, /⚠|exclamation/);
    assert.match(m.label.title, /Behind by 10 min 1 s/);
    assert.equal(m.options.permanent, true);
    assert.equal(m.options.pane, 'meshClockLabels');
    assert.equal(m.options.interactive, true);
});

test('clicking or using the keyboard on a clock opens that relay', () => {
    const m = marker();
    ctx.bindNodeLabel(m, badClock);
    m.label.listeners.click();
    m.label.listeners.keydown({ key: 'Enter', preventDefault() {}, stopPropagation() {} });
    m.label.listeners.keydown({ key: ' ', preventDefault() {}, stopPropagation() {} });
    assert.equal(m.clicks, 3);
});

test('a clock warning can be combined with the existing node label', () => {
    labels.checked = true;
    const m = marker();
    ctx.bindNodeLabel(m, badClock);
    assert.equal(m.label.textContent, 'Relay');
    assert.match(m.label.children[0].innerHTML, /mesh-clock-outline/);
    assert.equal(m.options.direction, 'top');
    labels.checked = false;
});

test('unknown and in-sync nodes do not acquire warnings', () => {
    for (const node of [{ name: 'Unknown' }, { ...badClock, clock_drift_exceeded: false, clock_drift_seconds: 24 }]) {
        const m = marker();
        ctx.bindNodeLabel(m, node);
        assert.equal(m.label, undefined);
    }
});

test('popup clock details include direction, duration, source and observation age', () => {
    const note = ctx.nodeClockHTML({ ...badClock, clock_observation_stale: true });
    assert.match(note, /Clock/);
    assert.match(note, /Behind by 10 min 1 s/);
    assert.match(note, /Measured at/);
    assert.match(note, /advert.*old observation/);
    assert.match(ctx.nodeClockHTML({ ...badClock, clock_signed_drift_seconds: 601 }), /Ahead/);
    assert.equal(ctx.nodeClockHTML({ name: '<script>' }), '');
    assert.equal(ctx.nodeClockHTML({ clock_drift_seconds: '<script>' }), '');
});

test('clock labels stay above ordinary labels and below popups after re-rendering', () => {
    const panes = new Map();
    const map = { getPane: name => panes.get(name), createPane: name => {
        const pane = { style: {} }; panes.set(name, pane); return pane;
    } };
    ctx.ensureNodeIndicatorPanes(map);
    ctx.ensureNodeIndicatorPanes(map);
    assert.equal(panes.size, 2);
    assert.ok(Number(panes.get('meshClockMarkers').style.zIndex) > 600);
    const clock = panes.get('meshClockLabels');
    assert.ok(Number(clock.style.zIndex) > 650);
    assert.ok(Number(clock.style.zIndex) < 700);
    assert.equal(clock.style.pointerEvents, 'none');
});

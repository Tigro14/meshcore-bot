import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = fs.readFileSync(new URL('../../modules/web_viewer/templates/mesh.html', import.meta.url), 'utf8');
const start = html.indexOf('    function nodeClockNumber');
const end = html.indexOf('    let entryMode', start);
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
    getNodeShortLabel: node => node.name, isWarningHighlighted: () => false,
    escapeHtml: value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
});
vm.runInContext(html.slice(start, end), ctx);
const badClock = { name: 'Relay', clock_drift_exceeded: true, clock_drift_seconds: 601,
    clock_signed_drift_seconds: -601, clock_observed_at: 1793890000, clock_drift_source: 'advert' };
function marker() {
    return { clicks: 0, fire(event) { assert.equal(event, 'click'); this.clicks++; },
        bindTooltip(label, options) { this.label = label; this.options = options; } };
}

test('clock-only relay gets a clock, never the region/configuration triangle', () => {
    labels.checked = false;
    const m = marker();
    ctx.bindNodeLabel(m, badClock);
    assert.equal(m.label.textContent, '');
    assert.equal(m.label.children.length, 1);
    assert.match(m.label.children[0].innerHTML, /mesh-clock-outline/);
    assert.doesNotMatch(m.label.children[0].innerHTML, /⚠|exclamation/);
    assert.match(m.label.children[0].innerHTML, /fill="none"/);
    assert.match(m.label.children[0].innerHTML, /stroke="currentColor"/);
    assert.match(m.label.title, /En retard de 10 min 1 s/);
    assert.equal(m.options.permanent, true);
    assert.equal(m.options.pane, 'meshClockLabels');
    assert.equal(m.options.interactive, true);
    assert.equal(ctx.nodeNeedsForeground(badClock), true);
});

test('region and clock symbols coexist and open the correct repeater on click/keyboard', () => {
    labels.checked = false;
    const m = marker();
    ctx.bindNodeLabel(m, { ...badClock, unscoped_only_observed: true });
    assert.equal(m.label.textContent, '⚠️');
    assert.equal(m.options.pane, 'meshConfigurationLabels');
    assert.match(m.label.children[0].innerHTML, /mesh-clock-outline/);
    m.label.listeners.click();
    m.label.listeners.keydown({ key: 'Enter', preventDefault() {}, stopPropagation() {} });
    assert.equal(m.clicks, 2);
    assert.match(m.label.title, /Aucun trafic avec région/);
    assert.match(m.label.title, /En retard/);
});

test('clock remains attached when node names are shown', () => {
    labels.checked = true;
    const m = marker();
    ctx.bindNodeLabel(m, badClock);
    assert.equal(m.label.textContent, 'Relay');
    assert.match(m.label.children[0].innerHTML, /mesh-clock-outline/);
    assert.equal(m.options.direction, 'top');
    labels.checked = false;
});

test('in-sync and unknown clocks do not acquire a clock warning', () => {
    for (const node of [{ name: 'Unknown' }, { ...badClock, clock_drift_exceeded: false, clock_drift_seconds: 24 }]) {
        const m = marker();
        ctx.bindNodeLabel(m, node);
        assert.equal(m.label, undefined);
        assert.equal(ctx.nodeNeedsForeground(node), false);
    }
});

test('popup note includes direction, numeric duration, source and old observation date', () => {
    const note = ctx.nodeClockHTML({ ...badClock, clock_observation_stale: true });
    assert.match(note, /Horloge/);
    assert.match(note, /En retard de 10 min 1 s/);
    assert.match(note, /Mesuré le/);
    assert.match(note, /annonce.*mesure ancienne/);
    assert.match(ctx.nodeClockHTML({ ...badClock, clock_signed_drift_seconds: 601 }), /En avance/);
    assert.equal(ctx.nodeClockHTML({ name: '<script>' }), '');
    assert.equal(ctx.nodeClockHTML({ clock_drift_seconds: '<script>' }), '');
});

test('existing region and one-byte indicators remain triangles without clock evidence', () => {
    for (const node of [{ name: 'Region', unscoped_only_observed: true },
        { name: 'One byte', path_encoding_badge: 'one_byte' }]) {
        const m = marker();
        ctx.bindNodeLabel(m, node);
        assert.equal(m.label.textContent, '⚠️');
        assert.equal(m.label.children.length, 0);
        assert.equal(m.options.permanent, true);
    }
});

test('configuration warning labels stay above clocks and ordinary labels after repeated renders', () => {
    const panes = new Map();
    const map = { getPane: name => panes.get(name), createPane: name => {
        const pane = { style: {} }; panes.set(name, pane); return pane;
    } };
    ctx.ensureNodeIndicatorPanes(map);
    ctx.ensureNodeIndicatorPanes(map);
    const warning = panes.get('meshConfigurationLabels');
    const clock = panes.get('meshClockLabels');
    assert.ok(Number(warning.style.zIndex) > Number(clock.style.zIndex));
    assert.ok(Number(clock.style.zIndex) > 650);
    assert.ok(Number(warning.style.zIndex) < 700);
    assert.equal(warning.style.pointerEvents, 'none');
});

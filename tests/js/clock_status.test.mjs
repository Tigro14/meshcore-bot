import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = fs.readFileSync(new URL('../../modules/web_viewer/templates/time.html', import.meta.url), 'utf8');
const start = html.indexOf('    class TimeViewer');
const end = html.lastIndexOf("    document.addEventListener('DOMContentLoaded'");
assert.ok(start >= 0 && end > start);
const container = { innerHTML: '' };
const now = Date.parse('2026-11-05T14:03:32Z');
class FixedDate extends Date {
    static now() { return now; }
}
const context = vm.createContext({ Date: FixedDate, document: {
    getElementById: () => container, querySelectorAll: () => [],
    // base.html renders data-is-admin on <body>; these tests assume the admin view.
    body: { dataset: { isAdmin: 'true' } },
} });
vm.runInContext(fs.readFileSync(new URL('../../modules/web_viewer/static/js/clock_drift.js', import.meta.url), 'utf8'), context);
vm.runInContext(html.slice(start, end) + '\nglobalThis.TimeClass = TimeViewer;', context);
const viewer = Object.create(context.TimeClass.prototype);
viewer.escapeHtml = value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;');

test('UTC sync time is minutes ago, not an hour ago, in the French timezone', () => {
    assert.equal(viewer.formatTimestamp('2026-11-05T14:00:45+00:00'), 'il y a 2 min');
    assert.equal(viewer.formatTimestamp('2026-11-05 14:00:45'), 'il y a 2 min');
    assert.equal(viewer.timestampDate('2026-11-05 14:00:45').toISOString(), '2026-11-05T14:00:45.000Z');
});

test('Concarneau keeps its numeric drift and restricts verification to the clock column', () => {
    viewer.updateTargets([{ id: 1, target: 'ab'.repeat(32), contact_name: 'Concarneau', enabled: true,
        clock_state: 'awaiting_verification', clock_drift_status: 'unset', clock_timestamp_valid: false, clock_drift: 1793177892,
        clock_observed_at: Date.parse('2026-11-05T00:58:15Z') / 1000, clock_source: 'advert',
        last_sync_success: true, last_sync_at: '2026-11-05T14:01:10+00:00',
    }]);
    assert.match(container.innerHTML, /Horloge à vérifier/);
    assert.match(container.innerHTML, /Réception confirmée/);
    assert.match(container.innerHTML, />\s*20754d8h</);
    assert.match(container.innerHTML, /Mesuré le/);
    assert.doesNotMatch(container.innerHTML, />\s*OK\s*</);
    assert.doesNotMatch(container.innerHTML, /Horloge non réglée/);
    assert.equal((container.innerHTML.match(/Horloge à vérifier/g) || []).length, 1);
});

test('Trédudon good measurement and earlier missing ACK are presented separately', () => {
    viewer.updateTargets([{ id: 2, target: 'cd'.repeat(32), contact_name: 'Trédudon', enabled: true,
        clock_state: 'in_sync', clock_drift_status: 'in_sync', clock_timestamp_valid: true, clock_drift: 9,
        clock_observed_at: Date.parse('2026-11-05T11:52:11Z') / 1000, clock_source: 'message',
        last_sync_success: false, last_sync_at: '2026-11-05T11:53:23+00:00',
    }]);
    assert.match(container.innerHTML, /À l’heure/);
    assert.match(container.innerHTML, /Réception non confirmée/);
    assert.match(container.innerHTML, /bg-success/);
    assert.match(container.innerHTML, />9s</);
    assert.doesNotMatch(container.innerHTML, /3591s|>\s*Fail\s*</);
});

test('anonymous visitors get a read-only table without admin controls', () => {
    const anonContainer = { innerHTML: '' };
    const anon = vm.createContext({ Date: FixedDate, document: {
        getElementById: () => anonContainer, querySelectorAll: () => [],
        body: { dataset: { isAdmin: 'false' } },
    } });
    vm.runInContext(fs.readFileSync(new URL('../../modules/web_viewer/static/js/clock_drift.js', import.meta.url), 'utf8'), anon);
    vm.runInContext(html.slice(start, end) + '\nglobalThis.TimeClass = TimeViewer;', anon);
    const instance = Object.create(anon.TimeClass.prototype);
    instance.escapeHtml = value => String(value);
    instance.updateTargets([{ id: 1, target: 'ab'.repeat(32), contact_name: 'Relay A', enabled: true,
        clock_state: 'in_sync', clock_drift_status: 'in_sync', clock_timestamp_valid: true, clock_drift: 9,
        clock_observed_at: Date.parse('2026-11-05T11:52:11Z') / 1000, clock_source: 'message',
        last_sync_success: true, last_sync_at: '2026-11-05T11:53:23+00:00',
    }]);
    assert.match(anonContainer.innerHTML, /Relay A/);
    assert.doesNotMatch(anonContainer.innerHTML, /<th class="text-end">Actions|Auto clkreboot/);
    assert.doesNotMatch(anonContainer.innerHTML, /toggleTarget|toggleAutoClkreboot|promptDelete|showHistory/);
});

test('clock rejection and stale measurements cannot render as success', () => {
    assert.match(viewer.clockStatusBadge('rejected'), /Commande refusée/);
    assert.match(viewer.clockStatusBadge('stale'), /Mesure ancienne/);
    assert.doesNotMatch(viewer.clockStatusBadge('rejected'), /bg-success|OK/);
});

test('real page initialization wires the switch to its saved-setting method', () => {
    let ready;
    let host;
    let refreshes = 0;
    const puts = [];
    const doc = {
        addEventListener: (name, callback) => { if (name === 'DOMContentLoaded') ready = callback; },
        createElement: () => ({ style: {} }),
        body: { appendChild: element => { host = element; } },
        getElementById: () => null,
    };
    const runtime = vm.createContext({ document: doc, fetch: async (url, options) => {
        puts.push({ url, options });
        return { ok: true };
    }, alert: message => { throw new Error(message); } });
    // Run the actual DOMContentLoaded block as well as the class. Avoid unrelated
    // background polling; the real switch callback and PUT method remain intact.
    const script = html.slice(start, html.indexOf('</script>', start));
    vm.runInContext(script + '\nglobalThis.TimeClass = TimeViewer;', runtime);
    runtime.TimeClass.prototype.init = function () {};
    runtime.TimeClass.prototype.loadData = async function () { refreshes++; };
    ready();
    assert.equal(typeof host.toggleAutoClkreboot, 'function');
    return host.toggleAutoClkreboot(7, true).then(() => {
        assert.equal(puts.length, 1);
        assert.equal(puts[0].url, '/api/clock-sync-targets-admin/7');
        assert.deepEqual(JSON.parse(puts[0].options.body), { auto_clkreboot_enabled: true });
        assert.equal(puts[0].options.method, 'PUT');
        assert.equal(refreshes, 1);
    });
});

test('rejected setting updates report the error and reload the persisted value', async () => {
    const errors = [];
    const runtime = vm.createContext({ fetch: async () => ({ ok: false, status: 403 }),
        alert: message => errors.push(message) });
    vm.runInContext(html.slice(start, end) + '\nglobalThis.TimeClass = TimeViewer;', runtime);
    const instance = Object.create(runtime.TimeClass.prototype);
    let reloads = 0;
    instance.loadData = async () => { reloads++; };
    await instance.toggleAutoClkreboot(7, true);
    assert.match(errors[0], /non enregistré.*HTTP 403/);
    assert.equal(reloads, 1);
});

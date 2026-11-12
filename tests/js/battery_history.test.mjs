import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const templates = new URL('../../modules/web_viewer/templates/', import.meta.url);
const points = [
    { observed_at: '2026-11-09 16:33:49', voltage: 4.07 },
    { observed_at: '2026-11-10T02:02:20', voltage: 4.03 },
    { observed_at: '2026-11-11T03:02:43+01:00', voltage: 3.90 },
];
const expectedLabels = points.map(point => {
    const timestamp = point.observed_at.replace(' ', 'T');
    return new Date(timestamp.endsWith('+01:00') ? timestamp : timestamp + 'Z').toLocaleString();
});

function loadViewer(page) {
    const source = fs.readFileSync(new URL(page + '.html', templates), 'utf8');
    const className = page === 'time' ? 'TimeViewer' : 'ModernContactsManager';
    const start = source.indexOf('class ' + className);
    const end = source.indexOf(page === 'time'
        ? "document.addEventListener('DOMContentLoaded'"
        : '\nfunction exportData', start);
    assert.ok(start >= 0 && end > start);
    const canvas = {};
    const content = { innerHTML: '' };
    const modal = { addEventListener() {}, remove() {} };
    const charts = [];
    const context = vm.createContext({
        Date,
        fetch: async () => ({ ok: true, json: async () => ({ points }) }),
        bootstrap: { Modal: class { show() {} } },
        document: {
            body: { insertAdjacentHTML() {} },
            getElementById(id) {
                if (id === 'battery-history-chart') return canvas;
                if (id === 'battery-history-content') return content;
                if (id === 'batteryHistoryModal') return modal;
                throw new Error('Unexpected element: ' + id);
            },
        },
        Chart: class {
            constructor(element, config) { charts.push({ element, config }); }
        },
    });
    vm.runInContext(source.slice(start, end) + '\nglobalThis.Viewer = ' + className + ';', context);
    const viewer = Object.create(context.Viewer.prototype);
    viewer.escapeHtml = String;
    viewer.lastTargetsData = [];
    viewer.filteredData = [];
    return { viewer, source, charts, canvas, content };
}

for (const page of ['time', 'contacts']) {
    test(page + ' loads the bundled Chart.js before its page script', () => {
        const { source } = loadViewer(page);
        const libraries = source.match(/{% block page_libs %}([\s\S]*?){% endblock %}/);
        assert.ok(libraries, 'missing page library block');
        assert.match(libraries[1], /filename='vendor\/chart\.js\/chart\.umd\.js'/);
        const base = fs.readFileSync(new URL('base.html', templates), 'utf8');
        assert.ok(base.indexOf('{% block page_libs %}') < base.indexOf('{% block extra_js %}'));
    });

    test(page + ' plots battery history with absolute plain-text dates and the recorded voltages', async () => {
        const { viewer, charts, canvas, content } = loadViewer(page);
        await viewer.showBatteryHistory('ab'.repeat(32));
        assert.equal(charts.length, 1, content.innerHTML);
        assert.equal(charts[0].element, canvas);
        const { config } = charts[0];
        assert.deepEqual(Array.from(config.data.labels), expectedLabels);
        assert.deepEqual(Array.from(config.data.datasets[0].data), points.map(point => point.voltage));
        assert.ok(config.data.labels.every(label => !/[<>]/.test(label)));
    });

    test(page + ' handles missing or invalid dates without throwing', () => {
        const { viewer } = loadViewer(page);
        for (const value of [null, '', 'invalid']) {
            assert.equal(viewer.formatBatteryTimestamp(value), '-');
        }
        assert.equal(viewer.formatBatteryTimestamp('2026-11-10 02:02:20'),
            viewer.formatBatteryTimestamp('2026-11-10T03:02:20+01:00'));
    });
}

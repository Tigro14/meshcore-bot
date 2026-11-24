import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../modules/web_viewer/templates/realtime.html', import.meta.url), 'utf8');
const start = source.indexOf('    function buildReply(tx)');
const end = source.indexOf('    function receptionTable', start);
assert.ok(start > 0 && end > start);
class Node {
    constructor(text = '') { this.text = text; this.children = []; }
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.children.push(child); }
    get textContent() { return [this.text, ...this.children.map(c => c.textContent ?? c)].join(' '); }
}
const ctx = vm.createContext({
    makeElement: (tag, cls, text = '') => new Node(text), makeIcon: () => new Node(),
    document: { createTextNode: text => new Node(text) }, clockText: () => '12:00',
    tipify: node => node, repeatSummary: () => 'Echo heard', repeatTooltip: () => '',
});
vm.runInContext(source.slice(start, end), ctx);
for (const [state, reason, label, detail] of [
    ['not_sent', 'contact_table_full', 'Non émis', 'contacts de la radio est pleine'],
    ['unconfirmed', 'ack_missing', 'Livraison non confirmée', 'Aucun accusé'],
    ['acknowledged', 'ack_received', 'Livraison confirmée', 'Accusé de réception reçu'],
    ['partial', 'ack_missing', 'Envoi partiel', 'Aucun accusé'],
]) {
    test(`DM delivery label reflects ${state}`, () => {
        const text = ctx.buildReply({ command: { success: state === 'acknowledged', dm_delivery: { state, reason } } }).textContent;
        assert.ok(text.includes(label));
        assert.ok(text.includes(detail));
        assert.ok(!text.includes("Didn't send"));
    });
}
test('legacy failure does not claim that no packet was emitted', () => {
    const text = ctx.buildReply({ command: { success: false } }).textContent;
    assert.ok(text.includes('Delivery failed'));
    assert.ok(!text.includes("Didn't send"));
});

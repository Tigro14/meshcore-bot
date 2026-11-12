import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function viewer() {
    const source = fs.readFileSync(new URL('../../modules/web_viewer/templates/airtime.html', import.meta.url), 'utf8');
    const start = source.indexOf('    const el =');
    const end = source.lastIndexOf("    document.addEventListener('DOMContentLoaded'");
    const elements = new Map();
    const context = vm.createContext({ document: {getElementById(id) {
        if (!elements.has(id)) elements.set(id, {textContent:'',innerHTML:'',value:''});
        return elements.get(id);
    }}, Date });
    vm.runInContext(source.slice(start, end)+'\nglobalThis.Viewer = Occupancy;', context);
    return {instance:Object.create(context.Viewer.prototype), elements};
}
const data = {
    local: {rx_packets:2,rx_airtime_ms:2000,unattributed_author_airtime_ms:1000,rx_percent:0.05,reference_budget_used_percent:0.5},
    coverage:{first_observation_at:200000,last_observation_at:200001,available_seconds:3600},
    quality:{ambiguous_hops:1,unknown_hops:2,collapsed_inferred_copies:3},
    authors:[{name:'<img src=x onerror=evil()>',rx_airtime_ms:1000,received_packets:1,
        categories:{messages:{airtime_ms:1000},adverts:{airtime_ms:0},other:{airtime_ms:0}}}],
    repeaters:[{name:'<script>evil()</script>',direct_airtime_ms:1000,inferred_airtime_ms:1000,airtime_ms:2000,last_hour_percent:0.05,peak_hour_percent:0.1,packets:2}],
};
test('airtime attribution escapes RF names and keeps unassigned traffic visible',()=>{
    const {instance,elements}=viewer();
    instance.renderAnalysis(data);
    assert.doesNotMatch(elements.get('oc-authors').innerHTML, /<img/);
    assert.match(elements.get('oc-authors').innerHTML, /&lt;img/);
    assert.match(elements.get('oc-authors').innerHTML, /Auteur non identifiable/);
    assert.doesNotMatch(elements.get('oc-repeaters').innerHTML, /<script>/);
    assert.doesNotMatch(elements.get('oc-repeaters').innerHTML, /bg-success|conforme/);
});
test('empty observations are unknown, not a quiet-network zero',()=>{
    const {instance,elements}=viewer();
    instance.renderAnalysis({...data,local:{rx_packets:0,rx_airtime_ms:0,rx_percent:null},coverage:{},authors:[],repeaters:[]});
    assert.equal(elements.get('oc-analysis-rx').textContent,'N/D');
    assert.match(elements.get('oc-analysis-coverage').textContent,/ne signifie pas une absence/);
});

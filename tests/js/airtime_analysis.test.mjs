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
        if (!elements.has(id)) elements.set(id, {textContent:'',innerHTML:'',value:'',style:{}});
        return elements.get(id);
    }}, Date, getComputedStyle:()=>({getPropertyValue:()=> '#123456'}),
    Chart:class {constructor(element, options) {this.data=options.data;this.options=options.options;}} });
    vm.runInContext(source.slice(start, end)+'\nglobalThis.Viewer = Occupancy;', context);
    return {instance:Object.create(context.Viewer.prototype), elements};
}
const data = {
    local: {rx_packets:3,rx_airtime_ms:3000,unattributed_author_airtime_ms:1000,rx_percent:0.05},
    traffic:{distinct_packets:2,additional_received_copies:1,unattributed_author_packets:1,
        reconstructed_radio_airtime_ms:5000,cost_reconstructed_packets:1,
        unknown_author:{name:'Auteur non identifiable',distinct_packets:1,received_packets:1,
            reconstructed_radio_airtime_ms:null,cost_reconstructed_packets:0,
            categories:{messages:{distinct_packets:0},adverts:{distinct_packets:1},other:{distinct_packets:0}}}},
    coverage:{first_observation_at:200000,last_observation_at:200001,available_seconds:3600},
    quality:{ambiguous_hops:1,unknown_hops:2,collapsed_inferred_copies:3},
    authors:[{name:'<img src=x onerror=evil()>',distinct_packets:1,rx_airtime_ms:2000,received_packets:2,
        reconstructed_radio_airtime_ms:5000,cost_reconstructed_packets:1,
        categories:{messages:{distinct_packets:1},adverts:{distinct_packets:0},other:{distinct_packets:0}}}],
    repeaters:[{name:'<script>evil()</script>',direct_airtime_ms:1000,inferred_airtime_ms:1000,airtime_ms:2000,
        last_hour_percent:0.05,peak_hour_percent:0.1,packets:2,path_packets:2,path_packets_total:3,
        participation_percent:66.667,trend:{change_percent:null}}],
    comparison:{status:'insufficient_history'},
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
    instance.renderAnalysis({...data,local:{rx_packets:0,rx_airtime_ms:0,rx_percent:null},
        traffic:{...data.traffic,distinct_packets:0,additional_received_copies:0,
            reconstructed_radio_airtime_ms:null,cost_reconstructed_packets:0,
            unknown_author:{distinct_packets:0}},coverage:{},authors:[],repeaters:[]});
    assert.equal(elements.get('oc-analysis-rx').textContent,'N/D');
    assert.match(elements.get('oc-analysis-coverage').textContent,/ne signifie pas une absence/);
    assert.equal(elements.get('oc-analysis-cost').textContent,'N/D');
});
test('distinct traffic, radio cost and participation have separate denominators',()=>{
    const {instance,elements}=viewer();instance.renderAnalysis(data);
    assert.equal(elements.get('oc-analysis-packets').textContent,'2');
    assert.match(elements.get('oc-analysis-packet-detail').textContent,/3 réceptions · 1 copies/);
    assert.equal(elements.get('oc-analysis-cost').textContent,'5.0 s');
    assert.equal(elements.get('oc-analysis-unknown').textContent,'50 %');
    assert.equal(elements.has('oc-analysis-reference'),false);
    assert.match(elements.get('oc-repeaters').innerHTML,/2 \/ 3 paquets/);
    assert.match(elements.get('oc-authors').innerHTML,/N\/D/);
});
test('author sorting can select reconstructed cost without counting received copies',()=>{
    const {instance,elements}=viewer();
    const first={...data.authors[0],name:'Many originals',distinct_packets:10,reconstructed_radio_airtime_ms:1000};
    const second={...data.authors[0],name:'High radio cost',distinct_packets:2,reconstructed_radio_airtime_ms:9000};
    instance.renderAnalysis({...data,authors:[first,second]});
    assert.ok(elements.get('oc-authors').innerHTML.indexOf('Many originals')<elements.get('oc-authors').innerHTML.indexOf('High radio cost'));
    elements.get('oc-author-sort').value='reconstructed_radio_airtime_ms';
    instance.renderAnalysis({...data,authors:[first,second]});
    assert.ok(elements.get('oc-authors').innerHTML.indexOf('High radio cost')<elements.get('oc-authors').innerHTML.indexOf('Many originals'));
});
test('local RX/TX chart has no global 10-percent duty-cycle reference',()=>{
    const {instance}=viewer();
    instance.renderHistory({start_time:1000,end_time:2000,sample_seconds:60,
        total_rx_ms:1000,total_tx_ms:100,packets:1,buckets:[{bucket_ts:1100,rx_ms:1000,tx_ms:100}]});
    assert.equal(instance.chart.data.datasets.length,2);
    assert.deepEqual(Array.from(instance.chart.data.datasets,x=>x.label),['rx %','tx %']);
});
test('negative observed trends sort above unknown rather than treating unknown as zero',()=>{
    const {instance,elements}=viewer();
    instance.renderAnalysis(data);elements.get('oc-relay-sort').value='change_percent';
    const nodes=[{...data.repeaters[0],name:'Unknown trend'},
        {...data.repeaters[0],name:'Lower activity',trend:{change_percent:-25}}];
    instance.renderAnalysis({...data,repeaters:nodes});
    const html=elements.get('oc-repeaters').innerHTML;
    assert.ok(html.indexOf('Lower activity')<html.indexOf('Unknown trend'));
    assert.match(html,/-25 %/);
    assert.match(html,/N\/D/);
});
test('local load is not clipped to 100, missing buckets stay gaps and neighbour names are text',()=>{
    const {instance,elements}=viewer();
    instance.renderAnalysis({...data,method:{modulation_profiles:[{sf:8,bw_khz:62.5,cr_den:8}]},
        local_load:{estimated_percent:125,peak_five_minutes:{percent:160,ending_at:200000},
            peak_hour:{percent:125},neighbour_count:1,
            dominant_neighbour:{name:'<script>evil()</script>',local_rx_share_percent:95},
            buckets:[{end_time:200000,percent:125},{end_time:200300,percent:null}]}});
    assert.equal(elements.get('oc-load-mean').textContent,'125 %');
    assert.equal(elements.get('oc-load-burst').textContent,'160 %');
    assert.match(elements.get('oc-load-context').textContent,/CR 4\/8/);
    assert.equal(elements.get('oc-load-context').innerHTML,'');
    assert.equal(instance.localChart.data.datasets.length,1);
    assert.equal(instance.localChart.data.datasets[0].data[1].y,null);
    assert.equal(instance.localChart.data.datasets[0].spanGaps,false);
});
test('a five-minute burst above 10 percent is not marked as an hourly duty infringement',()=>{
    const {instance,elements}=viewer();
    instance.renderAnalysis({...data,repeaters:[{...data.repeaters[0],peak_five_minute_percent:15,
        last_hour_percent:1,peak_hour_percent:2,direct_packets:1,inferred_packets:2,local_neighbour:true}]});
    const html=elements.get('oc-repeaters').innerHTML;
    assert.match(html,/15 %/);
    assert.match(html,/1 entendues<br>2 déduites/);
    assert.match(html,/Voisin entendu directement/);
    assert.doesNotMatch(html,/text-danger/);
});

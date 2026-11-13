import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function viewer() {
    const source = fs.readFileSync(new URL('../../modules/web_viewer/templates/airtime.html', import.meta.url), 'utf8');
    const elements = new Map();
    const context = vm.createContext({document: {getElementById(id) {
        if (!elements.has(id)) elements.set(id, {textContent:'',innerHTML:'',value:'',style:{},hidden:false,
            classList:{toggle() {}},setAttribute() {}});
        return elements.get(id);
    }}, Date, AbortController, Chart:class {
        constructor(element, options) {this.data=options.data;this.options=options.options;this.type=options.type;this.plugins=options.plugins;}
        update() {}
    }});
    vm.runInContext(source.slice(source.indexOf('    const el ='),
        source.lastIndexOf("    document.addEventListener('DOMContentLoaded'"))+'\nglobalThis.Page = AirtimePage; globalThis.midnights = parisMidnights;',context);
    const instance=Object.create(context.Page.prototype);
    instance.sequence=0; instance.chart=null;
    return {instance,elements,midnights:context.midnights};
}
const categories={messages:{distinct_packets:1},adverts:{distinct_packets:0},other:{distinct_packets:0}};
const data={window:'24h',window_seconds:86400,start_time:1000,end_time:87400,
    local:{rx_bucket_seconds:300,rx_percent:0.5,rx_peak_five_minute_percent:2,rx_series:[{end_time:1300,percent:1},{end_time:1600,percent:null}]},
    coverage:{available_seconds:86400},traffic:{distinct_packets:3,unattributed_author_packets:1,
        unknown_author:{name:'Auteur non identifiable',distinct_packets:1,categories}},
    authors:[{name:'<img src=x onerror=evil()>',public_key:'a',distinct_packets:2,categories}],
    repeaters:[{name:'Old relay',public_key:'r',last_hour_percent:9,peak_hour_percent:11}],
    forwarding_model:{status:'insufficient_training'}};
const hourly={repeaters:[{name:'Relay',public_key:'r',last_hour_percent:0.1}],forwarding_model:{nodes:[]}};

test('only five rows appear in each main list; full names are escaped in all details',()=>{
    const {instance,elements}=viewer();
    const authors=Array.from({length:9},(_,i)=>({...data.authors[0],distinct_packets:i+1}));
    const repeaters=Array.from({length:8},(_,i)=>({public_key:String(i),name:'<script>evil()</script>',last_hour_percent:i+1}));
    instance.render({...data,authors},{repeaters});
    assert.equal((elements.get('airtime-duty-list').innerHTML.match(/role="meter"/g)||[]).length,5);
    assert.equal((elements.get('airtime-source-list').innerHTML.match(/class="airtime-row/g)||[]).length,5);
    assert.doesNotMatch(elements.get('airtime-duty-list').innerHTML,/<script>/);
    assert.match(elements.get('airtime-relay-details').innerHTML,/&lt;script&gt;/);
    assert.doesNotMatch(elements.get('airtime-source-details').innerHTML,/<img/);
    assert.match(elements.get('airtime-source-details').innerHTML,/Auteur non identifiable/);
    assert.match(elements.get('airtime-source-note').textContent,/33,333 %/);
});
test('duty always uses the current hour independently of the selected activity period',()=>{
    const {instance,elements}=viewer();instance.render(data,hourly);
    assert.equal(elements.get('airtime-duty').textContent,'0,1 %');
    assert.match(elements.get('airtime-relay-details').innerHTML,/11 %/);
    instance.render({...data,window:'7d',window_seconds:604800},hourly);
    assert.equal(elements.get('airtime-duty').textContent,'0,1 %');
    instance.render(data,null);
    assert.equal(elements.get('airtime-duty').textContent,'N/D');
    assert.match(elements.get('airtime-relay-details').innerHTML,/N\/D/);
    assert.doesNotMatch(elements.get('airtime-duty-list').innerHTML,/≈ 0 %|bg-success|conforme/);
});
test('scenario duty stays labelled and the per-node bar clips without hiding the actual value',()=>{
    const {instance,elements}=viewer();
    instance.render(data,{...hourly,forwarding_model:{nodes:[{public_key:'r',scenario_hour_percent:12.5}]}});
    assert.equal(elements.get('airtime-duty').textContent,'12,5 %');
    assert.match(elements.get('airtime-duty-list').innerHTML,/12,5 %.*Scénario/);
    assert.match(elements.get('airtime-duty-list').innerHTML,/width:100%/);
    assert.match(elements.get('airtime-duty-list').innerHTML,/aria-valuemax="10"/);
    assert.match(elements.get('airtime-relay-details').innerHTML,/0,1 %.*12,5 %/);
});
test('the one RX chart has real gaps, no global threshold and updates its period bounds',()=>{
    const {instance}=viewer();instance.render(data,hourly);
    assert.equal(instance.chart.data.datasets.length,1);
    assert.equal(instance.chart.data.datasets[0].data[1].y,null);
    assert.equal(instance.chart.type,'bar');
    assert.equal(instance.chart.data.datasets[0].data[0].x,1150);
    assert.equal(instance.chart.options.scales.x.min,1000);
    assert.match(instance.chart.options.scales.x.ticks.callback(1000),/^\d{2}:\d{2}$/);
    instance.render({...data,start_time:500,end_time:90000},hourly);
    assert.equal(instance.chart.options.scales.x.min,500);
    assert.equal(instance.chart.options.scales.x.max,90000);
    assert.equal(instance.chart.data.datasets[0].label,'Occupation reçue (%)');
    instance.render({...data,window:'7d'},hourly);
    assert.match(instance.chart.options.scales.x.ticks.callback(1000),/^\d{2}\/\d{2}$/);
});
test('insufficient capture never becomes a quiet-network zero; historical coverage is explicit',()=>{
    const {instance,elements}=viewer();
    instance.render({...data,coverage:{available_seconds:10},local:{rx_percent:5,rx_series:[]}},null);
    assert.equal(elements.get('airtime-rx').textContent,'N/D');
    assert.equal(elements.get('airtime-chart-wrap').hidden,true);
    assert.equal(elements.get('airtime-chart-empty').hidden,false);
    assert.match(elements.get('airtime-model-note').textContent,/insuffisants/);
    instance.render({...data,coverage:{available_seconds:3600}},hourly);
    assert.match(elements.get('airtime-rx-note').textContent,/1 h d’historique disponible/);
});
test('source order is distinct originals rather than received copies and search covers every detail tab',()=>{
    const {instance,elements}=viewer();
    const authors=[{...data.authors[0],name:'High copies',distinct_packets:1,received_packets:200},
        {...data.authors[0],name:'More originals',distinct_packets:5,received_packets:5}];
    instance.render({...data,authors},hourly);
    const html=elements.get('airtime-source-list').innerHTML;
    assert.ok(html.indexOf('More originals')<html.indexOf('High copies'));
    elements.get('airtime-search').value='MORE';instance.renderDetails();
    assert.match(elements.get('airtime-source-details').innerHTML,/More originals/);
    assert.doesNotMatch(elements.get('airtime-source-details').innerHTML,/High copies/);
});
test('model details remain separate from current-hour duty and names cannot inject markup',()=>{
    const {instance,elements}=viewer();instance.render({...data,forwarding_model:{status:'partial_scenario',
        evaluation:{modelled_packets:2,distinct_packets:3},expected_packets:4,unretrieved_packets:1,
        additional_airtime_ms:1000,nodes:[{name:'<script>evil()</script>',expected_packets:4,
            retrieved_packets:3,unretrieved_packets:1,additional_airtime_ms:1000,scenario_hour_percent:9}]}},hourly);
    assert.match(elements.get('airtime-model-note').textContent,/2 \/ 3/);
    assert.match(elements.get('airtime-model-details').innerHTML,/&lt;script&gt;/);
    assert.doesNotMatch(elements.get('airtime-model-details').innerHTML,/<script>/);
    assert.equal(elements.get('airtime-duty').textContent,'0,1 %');
});
test('recording status distinguishes disabled history from an unavailable setting',()=>{
    const {instance,elements}=viewer();instance.renderRecording({enabled:false});
    assert.equal(elements.get('airtime-recording').hidden,false);
    assert.match(elements.get('airtime-recording').textContent,/historiques/);
    instance.renderRecording(null);
    assert.match(elements.get('airtime-recording').textContent,/indisponible/);
    instance.renderRecording({enabled:true});assert.equal(elements.get('airtime-recording').hidden,true);
});
test('an hourly request failure clears current duty instead of using stale daily fields',async()=>{
    const {instance,elements}=viewer();instance.render(data,hourly);
    elements.set('airtime-period',{value:'24h'});
    instance.request=async path=>{
        if(path.endsWith('window=1h')) throw new Error('unavailable');
        return path.includes('analysis?')?data:{enabled:true};
    };
    await instance.load();
    assert.equal(elements.get('airtime-duty').textContent,'N/D');
    assert.match(elements.get('airtime-status').textContent,/Duty cycle indisponible/);
});
test('a late old-period response cannot overwrite a newer period or its controls',async()=>{
    const {instance,elements}=viewer();elements.set('airtime-period',{value:'24h'});
    let resolveOld;
    const old=new Promise(resolve=>{resolveOld=resolve;});
    instance.request=path=>path.endsWith('window=24h')?old:Promise.resolve(path.includes('analysis?')?hourly:{enabled:true});
    let rendered;
    instance.render=value=>{rendered=value;};
    const pending=instance.load();elements.get('airtime-period').value='1h';
    await instance.load();assert.equal(rendered,hourly);
    resolveOld(data);await pending;
    assert.equal(rendered,hourly);
    assert.equal(elements.get('airtime-refresh').disabled,false);
});

test('all directly heard relays are hidden by default only in the overview',()=>{
    const {instance,elements}=viewer();
    const neighbour={public_key:'first',name:'Direct neighbour'};
    const second={public_key:'second',name:'Second direct neighbour'};
    const repeaters=[{...neighbour,last_hour_percent:2},{...second,last_hour_percent:1.5},
        ...Array.from({length:6},(_,i)=>({public_key:String(i),name:'Remote '+i,last_hour_percent:1/(i+1)}))];
    const current={repeaters,local_load:{neighbours:[neighbour,second]}};
    instance.render({...data,repeaters},current);
    assert.doesNotMatch(elements.get('airtime-duty-list').innerHTML,/Direct neighbour/);
    assert.equal((elements.get('airtime-duty-list').innerHTML.match(/role="meter"/g)||[]).length,5);
    assert.equal(elements.get('airtime-duty').textContent,'1 %');
    assert.doesNotMatch(elements.get('airtime-duty-note').textContent,/Hors premiers liens/);
    assert.doesNotMatch(elements.get('airtime-duty-list').innerHTML,/Second direct neighbour/);
    assert.match(elements.get('airtime-relay-details').innerHTML,/Direct neighbour/);
    assert.equal(instance.data.repeaters.length,8);
    elements.get('airtime-show-first-link').checked=true;
    instance.renderDuty();
    assert.equal(elements.get('airtime-duty').textContent,'2 %');
    assert.match(elements.get('airtime-duty-list').innerHTML,/Direct neighbour/);
    assert.match(elements.get('airtime-duty-list').innerHTML,/Second direct neighbour/);
    instance.render({...data,repeaters},current);
    assert.equal(elements.get('airtime-duty').textContent,'2 %');
    elements.get('airtime-show-first-link').checked=false;
    instance.renderDuty();
    assert.equal(elements.get('airtime-duty').textContent,'1 %');
});
test('a highest-duty remote relay is never hidden without direct-neighbour evidence',()=>{
    const {instance,elements}=viewer();
    instance.render(data,hourly);
    assert.equal(elements.get('airtime-duty').textContent,'0,1 %');
    // Identification follows the public key, regardless of a changed display name.
    instance.render({...data,local_load:{neighbours:[{public_key:'r',name:'Renamed neighbour'}]}},hourly);
    assert.equal(elements.get('airtime-duty').textContent,'N/D');
    assert.doesNotMatch(elements.get('airtime-duty-list').innerHTML,/Relay/);
});


test('only confirmed silence gets a marker, drawn before tooltips',()=>{
    const {instance}=viewer();
    instance.render({...data,local:{...data.local,rx_series:[{start_time:1000,end_time:1300,percent:0,coverage:'complete'},
        {start_time:1300,end_time:1600,percent:null,coverage:'unknown'},
        {start_time:1600,end_time:1900,percent:1,coverage:'partial'},
        {start_time:1900,end_time:2200,percent:0,coverage:'unknown'}]}},hourly);
    const drawn=[];
    assert.equal(instance.chart.plugins[0].afterDraw,undefined);
    instance.chart.plugins[0].afterDatasetsDraw({data:instance.chart.data,
        ctx:{save(){},restore(){},fillRect(...rect){drawn.push(rect);}},
        chartArea:{left:1000,right:1900,top:0,bottom:100},scales:{x:{getPixelForValue:x=>x}}});
    assert.deepEqual(drawn,[[1001,104,298,3]]);
    assert.equal(instance.chart.data.datasets[0].data[0].y,0);
    assert.equal(instance.chart.data.datasets[0].data[1].y,null);
    instance.render({...data,local:{rx_series:[{start_time:1000,end_time:1300,percent:null}]}},null);
    assert.equal(instance.chart.options.scales.y.display,false);
});


test('hourly, daily and weekly views show their bin duration and adjust bar spacing',()=>{
    const {instance,elements}=viewer();
    for (const [window,seconds,label] of [['1h',60,'Une barre = 1 min'],['24h',900,'Une barre = 15 min'],['7d',3600,'Une barre = 1 h']]) {
        instance.render({...data,window,local:{...data.local,rx_bucket_seconds:seconds}},hourly);
        assert.equal(elements.get('airtime-resolution').textContent,label);
        assert.equal(instance.chart.data.datasets[0].barPercentage,window==='1h'?.85:.65);
    }
});


test('midnight markers use French local dates and update with the chart bounds',()=>{
    const {instance,midnights}=viewer();
    const epoch=value=>Date.parse(value)/1000;
    const start=epoch('2026-11-12T15:00:00Z'), end=epoch('2026-11-13T15:00:00Z');
    assert.deepEqual(Array.from(midnights(start,end),marker=>({...marker})),[{at:epoch('2026-11-12T23:00:00Z'),label:'13/11'}]);
    assert.equal(midnights(epoch('2026-11-13T00:00:00Z'),epoch('2026-11-13T01:00:00Z')).length,0);
    instance.render({...data,start_time:start,end_time:end},hourly);
    const lines=[],labels=[];
    instance.chart.plugins[1].beforeDatasetsDraw({scales:{x:{min:start,max:end,getPixelForValue:()=>200}},
        chartArea:{left:0,right:500,top:20,bottom:220},ctx:{save(){},restore(){},setLineDash(){},beginPath(){},
            moveTo(...point){lines.push(point);},lineTo(...point){lines.push(point);},stroke(){},
            measureText:()=>({width:30}),fillText(...text){labels.push(text);}}});
    assert.deepEqual(lines,[[200,20],[200,220]]);
    assert.deepEqual(labels,[['13/11',204,14]]);
});
test('midnight markers follow both French daylight-saving transitions',()=>{
    const {midnights}=viewer();const epoch=value=>Date.parse(value)/1000;
    const spring=midnights(epoch('2026-03-28T22:00:00Z'),epoch('2026-03-30T00:00:00Z'));
    assert.equal(spring.length,2);assert.equal(spring[1].at-spring[0].at,23*3600);
    const autumn=midnights(epoch('2026-10-24T21:00:00Z'),epoch('2026-10-26T00:00:00Z'));
    assert.equal(autumn.length,2);assert.equal(autumn[1].at-autumn[0].at,25*3600);
});

test('midnight is a mandatory X-axis tick and nearby labels do not overlap it',()=>{
    const {instance}=viewer();instance.render(data,hourly);
    const epoch=value=>Date.parse(value)/1000;
    const midnight=epoch('2026-11-12T23:00:00Z');
    const scale={min:midnight-10*3600,max:midnight+14*3600,width:1000,
        ticks:[{value:midnight-10*3600},{value:midnight-300},{value:midnight+600},{value:midnight+14*3600}]};
    const axis=instance.chart.options.scales.x;
    axis.afterBuildTicks(scale);
    assert.deepEqual(Array.from(scale.ticks,tick=>tick.value),[scale.min,midnight,scale.max]);
    assert.equal(scale.ticks[1].major,true);
    assert.equal(axis.ticks.autoSkip,false);
    assert.equal(axis.ticks.callback(midnight),'00:00');
    instance.render({...data,window:'7d'},hourly);
    assert.equal(axis.ticks.callback(midnight),'00:00');
    const noMidnight={min:midnight+3600,max:midnight+7200,width:300,ticks:[{value:midnight+3600}]};
    axis.afterBuildTicks(noMidnight);
    assert.equal(noMidnight.ticks.length,1);
});

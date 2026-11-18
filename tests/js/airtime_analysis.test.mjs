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
const recent={rx_percent:1.5,rx_packets_per_minute:.6,rx_mean_frame_bytes:63.75};
const hourly={local:{rx_last_five_minutes:recent},repeaters:[{name:'Relay',public_key:'r',last_hour_percent:0.1}],forwarding_model:{nodes:[]}};

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
test('the combined chart has real gaps, no global threshold and updates its period bounds',()=>{
    const {instance}=viewer();instance.render(data,hourly);
    assert.equal(instance.chart.data.datasets.length,2);
    assert.equal(instance.chart.data.datasets[0].data[1].y,null);
    assert.equal(instance.chart.type,'bar');
    assert.equal(instance.chart.data.datasets[0].data[0].x,1150);
    assert.equal(instance.chart.options.scales.x.min,1000);
    assert.match(instance.chart.options.scales.x.ticks.callback(1000),/^\d{2}:\d{2}$/);
    instance.render({...data,start_time:500,end_time:90000},hourly);
    assert.equal(instance.chart.options.scales.x.min,500);
    assert.equal(instance.chart.options.scales.x.max,90000);
    assert.equal(instance.chart.data.datasets[0].label,'Occupation du canal');
    instance.render({...data,window:'7d'},hourly);
    assert.match(instance.chart.options.scales.x.ticks.callback(1000),/^\d{2}:\d{2}$/);
});

test('packet line shares bin centres with bars and its tooltip shows units and mean size',()=>{
    const {instance}=viewer();
    const series=[{start_time:1000,end_time:1300,percent:2,rx_packets_per_minute:.4,rx_mean_frame_bytes:63.75},
        {start_time:1300,end_time:1600,percent:null,rx_packets_per_minute:null,rx_mean_frame_bytes:null},
        {start_time:1600,end_time:1900,percent:0,rx_packets_per_minute:0,rx_mean_frame_bytes:null}];
    instance.render({...data,local:{...data.local,rx_series:series}},hourly);
    const [bars,line]=instance.chart.data.datasets;
    assert.equal(bars.type,'bar');assert.equal(line.type,'line');
    assert.equal(line.yAxisID,'packets');assert.equal(line.spanGaps,false);
    assert.ok(line.order<bars.order); // Chart.js draws the lower order last, above bars.
    assert.deepEqual(Array.from(line.data,point=>point.x),Array.from(bars.data,point=>point.x));
    assert.deepEqual(Array.from(line.data,point=>point.y),[.4,null,0]);
    const callbacks=instance.chart.options.plugins.tooltip.callbacks;
    assert.equal(callbacks.label({dataset:bars,parsed:{y:2}}),'Occupation : 2 %');
    assert.equal(callbacks.label({dataset:line,parsed:{y:.4}}),'Paquets/min : 0,4');
    assert.equal(callbacks.afterBody([{raw:line.data[0]}]),'Taille moyenne : 63,8 octets');
    assert.equal(callbacks.afterBody([{raw:line.data[2]}]),'');
    assert.equal(instance.chart.options.scales.packets.position,'right');
    assert.equal(instance.chart.options.plugins.legend.display,false);
    instance.render({...data,window:'1h',local:{...data.local,rx_series:[{...series[0],rx_packets_per_minute:5}]}},hourly);
    assert.equal(instance.chart.data.datasets[1].data[0].y,5);
    instance.render(data,hourly);
    assert.equal(instance.chart.options.scales.packets.display,false);
});
test('insufficient capture never becomes a quiet-network zero; historical coverage is explicit',()=>{
    const {instance,elements}=viewer();
    instance.render({...data,coverage:{available_seconds:10},local:{rx_percent:5,rx_series:[]}},null);
    assert.equal(elements.get('airtime-rx').textContent,'N/D');
    assert.equal(elements.get('airtime-chart-wrap').hidden,true);
    assert.equal(elements.get('airtime-chart-empty').hidden,false);
    assert.match(elements.get('airtime-model-note').textContent,/insuffisants/);
    instance.render({...data,coverage:{available_seconds:3600}},hourly);
    assert.equal(elements.get('airtime-rx-note').textContent,'Moyenne sur 1 h disponibles');
});
test('source order falls back to distinct originals when radio costs are unavailable and search covers every detail tab',()=>{
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
    assert.equal(elements.get('airtime-rx').textContent,'0,5 %');
    assert.equal(elements.get('airtime-packet-rate').textContent,'N/D');
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
    axis.afterBuildTicks(scale);
    assert.deepEqual(Array.from(scale.ticks,tick=>tick.value),[midnight]);
    const noMidnight={min:midnight+3600,max:midnight+7200,width:300,ticks:[{value:midnight+3600}]};
    axis.afterBuildTicks(noMidnight);
    assert.equal(noMidnight.ticks.length,1);
});


test('occupation and packet averages follow the selected period while duty stays hourly',()=>{
    const {instance,elements}=viewer();
    const peakAt=Date.parse('2026-11-14T08:34:00Z')/1000;
    instance.render({...data,local:{...data.local,rx_packets_per_minute:9,rx_mean_frame_bytes:150,
        rx_peak_five_minute_ending_at:peakAt}},hourly);
    assert.equal(elements.get('airtime-rx').textContent,'0,5 %');
    assert.equal(elements.get('airtime-rx-note').textContent,'Moyenne sur 24 heures');
    assert.equal(elements.get('airtime-peak-note').textContent,'5 min se terminant le 14/11 à 09:34');
    assert.equal(elements.get('airtime-packet-rate').textContent,'9');
    assert.equal(elements.get('airtime-packet-size').textContent,'Taille moyenne : 150 octets');
    instance.render({...data,window:'7d',window_seconds:604800,coverage:{available_seconds:604800},
        local:{...data.local,rx_percent:.2,rx_packets_per_minute:.8,rx_mean_frame_bytes:63.75,
            rx_peak_five_minute_percent:4,rx_peak_five_minute_ending_at:peakAt-86400}},hourly);
    assert.equal(elements.get('airtime-rx').textContent,'0,2 %');
    assert.equal(elements.get('airtime-rx-note').textContent,'Moyenne sur 7 jours');
    assert.equal(elements.get('airtime-packet-rate').textContent,'0,8');
    assert.equal(elements.get('airtime-packet-size').textContent,'Taille moyenne : 63,8 octets');
    assert.equal(elements.get('airtime-duty').textContent,'0,1 %');
    assert.equal(elements.get('airtime-peak').textContent,'4 %');
    assert.equal(elements.get('airtime-peak-note').textContent,'5 min se terminant le 13/11 à 09:34');
    instance.render({...data,local:{...data.local,rx_percent:0,rx_packets_per_minute:0,rx_mean_frame_bytes:null}},hourly);
    assert.equal(elements.get('airtime-rx').textContent,'0 %');
    assert.equal(elements.get('airtime-packet-rate').textContent,'0');
    assert.equal(elements.get('airtime-packet-size').textContent,'Taille moyenne : N/D');
    instance.render({...data,local:{...data.local,rx_percent:null,rx_packets_per_minute:null,rx_mean_frame_bytes:null}},hourly);
    assert.equal(elements.get('airtime-rx').textContent,'N/D');
    assert.equal(elements.get('airtime-packet-rate').textContent,'N/D');
    instance.render(data,null);
    assert.equal(elements.get('airtime-rx').textContent,'0,5 %');
    assert.equal(elements.get('airtime-packet-rate').textContent,'N/D');
    assert.equal(elements.get('airtime-peak-note').textContent,'Pic sur 5 minutes indisponible');
    instance.render({...data,coverage:{available_seconds:10},local:{...data.local,rx_packets_per_minute:6}},hourly);
    assert.equal(elements.get('airtime-rx').textContent,'N/D');
    assert.equal(elements.get('airtime-packet-rate').textContent,'N/D');
    instance.render({...data,local:{...data.local,rx_packets_per_minute:.000099}},hourly);
    assert.equal(elements.get('airtime-packet-rate').textContent,'< 0,001');
});

test('source ranking uses reconstructed radio cost rather than message or received-copy counts',()=>{
    const {instance,elements}=viewer();
    const authors=[{...data.authors[0],name:'Many small',distinct_packets:20,reconstructed_radio_airtime_ms:500},
        {...data.authors[0],name:'Few costly',distinct_packets:2,reconstructed_radio_airtime_ms:2000},
        {...data.authors[0],name:'Unknown cost',distinct_packets:99,reconstructed_radio_airtime_ms:null}];
    instance.render({...data,authors},hourly);
    const list=elements.get('airtime-source-list').innerHTML;
    assert.ok(list.indexOf('Few costly')<list.indexOf('Many small'));
    assert.ok(list.indexOf('Many small')<list.indexOf('Unknown cost'));
    assert.match(list,/2 s/);
    assert.match(list,/Unknown cost.*N\/D/);
});

test('copy summary and tooltip use weighted airtime without adding a graph dataset',()=>{
    const {instance,elements}=viewer();
    const receptions={received_packets:100,additional_received_airtime_percent:68,status:'available',comparison:{status:'insufficient_history'}};
    const series=[{start_time:1000,end_time:1300,percent:2,rx_packets_per_minute:1,distinct_packets:4,
        additional_received_airtime_percent:68,rx_mean_frame_bytes:80}];
    instance.render({...data,receptions,local:{...data.local,rx_series:series}},hourly);
    assert.match(elements.get('airtime-volume').textContent,/3 paquets distincts · 100 réceptions · 68 %/);
    assert.equal(instance.chart.data.datasets.length,2);
    const text=instance.chart.options.plugins.tooltip.callbacks.afterBody([{raw:instance.chart.data.datasets[0].data[0]}]);
    assert.match(text,/Paquets distincts : 4/);
    assert.match(text,/copies supplémentaires : 68 %/);
    instance.render({...data,receptions:{...receptions,status:'truncated',additional_received_airtime_percent:null}},hourly);
    assert.doesNotMatch(elements.get('airtime-volume').textContent,/%/);
    assert.match(elements.get('airtime-volume').textContent,/partielles/);
});

test('the existing detail table switches roles without mixing author and receiver-side costs',()=>{
    const {instance,elements}=viewer();
    const roles={entry_relays:[{name:'First relay',received_packets:4,rx_airtime_ms:2500,
        categories:{messages:2,adverts:2,other:0}}],emitters:[{name:'Last relay',received_packets:4,rx_airtime_ms:2500,
        categories:{messages:2,adverts:2,other:0}}],unknown_emitter:{name:'Unknown emitter',received_packets:1,
        rx_airtime_ms:200,prefix:null,identification:'unknown',categories:{messages:0,adverts:0,other:1}}};
    instance.render({...data,receptions:{roles}},hourly);
    elements.get('airtime-role').value='entry_relays';instance.renderDetails();
    assert.match(elements.get('airtime-source-details').innerHTML,/First relay/);
    assert.doesNotMatch(elements.get('airtime-source-details').innerHTML,/Last relay|Auteur non identifiable/);
    assert.equal(elements.get('airtime-cost-label').textContent,'Airtime reçu');
    assert.equal(elements.get('airtime-count-label').textContent,'Copies reçues');
    elements.get('airtime-role').value='emitters';instance.renderDetails();
    assert.match(elements.get('airtime-source-details').innerHTML,/Last relay/);
    assert.match(elements.get('airtime-source-details').innerHTML,/Unknown emitter/);
    assert.doesNotMatch(elements.get('airtime-source-details').innerHTML,/Préfixe inconnu/);
    assert.doesNotMatch(elements.get('airtime-source-details').innerHTML,/First relay/);
});

test('evolution states incomplete observation rather than inventing a trend',()=>{
    const {instance,elements}=viewer();
    const comparison={status:'incomplete_observation',previous:{additional_received_airtime_percent:40},additional_airtime_change_points:null};
    instance.render({...data,receptions:{comparison}},hourly);
    assert.match(elements.get('airtime-repetition-trend').textContent,/non comparable/);
    instance.render({...data,receptions:{comparison:{...comparison,status:'comparable',additional_airtime_change_points:10,
        distinct_packets_change_percent:2,received_airtime_change_percent:30}}},hourly);
    assert.match(elements.get('airtime-repetition-trend').textContent,/airtime reçu \+30 %.*distincts \+2 %.*copies \+10 points/);
});

const ledgers={enabled:true,share_window_days:1,period_start_ts:1000,period_capacity_ms:86400000,
    period_totals:{senders:{airtime_ms:1000,capacity_percent:0,packets:3,stations:7,advert_percent:40,last_seen:2000},
        emitters:null,relays:{airtime_ms:null,packets:0,stations:0,advert_percent:null,last_seen:null}},
    top_senders:[{sender:'a'.repeat(64),name:'Alice',short:'aaaaaa',airtime_ms:600,packets:2,advert_percent:50,last_seen:1900,capacity_percent:0.7},
        {sender:'~',unattributed:true,airtime_ms:400,packets:1,advert_percent:null,last_seen:1800,capacity_percent:0}],
    top_emitters:[{emitter:'6201',name:null,short:'6201',airtime_ms:500,packets:4,advert_percent:null,last_seen:1700,
        capacity_percent:0.5,merged_prefixes:[{label:'62010a'}]}],
    top_relays:[{relay:'62010a',name:'Baliz',short:'62010a',airtime_ms:300,packets:2,advert_percent:10,last_seen:1600,capacity_percent:0.3}]};

test('each by-airtime ledger leads with its whole-ledger recap and states the span it answers for',()=>{
    const {instance,elements}=viewer();
    instance.renderLedgers(ledgers);
    assert.match(elements.get('airtime-ledger-span').textContent,
        /Les tableaux répondent pour : .*→ maintenant \(24 h\), calés sur des jours UTC entiers/);
    const authors=elements.get('airtime-ledger-rows').innerHTML;
    assert.match(authors,/<tr class="airtime-totals-row"><td><strong>Tout le registre<\/strong>/);
    assert.match(authors,/7 entrées · 2 listées/);
    assert.match(authors,/Tout le registre[\s\S]*?<td>1 s 0 %<\/td><td>3<\/td><td>40 %<\/td>/);
    assert.match(authors,/Alice <code class="small">aaaaaa<\/code>/);
    assert.match(authors,/0,6 s 0,7 %/);
    assert.match(authors,/Non attribué/);
    assert.match(authors,/Contenu sans identité émettrice sur le fil : messages, requêtes, inconnus\. Chaque copie compte\./);
    assert.match(elements.get('airtime-ledger-note').textContent,/Qui a écrit le contenu/);
});

test('the ledger selector switches registers without refetch, keeps folds and unknowns unmeasured',()=>{
    const {instance,elements}=viewer();
    instance.renderLedgers(ledgers);
    elements.get('airtime-ledger-role').value='emitters';instance.renderLedgerTable();
    const emitters=elements.get('airtime-ledger-rows').innerHTML;
    assert.doesNotMatch(emitters,/airtime-totals-row/);
    assert.match(emitters,/<code title="6201">6201<\/code>/);
    assert.match(emitters,/Inclus les copies 62010a : même relais, adresse tronquée à 2 octets/);
    assert.match(emitters,/0,5 s 0,5 %<\/td><td>4<\/td><td>N\/D<\/td>/);
    assert.match(elements.get('airtime-ledger-note').textContent,/dernier relais du chemin/);
    elements.get('airtime-ledger-role').value='entry_relays';instance.renderLedgerTable();
    const relays=elements.get('airtime-ledger-rows').innerHTML;
    assert.match(relays,/Baliz <code class="small">62010a<\/code>/);
    assert.doesNotMatch(relays,/airtime-totals-row/);
    assert.match(elements.get('airtime-ledger-note').textContent,/ni l’auteur ni l’antenne émettrice/);
    instance.renderLedgers({enabled:true});
    assert.match(elements.get('airtime-ledger-rows').innerHTML,/Baliz/);
    elements.get('airtime-ledger-role').value='authors';
    instance.renderLedgers({...ledgers,top_senders:[],period_totals:{...ledgers.period_totals,senders:{airtime_ms:null}}});
    assert.match(elements.get('airtime-ledger-rows').innerHTML,/Aucune entrée enregistrée pour cette période/);
});

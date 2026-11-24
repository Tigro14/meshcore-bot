
window.createMeshEntryView = function(map) {
    const layer = L.layerGroup().addTo(map);
    const status = document.getElementById('entry-status');
    const country = document.getElementById('entry-country');
    const days = document.getElementById('filter-edge-days');
    const unscoped = document.getElementById('entry-unscoped');
    const globalPanel = document.getElementById('entry-global');
    const packetType = document.getElementById('entry-type');
    const relayPanels = new Map();
    let generation = 0;
    function renderHops(details, n) {
        n.arrival_hops.forEach(bin=>{
            const row=document.createElement('div');row.className='mb-3';
            const caption=document.createElement('div');caption.className='small hop-caption';
            const hops=document.createElement('span');hops.textContent=`${bin.hops} saut${bin.hops>1?'s':''}`;
            const count=document.createElement('span');count.textContent=`${bin.count} paquet${bin.count>1?'s':''} · ${Math.round(100*bin.count/n.count)} %`;
            caption.append(hops,count);
            const track=document.createElement('div');track.className='progress';track.style.height='12px';
            const bar=document.createElement('div');bar.className='progress-bar bg-warning';bar.style.width=(100*bin.count/n.count)+'%';
            track.appendChild(bar);row.append(caption,track);details.appendChild(row);
        });
    }
    const located = n => Number.isFinite(n.latitude) && Number.isFinite(n.longitude) && Math.abs(n.latitude)<=90 && Math.abs(n.longitude)<=180 && !(n.latitude===0 && n.longitude===0);
    const point = n => [n.latitude,n.longitude];
    const label = text => {const el=document.createElement('span');el.textContent=text;return el;};
    let controller;
    async function load(options) {
        if (controller) controller.abort();
        controller = new AbortController();
        const current = ++generation;
        status.textContent='Chargement des observations…';
        layer.clearLayers();
        relayPanels.clear();
        globalPanel.textContent='Chargement…';
        document.getElementById('entry-ranking').replaceChildren();
        try {
            const resp=await fetch('/api/mesh/entry-points?'+new URLSearchParams({days:days.value || '0',min_packets:options.minPackets,evidence:options.evidence,country:country.value,unscoped:unscoped.checked?'1':'0',packet_type:packetType.value}), {method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},body:JSON.stringify({target_keys:options.targetKeys}),signal:controller.signal});
            if(!resp.ok) throw new Error('Chargement impossible');
            const data=await resp.json();
            if(current!==generation) return;
            globalPanel.replaceChildren();
            if(data.global_summary.count){
                const total=document.createElement('p');total.className='fw-semibold';
                total.textContent=`${data.global_summary.count} paquets distincts`;
                globalPanel.appendChild(total);renderHops(globalPanel,data.global_summary);
            } else globalPanel.textContent='Aucun passage identifiable avec ces filtres.';
            const selected=country.value;
            country.replaceChildren(new Option('Tous les pays étrangers',''));
            data.countries.forEach(c=>country.add(new Option(c,c)));
            if(selected && !data.countries.includes(selected))country.add(new Option(selected,selected));
            country.value=selected;
            const nodes=new Map();const bounds=[];
            data.edges.forEach(e=>{
                nodes.set(e.source.public_key,e.source);nodes.set(e.target.public_key,e.target);
                if(!located(e.source)||!located(e.target))return;
                const a=point(e.source),b=point(e.target);
                const shortOnly=e.reliable_count===0;
                const evidence=e.inferred_count>0?` · ${e.inferred_count} paquet(s) : arrivée probable` : shortOnly?(e.count===1?' · 1 octet, un seul paquet : attribution fragile':' · 1 octet uniquement : attribution à confirmer'):` · ${e.multi_byte_count} paquet(s) à identifiants de 2 ou 3 octets`;
                L.polyline([a,b],{color:'#d97706',weight:Math.min(7,1+Math.log2(e.count+1)),opacity:shortOnly?0.45:0.75,dashArray:shortOnly?'6 6':null}).bindPopup(label(`${e.source.name} → ${e.target.name} : ${e.count} paquets distincts${evidence}`)).addTo(layer);
                const pa=map.project(a,8),pb=map.project(b,8);
                const angle=Math.atan2(pb.y-pa.y,pb.x-pa.x)*180/Math.PI;
                L.marker([(a[0]+b[0])/2,(a[1]+b[1])/2],{interactive:false,icon:L.divIcon({className:'entry-arrow',html:`<span style="display:block;transform:rotate(${angle}deg)">➤</span>`,iconSize:[20,20],iconAnchor:[10,10]})}).addTo(layer);
            });
            const targets=new Map(data.ranking.map(n=>[n.public_key,n]));
            nodes.forEach(n=>{
                if(!located(n))return;
                const t=targets.get(n.public_key);bounds.push(point(n));
                const marker=L.circleMarker(point(n),{radius:t?Math.min(17,6+Math.log2(t.count+1)):5,color:'#fff',weight:1,fillColor:t?'#f59e0b':'#1687ec',fillOpacity:0.9}).bindPopup(label(`${n.name} (${n.country})${t?' — '+t.count+' paquets distincts':''}`)).on('click',()=>{if(t){const panel=relayPanels.get(t.public_key);if(panel)panel.open=true;}}).addTo(layer);
                if(options.labels)marker.bindTooltip(label(n.name),{permanent:true,direction:'top'});
            });
            map.invalidateSize();
            if(options.fit && bounds.length)map.fitBounds(bounds,{padding:[25,25],maxZoom:10});
            const ranking=document.getElementById('entry-ranking');
            data.ranking.forEach((n,i)=>{
                const panel=document.createElement('details');panel.className='border rounded mb-2';
                const button=document.createElement('summary');button.className='p-2 entry-row';
                const heading=document.createElement('span');heading.className='entry-heading';heading.textContent=`${i+1}. ${n.name}`;
                const total=document.createElement('span');total.className='badge bg-secondary entry-count';total.textContent=`${n.count} paquet${n.count>1?'s':''}`;
                const meta=document.createElement('span');meta.className='entry-meta';
                const origins=document.createElement('span');origins.textContent='Depuis : '+n.origins.map(c=>c==='United Kingdom'?'Royaume-Uni':c).join(', ');
                const last=document.createElement('span');last.className='d-block';last.textContent='Dernier passage : '+new Date(n.last_seen).toLocaleString('fr-FR')+(located(n)?'':' · position inconnue');
                meta.append(origins,last);button.append(heading,total,meta);
                const content=document.createElement('div');content.className='border-top p-3';
                renderHops(content,n);
                panel.append(button,content);ranking.appendChild(panel);relayPanels.set(n.public_key,panel);
                panel.addEventListener('toggle',()=>{if(panel.open&&located(n))map.setView(point(n),11);});
            });
            if(!data.ranking.length) ranking.appendChild(label('Aucun passage identifiable sur cette période.'));
            document.getElementById('label-min-obs').textContent=`≥ ${options.minPackets} paquets · ${data.edges.length} liaisons`;
            status.textContent=`${data.ranking.length} relais d’entrée · ${data.edges.length} liaisons · ${data.scanned} paquets examinés`;

        } catch(e) {if(current===generation){status.textContent='Impossible de charger les points d’entrée. Réessaie plus tard.';globalPanel.textContent='Données indisponibles.';}}
    }
    return {load, clear() {generation++;if(controller)controller.abort();layer.clearLayers();relayPanels.clear();}};
};

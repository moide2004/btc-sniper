// Live par polling (§6). Pas de websocket entrant : le navigateur interroge des
// endpoints JSON. Bandeau ⚠ si heartbeat worker > 120 s.
function fmtUtcMs(ms){ if(ms==null) return "—";
  return new Date(ms).toISOString().replace("T"," ").replace(".000Z"," UTC"); }
function fmtAge(s){ if(s==null) return "—";
  if(s<60) return Math.round(s)+" s"; if(s<3600) return Math.round(s/60)+" min";
  return Math.round(s/3600)+" h"; }

let healthFails = 0;
async function getJSON(url){
  const r = await fetch(url,{credentials:"same-origin"});
  if(r.status===401){ window.location="/login"; return null; }
  if(!r.ok) throw new Error(url+" → "+r.status);
  return r.json();
}

async function refreshHealth(){
  try{
    const h = await getJSON("/api/health"); if(!h) return; healthFails=0;
    document.getElementById("src").textContent = h.source || "—";
    document.getElementById("age").textContent = fmtAge(h.heartbeat_age_s);
    document.getElementById("cycle").textContent = h.worker ? h.worker.cycle : "—";
    document.getElementById("unread").textContent = h.unread_events;
    document.getElementById("gen").textContent = h.generated_at;
    const box = document.getElementById("assets");
    box.innerHTML = Object.entries(h.par_actif||{}).map(([sym,d])=>
      `<div class="row"><span class="k">${sym}</span>
       <span>${(d.bars_1m||0).toLocaleString("fr-FR")} bougies · dernière ${fmtUtcMs(d.last_1m_open_ms)}</span></div>`
    ).join("");
    const band=document.getElementById("band"), live=document.getElementById("livedot"),
          ws=document.getElementById("wstatus");
    if(h.worker_stale){
      band.classList.add("warn"); band.classList.remove("live");
      live.querySelector("#livetxt").textContent="⚠ worker en retard";
      ws.textContent="⚠ "+(h.worker?h.worker.status:"absent");
    }else{
      band.classList.remove("warn"); band.classList.add("live");
      live.querySelector("#livetxt").textContent="live";
      ws.textContent="✓ "+(h.worker?h.worker.status:"ok");
    }
  }catch(e){
    healthFails+=1; if(healthFails<2) return;
    const band=document.getElementById("band");
    band.classList.add("warn"); band.classList.remove("live");
    document.getElementById("livedot").querySelector("#livetxt").textContent="⚠ hors ligne";
  }
}

async function refreshEvents(){
  try{
    const d = await getJSON("/api/evenements"); if(!d) return;
    const ul=document.getElementById("events");
    if(!d.events.length){ ul.innerHTML='<li class="muted">Aucun événement.</li>'; return; }
    ul.innerHTML = d.events.map(e=>
      `<li><b>${e.ts_utc}</b> · [${e.level}] ${e.kind} — ${e.message}</li>`).join("");
  }catch(e){}
}
async function refreshLivre(){ try{ await getJSON("/api/livre"); }catch(e){} }

refreshHealth(); refreshEvents(); refreshLivre();
setInterval(refreshLivre, 5000);
setInterval(()=>{ refreshHealth(); refreshEvents(); }, 15000);

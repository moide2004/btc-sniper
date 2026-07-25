// Live par polling (§6). Pas de websocket entrant : le navigateur interroge des
// endpoints JSON. 4 vues (Santé · Matrice · Tickets · Livre). ⚠ si worker en retard.
const TF_ORDER = ["15m", "30m", "1h", "4h", "12h", "1D"];

function fmtUtcMs(ms){ if(ms==null) return "—";
  return new Date(ms).toISOString().replace("T"," ").replace(".000Z"," UTC"); }
function fmtAge(s){ if(s==null) return "—";
  if(s<60) return Math.round(s)+" s"; if(s<3600) return Math.round(s/60)+" min";
  return Math.round(s/3600)+" h"; }
function pct(p,d=1){ return p==null? "—" : (100*p).toFixed(d)+" %"; }
function num(x,d=2){ return x==null? "—" : Number(x).toFixed(d); }
function signed(x,d=2){ if(x==null) return "—"; const v=Number(x); return (v>=0?"+":"")+v.toFixed(d); }
function evClass(x){ return x==null? "" : (x>0?"good":"bad"); }
function wfClass(s){ return s? "wf-"+s : ""; }
function isSolide(c){ return c && c.ev_prudent_taker!=null && c.ev_prudent_taker>0
  && (c.n||0)>=200 && !["overfit","disqualifie"].includes((c.wf||{}).status); }

let healthFails = 0;
async function getJSON(url){
  const r = await fetch(url,{credentials:"same-origin"});
  if(r.status===401){ window.location="/login"; return null; }
  if(!r.ok) throw new Error(url+" → "+r.status);
  return r.json();
}

// ---- Onglets ---------------------------------------------------------------
document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
  document.querySelectorAll(".view").forEach(x=>x.classList.remove("active"));
  t.classList.add("active");
  document.getElementById("v-"+t.dataset.view).classList.add("active");
}));

// ---- Vue Santé -------------------------------------------------------------
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

// ---- Vue Matrice (§3) ------------------------------------------------------
async function refreshMatrice(){
  try{
    const d = await getJSON("/api/probas"); if(!d) return;
    document.getElementById("rrlive").textContent = num(d.rr_live,1);
    const c = d.corr_btc_eth;
    document.getElementById("corr").textContent = c==null? "—" : num(c,2);
    document.getElementById("corr2").textContent = c==null? "—" : num(c,2);
    const cases = (d.cases||[]).slice().sort((a,b)=>
      a.symbol.localeCompare(b.symbol) ||
      TF_ORDER.indexOf(a.timeframe)-TF_ORDER.indexOf(b.timeframe) ||
      a.direction.localeCompare(b.direction));
    const body=document.getElementById("mat-body");
    if(!cases.length){ body.innerHTML='<tr><td class="muted" colspan="10">Matrice vide — lancer <code>jobs/daily_update.py</code>.</td></tr>'; }
    else{
      body.innerHTML = cases.map(c=>{
        const wf=c.wf||{}, w=c.wilson||[null,null], sol=isSolide(c);
        return `<tr>
          <td><span class="pill ${sol?'solide':'spec'}">${sol?'solide':'spéc.'}</span>
              ${c.symbol} · ${c.timeframe} · <span class="${c.direction}">${c.direction}</span></td>
          <td>${c.n==null?'—':c.n}</td><td>${pct(c.p_hat)}</td>
          <td>${w[0]==null?'—':pct(w[0],0)+'–'+pct(w[1],0)}</td>
          <td>${pct(c.p_prudent)}</td>
          <td class="${evClass(c.ev_prudent_taker)}">${signed(c.ev_prudent_taker)}</td>
          <td class="${evClass(c.ev_prudent_maker)}">${signed(c.ev_prudent_maker)}</td>
          <td>${c.k_max==null?'—':c.k_max}</td><td>${num(c.cvar99_r)}</td>
          <td class="${wfClass(wf.status)}">${wf.status||'—'}${wf.retention!=null?' ('+num(wf.retention,2)+')':''}</td>
        </tr>`;
      }).join("");
    }
    document.getElementById("b-mat").textContent = cases.filter(isSolide).length;
  }catch(e){}
}

// ---- Vue Tickets -----------------------------------------------------------
async function refreshTickets(){
  try{
    const d = await getJSON("/api/tickets"); if(!d) return;
    const tks=d.tickets||[];
    document.getElementById("b-tk").textContent = tks.length;
    const grid=document.getElementById("tk-grid");
    if(!tks.length){ grid.innerHTML='<div class="muted">Aucun ticket. Un ticket est émis dès qu\'un setup §2 se valide à la clôture d\'une bougie d\'analyse.</div>'; }
    else{
      grid.innerHTML = tks.map(t=>{
        const p=t.proba||{}, s=t.sizing||{}, sol=p.solide;
        return `<div class="tk">
          <h3><span class="${t.direction}">${(t.direction||'').toUpperCase()}</span>
              ${t.symbol} · ${t.timeframe}
              <span class="pill ${sol?'solide':'spec'}" style="margin-left:auto">${p.annotation||'—'}</span></h3>
          <div class="lv"><span class="k">Entrée${t.entry_is_proxy?' (proxy)':''}</span><span>${num(t.entry_ref)}</span></div>
          <div class="lv"><span class="k">SL / TP</span><span>${num(t.sl)} / ${num(t.tp)}</span></div>
          <div class="lv"><span class="k">Taille (risque)</span><span>${num(s.size_units,4)} · ${num(s.risk_usd,0)}$ (${num(s.risk_pct_capital,2)}%)</span></div>
          <div class="lv"><span class="k">p̂ · p prudent</span><span>${pct(p.p_hat)} · ${pct(p.p_prudent)} <span class="k">(n=${p.n??'—'})</span></span></div>
          <div class="lv"><span class="k">EV prud. (taker)</span><span class="${evClass(p.ev_prudent_taker)}">${signed(p.ev_prudent_taker)}</span></div>
          <div class="lv"><span class="k">walk-forward</span><span class="${wfClass(p.wf_status)}">${p.wf_status||'—'}</span></div>
          <div class="lv"><span class="k">${t.ts_utc}</span><span></span></div>
        </div>`;
      }).join("");
    }
    const nsol=tks.filter(t=>(t.proba||{}).solide).length;
    document.getElementById("tk-counts").textContent = nsol+" / "+(tks.length-nsol);
  }catch(e){}
}

// ---- Vue Livre (§5.4 + paper trading P4) -----------------------------------
async function refreshLivre(){
  try{
    const d = await getJSON("/api/livre"); if(!d) return;
    document.getElementById("risk-open").textContent = num(d.risque_ouvert_pct,2)+" %";
    document.getElementById("risk-cap").textContent = num(d.plafond_pct,1)+" %";
    const bt = d.backtest;
    document.getElementById("equity").textContent = bt&&bt.portfolio ?
      num(bt.portfolio.equity_usd,0)+" $ ("+signed(bt.portfolio.pnl_usd,0)+" $)" : "—";

    // Positions ouvertes.
    const pb=document.getElementById("pos-body"), pos=d.positions||[];
    pb.innerHTML = pos.length ? pos.map(p=>
      `<tr><td>${p.symbol} · ${p.timeframe} · <span class="${p.direction}">${p.direction}</span></td>
       <td>${num(p.entry)}</td><td>${num(p.sl)}</td><td>${num(p.tp)}</td>
       <td>${p.be_done?"✓":"—"}</td><td>${num(p.risk_usd,0)}</td></tr>`).join("")
      : '<tr><td class="muted" colspan="6">Aucune position ouverte.</td></tr>';

    // Bilan récapitulatif P5.
    const bl=bt&&bt.bilan;
    document.getElementById("bilan").innerHTML = bl ?
      `<span class="pill solide">${bl["go"]||0} go</span>
       <span class="pill spec" style="margin-left:6px">${bl["no-go"]||0} no-go</span>
       <span class="pill" style="margin-left:6px;background:#2a2f3a;color:var(--dim)">${bl["insuffisant"]||0} insuffisants</span>
       <span class="muted" style="margin-left:10px">recalculé ${bt.generated_at||""}</span>` : "—";

    // Backtest par flux + verdict (5 critères en infobulle).
    const bb=document.getElementById("bt-body");
    const fx = bt&&bt.fluxes ? bt.fluxes.slice().sort((a,b)=>
      a.symbol.localeCompare(b.symbol) ||
      TF_ORDER.indexOf(a.timeframe)-TF_ORDER.indexOf(b.timeframe) ||
      a.direction.localeCompare(b.direction)) : [];
    if(!fx.length){ bb.innerHTML='<tr><td class="muted" colspan="12">Backtest calculé à la tâche quotidienne (00:10 UTC).</td></tr>'; }
    else{
      const ok=b=>b?"✓":"✗";
      bb.innerHTML = fx.map(f=>{
        const v=f.verdict||{}, go=v.go, cls=v.statut==="go"?"good":v.statut==="insuffisant"?"muted":"bad";
        const tip=`n ${ok(v.n_ok)} · PF ${ok(v.pf_ok)} · t ${ok(v.t_ok)} · DD ${ok(v.dd_ok)}`+
          ` · dégr. ${ok(v.degr_ok)} · rétention ${ok(v.ret_ok)}`+
          (v.dd_capital_pct!=null?` · DD cap. ${num(v.dd_capital_pct,1)}%`:"");
        return `<tr>
          <td>${f.symbol} · ${f.timeframe} · <span class="${f.direction}">${f.direction}</span></td>
          <td>${f.n}</td><td>${pct(f.winrate,0)}</td><td>${num(f.profit_factor)}</td>
          <td class="${evClass(f.expectancy_r)}">${signed(f.expectancy_r,3)}</td>
          <td class="${evClass(f.sum_r)}">${signed(f.sum_r,1)}</td>
          <td>${num(f.t_stat)}</td><td>${num(f.max_dd_r,1)}</td>
          <td>${num(f.mc_dd_p95_r,1)}</td>
          <td class="${f.retention!=null&&f.retention>=0.5?'good':f.retention!=null?'bad':''}">${num(f.retention,2)}</td>
          <td class="${evClass(f.pnl_usd)}">${signed(f.pnl_usd,0)}</td>
          <td class="${cls}" title="${tip}">${go?"✓ go":v.statut||"—"}</td></tr>`;
      }).join("");
    }

    // Journal.
    const jl=document.getElementById("journal"), j=d.journal||[];
    jl.innerHTML = j.length ? j.map(e=>{
      const isC=e.event==="close";
      return `<li><b>${e.ts_utc}</b> · ${e.symbol} ${e.timeframe}
        <span class="${e.direction}">${(e.direction||"").toUpperCase()}</span> — ${e.event}
        ${isC?`(${e.reason}, <span class="${evClass(e.r_net)}">${signed(e.r_net,2)} R</span>, ${signed(e.pnl_usd,0)}$)`:`@ ${num(e.entry)}`}</li>`;
    }).join("") : '<li class="muted">Aucun trade encore.</li>';
  }catch(e){}
}

function refreshAll(){ refreshHealth(); refreshEvents(); refreshMatrice(); refreshTickets(); refreshLivre(); }
refreshAll();
setInterval(refreshAll, 15000);

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

// ---- Vue Matrice (§3) — profil rrMult sélectionnable -----------------------
let matriceData = null;
async function refreshMatrice(){
  try{
    const d = await getJSON("/api/probas"); if(!d) return;
    matriceData = d;
    document.getElementById("rrlive").textContent = num(d.rr_live,1);
    const c = d.corr_btc_eth;
    document.getElementById("corr").textContent = c==null? "—" : num(c,2);
    document.getElementById("corr2").textContent = c==null? "—" : num(c,2);
    // Alimente le sélecteur avec la grille des rr disponibles (union des cases).
    const sel=document.getElementById("rr-sel");
    const keys=[...new Set((d.cases||[]).flatMap(x=>Object.keys(x.rr_grid||{})))].sort(
      (a,b)=>parseFloat(a)-parseFloat(b));
    const liveKey=Number(d.rr_live).toFixed(2);
    const cur = sel.value || liveKey;
    if(sel.options.length !== keys.length){
      sel.innerHTML = keys.map(k=>`<option value="${k}">${parseFloat(k)} R${k===liveKey?" (vivant)":""}</option>`).join("");
    }
    sel.value = keys.includes(cur) ? cur : liveKey;
    renderMatrice();
  }catch(e){}
}

function renderMatrice(){
  const d = matriceData; if(!d) return;
  const key = document.getElementById("rr-sel").value || Number(d.rr_live).toFixed(2);
  const cases = (d.cases||[]).slice().sort((a,b)=>
    a.symbol.localeCompare(b.symbol) ||
    TF_ORDER.indexOf(a.timeframe)-TF_ORDER.indexOf(b.timeframe) ||
    a.direction.localeCompare(b.direction));
  const body=document.getElementById("mat-body");
  // Vue par case au rr choisi (repli sur les champs du rr vivant si absent).
  const views = cases.map(c=>{
    const blk=(c.rr_grid||{})[key];
    const wf=((c.wf_grid||{})[key]) || (blk?{}:c.wf) || {};
    const v = blk ? {...c, ...blk, wf} : {...c, wf:c.wf||{}};
    return v;
  });
  if(!views.length){ body.innerHTML='<tr><td class="muted" colspan="10">Matrice vide — lancer <code>jobs/daily_update.py</code>.</td></tr>'; }
  else{
    body.innerHTML = views.map(c=>{
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
  document.getElementById("b-mat").textContent = views.filter(isSolide).length;
}
{
  const rrSel=document.getElementById("rr-sel");
  if(rrSel) rrSel.addEventListener("change", renderMatrice);
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
// Simulateur : PnL par flux = Σ R × capital × risque% (× facteur short).
let livreData = null;
function simParams(){
  const p = (livreData&&livreData.params)||{};
  const cap = parseFloat(document.getElementById("sim-cap").value);
  const rk = parseFloat(document.getElementById("sim-risk").value);
  return {
    cap: (isFinite(cap)&&cap>0) ? cap : (p.capital_usd||3000),
    riskFrac: (isFinite(rk)&&rk>0) ? rk/100 : (p.risk_pct||0.015),
    srf: p.short_risk_factor!=null ? p.short_risk_factor : 0.75,
  };
}
function fluxPnl(f, sp){
  if(f.sum_r==null) return null;
  const factor = f.direction==="short" ? sp.srf : 1.0;
  return f.sum_r * sp.cap * sp.riskFrac * factor;
}

// Profil rrMult sélectionné dans la vue Livre (repli : données du rr vivant).
function livreProfile(){
  const bt = livreData && livreData.backtest;
  if(!bt) return null;
  const liveKey = bt.rr!=null ? Number(bt.rr).toFixed(2) : null;
  const sel = document.getElementById("lv-rr");
  const profs = bt.profiles;
  if(!profs){          // ancien format : uniquement le rr vivant
    if(sel && !sel.options.length && liveKey)
      sel.innerHTML = `<option value="${liveKey}">${parseFloat(liveKey)} R (vivant)</option>`;
    return {key:liveKey, fluxes:bt.fluxes||[], bilan:bt.bilan, portfolio:bt.portfolio};
  }
  const keys = Object.keys(profs).sort((a,b)=>parseFloat(a)-parseFloat(b));
  if(sel && sel.options.length !== keys.length){
    const cur = sel.value;
    sel.innerHTML = keys.map(k=>
      `<option value="${k}">${parseFloat(k)} R${k===liveKey?" (vivant)":""}</option>`).join("");
    sel.value = keys.includes(cur) ? cur : liveKey;
  }
  const key = (sel && sel.value) || liveKey;
  const p = profs[key] || profs[liveKey] || {};
  return {key, fluxes:p.fluxes||[], bilan:p.bilan, portfolio:p.portfolio};
}

async function refreshLivre(){
  try{
    const d = await getJSON("/api/livre"); if(!d) return;
    livreData = d;
    const p = d.params||{};
    const srfEl=document.getElementById("sim-srf");
    if(srfEl) srfEl.textContent = num(p.short_risk_factor,2);
    // Pré-remplir les cases une seule fois avec les valeurs du serveur.
    const capEl=document.getElementById("sim-cap"), rkEl=document.getElementById("sim-risk");
    if(capEl && capEl.value==="" && !capEl.dataset.touched) capEl.placeholder = p.capital_usd||3000;
    if(rkEl && rkEl.value==="" && !rkEl.dataset.touched) rkEl.placeholder = 100*(p.risk_pct||0.015);
    renderLivre();
  }catch(e){}
}

function renderLivre(){
    const d = livreData; if(!d) return;
    document.getElementById("risk-open").textContent = num(d.risque_ouvert_pct,2)+" %";
    document.getElementById("risk-cap").textContent = num(d.plafond_pct,1)+" %";
    const bt = d.backtest;
    const prof = livreProfile();
    const sp = simParams();
    let totalPnl = null;
    if(prof&&prof.fluxes.length){
      totalPnl = 0;
      for(const f of prof.fluxes){ const v=fluxPnl(f,sp); if(v!=null) totalPnl+=v; }
    }
    document.getElementById("equity").textContent = totalPnl!=null ?
      num(sp.cap+totalPnl,0)+" $ ("+signed(totalPnl,0)+" $ sur capital "+num(sp.cap,0)+" $"
      +(prof&&prof.key?" · profil "+parseFloat(prof.key)+" R":"")+")" : "—";

    // Positions ouvertes.
    const pb=document.getElementById("pos-body"), pos=d.positions||[];
    pb.innerHTML = pos.length ? pos.map(p=>
      `<tr><td>${p.symbol} · ${p.timeframe} · <span class="${p.direction}">${p.direction}</span></td>
       <td>${num(p.entry)}</td><td>${num(p.sl)}</td><td>${num(p.tp)}</td>
       <td>${p.be_done?"✓":"—"}</td><td>${num(p.risk_usd,0)}</td></tr>`).join("")
      : '<tr><td class="muted" colspan="6">Aucune position ouverte.</td></tr>';

    // Bilan récapitulatif P5 (au profil sélectionné).
    const bl=prof&&prof.bilan;
    document.getElementById("bilan").innerHTML = bl ?
      `<span class="pill solide">${bl["go"]||0} go</span>
       <span class="pill spec" style="margin-left:6px">${bl["no-go"]||0} no-go</span>
       <span class="pill" style="margin-left:6px;background:#2a2f3a;color:var(--dim)">${bl["insuffisant"]||0} insuffisants</span>
       <span class="muted" style="margin-left:10px">profil ${prof.key?parseFloat(prof.key)+" R":""}
         · recalculé ${bt&&bt.generated_at||""}</span>` : "—";

    // Backtest par flux + verdict (5 critères en infobulle), au profil choisi.
    const bb=document.getElementById("bt-body");
    const fx = prof&&prof.fluxes.length ? prof.fluxes.slice().sort((a,b)=>
      a.symbol.localeCompare(b.symbol) ||
      TF_ORDER.indexOf(a.timeframe)-TF_ORDER.indexOf(b.timeframe) ||
      a.direction.localeCompare(b.direction)) : [];
    if(!fx.length){ bb.innerHTML='<tr><td class="muted" colspan="13">Backtest calculé à la tâche quotidienne (00:10 UTC).</td></tr>'; }
    else{
      const ok=b=>b?"✓":"✗";
      bb.innerHTML = fx.map(f=>{
        const v=f.verdict||{}, go=v.go, cls=v.statut==="go"?"good":v.statut==="insuffisant"?"muted":"bad";
        const tip=`n ${ok(v.n_ok)} · PF ${ok(v.pf_ok)} · t ${ok(v.t_ok)} · DD ${ok(v.dd_ok)}`+
          ` · dégr. ${ok(v.degr_ok)} · rétention ${ok(v.ret_ok)}`+
          (v.dd_capital_pct!=null?` · DD cap. ${num(v.dd_capital_pct,1)}%`:"");
        const pnl = fluxPnl(f, sp);
        const pnlPct = pnl==null ? null : 100*pnl/sp.cap;
        return `<tr>
          <td>${f.symbol} · ${f.timeframe} · <span class="${f.direction}">${f.direction}</span></td>
          <td>${f.n}</td><td>${pct(f.winrate,0)}</td><td>${num(f.profit_factor)}</td>
          <td class="${evClass(f.expectancy_r)}">${signed(f.expectancy_r,3)}</td>
          <td class="${evClass(f.sum_r)}">${signed(f.sum_r,1)}</td>
          <td>${num(f.t_stat)}</td><td>${num(f.max_dd_r,1)}</td>
          <td>${num(f.mc_dd_p95_r,1)}</td>
          <td class="${f.retention!=null&&f.retention>=0.5?'good':f.retention!=null?'bad':''}">${num(f.retention,2)}</td>
          <td class="${evClass(pnl)}"><b>${pnl==null?"—":signed(pnl,0)}</b></td>
          <td class="${evClass(pnlPct)}">${pnlPct==null?"—":signed(pnlPct,1)+" %"}</td>
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

    renderSim();
}

// ---- Simulateur « un flux, une réponse » ------------------------------------
function renderSim(){
  const res=document.getElementById("sim-res"), det=document.getElementById("sim-det");
  if(!res) return;
  const prof = livreProfile();
  if(!prof || !prof.fluxes.length){
    res.textContent="—"; res.className="";
    det.textContent="Backtest pas encore calculé (tâche quotidienne 00:10 UTC).";
    return;
  }
  const sym=document.getElementById("sim-sym").value,
        tf=document.getElementById("sim-tf").value,
        dir=document.getElementById("sim-dir").value;
  const sp=simParams();
  const f=prof.fluxes.find(x=>x.symbol===sym&&x.timeframe===tf&&x.direction===dir);
  if(!f || !f.n){
    res.textContent="Aucun trade mesuré sur ce flux";
    res.className="";
    det.textContent="La stratégie n'a produit aucun setup résolu sur "+sym+" "+tf+" "+dir+" en 2 ans.";
    return;
  }
  const pnl=fluxPnl(f,sp), pctCap=100*pnl/sp.cap;
  const gain=pnl>=0;
  res.innerHTML=(gain?"GAIN de ":"PERTE de ")+
    `<span class="${gain?'good':'bad'}">${Math.abs(pnl).toLocaleString("fr-FR",{maximumFractionDigits:0})} $`+
    ` (${signed(pctCap,1)} %)</span> sur ~2 ans`;
  res.className="";
  const v=f.verdict||{};
  const warn = v.statut==="go" ? "" :
    v.statut==="insuffisant" ? " · ⚠ échantillon insuffisant (n<200) : chiffre indicatif, pas une preuve"
    : " · ⚠ flux no-go : la stratégie n'a pas d'edge prouvé ici";
  det.textContent=`${f.n} trades · taux ${f.winrate==null?"—":Math.round(100*f.winrate)+" %"}`+
    ` · PF ${f.profit_factor==null?"—":f.profit_factor.toFixed(2)}`+
    ` · profil ${prof.key?parseFloat(prof.key)+" R":"—"}`+
    ` · capital ${sp.cap.toLocaleString("fr-FR")} $ · risque ${(100*sp.riskFrac).toFixed(1)} %`+
    `${dir==="short"?" (×"+sp.srf+")":""} · verdict : ${v.statut||"—"}${warn}`;
}

// Recalcul instantané quand on change un réglage du simulateur.
for(const id of ["sim-cap","sim-risk","sim-sym","sim-tf","sim-dir","lv-rr"]){
  const el=document.getElementById(id);
  if(el){
    const h=()=>{ el.dataset.touched="1"; renderLivre(); };
    el.addEventListener("input", h);
    el.addEventListener("change", h);
  }
}

// ---- Vue Mon journal (saisie manuelle) -------------------------------------
function esc(s){ const d=document.createElement("div"); d.textContent=s==null?"":String(s);
  return d.innerHTML; }
async function postJSON(url, body){
  try{
    const r = await fetch(url,{method:"POST",credentials:"same-origin",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});
    if(r.status===401){ window.location="/login"; return null; }
    let j={};
    try{ j=await r.json(); }catch(_){ j={error:"réponse serveur invalide (HTTP "+r.status+")"}; }
    return {ok:r.ok, ...j};
  }catch(e){
    return {ok:false, error:"réseau : "+e.message};
  }
}

function jpRatio(t){
  if(t.tp==null||t.entry==null||t.sl==null) return null;
  const stop=Math.abs(t.entry-t.sl);
  return stop>0 ? Math.abs(t.tp-t.entry)/stop : null;
}
function jpRisk(t){
  if(t.capital_usd&&t.risk_pct) return t.capital_usd*t.risk_pct/100.0;
  if(t.size_units&&t.entry!=null&&t.sl!=null) return t.size_units*Math.abs(t.entry-t.sl);
  return null;
}

async function refreshJournalPerso(){
  try{
    const d = await getJSON("/api/journal-perso"); if(!d) return;
    const rows=d.trades||[];
    document.getElementById("b-jp").textContent = rows.filter(t=>!t.closed_utc).length;
    const body=document.getElementById("jp-body");
    if(!rows.length){ body.innerHTML='<tr><td class="muted" colspan="12">Aucun trade saisi.</td></tr>'; return; }
    body.innerHTML = rows.map(t=>{
      const open=!t.closed_utc, ratio=jpRatio(t), risk=jpRisk(t);
      return `<tr>
        <td>${esc((t.ts_utc||"").replace("T"," ").slice(0,16))}</td>
        <td>${esc(t.symbol)}${t.timeframe?" · "+esc(t.timeframe):""}${t.direction?` · <span class="${esc(t.direction)}">${esc(t.direction)}</span>`:""}</td>
        <td>${num(t.entry)}</td><td>${num(t.sl)}</td><td>${num(t.tp)}</td>
        <td>${ratio==null?"—":"1:"+num(ratio,1)}</td>
        <td>${risk==null?"—":num(risk,0)}</td>
        <td>${open?'<span class="muted">ouvert</span>':num(t.exit_price)}</td>
        <td class="${evClass(t.r_result)}">${t.r_result==null?"—":signed(t.r_result,2)}</td>
        <td class="${evClass(t.pnl_usd)}">${t.pnl_usd==null?"—":signed(t.pnl_usd,0)}</td>
        <td style="text-align:left;max-width:260px;white-space:normal">${esc(t.note)}</td>
        <td style="white-space:nowrap">
          ${open?`<button class="btn small" onclick="jpClose(${t.id})">clôturer</button> `:""}
          <button class="btn small danger" onclick="jpDelete(${t.id})">✕</button></td>
      </tr>`;
    }).join("");
  }catch(e){}
}

async function jpAdd(){
  const v=id=>document.getElementById(id).value;
  const msg=document.getElementById("jp-msg");
  msg.textContent="envoi…";
  const res=await postJSON("/api/journal-perso",{symbol:v("jp-sym"),timeframe:v("jp-tf"),
    direction:v("jp-dir"),entry:v("jp-entry"),sl:v("jp-sl"),tp:v("jp-tp"),
    ratio:v("jp-ratio"),capital_usd:v("jp-cap"),risk_pct:v("jp-risk"),note:v("jp-note")});
  if(!res) return;
  if(!res.ok){ msg.textContent="⚠ "+(res.error||"erreur"); return; }
  msg.textContent = "✓ ajouté" + (res.tp!=null && v("jp-tp")==="" ? " (TP auto : "+num(res.tp)+")" : "");
  // Capital et risque sont mémorisés pour les prochains trades.
  try{ localStorage.setItem("jp-cap", v("jp-cap")); localStorage.setItem("jp-risk", v("jp-risk")); }catch(_){}
  ["jp-entry","jp-sl","jp-tp","jp-ratio","jp-note"].forEach(i=>document.getElementById(i).value="");
  refreshJournalPerso();
}
// Restaurer capital/risque mémorisés.
try{
  const c=localStorage.getItem("jp-cap"), r=localStorage.getItem("jp-risk");
  if(c) document.getElementById("jp-cap").value=c;
  if(r) document.getElementById("jp-risk").value=r;
}catch(_){}
async function jpClose(id){
  const p=prompt("Prix de sortie ?"); if(p==null||p==="") return;
  const res=await postJSON(`/api/journal-perso/${id}/cloture`,{exit_price:p});
  if(res&&!res.ok) alert(res.error||"erreur");
  refreshJournalPerso();
}
async function jpDelete(id){
  if(!confirm("Supprimer cette ligne du journal ?")) return;
  await postJSON(`/api/journal-perso/${id}/supprimer`,{});
  refreshJournalPerso();
}
window.jpClose = jpClose;
window.jpDelete = jpDelete;
const _jpBtn = document.getElementById("jp-add");
if(_jpBtn) _jpBtn.addEventListener("click", jpAdd);

// ---- Laboratoires (Bot 2 pullback, Bot 3 VuManChu) — rendu générique ------
function makeLab(botId, p, badgeId){
  const S={data:null,market:null,tickets:[]};
  const $=id=>document.getElementById(id);
  async function refresh(){
    try{
      const d=await getJSON("/api/bot"+botId); if(!d) return;
      S.data=d.backtest; S.market=d.market; S.tickets=d.tickets||[];
      render();
    }catch(e){}
  }
  function params(){
    const cap=parseFloat($(p+"-cap").value), rk=parseFloat($(p+"-risk").value);
    const q=(livreData&&livreData.params)||{};
    return {cap:(isFinite(cap)&&cap>0)?cap:(q.capital_usd||10000),
            riskFrac:(isFinite(rk)&&rk>0)?rk/100:(q.risk_pct||0.01),
            srf:q.short_risk_factor!=null?q.short_risk_factor:0.75};
  }
  function oscCell(s){
    if(s.k!=null) return `${num(s.k,1)} <span class="muted">(préc. ${num(s.k_prev,1)})</span>`;
    if(s.wt1!=null) return `${num(s.wt1,1)} <span class="muted">/ ${num(s.wt2,1)}</span>`;
    return "—";
  }
  function hint(s){
    const up=s.trend==="haussier";
    if(s.k!=null) return up?(s.oversold?"%K doit re-croiser 20":"attendre repli <20")
                          :(s.overbought?"%K doit re-croiser 80":"attendre rebond >80");
    return up?(s.oversold?"attendre croisement haussier WT":"attendre survente < −53")
             :(s.overbought?"attendre croisement baissier WT":"attendre surachat > +53");
  }
  function renderMarket(){
    const body=$(p+"-mkt"), tl=$(p+"-tickets");
    if(tl){
      tl.innerHTML = S.tickets.length ? S.tickets.map(t=>{
        const fs=t.flux_stats||{};
        return `<li><b>${esc(t.ts_utc)}</b> · ${esc(t.symbol)} ${esc(t.timeframe)}
          <span class="${esc(t.direction)}">${esc((t.direction||"").toUpperCase())}</span>
          — entrée ≈ ${num(t.entry_ref)} · SL ${num(t.sl)} · TP ${num(t.tp)}
          · <span class="muted">flux : ${esc(fs.verdict||"non jugé")} (PF ${num(fs.pf)}, n=${fs.n??"—"})</span></li>`;
      }).join("") : '<li class="muted">Aucun ticket pour l\'instant.</li>';
    }
    if(!body) return;
    const sts=(S.market&&S.market.states)||[];
    if(!sts.length){
      body.innerHTML='<tr><td class="muted" colspan="7">En attente du worker (rafraîchi ~2 min ; redémarrer l\'always-on après mise à jour).</td></tr>';
      return;
    }
    const vcls=v=>v==="go"?"good":v==="no-go"?"bad":"muted";
    body.innerHTML = sts.map(s=>{
      const up=s.trend==="haussier";
      const zone = s.oversold?"survente":s.overbought?"surachat":"neutre";
      const sig = s.signal ? `<b class="${s.signal}">⚡ ${s.signal.toUpperCase()}</b>` :
        `<span class="muted">aucun — ${hint(s)}</span>`;
      const fx=d=>{const f=(s.flux||{})[d]||{};return `<span class="${vcls(f.verdict)}">${f.verdict||"—"}</span> <span class="muted">(PF ${num(f.pf)}, n=${f.n??"—"})</span>`;};
      return `<tr>
        <td>${esc(s.symbol)} · ${esc(s.timeframe)}</td>
        <td class="${up?'good':'bad'}">${up?"▲":"▼"} ${esc(s.trend)}</td>
        <td>${oscCell(s)}</td>
        <td>${zone}</td><td>${sig}</td>
        <td>${fx("long")}</td><td>${fx("short")}</td></tr>`;
    }).join("");
  }
  function render(){
    renderMarket();
    const bt=S.data, badge=$(badgeId);
    if(!bt){ if(badge) badge.textContent="—"; return; }
    const liveKey = bt.rr!=null ? Number(bt.rr).toFixed(2) : null;
    const sel=$(p+"-rr");
    const profs=bt.profiles||{};
    const keys=Object.keys(profs).sort((a,b)=>parseFloat(a)-parseFloat(b));
    if(sel && keys.length && sel.options.length!==keys.length){
      const cur=sel.value;
      sel.innerHTML=keys.map(k=>`<option value="${k}">${parseFloat(k)} R${k===liveKey?" (vivant)":""}</option>`).join("");
      sel.value = keys.includes(cur)?cur:liveKey;
    }
    const key=(sel&&sel.value)||liveKey;
    const pr=profs[key]||{fluxes:bt.fluxes||[],bilan:bt.bilan,portfolio:bt.portfolio};
    $(p+"-gen").textContent="recalculé "+(bt.generated_at||"—");
    const tfsEl=$(p+"-tfs");
    if(tfsEl && bt.params&&bt.params.timeframes) tfsEl.textContent=bt.params.timeframes.join(" · ");
    const bl=pr.bilan||{};
    $(p+"-bilan").innerHTML=
      `<span class="pill solide">${bl["go"]||0} go</span>
       <span class="pill spec" style="margin-left:6px">${bl["no-go"]||0} no-go</span>
       <span class="pill" style="margin-left:6px;background:#2a2f3a;color:var(--dim)">${bl["insuffisant"]||0} insuffisants</span>
       <span class="muted" style="margin-left:10px">profil ${key?parseFloat(key)+" R":""}</span>`;
    if(badge) badge.textContent=(bl["go"]||0);
    const sp=params();
    const fluxes=pr.fluxes||[];
    let totalPnl=null, totN=0;
    if(fluxes.length){
      totalPnl=0;
      for(const f of fluxes){
        if(f.sum_r!=null){ totalPnl += f.sum_r*sp.cap*sp.riskFrac*(f.direction==="short"?sp.srf:1); }
        totN += f.n||0;
      }
    }
    const port=pr.portfolio;
    $(p+"-port").innerHTML = totalPnl!=null && totN ?
      `Portefeuille : <span class="${evClass(totalPnl)}">${signed(totalPnl,0)} $</span>
       <span class="muted" style="font-weight:400">sur capital ${num(sp.cap,0)} $ · risque ${(100*sp.riskFrac).toFixed(1)} %
       (${totN} trades · taux ${port&&port.winrate!=null?Math.round(100*port.winrate)+" %":"—"}
       · PF ${port&&port.profit_factor!=null?port.profit_factor.toFixed(2):"—"} · Σ ${port?signed(port.sum_r,1):"—"} R)</span>`
      : "Portefeuille : aucun trade.";
    const body=$(p+"-body");
    const fx=fluxes.slice().sort((a,b)=>
      a.symbol.localeCompare(b.symbol) ||
      TF_ORDER.indexOf(a.timeframe)-TF_ORDER.indexOf(b.timeframe) ||
      a.direction.localeCompare(b.direction));
    if(!fx.length){ body.innerHTML='<tr><td class="muted" colspan="13">Pas encore calculé.</td></tr>'; return; }
    const ok=b=>b?"✓":"✗";
    body.innerHTML = fx.map(f=>{
      const v=f.verdict||{}, cls=v.statut==="go"?"good":v.statut==="insuffisant"?"muted":"bad";
      const tip=`n ${ok(v.n_ok)} · PF ${ok(v.pf_ok)} · t ${ok(v.t_ok)} · DD ${ok(v.dd_ok)}`+
        ` · dégr. ${ok(v.degr_ok)} · rétention ${ok(v.ret_ok)}`;
      const pnl = f.sum_r==null?null:f.sum_r*sp.cap*sp.riskFrac*(f.direction==="short"?sp.srf:1);
      const pnlPct = pnl==null?null:100*pnl/sp.cap;
      return `<tr>
        <td>${f.symbol} · ${f.timeframe} · <span class="${f.direction}">${f.direction}</span></td>
        <td>${f.n}</td><td>${pct(f.winrate,0)}</td><td>${num(f.profit_factor)}</td>
        <td class="${evClass(f.expectancy_r)}">${signed(f.expectancy_r,3)}</td>
        <td class="${evClass(f.sum_r)}">${signed(f.sum_r,1)}</td>
        <td>${num(f.t_stat)}</td><td>${num(f.max_dd_r,1)}</td>
        <td>${num(f.mc_dd_p95_r,1)}</td>
        <td class="${f.retention!=null&&f.retention>=0.5?'good':f.retention!=null?'bad':''}">${num(f.retention,2)}</td>
        <td class="${evClass(pnl)}"><b>${pnl==null?"—":signed(pnl,0)}</b></td>
        <td class="${evClass(pnlPct)}">${pnlPct==null?"—":signed(pnlPct,1)+" %"}</td>
        <td class="${cls}" title="${tip}">${v.go?"✓ go":v.statut||"—"}</td></tr>`;
    }).join("");
  }
  for(const id of [p+"-rr",p+"-cap",p+"-risk"]){
    const el=$(id);
    if(el){ el.addEventListener("change", render); el.addEventListener("input", render); }
  }
  return {refresh, render};
}
const lab2 = makeLab("2","b2","b-b2");
const lab3 = makeLab("3","b3","b-b3");
function refreshBot2(){ lab2.refresh(); lab3.refresh(); }

function refreshAll(){ refreshHealth(); refreshEvents(); refreshMatrice(); refreshTickets(); refreshLivre(); refreshJournalPerso(); refreshBot2(); }
refreshAll();
setInterval(refreshAll, 15000);

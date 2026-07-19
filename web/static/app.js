// Live par polling (§6.3). Pas de websocket entrant : la web app PythonAnywhere
// ne peut pas en servir ; le navigateur interroge des endpoints JSON.
//   /api/livre       toutes les 5 s
//   /api/health + /api/evenements   toutes les 15 s
// Chaque réponse porte generated_at ; l'app affiche l'ÂGE et passe en ⚠ si le
// heartbeat worker dépasse 120 s (§6.3, §7.3).

function fmtUtcMs(ms) {
  if (ms === null || ms === undefined) return "—";
  return new Date(ms).toISOString().replace("T", " ").replace(".000Z", " UTC");
}
function fmtAge(s) {
  if (s === null || s === undefined) return "—";
  if (s < 60) return Math.round(s) + " s";
  if (s < 3600) return Math.round(s / 60) + " min";
  return Math.round(s / 3600) + " h";
}

async function getJSON(url) {
  const r = await fetch(url, { credentials: "same-origin" });
  if (r.status === 401) { window.location = "/login"; return null; }
  if (!r.ok) throw new Error(url + " → " + r.status);
  return r.json();
}

let healthFails = 0; // tolère UN raté réseau isolé avant de passer le bandeau en ⚠

async function refreshHealth() {
  try {
    const h = await getJSON("/api/health");
    if (!h) return;
    healthFails = 0;
    document.getElementById("src").textContent = h.source || "—";
    document.getElementById("age").textContent = fmtAge(h.heartbeat_age_s);
    document.getElementById("bars").textContent = (h.bars_1m || 0).toLocaleString("fr-FR");
    document.getElementById("last1m").textContent = fmtUtcMs(h.last_1m_open_ms);
    document.getElementById("cycle").textContent = h.worker ? h.worker.cycle : "—";
    document.getElementById("unread").textContent = h.unread_events;
    document.getElementById("gen").textContent = h.generated_at;

    const band = document.getElementById("band");
    const live = document.getElementById("livedot");
    const wstatus = document.getElementById("wstatus");
    if (h.worker_stale) {
      band.classList.add("warn"); band.classList.remove("live");
      live.querySelector("#livetxt").textContent = "⚠ worker en retard";
      wstatus.textContent = "⚠ " + (h.worker ? h.worker.status : "absent");
    } else {
      band.classList.remove("warn"); band.classList.add("live");
      live.querySelector("#livetxt").textContent = "live";
      wstatus.textContent = "✓ " + (h.worker ? h.worker.status : "ok");
    }
  } catch (e) {
    healthFails += 1;
    if (healthFails < 2) return; // un raté isolé : on garde l'état affiché
    const band = document.getElementById("band");
    band.classList.add("warn"); band.classList.remove("live");
    document.getElementById("livedot").querySelector("#livetxt").textContent = "⚠ hors ligne";
  }
}

async function refreshEvents() {
  try {
    const d = await getJSON("/api/evenements");
    if (!d) return;
    const badge = document.getElementById("bell-badge");
    if (d.unread > 0) { badge.style.display = "inline-block"; badge.textContent = d.unread + " non lus"; }
    else { badge.style.display = "none"; }
    const ul = document.getElementById("events");
    if (!d.events.length) { ul.innerHTML = '<li class="muted">Aucun événement.</li>'; return; }
    ul.innerHTML = d.events.map(e =>
      `<li><b>${e.ts_utc}</b> · [${e.level}] ${e.kind} — ${e.message}</li>`
    ).join("");
  } catch (e) { /* silencieux : réessai au prochain tick */ }
}

async function markRead() {
  try {
    await fetch("/api/evenements/lu", { method: "POST", credentials: "same-origin" });
    refreshEvents();
  } catch (e) {}
}

async function refreshSynthese() {
  try {
    const d = await getJSON("/api/synthese");
    if (!d) return;
    const box = document.getElementById("synthese");
    document.getElementById("syn-gen").textContent = d.generated_at || (d.note || "—");
    const s = d.synthese;
    if (!s || isNaN(s.p_up)) { box.innerHTML = `<div class="muted">${d.note || "Pas encore de synthèse."}</div>`; return; }
    const ref = d.reference_neutre || {};
    const contribs = (s.contributions || []).slice(0, 6).map(c => {
      // p_vote = probabilité AMORTIE par l'échantillon (celle qui vote, §5.6) ;
      // le p̂ brut est rappelé quand l'amortisseur l'a nettement corrigé.
      const pv = c.p_vote ?? c.p_up;
      const brut = (c.p_vote !== undefined && Math.abs(c.p_vote - c.p_up) > 0.02)
        ? ` <span class="lab">(p̂ brut ${pct(c.p_up)}, n=${c.n})</span>` : "";
      return `<span>${c.timeframe}: ${(c.contribution >= 0 ? "+" : "")}${c.contribution.toFixed(2)} (p ${pct(pv)})${brut}</span>`;
    }).join("");
    box.innerHTML = `<div class="syn">
        <div><div class="k">P(hausse) globale</div><div class="big">${pct(s.p_up)}</div>
          ${s.capped ? '<span class="muted">(plafonné 85 %)</span>' : ''}</div>
        <div><div class="k">Référence neutre (~1 an)</div><div class="big">${pct(ref.p_up)}</div></div>
        <div><div class="k">Échelles combinées</div><div class="big">${s.n_timeframes}</div></div>
      </div>
      <div class="contrib">Contributions principales : ${contribs || "—"}</div>`;
  } catch (e) { /* réessai au prochain tick */ }
}

function vcRow(name, c) {
  if (!c || !c.n) return `<tr><td>${name}</td><td colspan="9" style="text-align:left" class="muted">aucun trade</td></tr>`;
  return `<tr><td>${name}</td><td>${c.n}</td><td>${pct(c.p_hat)} [${pct(c.wilson.low)}–${pct(c.wilson.high)}]</td>
    <td>${num(c.ev_realisee)}</td><td>${num(c.sigma_r)}</td><td>${num(c.t_stat)}</td>
    <td>${c.serie_perdante_max}/${num(c.serie_attendue, 1)}</td>
    <td>${num(c.mc_dd_median, 1)} / ${num(c.mc_dd_p95, 1)}</td>
    <td>${c.profit_factor === null ? "—" : num(c.profit_factor)}</td><td>${num(c.brier, 3)}</td></tr>`;
}

async function refreshLivre() {
  try {
    const d = await getJSON("/api/livre");
    if (!d) return;
    document.getElementById("livre-gen").textContent = d.generated_at || "—";
    document.getElementById("risk-now").textContent = d.risque_ouvert_pct;
    document.getElementById("risk-cap").textContent = d.plafond_pct;
    document.getElementById("journal-n").textContent = d.trades_journal || 0;

    const posBox = document.getElementById("livre-positions");
    if (!d.positions_ouvertes.length) {
      posBox.innerHTML = '<div class="muted">Aucune position ouverte.</div>';
    } else {
      posBox.innerHTML = '<div class="k">Positions ouvertes</div>' + d.positions_ouvertes.map(p =>
        `<div class="pos">${p.stage} <b>${p.direction}</b> · état ${p.etat} · fill ${num(p.fill)} ·
         SL ${num(p.sl)} · TP ${num(p.tp)} (RR ${p.rr}) · risque ${pct(p.risque_pct)}</div>`).join("");
    }

    const tkBox = document.getElementById("livre-tickets");
    const actifs = d.tickets_actifs.map(t =>
      `<div class="pos">🎫 ${t.stage} <b>${t.direction}</b> · limite ${num(t.limite)} ·
       p̂ ${pct(t.p_annonce)} · EV prud. ${num(t.ev_nette_prudente)} R</div>`).join("");
    const bloques = d.tickets_bloques.slice(0, 8).map(t =>
      `<div class="pos" style="opacity:.6">⛔ ${t.stage} ${t.direction} —
       ${t.motif_blocage || t.motif_expiration || t.status}</div>`).join("");
    tkBox.innerHTML = (actifs || bloques)
      ? `<div class="k">Tickets</div>${actifs}${bloques}` : "";

    const v = d.verdicts || {};
    const vBox = document.getElementById("livre-verdicts");
    if (v.global && v.global.n) {
      vBox.innerHTML = `<div class="k">Cartes de verdict</div>
        <table class="det"><tr><th>Étage</th><th>n</th><th>p̂ [Wilson]</th><th>EV réal.</th>
        <th>σ_R</th><th>t-stat</th><th>série max/att.</th><th>DD MC méd/p95</th>
        <th>PF</th><th>Brier</th></tr>
        ${vcRow("global", v.global)}${vcRow("1h", v["1h"])}${vcRow("4h", v["4h"])}${vcRow("1D", v["1D"])}
        </table>`;
    } else { vBox.innerHTML = ""; }

    const mBox = document.getElementById("livre-monitors");
    const mons = d.surveillance || {};
    mBox.innerHTML = '<div class="k">Surveillance (CUSUM)</div>' +
      ["1h", "4h", "1D"].map(s => {
        const m = mons[s];
        if (!m) return `<div class="pos">${s} : <span class="muted">pas encore de suivi</span></div>`;
        const alarm = m.en_enquete
          ? ` <span class="pill no">EN ENQUÊTE — ${m.motif || ""}</span>
              <button class="btnlu" onclick="ackAlarm('${s}')">Acquitter</button>`
          : ' <span class="pill ok">ok</span>';
        return `<div class="pos">${s} : CUSUM ${num(m.cusum)} / h ${m.h ? num(m.h) : "—"} ·
                clôtures ${m.closes}${alarm}</div>`;
      }).join("");
  } catch (e) {}
}

async function ackAlarm(stage) {
  try {
    await fetch("/api/alarme/ack", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage })
    });
    refreshLivre();
  } catch (e) {}
}

async function showDetail(tf) {
  try {
    const d = await getJSON("/api/detail/" + tf);
    if (!d || !d.table) return;
    document.getElementById("detail-card").style.display = "block";
    document.getElementById("detail-tf").textContent = tf;
    const t = d.table;
    const rows = [];
    const pushRow = (label, nEtat, b) => {
      const fh = b.horizon_fixe || {}; const best = b.best;
      const hz = b.horizons || {};
      const hzTxt = ["5", "10", "20"].map(h => {
        const x = hz[h]; if (!x) return "";
        return `H${h} ${pct(x.p_hat)}<span class="lab"> (n=${x.n})</span>`;
      }).filter(Boolean).join(" · ") || `${pct(fh.p_hat)}`;
      const rrTxt = (b.barrieres || []).map(x => {
        const isBest = best && x.rr === best.rr;
        const v = `RR${x.rr}: ${num(x.ev_nette_prudente)}`;
        return isBest ? `<b>${v}</b>` : v;
      }).join(" · ") || "—";
      rows.push(`<tr><td>${label}</td><td>${nEtat}</td>
        <td>${hzTxt}<br><span class="lab">H10 Wilson [${pct((fh.wilson||{}).low)}–${pct((fh.wilson||{}).high)}]</span></td>
        <td>${fh.n ?? 0}</td>
        <td>${rrTxt} R</td>
        <td>${b.walkforward || "n/a"}</td>
        <td>${b.candidate ? '<span class="pill ok">candidate</span>'
                          : (b.motifs || []).join(", ") || "—"}</td></tr>`);
    };
    for (const [etat, blk] of Object.entries(t.etats || {})) {
      for (const dir of ["long", "short"]) pushRow(`${etat} · ${dir}`, blk.n_etat, blk[dir]);
      // Dimension volatilité v1.5 (§4) — affichée quand active (n≥200 partout).
      for (const [zone, zb] of Object.entries(blk.vol_zones || {})) {
        for (const dir of ["long", "short"])
          pushRow(`&nbsp;&nbsp;↳ vol ${zone} · ${dir}`, zb.n_etat, zb[dir]);
      }
    }
    const r = t.realisme || {};
    document.getElementById("detail-body").innerHTML =
      `<div class="muted" style="margin-bottom:6px">σ_bougie ${pct(r.sigma_bougie)} ·
       CVaR99 ${pct(r.cvar99)} · k_max ${num(r.k_max, 1)} ·
       coûts (taker) : ${((t.couts||{}).taker||{}).verdict || "—"} ·
       zone de vol courante (v1.5) : ${t.vol_zone_courante || "—"}</div>
      <table class="det"><tr><th>État · sens</th><th>n état</th>
      <th>p̂ aux 3 horizons (§5.1)</th>
      <th>n (H10)</th><th>EV nette prudente par RR (meilleur en gras)</th>
      <th>walk-fwd</th><th>Statut</th></tr>
      ${rows.join("")}</table>`;
  } catch (e) {}
}

function pct(x) { return (x === null || x === undefined || isNaN(x)) ? "—" : (100 * x).toFixed(1) + " %"; }
function num(x, d = 2) { return (x === null || x === undefined || isNaN(x)) ? "—" : Number(x).toFixed(d); }

function dirRow(label, d) {
  if (!d) return "";
  const fh = d.horizon_fixe || {};
  const w = fh.wilson || {};
  const best = d.best;
  const ev = best ? num(best.ev_nette_prudente) + " R" : "—";
  const evOk = best && best.ev_nette_prudente > 0;
  const cls = d.candidate ? "ok" : "no";
  const pill = d.candidate ? "candidate" : (d.motifs && d.motifs[0] ? d.motifs[0] : "—");
  return `<div class="dir"><span class="lab">${label}</span>
    <span>p̂ ${pct(fh.p_hat)} <span class="lab">[${pct(w.low)}–${pct(w.high)}]</span> · n=${fh.n ?? 0}</span></div>
    <div class="dir"><span class="lab">EV nette prud. / meilleur RR</span>
    <span>${ev} <span class="pill ${cls}">${pill}</span></span></div>`;
}

async function refreshMatrix() {
  try {
    const d = await getJSON("/api/probas");
    if (!d) return;
    document.getElementById("mtx-gen").textContent =
      d.timeframes.length ? ("calculée " + (d.generated_at || "")) : (d.note || "—");
    const box = document.getElementById("matrix");
    if (!d.timeframes.length) { box.innerHTML = `<div class="muted">${d.note || "Aucune donnée."}</div>`; return; }
    box.innerHTML = d.timeframes.map(tf => {
      if (tf.insuffisant) return `<div class="tf grey"><h3>${tf.timeframe}</h3><div class="muted">historique insuffisant</div></div>`;
      const c = tf.couts && tf.couts.taker ? tf.couts.taker : {};
      const cand = tf.candidate ? "cand" : "";
      const grey = (!tf.long.candidate && !tf.short.candidate) ? "grey" : "";
      return `<div class="tf ${cand} ${grey}" onclick="showDetail('${tf.timeframe}')">
        <h3><span>${tf.timeframe}</span><span class="st">${tf.etat} · n=${tf.n_etat}</span></h3>
        ${dirRow("Long", tf.long)}
        ${dirRow("Short", tf.short)}
        <div class="cost">coûts (taker) : ${c.verdict || "—"} · c/σ=${pct(c.cost_pct_sigma)}</div>
      </div>`;
    }).join("");
  } catch (e) { /* réessai au prochain tick */ }
}

const mrBtn = document.getElementById("mark-read");
if (mrBtn) mrBtn.addEventListener("click", markRead);
const dcBtn = document.getElementById("detail-close");
if (dcBtn) dcBtn.addEventListener("click", () => {
  document.getElementById("detail-card").style.display = "none";
});

async function refreshBilan() {
  try {
    const d = await getJSON("/api/bilan");
    if (!d) return;
    document.getElementById("bilan-note").textContent = d.note || "—";
    const p = d.periode || {};
    document.getElementById("bilan-jours").textContent = p.jours ?? 0;
    document.getElementById("bilan-trades").textContent = p.trades ?? 0;
    const f = d.funding || {};
    document.getElementById("bilan-funding").textContent =
      f.integre ? (100 * f.annualized).toFixed(2) + " %/an" : "non intégré (F=0)";
    const box = document.getElementById("bilan-etages");
    const pe = d.par_etage || {};
    if (!Object.keys(pe).length) { box.innerHTML = ""; return; }
    box.innerHTML = `<table class="det">
      <tr><th>Étage</th><th>n trades</th><th>t-stat</th><th>EV réal.</th>
      <th>DD MC p95</th><th>Brier</th><th>Walk-fwd (sain/fragile/overfit)</th></tr>` +
      ["1h", "4h", "1D"].map(s => {
        const e = pe[s] || {}; const r = e.retention || {};
        return `<tr><td>${s}</td><td>${e.n_trades ?? 0}</td><td>${num(e.t_stat)}</td>
          <td>${num(e.ev_realisee)}</td><td>${num(e.dd_mc_p95, 1)}</td>
          <td>${num(e.brier, 3)}</td>
          <td>${r.sain ?? 0} / ${r.fragile ?? 0} / ${r.overfit ?? 0}</td></tr>`;
      }).join("") + "</table>";
  } catch (e) {}
}

refreshHealth(); refreshEvents(); refreshLivre(); refreshMatrix(); refreshBilan();
setInterval(refreshBilan, 60000); refreshSynthese();
setInterval(refreshLivre, 5000);           // §6.3
setInterval(() => { refreshHealth(); refreshEvents(); }, 15000);
setInterval(() => { refreshMatrix(); refreshSynthese(); }, 15000);

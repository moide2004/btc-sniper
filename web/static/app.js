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

async function refreshHealth() {
  try {
    const h = await getJSON("/api/health");
    if (!h) return;
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
    const band = document.getElementById("band");
    band.classList.add("warn"); band.classList.remove("live");
    document.getElementById("livedot").querySelector("#livetxt").textContent = "⚠ hors ligne";
  }
}

async function refreshEvents() {
  try {
    const d = await getJSON("/api/evenements");
    if (!d) return;
    const ul = document.getElementById("events");
    if (!d.events.length) { ul.innerHTML = '<li class="muted">Aucun événement.</li>'; return; }
    ul.innerHTML = d.events.map(e =>
      `<li><b>${e.ts_utc}</b> · [${e.level}] ${e.kind} — ${e.message}</li>`
    ).join("");
  } catch (e) { /* silencieux : réessai au prochain tick */ }
}

async function refreshLivre() {
  try { await getJSON("/api/livre"); } catch (e) {}
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
      return `<div class="tf ${cand} ${grey}">
        <h3><span>${tf.timeframe}</span><span class="st">${tf.etat} · n=${tf.n_etat}</span></h3>
        ${dirRow("Long", tf.long)}
        ${dirRow("Short", tf.short)}
        <div class="cost">coûts (taker) : ${c.verdict || "—"} · c/σ=${pct(c.cost_pct_sigma)}</div>
      </div>`;
    }).join("");
  } catch (e) { /* réessai au prochain tick */ }
}

refreshHealth(); refreshEvents(); refreshLivre(); refreshMatrix();
setInterval(refreshLivre, 5000);           // §6.3
setInterval(() => { refreshHealth(); refreshEvents(); }, 15000);
setInterval(refreshMatrix, 15000);

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

refreshHealth(); refreshEvents(); refreshLivre();
setInterval(refreshLivre, 5000);           // §6.3
setInterval(() => { refreshHealth(); refreshEvents(); }, 15000);

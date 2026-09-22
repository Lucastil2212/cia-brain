const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  browseOffset: 0,
  browseLimit: 25,
  browseTotal: 0,
  config: null,
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    const msg = data?.detail || data?.error || res.statusText || "request failed";
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

function fmtNum(n) {
  return new Intl.NumberFormat().format(n ?? 0);
}

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function setStatus(el, msg, show = true) {
  el.hidden = !show;
  el.textContent = msg;
}

function showView(name) {
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.id === `view-${name}`));
  $$(".nav-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.view === name));
  if (name === "sources") loadSources();
  if (name === "graph") loadGraph();
  if (name === "pipeline") refreshPipeline();
  if (name === "browse") loadBrowse();
}

function resultCard(r, index) {
  const title = r.title || "(untitled)";
  const score = typeof r.score === "number" ? r.score.toFixed(4) : "—";
  return `
    <article class="result" style="animation-delay:${index * 40}ms">
      <h3>${escapeHtml(title)}</h3>
      <div class="meta">
        <span class="score">RRF ${score}</span>
        <span>${escapeHtml(r.mime || "unknown")}</span>
        <span>chunk ${r.chunk_no ?? 0}</span>
        <span>${escapeHtml((r.fetched_at || "").slice(0, 10))}</span>
        <a href="${escapeHtml(r.source_url)}" target="_blank" rel="noopener">source</a>
        <a href="/v1/raw/${escapeHtml(r.source_sha256)}" target="_blank" rel="noopener">raw</a>
        <button type="button" data-sha="${escapeHtml(r.source_sha256)}" class="linkish open-doc">document</button>
      </div>
      <p class="excerpt">${escapeHtml(r.text)}</p>
    </article>
  `;
}

async function refreshMeta() {
  try {
    const [stats, config] = await Promise.all([
      api("/v1/stats"),
      api("/v1/config/public"),
    ]);
    state.config = config;
    const frontier = stats.frontier || {};
    const queued = frontier.queued ?? 0;
    $("#top-meta").textContent =
      `${fmtNum(stats.documents)} docs · ${fmtNum(stats.chunks)} chunks · frontier queued ${fmtNum(queued)}`;
  } catch (err) {
    $("#top-meta").textContent = `lake offline: ${err.message}`;
  }
}

async function loadFacets() {
  try {
    const facets = await api("/v1/facets");
    const sel = $("#mime-filter");
    for (const row of facets.mime || []) {
      const opt = document.createElement("option");
      opt.value = row.value === "(unknown)" ? "" : row.value;
      opt.textContent = `${row.value} (${row.count})`;
      if (row.value !== "(unknown)") sel.appendChild(opt);
    }
  } catch {
    /* facets optional */
  }
}

async function runSearch(ev) {
  ev.preventDefault();
  const q = $("#q").value.trim();
  const top_k = Number($("#top-k").value);
  const mime = $("#mime-filter").value || null;
  const status = $("#search-status");
  const box = $("#results");
  setStatus(status, "Searching…");
  box.innerHTML = "";
  try {
    const data = await api("/v1/search", {
      method: "POST",
      body: JSON.stringify({ query: q, top_k, mime }),
    });
    setStatus(status, `${data.count} results for “${data.query}”`);
    box.innerHTML = (data.results || []).map(resultCard).join("") || "<p class='status'>No matches.</p>";
    bindDocButtons(box);
  } catch (err) {
    setStatus(status, err.message);
  }
}

function bindDocButtons(root) {
  $$(".open-doc", root).forEach((btn) => {
    btn.addEventListener("click", () => openDocument(btn.dataset.sha));
  });
}

async function openDocument(sha) {
  const dialog = $("#doc-dialog");
  const body = $("#dialog-body");
  $("#dialog-title").textContent = "Loading…";
  body.innerHTML = "";
  dialog.showModal();
  try {
    const doc = await api(`/v1/documents/${sha}`);
    $("#dialog-title").textContent = doc.title || doc.source_url || sha.slice(0, 12);
    body.innerHTML = `
      <div class="meta" style="margin-bottom:1rem;font-family:var(--font-mono);font-size:0.75rem;color:var(--ink-soft)">
        <div>${escapeHtml(doc.mime || "")} · ${fmtNum(doc.text_chars)} chars · fetched ${escapeHtml((doc.fetched_at || "").slice(0, 19))}</div>
        <div><a href="${escapeHtml(doc.source_url)}" target="_blank" rel="noopener">${escapeHtml(doc.source_url)}</a></div>
        <div>sha256 ${escapeHtml(doc.source_sha256)} · <a href="/v1/raw/${escapeHtml(doc.source_sha256)}" target="_blank" rel="noopener">download raw</a></div>
      </div>
      ${(doc.chunks || []).map((c) => `<div class="chunk"><strong>Chunk ${c.chunk_no}</strong>\n${escapeHtml(c.text)}</div>`).join("")}
    `;
  } catch (err) {
    body.textContent = err.message;
  }
}

async function runReason(ev) {
  ev.preventDefault();
  const question = $("#reason-q").value.trim();
  const mode = $("#reason-mode").value;
  const status = $("#reason-status");
  const out = $("#reason-out");
  const steps = $("#reason-steps");
  const sources = $("#reason-sources");
  out.hidden = true;
  steps.hidden = true;
  sources.innerHTML = "";
  setStatus(status, "Agent working… this can take a while on local LLMs.");

  let path = "/agent/v1/reason";
  let body = { question, top_k: 12 };
  if (mode === "chat") {
    path = "/agent/v1/chat";
  } else if (mode !== "reason") {
    path = "/agent/v1/product";
    body = { topic: question, product: mode, top_k: 12 };
  }

  try {
    const data = await api(path, { method: "POST", body: JSON.stringify(body) });
    const text = data.answer || data.content || "";
    out.hidden = false;
    out.textContent = text;
    if (data.steps?.length) {
      steps.hidden = false;
      steps.innerHTML = `<strong>Orchestration</strong><ol>${data.steps
        .map((s) => {
          if (s.type === "plan") return `<li>Plan: ${escapeHtml((s.queries || []).join(" · "))}</li>`;
          if (s.type === "retrieve") return `<li>Retrieve “${escapeHtml(s.query)}” → ${s.hits} hits</li>`;
          if (s.type === "synthesize") return `<li>Synthesize from ${s.sources_used} excerpts</li>`;
          return `<li>${escapeHtml(JSON.stringify(s))}</li>`;
        })
        .join("")}</ol>`;
    }
    sources.innerHTML = (data.sources || []).map(resultCard).join("");
    bindDocButtons(sources);
    setStatus(status, data.model ? `Done · model ${data.model}` : "Done");
  } catch (err) {
    setStatus(status, err.message);
  }
}

async function loadBrowse() {
  const status = $("#browse-status");
  const q = $("#browse-q").value.trim();
  setStatus(status, "Loading…");
  try {
    const data = await api(
      `/v1/documents?limit=${state.browseLimit}&offset=${state.browseOffset}${q ? `&q=${encodeURIComponent(q)}` : ""}`
    );
    state.browseTotal = data.total;
    const page = Math.floor(state.browseOffset / state.browseLimit) + 1;
    const pages = Math.max(1, Math.ceil(data.total / state.browseLimit));
    $("#browse-page").textContent = `Page ${page} / ${pages} · ${fmtNum(data.total)} docs`;
    $("#browse-prev").disabled = state.browseOffset <= 0;
    $("#browse-next").disabled = state.browseOffset + state.browseLimit >= data.total;
    $("#browse-list").innerHTML = (data.documents || [])
      .map(
        (d) => `
      <button type="button" class="doc-row" data-sha="${escapeHtml(d.source_sha256)}">
        <strong>${escapeHtml(d.title || d.source_url || d.source_sha256)}</strong>
        <span class="meta">${escapeHtml(d.mime || "")} · ${fmtNum(d.text_chars)} chars · ${(d.fetched_at || "").slice(0, 10)}</span>
      </button>`
      )
      .join("");
    $$(".doc-row").forEach((row) => row.addEventListener("click", () => openDocument(row.dataset.sha)));
    setStatus(status, "", false);
  } catch (err) {
    setStatus(status, err.message);
  }
}

function fmtInterval(sec) {
  if (!sec || sec <= 0) return "disabled";
  if (sec < 60) return `every ${sec}s`;
  if (sec < 3600) return `every ${Math.round(sec / 60)}m`;
  if (sec < 86400) return `every ${(sec / 3600).toFixed(sec % 3600 ? 1 : 0)}h`;
  return `every ${(sec / 86400).toFixed(sec % 86400 ? 1 : 0)}d`;
}

async function refreshPipeline() {
  const [stats, crawl, jobs] = await Promise.all([
    api("/v1/stats"),
    api("/v1/crawl/stats"),
    api("/v1/pipeline/jobs"),
  ]);
  $("#pipeline-stats").innerHTML = [
    ["Documents", stats.documents],
    ["Chunks", stats.chunks],
    ["Text chars", stats.text_chars],
    ["Downloads", crawl.downloads],
    ["Frontier queued", crawl.frontier?.queued ?? 0],
    ["Frontier done", crawl.frontier?.done ?? 0],
  ]
    .map(
      ([label, value]) => `
    <div class="stat"><div class="label">${label}</div><div class="value">${fmtNum(value)}</div></div>`
    )
    .join("");

  $("#job-list").innerHTML = (jobs.jobs || [])
    .map(
      (j) => `
    <div class="job">
      <div>
        <h3>${escapeHtml(j.name)}</h3>
        <p>${escapeHtml(j.description)}</p>
        <div class="cadence">${fmtInterval(j.interval_seconds)} · last ${escapeHtml(j.last_status || "never")} · runs ${j.run_count || 0}</div>
      </div>
      <button type="button" data-job="${escapeHtml(j.name)}" class="run-job"${j.enabled ? "" : ""}>Run now</button>
    </div>`
    )
    .join("");

  $$(".run-job").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const log = $("#job-log");
      log.hidden = false;
      log.textContent = `Triggering ${btn.dataset.job}…`;
      try {
        const res = await api("/v1/pipeline/jobs/trigger", {
          method: "POST",
          body: JSON.stringify({ job: btn.dataset.job }),
        });
        log.textContent = JSON.stringify(res, null, 2);
        refreshPipeline();
      } catch (err) {
        log.textContent = err.message;
      }
    });
  });
}

function wire() {
  $$(".nav-btn").forEach((btn) => btn.addEventListener("click", () => showView(btn.dataset.view)));
  $("#search-form").addEventListener("submit", runSearch);
  $("#reason-form").addEventListener("submit", runReason);
  $("#browse-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    state.browseOffset = 0;
    loadBrowse();
  });
  $("#browse-prev").addEventListener("click", () => {
    state.browseOffset = Math.max(0, state.browseOffset - state.browseLimit);
    loadBrowse();
  });
  $("#browse-next").addEventListener("click", () => {
    state.browseOffset += state.browseLimit;
    loadBrowse();
  });

  // Style document open buttons inside result cards without looking like CTAs.
  const style = document.createElement("style");
  style.textContent = `
    button.linkish {
      background: transparent;
      border: 0;
      color: var(--accent-ink);
      padding: 0;
      font: inherit;
      font-family: var(--font-mono);
      font-size: 0.72rem;
      font-weight: 500;
      text-decoration: underline;
      cursor: pointer;
    }
  `;
  document.head.appendChild(style);
}

wire();
refreshMeta();
loadFacets();
setInterval(refreshMeta, 30000);


async function loadSources() {
  try {
    const data = await api("/v1/sources");
    $("#source-list").innerHTML = data.sources.map(s => `<article class="result"><h3>${escapeHtml(s.agency)} · ${escapeHtml(s.id)}</h3><p>${escapeHtml(s.modalities.join(", "))} · ${s.enabled ? "enabled" : "disabled"} · ${s.interval ? `polls every ${s.interval}s` : "robots-aware archive crawl"}</p><p>${escapeHtml(s.url)}</p><p>${escapeHtml(s.status?.error || (s.status?.last_success ? `Last success: ${s.status.last_success}` : "No feed poll recorded"))}</p></article>`).join("");
  } catch (err) { $("#source-list").textContent = err.message; }
}

async function loadGraph() {
  try {
    const [stats, data] = await Promise.all([api("/v1/graph/stats"), api(`/v1/graph/entities?q=${encodeURIComponent($("#graph-q").value)}`)]);
    $("#graph-stats").textContent = `${stats.documents} documents · ${stats.entities} entities · ${stats.edges} evidence links`;
    $("#graph-entities").innerHTML = data.entities.map(e => `<article class="result"><button type="button" data-entity="${escapeHtml(e.id)}">${escapeHtml(e.label)}</button><p>${escapeHtml(e.kind)} · ${e.documents} documents</p></article>`).join("") || "No entities yet. Ingest documents or run graph_rebuild for the existing archive.";
  } catch (err) { $("#graph-stats").textContent = err.message; }
}
$("#graph-form").addEventListener("submit", e => { e.preventDefault(); loadGraph(); });
$("#graph-entities").addEventListener("click", async e => {
  const button = e.target.closest("[data-entity]");
  if (!button) return;
  try {
    const data = await api(`/v1/graph/entities/${button.dataset.entity}`);
    $("#graph-detail").innerHTML = data.edges.map(x => `<article class="result"><h3>${escapeHtml(x.subject_label)} ↔ ${escapeHtml(x.object_label)}</h3><p>${escapeHtml(x.relation || "co_mentioned")} · inferred candidate</p><blockquote>${escapeHtml(x.evidence)}</blockquote><p>${escapeHtml(x.url)}</p><p>Characters ${x.start}–${x.end}</p><a href="/v1/raw/${escapeHtml(x.sha)}" target="_blank" rel="noopener">Archived source</a> <button data-correlate="${escapeHtml(x.sha)}">Related documents</button> <button data-spatial="${escapeHtml(x.sha)}">Nearby (geo)</button></article>`).join("") || `${data.mentions.length} mentions; no sentence-level connections found.`;
  } catch (err) { $("#graph-detail").textContent = err.message; }
});
$("#graph-detail").addEventListener("click", async e => {
  const correlate = e.target.closest("[data-correlate]");
  const spatial = e.target.closest("[data-spatial]");
  const button = correlate || spatial;
  if (!button) return;
  const mode = spatial ? "spatial" : "entity";
  const sha = button.dataset.correlate || button.dataset.spatial;
  try {
    const data = await api(`/v1/graph/correlations/${sha}?mode=${mode}`);
    const out = document.createElement("div");
    if (mode === "spatial") {
      out.innerHTML = (data.results || []).map(x =>
        `<p>${escapeHtml(x.title || x.sha)} · ${x.distance_km} km · score ${Number(x.score).toFixed(3)} · <a href="/v1/raw/${escapeHtml(x.sha)}">source</a></p>`
      ).join("") || data.note || "No nearby geo documents.";
    } else {
      out.innerHTML = (data.results || []).map(x =>
        `<p>${escapeHtml(x.title)} · shared: ${escapeHtml(x.evidence_entities)} · score ${Number(x.score).toFixed(3)} · <a href="/v1/raw/${escapeHtml(x.sha)}">source</a></p>`
      ).join("") || "No shared-entity correlations yet.";
    }
    button.replaceWith(out);
  } catch (err) { button.textContent = err.message; }
});

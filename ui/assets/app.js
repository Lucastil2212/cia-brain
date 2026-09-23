const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  browseOffset: 0,
  browseLimit: 25,
  browseTotal: 0,
  config: null,
  sources: [],
  graphSim: null,
  selectedEntity: null,
  token: localStorage.getItem("cia_brain_token") || "",
  user: null,
};

async function api(path, options = {}) {
  const headers = { "content-type": "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, {
    ...options,
    headers,
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
  if (!el) return;
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
  if (name === "gallery") loadGallery();
  if (name === "search") loadCatalogViz();
  if (name === "account") loadAccount();
}

function renderBarChart(el, rows, { labelKey = "label", valueKey = "value", maxBars = 8 } = {}) {
  if (!el) return;
  const data = (rows || []).slice(0, maxBars);
  if (!data.length) {
    el.innerHTML = `<p class="chart-bar-label">No data yet</p>`;
    return;
  }
  const max = Math.max(...data.map((r) => Number(r[valueKey]) || 0), 1);
  el.innerHTML = data
    .map((r, i) => {
      const value = Number(r[valueKey]) || 0;
      const pct = Math.max(4, Math.round((value / max) * 100));
      return `<div class="chart-bar-row" style="animation-delay:${i * 40}ms">
        <span class="chart-bar-label" title="${escapeHtml(r[labelKey])}">${escapeHtml(r[labelKey])}</span>
        <div class="chart-bar-track"><div class="chart-bar-fill" style="width:${pct}%"></div></div>
        <span class="chart-bar-value">${fmtNum(value)}</span>
      </div>`;
    })
    .join("");
}

function resultCard(r, index) {
  const title = r.title || "(untitled)";
  const score = typeof r.score === "number" ? r.score.toFixed(4) : "—";
  const mods = (r.modalities || []).map((m) => `<span class="modality-tag">${escapeHtml(m)}</span>`).join(" ");
  return `
    <article class="result" style="animation-delay:${index * 40}ms">
      <h3>${escapeHtml(title)}</h3>
      <div class="meta">
        <span class="score">RRF ${score}</span>
        <span>${escapeHtml(r.mime || "unknown")}</span>
        <span>chunk ${r.chunk_no ?? 0}</span>
        <span>${escapeHtml((r.fetched_at || "").slice(0, 10))}</span>
        ${mods}
        <a href="${escapeHtml(r.source_url)}" target="_blank" rel="noopener">source</a>
        <a href="/v1/raw/${escapeHtml(r.source_sha256)}" target="_blank" rel="noopener">raw</a>
        <button type="button" data-sha="${escapeHtml(r.source_sha256)}" class="linkish open-doc">open record</button>
      </div>
      <p class="excerpt">${escapeHtml(r.text)}</p>
    </article>
  `;
}

async function refreshMeta() {
  try {
    const [stats, config] = await Promise.all([api("/v1/stats"), api("/v1/config/public")]);
    state.config = config;
    const queued = stats.frontier?.queued ?? 0;
    $("#top-meta").textContent =
      `${fmtNum(stats.documents)} holdings · ${fmtNum(stats.chunks)} passages · queued ${fmtNum(queued)}`;
  } catch (err) {
    $("#top-meta").textContent = `catalog offline: ${err.message}`;
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
    /* optional */
  }
}

async function loadCatalogViz() {
  const row = $("#catalog-viz");
  try {
    const [facets, sources] = await Promise.all([api("/v1/facets"), api("/v1/sources")]);
    state.sources = sources.sources || [];
    row.hidden = false;
    renderBarChart(
      $("#mime-chart"),
      (facets.mime || []).map((m) => ({ label: m.value, value: m.count })),
    );
    const modalityCounts = {};
    for (const s of state.sources) {
      for (const m of s.modalities || []) {
        modalityCounts[m] = (modalityCounts[m] || 0) + (s.enabled ? 1 : 0);
      }
    }
    renderBarChart(
      $("#modality-chart"),
      Object.entries(modalityCounts).map(([label, value]) => ({ label, value })),
    );
  } catch {
    row.hidden = true;
  }
}

async function runSearch(ev) {
  ev.preventDefault();
  const q = $("#q").value.trim();
  const top_k = Number($("#top-k").value);
  const mime = $("#mime-filter").value || null;
  const status = $("#search-status");
  const box = $("#results");
  setStatus(status, "Searching the catalog…");
  box.innerHTML = "";
  try {
    const data = await api("/v1/search", {
      method: "POST",
      body: JSON.stringify({ query: q, top_k, mime }),
    });
    setStatus(status, `${data.count} results for “${data.query}”`);
    box.innerHTML = (data.results || []).map(resultCard).join("") || "<p class='status'>No matches in holdings.</p>";
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

function sparklineSvg(values) {
  const nums = values.map(Number).filter((n) => Number.isFinite(n));
  if (nums.length < 2) return "";
  const w = 220;
  const h = 48;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const pts = nums
    .map((v, i) => {
      const x = (i / (nums.length - 1)) * w;
      const y = h - ((v - min) / span) * (h - 6) - 3;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return `<svg class="sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><polyline fill="none" stroke="#0d5c4d" stroke-width="2" points="${pts}"/></svg>`;
}

function multimodalBlock(doc) {
  const parts = [];
  const mime = doc.mime || "";
  const isImage = mime.startsWith("image/") || (doc.modalities || []).includes("image");
  if (isImage) {
    parts.push(`<div class="media-frame"><img src="/v1/raw/${escapeHtml(doc.source_sha256)}" alt="${escapeHtml(doc.title || "Archived image")}" loading="lazy" /></div>`);
  }
  if (doc.geometry?.type === "Point" && Array.isArray(doc.geometry.coordinates)) {
    const [lon, lat] = doc.geometry.coordinates;
    const x = ((Number(lon) + 180) / 360) * 100;
    const y = ((90 - Number(lat)) / 180) * 100;
    parts.push(`<div class="geo-dot" style="--x:${x}%;--y:${y}%" title="${lat}, ${lon}" role="img" aria-label="Point at ${lat}, ${lon}"></div>
      <p class="meta">Point geometry · ${Number(lat).toFixed(4)}, ${Number(lon).toFixed(4)}</p>`);
  }
  if (doc.measurements && typeof doc.measurements === "object") {
    const entries = Object.entries(doc.measurements).slice(0, 12);
    const numeric = entries.map(([, v]) => Number(v)).filter((n) => Number.isFinite(n));
    parts.push(`<h4>Telemetry</h4>${sparklineSvg(numeric)}
      <table class="measure-table"><tbody>${entries
        .map(([k, v]) => `<tr><th>${escapeHtml(k)}</th><td>${escapeHtml(v)}</td></tr>`)
        .join("")}</tbody></table>`);
  }
  if (doc.media) {
    const links = [];
    if (doc.media.assets) links.push(`<a href="${escapeHtml(doc.media.assets)}" target="_blank" rel="noopener">asset index</a>`);
    for (const link of doc.media.links || []) {
      const href = link.href || link;
      if (href) links.push(`<a href="${escapeHtml(href)}" target="_blank" rel="noopener">${escapeHtml(link.rel || "media")}</a>`);
    }
    parts.push(`<p class="meta">Media · ${escapeHtml(doc.media.type || "reference")} · ${links.join(" · ") || "no links"}</p>`);
  }
  if (doc.graph?.entities?.length) {
    parts.push(`<p class="meta">Graph entities · ${doc.graph.entities
      .slice(0, 12)
      .map((e) => `${escapeHtml(e.label)} (${escapeHtml(e.kind)})`)
      .join(" · ")}</p>`);
  }
  return parts.join("");
}

async function openDocument(sha) {
  const dialog = $("#doc-dialog");
  const body = $("#dialog-body");
  $("#dialog-title").textContent = "Loading record…";
  body.innerHTML = "";
  dialog.showModal();
  try {
    const doc = await api(`/v1/documents/${sha}`);
    $("#dialog-title").textContent = doc.title || doc.source_url || sha.slice(0, 12);
    const mods = (doc.modalities || []).map((m) => `<span class="modality-tag">${escapeHtml(m)}</span>`).join(" ");
    body.innerHTML = `
      <div class="meta" style="margin-bottom:1rem;font-family:var(--font-mono);font-size:0.75rem;color:var(--ink-soft)">
        <div>${escapeHtml(doc.mime || "")} · ${fmtNum(doc.text_chars)} chars · fetched ${escapeHtml((doc.fetched_at || "").slice(0, 19))}
          ${doc.agency ? ` · ${escapeHtml(doc.agency)}` : ""} ${mods}</div>
        <div><a href="${escapeHtml(doc.source_url)}" target="_blank" rel="noopener">${escapeHtml(doc.source_url)}</a></div>
        <div>sha256 ${escapeHtml(doc.source_sha256)} · <a href="/v1/raw/${escapeHtml(doc.source_sha256)}" target="_blank" rel="noopener">download raw</a></div>
      </div>
      ${multimodalBlock(doc)}
      ${(doc.chunks || []).map((c) => `<div class="chunk"><strong>Passage ${c.chunk_no}</strong>\n${escapeHtml(c.text)}</div>`).join("")
        || (doc.text ? `<div class="chunk">${escapeHtml(doc.text.slice(0, 8000))}</div>` : "")}
    `;
  } catch (err) {
    body.textContent = err.message;
  }
}

/* —— Force-directed knowledge graph —— */
const KIND_COLORS = {
  organization: "#0d5c4d",
  person: "#8b6914",
  location: "#2f5d8c",
  date: "#5c6b63",
  event: "#6b3a5c",
  group: "#3d6b4f",
  named_entity: "#4a5c56",
};

function createForceGraph(canvas) {
  const ctx = canvas.getContext("2d");
  let nodes = [];
  let links = [];
  let raf = 0;
  let running = false;
  let scale = 1;
  let panX = 0;
  let panY = 0;
  let drag = null;
  let hover = null;
  let panning = false;
  let panLast = null;
  let onSelect = null;
  const positions = new Map();

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const cssW = Math.max(320, rect.width || 640);
    const cssH = Math.max(280, cssW * 0.55);
    canvas.width = Math.floor(cssW * dpr);
    canvas.height = Math.floor(cssH * dpr);
    canvas.style.width = `${cssW}px`;
    canvas.style.height = `${cssH}px`;
    return { w: canvas.width, h: canvas.height };
  }

  function setData(payload, selectCb, { preserve = true } = {}) {
    onSelect = selectCb;
    const { w, h } = resize();
    const map = new Map();
    for (const n of payload.nodes || []) {
      const prior = preserve ? positions.get(n.id) : null;
      const node = {
        id: n.id,
        label: n.label || String(n.id).slice(0, 8),
        kind: n.kind || "named_entity",
        degree: n.degree || n.weight || 1,
        x: prior?.x ?? w * (0.2 + Math.random() * 0.6),
        y: prior?.y ?? h * (0.2 + Math.random() * 0.6),
        vx: 0,
        vy: 0,
      };
      map.set(n.id, node);
    }
    links = (payload.links || [])
      .filter((e) => map.has(e.source) && map.has(e.target))
      .map((e) => ({
        source: map.get(e.source),
        target: map.get(e.target),
        relation: e.relation || "co_mentioned",
        weight: e.weight || 1,
      }));
    nodes = [...map.values()];
    for (const n of nodes) positions.set(n.id, { x: n.x, y: n.y });
    if (!running) {
      running = true;
      loop();
    }
    renderLegend(nodes);
  }

  function step() {
    const cx = canvas.width / 2;
    const cy = canvas.height / 2;
    const nCount = Math.max(nodes.length, 1);
    const repulse = Math.min(2800, 400 + nCount * 40);
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i];
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let dist = Math.hypot(dx, dy) || 0.01;
        const force = repulse / (dist * dist);
        dx = (dx / dist) * force;
        dy = (dy / dist) * force;
        a.vx += dx;
        a.vy += dy;
        b.vx -= dx;
        b.vy -= dy;
      }
    }
    for (const link of links) {
      const a = link.source;
      const b = link.target;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.hypot(dx, dy) || 0.01;
      const ideal = 90 + Math.min(80, (link.weight || 1) * 4);
      const force = (dist - ideal) * 0.025;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }
    for (const n of nodes) {
      n.vx += (cx - n.x) * 0.004;
      n.vy += (cy - n.y) * 0.004;
      n.vx *= 0.82;
      n.vy *= 0.82;
      if (drag !== n) {
        n.x += n.vx;
        n.y += n.vy;
      }
      positions.set(n.id, { x: n.x, y: n.y });
    }
  }

  function draw() {
    const w = canvas.width;
    const h = canvas.height;
    const dpr = window.devicePixelRatio || 1;
    ctx.clearRect(0, 0, w, h);
    ctx.save();
    ctx.translate(panX, panY);
    ctx.scale(scale, scale);
    for (const link of links) {
      const typed = link.relation && link.relation !== "co_mentioned";
      ctx.beginPath();
      ctx.strokeStyle = typed ? "rgba(13,92,77,0.55)" : "rgba(26,46,40,0.22)";
      ctx.lineWidth = Math.min(4, 1 + Math.log2(1 + (link.weight || 1)));
      ctx.moveTo(link.source.x, link.source.y);
      ctx.lineTo(link.target.x, link.target.y);
      ctx.stroke();
    }
    for (const n of nodes) {
      const selected = n.id === state.selectedEntity;
      const r = (selected || n === hover ? 10 : 6) + Math.min(6, Math.log2(1 + (n.degree || 1)));
      ctx.beginPath();
      ctx.fillStyle = KIND_COLORS[n.kind] || KIND_COLORS.named_entity;
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fill();
      if (selected) {
        ctx.strokeStyle = "#c5a35a";
        ctx.lineWidth = 2.5;
        ctx.stroke();
      }
      ctx.fillStyle = "#1a2e28";
      ctx.font = `${Math.round(11 * dpr)}px "IBM Plex Mono", monospace`;
      ctx.fillText((n.label || "").slice(0, 28), n.x + r + 4, n.y + 4);
    }
    ctx.restore();
  }

  function loop() {
    step();
    draw();
    raf = requestAnimationFrame(loop);
  }

  function toWorld(ev) {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const x = ((ev.clientX - rect.left) * dpr - panX) / scale;
    const y = ((ev.clientY - rect.top) * dpr - panY) / scale;
    return { x, y };
  }

  function hit(ev) {
    const p = toWorld(ev);
    let best = null;
    let bestDist = 18;
    for (const n of nodes) {
      const d = Math.hypot(n.x - p.x, n.y - p.y);
      if (d < bestDist) {
        best = n;
        bestDist = d;
      }
    }
    return best;
  }

  canvas.addEventListener("pointerdown", (ev) => {
    const n = hit(ev);
    if (n) {
      drag = n;
      canvas.setPointerCapture(ev.pointerId);
      state.selectedEntity = n.id;
      if (onSelect) onSelect(n.id, { fromCanvas: true });
    } else {
      panning = true;
      panLast = { x: ev.clientX, y: ev.clientY };
      canvas.setPointerCapture(ev.pointerId);
    }
  });
  canvas.addEventListener("pointermove", (ev) => {
    if (drag) {
      const p = toWorld(ev);
      drag.x = p.x;
      drag.y = p.y;
      drag.vx = 0;
      drag.vy = 0;
    } else if (panning && panLast) {
      const dpr = window.devicePixelRatio || 1;
      panX += (ev.clientX - panLast.x) * dpr;
      panY += (ev.clientY - panLast.y) * dpr;
      panLast = { x: ev.clientX, y: ev.clientY };
    } else {
      hover = hit(ev);
      canvas.style.cursor = hover ? "pointer" : "grab";
    }
  });
  canvas.addEventListener("pointerup", () => {
    drag = null;
    panning = false;
    panLast = null;
  });
  canvas.addEventListener(
    "wheel",
    (ev) => {
      ev.preventDefault();
      const factor = ev.deltaY < 0 ? 1.08 : 0.92;
      scale = Math.min(3.5, Math.max(0.35, scale * factor));
    },
    { passive: false },
  );
  window.addEventListener("resize", () => {
    if (nodes.length) resize();
  });

  return {
    setData,
    highlight(id) {
      state.selectedEntity = id;
    },
    destroy() {
      cancelAnimationFrame(raf);
      running = false;
    },
  };
}

function renderLegend(nodes) {
  const el = $("#graph-legend");
  if (!el) return;
  const kinds = [...new Set(nodes.map((n) => n.kind))];
  el.innerHTML = kinds
    .map(
      (k) =>
        `<span><i style="background:${KIND_COLORS[k] || KIND_COLORS.named_entity}"></i>${escapeHtml(String(k).replaceAll("_", " "))}</span>`,
    )
    .join("");
}

function fillEntitySelect(sel, entities, selected) {
  if (!sel) return;
  sel.innerHTML = entities
    .map(
      (e) =>
        `<option value="${escapeHtml(e.id)}"${e.id === selected ? " selected" : ""}>${escapeHtml(e.label)} (${escapeHtml(e.kind)})</option>`,
    )
    .join("");
}

function renderProfile(profile) {
  const box = $("#graph-profile");
  if (!box) return;
  if (!profile) {
    box.innerHTML = "";
    return;
  }
  const relMax = Math.max(...(profile.relations || []).map((x) => x.count), 1);
  box.innerHTML = `
    <h3 style="margin:0;font-family:var(--font-display)">${escapeHtml(profile.label)}</h3>
    <p class="meta">${escapeHtml(profile.kind)} · degree ${fmtNum(profile.degree)}</p>
    <div class="profile-grid">
      <div class="profile-stat"><div class="label">Degree</div><div class="value">${fmtNum(profile.degree)}</div></div>
      <div class="profile-stat"><div class="label">Neighbors</div><div class="value">${fmtNum((profile.neighbors || []).length)}</div></div>
    </div>
    <div class="chart">${(profile.relations || [])
      .slice(0, 6)
      .map((r) => {
        const pct = Math.max(4, Math.round((r.count / relMax) * 100));
        return `<div class="chart-bar-row"><span class="chart-bar-label">${escapeHtml(r.relation)}</span><div class="chart-bar-track"><div class="chart-bar-fill" style="width:${pct}%"></div></div><span class="chart-bar-value">${fmtNum(r.count)}</span></div>`;
      })
      .join("")}</div>
    <p class="meta" style="margin-top:0.75rem">Top neighbors</p>
    ${(profile.neighbors || [])
      .slice(0, 8)
      .map(
        (n) =>
          `<button type="button" class="entity-chip" data-entity="${escapeHtml(n.id)}" data-kind="${escapeHtml(n.kind)}"><span>${escapeHtml(n.label)}</span><small>${escapeHtml(n.relation)} · w${n.weight}</small></button>`,
      )
      .join("")}
  `;
}

async function showOverview() {
  const relation = $("#graph-relation")?.value || "";
  const data = await api(
    `/v1/graph/overview?limit=40${relation ? `&relation=${encodeURIComponent(relation)}` : ""}`,
  );
  if (!state.graphSim) state.graphSim = createForceGraph($("#graph-canvas"));
  state.graphSim.setData(
    {
      nodes: data.nodes || [],
      links: (data.links || []).map((l) => ({
        source: l.source,
        target: l.target,
        relation: l.relation,
        weight: l.weight,
      })),
    },
    (id) => selectEntity(id, { mode: "neighborhood" }),
    { preserve: false },
  );
  $("#graph-detail").innerHTML = `<p class="status">${fmtNum((data.nodes || []).length)} hubs · ${fmtNum((data.links || []).length)} weighted links · ${escapeHtml(data.method || "")}</p>`;
  if ((data.nodes || []).length && !state.selectedEntity) {
    state.selectedEntity = data.nodes[0].id;
  }
  if (state.selectedEntity) {
    try {
      const nb = await api(`/v1/graph/entities/${state.selectedEntity}`);
      renderProfile(nb.profile);
    } catch {
      /* overview without profile is fine */
    }
  }
}

async function selectEntity(entityId, opts = {}) {
  state.selectedEntity = entityId;
  if (opts.mode && $("#graph-mode")) $("#graph-mode").value = opts.mode;
  $$(".entity-chip").forEach((el) => el.classList.toggle("is-active", el.dataset.entity === entityId));
  if (state.graphSim) state.graphSim.highlight(entityId);
  const detail = $("#graph-detail");
  detail.innerHTML = `<p class="status">Loading neighborhood…</p>`;
  try {
    const data = await api(`/v1/graph/entities/${entityId}`);
    renderProfile(data.profile);
    const nodes = new Map();
    const links = [];
    if (data.profile) {
      nodes.set(entityId, {
        id: entityId,
        label: data.profile.label,
        kind: data.profile.kind,
        degree: data.profile.degree,
      });
    }
    for (const edge of data.edges || []) {
      nodes.set(edge.subject, {
        id: edge.subject,
        label: edge.subject_label,
        kind: nodes.get(edge.subject)?.kind || "named_entity",
      });
      nodes.set(edge.object, {
        id: edge.object,
        label: edge.object_label,
        kind: nodes.get(edge.object)?.kind || "named_entity",
      });
      links.push({
        source: edge.subject,
        target: edge.object,
        relation: edge.relation,
        weight: 1,
      });
    }
    for (const n of data.profile?.neighbors || []) {
      if (nodes.has(n.id)) nodes.get(n.id).kind = n.kind;
      else nodes.set(n.id, { id: n.id, label: n.label, kind: n.kind, degree: n.weight });
    }
    for (const chip of $$("#graph-entities .entity-chip, #graph-profile .entity-chip")) {
      const id = chip.dataset.entity;
      if (nodes.has(id)) {
        nodes.set(id, {
          ...nodes.get(id),
          label: chip.querySelector("span")?.textContent || nodes.get(id).label,
          kind: chip.dataset.kind || nodes.get(id).kind,
        });
      }
    }
    if (!nodes.size) {
      nodes.set(entityId, {
        id: entityId,
        label: data.profile?.label || entityId.slice(0, 8),
        kind: data.profile?.kind || "named_entity",
      });
    }
    if (!state.graphSim) state.graphSim = createForceGraph($("#graph-canvas"));
    state.graphSim.setData({ nodes: [...nodes.values()], links }, (id) => selectEntity(id), {
      preserve: Boolean(opts.fromCanvas),
    });

    detail.innerHTML =
      (data.edges || [])
        .slice(0, 40)
        .map(
          (x) => `<article class="result">
          <h3>${escapeHtml(x.subject_label)} · ${escapeHtml(x.relation || "co_mentioned")} · ${escapeHtml(x.object_label)}</h3>
          <blockquote>${escapeHtml(x.evidence)}</blockquote>
          <p class="meta">${escapeHtml(x.title || "")} · ${escapeHtml(x.url || "")}</p>
          <p>
            <a href="/v1/raw/${escapeHtml(x.sha)}" target="_blank" rel="noopener">Archived source</a>
            <button type="button" class="linkish" data-correlate="${escapeHtml(x.sha)}">Related</button>
            <button type="button" class="linkish" data-spatial="${escapeHtml(x.sha)}">Nearby</button>
            <button type="button" class="linkish open-doc" data-sha="${escapeHtml(x.sha)}">Open record</button>
          </p>
        </article>`,
        )
        .join("") ||
      `<p>${(data.mentions || []).length} mentions; no sentence-level connections yet.</p>`;
    bindDocButtons(detail);
  } catch (err) {
    detail.textContent = err.message;
  }
}

async function runPathFind() {
  const source = $("#graph-path-from").value;
  const target = $("#graph-path-to").value;
  if (!source || !target) return;
  const detail = $("#graph-detail");
  detail.innerHTML = `<p class="status">Searching path…</p>`;
  try {
    const data = await api(
      `/v1/graph/path?source=${encodeURIComponent(source)}&target=${encodeURIComponent(target)}&max_depth=6`,
    );
    if (!data.path?.length) {
      detail.innerHTML = `<p class="status">${escapeHtml(data.note || "No path found")}</p>`;
      return;
    }
    if (!state.graphSim) state.graphSim = createForceGraph($("#graph-canvas"));
    state.graphSim.setData(
      {
        nodes: data.nodes || [],
        links: (data.edges || []).map((e) => ({
          source: e.subject,
          target: e.object,
          relation: e.relation,
          weight: 2,
        })),
      },
      (id) => selectEntity(id),
      { preserve: false },
    );
    state.selectedEntity = target;
    detail.innerHTML = `<p class="status">${data.hops} hop${data.hops === 1 ? "" : "s"} · inferred path</p>${(data.edges || [])
      .map(
        (e, i) => `<div class="path-hop"><strong>${i + 1}.</strong> ${escapeHtml(e.subject_label)}
        — <em>${escapeHtml(e.relation)}</em> → ${escapeHtml(e.object_label)}
        <blockquote>${escapeHtml(e.evidence)}</blockquote>
        <button type="button" class="linkish open-doc" data-sha="${escapeHtml(e.sha)}">Open record</button>
      </div>`,
      )
      .join("")}`;
    bindDocButtons(detail);
    try {
      const nb = await api(`/v1/graph/entities/${target}`);
      renderProfile(nb.profile);
    } catch {
      /* ignore */
    }
  } catch (err) {
    detail.textContent = err.message;
  }
}

async function loadGraphAnalytics() {
  const analytics = await api("/v1/graph/analytics?hub_limit=12");
  $("#graph-stats").textContent =
    `${fmtNum(analytics.documents)} documents · ${fmtNum(analytics.entities)} entities · ${fmtNum(analytics.edges)} links · ${fmtNum(analytics.typed_edges || 0)} typed · NER ${analytics.ner_backend || "rules"}`;
  renderBarChart(
    $("#graph-relation-chart"),
    (analytics.relations || []).map((r) => ({ label: r.relation, value: r.count })),
  );
  renderBarChart(
    $("#graph-kind-chart"),
    (analytics.kinds || []).map((k) => ({ label: k.kind, value: k.count })),
  );
  renderBarChart(
    $("#graph-hub-chart"),
    (analytics.hubs || []).map((h) => ({ label: h.label, value: h.degree })),
  );
  const relSel = $("#graph-relation");
  if (relSel) {
    const current = relSel.value;
    relSel.innerHTML =
      `<option value="">All relations</option>` +
      (analytics.relations || [])
        .map(
          (r) =>
            `<option value="${escapeHtml(r.relation)}">${escapeHtml(r.relation)} (${r.count})</option>`,
        )
        .join("");
    if ([...relSel.options].some((o) => o.value === current)) relSel.value = current;
  }
  return analytics;
}

async function loadGraph() {
  try {
    const q = $("#graph-q").value.trim();
    const mode = $("#graph-mode")?.value || "overview";
    if ($("#graph-path-tools")) $("#graph-path-tools").hidden = mode !== "path";

    let analytics;
    try {
      analytics = await loadGraphAnalytics();
    } catch (err) {
      $("#graph-stats").textContent =
        `${err.message} — rebuild/restart the API and run graph_rebuild if normalized docs exist but the graph is empty.`;
      return;
    }

    const data = await api(`/v1/graph/entities?q=${encodeURIComponent(q)}&limit=40`);
    let entities = data.entities || [];
    if (!entities.length && analytics.hubs?.length) {
      entities = analytics.hubs.map((h) => ({
        id: h.id,
        label: h.label,
        kind: h.kind,
        documents: h.documents,
      }));
    }
    $("#graph-entities").innerHTML =
      entities
        .map(
          (e) => `<button type="button" class="entity-chip" data-entity="${escapeHtml(e.id)}" data-kind="${escapeHtml(e.kind)}">
            <span>${escapeHtml(e.label)}</span>
            <small>${escapeHtml(e.kind)} · ${e.documents ?? e.degree ?? 0}</small>
          </button>`,
        )
        .join("") ||
      "<p class='status'>No entities yet. Ingest documents or run job graph_rebuild.</p>";

    fillEntitySelect($("#graph-path-from"), entities, entities[0]?.id);
    fillEntitySelect($("#graph-path-to"), entities, entities[1]?.id || entities[0]?.id);

    if (!state.graphSim) state.graphSim = createForceGraph($("#graph-canvas"));

    if (mode === "path") {
      $("#graph-detail").innerHTML = `<p class="status">Choose two entities and run Find path.</p>`;
      if (entities.length >= 2) await runPathFind();
      return;
    }
    if (mode === "neighborhood" || q) {
      const focus =
        state.selectedEntity && entities.some((e) => e.id === state.selectedEntity)
          ? state.selectedEntity
          : entities[0]?.id;
      if (focus) await selectEntity(focus);
      else {
        state.graphSim.setData({ nodes: [], links: [] });
        $("#graph-detail").innerHTML = "";
        renderProfile(null);
      }
      return;
    }
    await showOverview();
  } catch (err) {
    $("#graph-stats").textContent = err.message;
  }
}

async function loadSources() {
  try {
    const data = await api("/v1/sources");
    state.sources = data.sources || [];
    const enabled = state.sources.filter((s) => s.enabled);
    const byKind = {};
    for (const s of enabled) byKind[s.kind] = (byKind[s.kind] || 0) + 1;
    $("#source-viz").innerHTML = `<figure class="viz-panel"><figcaption>Enabled sources by kind</figcaption><div id="source-kind-chart" class="chart"></div></figure>
      <figure class="viz-panel"><figcaption>Agencies represented</figcaption><div id="source-agency-chart" class="chart"></div></figure>`;
    renderBarChart(
      $("#source-kind-chart"),
      Object.entries(byKind).map(([label, value]) => ({ label, value })),
    );
    const agencies = {};
    for (const s of enabled) agencies[s.agency] = (agencies[s.agency] || 0) + 1;
    renderBarChart(
      $("#source-agency-chart"),
      Object.entries(agencies).map(([label, value]) => ({ label, value })),
    );

    $("#source-list").innerHTML = state.sources
      .map(
        (s) => `<article class="collection">
        <h3>${escapeHtml(s.agency)} · <span class="modality-tag">${escapeHtml(s.id)}</span></h3>
        <p class="meta">${escapeHtml((s.modalities || []).join(" · "))} · ${s.enabled ? "on shelf" : "not selected"} · ${
          s.interval ? `polls every ${s.interval}s` : "robots-aware archive crawl"
        }</p>
        <p><a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.url)}</a></p>
        <p class="meta">${escapeHtml(
          s.status?.error || (s.status?.last_success ? `Last success: ${s.status.last_success}` : "No feed poll recorded"),
        )}</p>
      </article>`,
      )
      .join("");
  } catch (err) {
    $("#source-list").textContent = err.message;
  }
}

function galleryPreview(doc) {
  const mime = doc.mime || "";
  if (mime.startsWith("image/")) {
    return `<img class="gallery-thumb" src="/v1/raw/${escapeHtml(doc.source_sha256)}" alt="" loading="lazy" />`;
  }
  if (mime.includes("geo") || /earthquake|weather\.gov|geometry/i.test(doc.source_url || "")) {
    return `<div class="gallery-placeholder">Geospatial / alert record</div>`;
  }
  if (/solar-wind|timeseries|swpc/i.test(doc.source_url || "") || mime.includes("json")) {
    return `<div class="gallery-placeholder">Structured / telemetry record</div>`;
  }
  return `<div class="gallery-placeholder">${escapeHtml((mime || "document").slice(0, 40))}</div>`;
}

async function loadAccount() {
  const status = $("#account-status");
  try {
    if (state.token) {
      const me = await api("/v1/auth/me");
      state.user = me.user;
      setStatus(status, `Signed in as ${me.user.email}`);
      const keys = await api("/v1/auth/keys");
      $("#key-list").innerHTML =
        (keys.keys || [])
          .map(
            (k) => `<article class="collection">
            <h3>${escapeHtml(k.name)} · <span class="modality-tag">${escapeHtml(k.key_prefix)}…</span></h3>
            <p class="meta">${k.rate_limit_per_minute}/min · ${k.rate_limit_per_day}/day · ${k.is_active ? "active" : "revoked"}</p>
            ${k.is_active ? `<button type="button" class="pager" data-revoke="${escapeHtml(k.id)}">Revoke</button>` : ""}
          </article>`,
          )
          .join("") || "<p class='status'>No API keys yet.</p>";
      $$("[data-revoke]").forEach((btn) =>
        btn.addEventListener("click", async () => {
          await api(`/v1/auth/keys/${btn.dataset.revoke}`, { method: "DELETE" });
          loadAccount();
        }),
      );
    } else {
      state.user = null;
      setStatus(
        status,
        state.config?.auth_required
          ? "Authentication required for catalog API calls. Sign in or register."
          : "Optional account — mint API keys for programmatic access.",
      );
      $("#key-list").innerHTML = "";
    }
  } catch (err) {
    state.token = "";
    localStorage.removeItem("cia_brain_token");
    setStatus(status, err.message);
  }
}

async function authLogin(ev) {
  ev.preventDefault();
  const email = $("#auth-email").value.trim();
  const password = $("#auth-password").value;
  try {
    const data = await api("/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    state.token = data.access_token;
    localStorage.setItem("cia_brain_token", state.token);
    $("#auth-password").value = "";
    loadAccount();
  } catch (err) {
    setStatus($("#account-status"), err.message);
  }
}

async function authRegister() {
  const email = $("#auth-email").value.trim();
  const password = $("#auth-password").value;
  const display_name = $("#auth-name").value.trim();
  try {
    const data = await api("/v1/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password, display_name }),
    });
    state.token = data.access_token;
    localStorage.setItem("cia_brain_token", state.token);
    $("#auth-password").value = "";
    loadAccount();
  } catch (err) {
    setStatus($("#account-status"), err.message);
  }
}

function authLogout() {
  state.token = "";
  state.user = null;
  localStorage.removeItem("cia_brain_token");
  $("#key-secret").hidden = true;
  loadAccount();
}

async function mintKey() {
  try {
    const data = await api("/v1/auth/keys", {
      method: "POST",
      body: JSON.stringify({ name: $("#key-name").value.trim() || "default" }),
    });
    const box = $("#key-secret");
    box.hidden = false;
    box.textContent = `${data.warning}\n\n${data.api_key}`;
    loadAccount();
  } catch (err) {
    setStatus($("#account-status"), err.message);
  }
}

async function loadGallery() {
  const status = $("#gallery-status");
  const modality = $("#gallery-modality").value;
  setStatus(status, "Pulling multimodal holdings…");
  try {
    // Prefer image MIME shelf, then broaden.
    const mimeQuery = modality === "image" ? "image/" : "";
    const data = await api(
      `/v1/documents?limit=48&offset=0${mimeQuery ? `&mime=${encodeURIComponent(mimeQuery)}` : ""}`,
    );
    let docs = data.documents || [];
    if (modality && modality !== "image") {
      // Heuristic filter on URL/title for feed modalities until index stores modalities.
      const re = {
        geospatial: /earthquake|weather\.gov|alert|geojson|usgs|nws/i,
        telemetry: /solar-wind|swpc|timeseries|propagated/i,
        video: /video|nasa/i,
        audio: /audio|nasa/i,
      }[modality];
      if (re) docs = docs.filter((d) => re.test(`${d.title} ${d.source_url} ${d.mime}`));
    }
    $("#gallery-grid").innerHTML =
      docs
        .map(
          (d) => `<button type="button" class="gallery-item" data-sha="${escapeHtml(d.source_sha256)}">
          ${galleryPreview(d)}
          <strong>${escapeHtml((d.title || d.source_url || "").slice(0, 80))}</strong>
          <span class="meta">${escapeHtml(d.mime || "")} · ${(d.fetched_at || "").slice(0, 10)}</span>
        </button>`,
        )
        .join("") || "<p class='status'>No multimodal holdings matched this shelf yet.</p>";
    $$(".gallery-item").forEach((btn) =>
      btn.addEventListener("click", () => openDocument(btn.dataset.sha)),
    );
    setStatus(status, `${docs.length} items on this shelf`, true);
  } catch (err) {
    setStatus(status, err.message);
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
  setStatus(status, "Consulting the catalog… local models can take a minute.");

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
      steps.innerHTML = `<strong>Research steps</strong><ol>${data.steps
        .map((s) => {
          if (s.type === "plan") return `<li>Plan: ${escapeHtml((s.queries || []).join(" · "))}</li>`;
          if (s.type === "retrieve") return `<li>Retrieve “${escapeHtml(s.query)}” → ${s.hits} hits</li>`;
          if (s.type === "synthesize") return `<li>Synthesize from ${s.sources_used} passages</li>`;
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
  setStatus(status, "Loading holdings…");
  try {
    const data = await api(
      `/v1/documents?limit=${state.browseLimit}&offset=${state.browseOffset}${q ? `&q=${encodeURIComponent(q)}` : ""}`,
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
      </button>`,
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
  const cards = [
    ["Documents", stats.documents],
    ["Chunks", stats.chunks],
    ["Text chars", stats.text_chars],
    ["Downloads", crawl.downloads],
    ["Frontier queued", crawl.frontier?.queued ?? 0],
    ["Frontier done", crawl.frontier?.done ?? 0],
  ];
  $("#pipeline-stats").innerHTML = cards
    .map(
      ([label, value]) => `
    <div class="stat"><div class="label">${label}</div><div class="value">${fmtNum(value)}</div></div>`,
    )
    .join("");
  renderBarChart(
    $("#pipeline-chart"),
    cards.map(([label, value]) => ({ label, value })),
  );

  $("#job-list").innerHTML = (jobs.jobs || [])
    .map(
      (j) => `
    <div class="job">
      <div>
        <h3>${escapeHtml(j.name)}</h3>
        <p>${escapeHtml(j.description)}</p>
        <div class="cadence">${fmtInterval(j.interval_seconds)} · last ${escapeHtml(j.last_status || "never")} · runs ${j.run_count || 0}</div>
      </div>
      <button type="button" data-job="${escapeHtml(j.name)}" class="run-job">Run now</button>
    </div>`,
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
  $$("[data-view-link]").forEach((el) =>
    el.addEventListener("click", (ev) => {
      ev.preventDefault();
      showView(el.dataset.viewLink);
    }),
  );
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
  $("#graph-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if ($("#graph-mode")) $("#graph-mode").value = "neighborhood";
    state.selectedEntity = null;
    loadGraph();
  });
  $("#graph-entities").addEventListener("click", (e) => {
    const button = e.target.closest("[data-entity]");
    if (button) selectEntity(button.dataset.entity, { mode: "neighborhood" });
  });
  $("#graph-profile")?.addEventListener("click", (e) => {
    const button = e.target.closest("[data-entity]");
    if (button) selectEntity(button.dataset.entity, { mode: "neighborhood" });
  });
  $("#graph-mode")?.addEventListener("change", () => {
    state.selectedEntity = null;
    loadGraph();
  });
  $("#graph-relation")?.addEventListener("change", () => loadGraph());
  $("#graph-refresh")?.addEventListener("click", () => loadGraph());
  $("#graph-path-run")?.addEventListener("click", () => runPathFind());
  $("#graph-detail").addEventListener("click", async (e) => {
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
        out.innerHTML =
          (data.results || [])
            .map(
              (x) =>
                `<p>${escapeHtml(x.title || x.sha)} · ${x.distance_km} km · score ${Number(x.score).toFixed(3)} · <button type="button" class="linkish open-doc" data-sha="${escapeHtml(x.sha)}">open</button></p>`,
            )
            .join("") ||
          data.note ||
          "No nearby geo documents.";
      } else {
        out.innerHTML =
          (data.results || [])
            .map(
              (x) =>
                `<p>${escapeHtml(x.title)} · shared: ${escapeHtml(x.evidence_entities)} · score ${Number(x.score).toFixed(3)} · <button type="button" class="linkish open-doc" data-sha="${escapeHtml(x.sha)}">open</button></p>`,
            )
            .join("") || "No shared-entity correlations yet.";
      }
      button.replaceWith(out);
      bindDocButtons(out);
    } catch (err) {
      button.textContent = err.message;
    }
  });
  $("#gallery-refresh").addEventListener("click", loadGallery);
  $("#gallery-modality").addEventListener("change", loadGallery);
  $("#auth-form").addEventListener("submit", authLogin);
  $("#auth-register").addEventListener("click", authRegister);
  $("#auth-logout").addEventListener("click", authLogout);
  $("#key-create").addEventListener("click", mintKey);
}

wire();
refreshMeta();
loadFacets();
loadCatalogViz();
setInterval(refreshMeta, 30000);

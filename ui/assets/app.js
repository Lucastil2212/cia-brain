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
  selectedEdgeKey: "",
  relSamples: [],
  overviewToken: 0,
  token: localStorage.getItem("cia_brain_token") || "",
  user: null,
};

const VIEW_HASH = {
  search: "catalog",
  graph: "graph",
  gallery: "images",
  sources: "collections",
  reason: "reading",
  browse: "holdings",
  account: "account",
  pipeline: "stacks",
};
const HASH_VIEW = Object.fromEntries(Object.entries(VIEW_HASH).map(([view, hash]) => [hash, view]));

const RELATION_LABELS = {
  co_mentioned: "mentioned together",
  worked_with: "worked with",
  released_by: "released by",
  reported_by: "reported by",
  located_in: "located in",
  transferred_to: "transferred to",
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

/** Allow only http(s) or same-origin relative URLs in href attributes. */
function safeHref(url) {
  const raw = String(url ?? "").trim();
  if (!raw) return "#";
  if (raw.startsWith("/") && !raw.startsWith("//")) return escapeHtml(raw);
  try {
    const u = new URL(raw, window.location.origin);
    if (u.protocol === "http:" || u.protocol === "https:") return escapeHtml(u.href);
  } catch {
    /* ignore */
  }
  return "#";
}

function setStatus(el, msg, show = true) {
  if (!el) return;
  el.hidden = !show;
  el.textContent = msg;
}

function relationLabel(name) {
  if (!name) return "mentioned together";
  return RELATION_LABELS[name] || String(name).replaceAll("_", " ");
}

function kindLabel(kind) {
  return String(kind || "named entity").replaceAll("_", " ");
}

function agencyName(sourceId) {
  if (!sourceId) return "";
  const match = (state.sources || []).find((source) => source.id === sourceId);
  return match?.agency || sourceId;
}

function rememberLocation(name) {
  let hash = VIEW_HASH[name] || "catalog";
  if (name === "graph" && state.selectedEntity && $("#graph-mode")?.value === "neighborhood") {
    hash = `graph/${state.selectedEntity}`;
  }
  const next = `#${hash}`;
  if (location.hash !== next) history.replaceState(null, "", next);
}

function showView(name) {
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.id === `view-${name}`));
  $$(".nav-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.view === name));
  rememberLocation(name);
  if (name === "sources") loadSources();
  if (name === "graph") loadGraph();
  if (name === "pipeline") refreshPipeline();
  if (name === "browse") loadBrowse();
  if (name === "gallery") loadGallery();
  if (name === "search") loadCatalogViz();
  if (name === "account") loadAccount();
}

function applyHash() {
  const raw = decodeURIComponent(location.hash.replace(/^#/, ""));
  if (!raw) return;
  if (raw.startsWith("graph/")) {
    state.selectedEntity = raw.slice("graph/".length);
    if ($("#graph-mode")) $("#graph-mode").value = "neighborhood";
    showView("graph");
    return;
  }
  const view = HASH_VIEW[raw];
  if (view) showView(view);
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
  const agency = r.citation?.agency || r.agency || "";
  const when = (r.fetched_at || "").slice(0, 10);
  const sha = r.source_sha256 || "";
  const kicker = [agency, r.mime || "unknown format", when, r.current === false ? "earlier capture" : ""]
    .filter(Boolean)
    .map((part) => escapeHtml(part))
    .join(" · ");
  return `
    <article class="record" style="animation-delay:${index * 30}ms">
      <p class="record-kicker">${kicker}</p>
      <h3><a href="/d/${escapeHtml(sha)}">${escapeHtml(title)}</a></h3>
      <p class="excerpt">${escapeHtml(r.text || "")}</p>
      <div class="record-actions">
        <a href="/d/${escapeHtml(sha)}">Open record</a>
        <button type="button" class="linkish analyze-doc" data-sha="${escapeHtml(sha)}">Trace names</button>
        <a href="${safeHref(r.source_url)}" target="_blank" rel="noopener">Government source</a>
      </div>
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
    const sources = await api("/v1/sources");
    state.sources = sources.sources || [];
    fillCollectionSelects(state.sources);
    row.hidden = !state.sources.length;
    row.innerHTML = state.sources
      .map(
        (source) => `<button type="button" class="shelf-chip" data-collection="${escapeHtml(source.id)}">
          <span>${escapeHtml(source.agency)}</span>
          <small>${fmtNum(source.document_count || 0)}</small>
        </button>`,
      )
      .join("");
  } catch {
    row.hidden = true;
  }
}

function fillCollectionSelects(sources) {
  for (const id of ["collection-filter", "browse-collection"]) {
    const sel = document.getElementById(id);
    if (!sel) continue;
    const current = sel.value || "cia";
    const options = (sources || [])
      .map((source) => `<option value="${escapeHtml(source.id)}">${escapeHtml(source.agency)}</option>`)
      .join("");
    sel.innerHTML = `${options}<option value="">All collections</option>`;
    sel.value = [...sel.options].some((opt) => opt.value === current) ? current : "cia";
  }
}

async function runSearch(ev) {
  ev.preventDefault();
  const q = $("#q").value.trim();
  const top_k = Number($("#top-k").value);
  const mime = $("#mime-filter").value || null;
  const source_id = $("#collection-filter").value || null;
  const include_history = $("#include-history")?.checked || false;
  const status = $("#search-status");
  const box = $("#results");
  setStatus(status, "Searching the catalog…");
  box.innerHTML = "";
  try {
    const data = await api("/v1/search", {
      method: "POST",
      body: JSON.stringify({ query: q, top_k, mime, source_id, include_history }),
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

function multimodalBlock(doc) {
  const parts = [];
  const mime = doc.mime || "";
  if (mime.startsWith("image/")) {
    parts.push(`<div class="media-frame"><img src="/v1/raw/${escapeHtml(doc.source_sha256)}" alt="${escapeHtml(doc.title || "Archived image")}" loading="lazy" /></div>`);
  }
  if (doc.graph?.entities?.length) {
    parts.push(`<p class="meta">Graph entities · ${doc.graph.entities
      .slice(0, 12)
      .map((e) => `${escapeHtml(e.label)} (${escapeHtml(e.kind)})`)
      .join(" · ")}</p>`);
  }
  return parts.join("");
}

function openDocument(sha) {
  if (sha) window.location.assign(`/d/${sha}`);
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
  let onEdgeSelect = null;
  let cssW = 640;
  let cssH = 360;
  let alpha = 1;
  let settleFrames = 0;
  let needsFit = false;
  const positions = new Map();
  const LABEL_FONT = '11px "IBM Plex Mono", monospace';
  const LABEL_LIMIT = 28;
  const SURFACE = "#f3f5f3";
  const LABEL_BG = "rgba(243,245,243,0.92)";

  function nodeRadius(n) {
    const selected = n.id === state.selectedEntity;
    const hot = selected || n === hover;
    return (hot ? 9 : 5.5) + Math.min(10, Math.log2(1 + (n.degree || 1)) * 1.6);
  }

  function truncateLabel(label) {
    const text = String(label || "");
    return text.length > LABEL_LIMIT ? `${text.slice(0, LABEL_LIMIT - 1)}…` : text;
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    cssW = Math.max(320, rect.width || 640);
    cssH = Math.max(320, Math.min(560, cssW * 0.62));
    canvas.width = Math.floor(cssW * dpr);
    canvas.height = Math.floor(cssH * dpr);
    canvas.style.width = `${cssW}px`;
    canvas.style.height = `${cssH}px`;
    return { w: cssW, h: cssH };
  }

  function seedPositions(count, w, h) {
    const cx = w / 2;
    const cy = h / 2;
    const radius = Math.min(w, h) * 0.32;
    return Array.from({ length: count }, (_, i) => {
      const t = (i / Math.max(count, 1)) * Math.PI * 2;
      const jitter = 0.75 + Math.random() * 0.5;
      return {
        x: cx + Math.cos(t) * radius * jitter + (Math.random() - 0.5) * 24,
        y: cy + Math.sin(t) * radius * jitter + (Math.random() - 0.5) * 24,
      };
    });
  }

  function fitView({ animate = false } = {}) {
    if (!nodes.length) {
      scale = 1;
      panX = 0;
      panY = 0;
      return;
    }
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const n of nodes) {
      const r = nodeRadius(n) + 36;
      minX = Math.min(minX, n.x - r);
      minY = Math.min(minY, n.y - r);
      maxX = Math.max(maxX, n.x + r + 110);
      maxY = Math.max(maxY, n.y + r);
    }
    const bw = Math.max(80, maxX - minX);
    const bh = Math.max(80, maxY - minY);
    const pad = 28;
    const nextScale = Math.min(2.4, Math.max(0.45, Math.min((cssW - pad * 2) / bw, (cssH - pad * 2) / bh)));
    const nextPanX = (cssW - bw * nextScale) / 2 - minX * nextScale;
    const nextPanY = (cssH - bh * nextScale) / 2 - minY * nextScale;
    if (!animate) {
      scale = nextScale;
      panX = nextPanX;
      panY = nextPanY;
      return;
    }
    scale = scale * 0.7 + nextScale * 0.3;
    panX = panX * 0.7 + nextPanX * 0.3;
    panY = panY * 0.7 + nextPanY * 0.3;
  }

  function setData(payload, selectCb, { preserve = true, onEdge = null } = {}) {
    onSelect = selectCb;
    onEdgeSelect = onEdge;
    const { w, h } = resize();
    const incoming = payload.nodes || [];
    const seeds = seedPositions(incoming.length, w, h);
    const map = new Map();
    incoming.forEach((n, i) => {
      const prior = preserve ? positions.get(n.id) : null;
      const node = {
        id: n.id,
        label: n.label || String(n.id).slice(0, 8),
        kind: n.kind || "named_entity",
        degree: n.degree || n.weight || 1,
        x: prior?.x ?? seeds[i].x,
        y: prior?.y ?? seeds[i].y,
        vx: 0,
        vy: 0,
      };
      map.set(n.id, node);
    });
    links = (payload.links || [])
      .filter((e) => map.has(e.source) && map.has(e.target))
      .map((e) => ({
        source: map.get(e.source),
        target: map.get(e.target),
        relation: e.relation || "co_mentioned",
        weight: e.weight || 1,
        evidence: e.evidence || "",
        sha: e.sha || "",
        title: e.title || "",
        url: e.url || "",
        fetched_at: e.fetched_at || "",
        source_id: e.source_id || "",
        subject_label: e.subject_label || map.get(e.source)?.label || "",
        object_label: e.object_label || map.get(e.target)?.label || "",
        subject_kind: e.subject_kind || map.get(e.source)?.kind || "",
        object_kind: e.object_kind || map.get(e.target)?.kind || "",
      }));
    nodes = [...map.values()];
    for (const n of nodes) positions.set(n.id, { x: n.x, y: n.y });
    alpha = 1;
    settleFrames = 0;
    needsFit = !preserve || nodes.length <= 2;
    if (needsFit) fitView();
    if (!running) {
      running = true;
      loop();
    }
    renderLegend(nodes);
  }

  function step() {
    if (!nodes.length) return;
    if (alpha < 0.012 && !drag) {
      settleFrames += 1;
      if (settleFrames === 8) fitView({ animate: false });
      return;
    }
    settleFrames = 0;
    const cx = cssW / 2;
    const cy = cssH / 2;
    const nCount = Math.max(nodes.length, 1);
    const a = alpha;
    const repulse = (3200 + nCount * 140) * a;
    const linkStrength = 0.028 * a;
    const gravity = 0.008 * a;

    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const na = nodes[i];
        const nb = nodes[j];
        let dx = na.x - nb.x;
        let dy = na.y - nb.y;
        let dist = Math.hypot(dx, dy) || 0.01;
        const minDist = nodeRadius(na) + nodeRadius(nb) + 42;
        let force = repulse / (dist * dist);
        if (dist < minDist) force += ((minDist - dist) / minDist) * 3.2 * a;
        dx = (dx / dist) * force;
        dy = (dy / dist) * force;
        na.vx += dx;
        na.vy += dy;
        nb.vx -= dx;
        nb.vy -= dy;
      }
    }

    for (const link of links) {
      const na = link.source;
      const nb = link.target;
      const dx = nb.x - na.x;
      const dy = nb.y - na.y;
      const dist = Math.hypot(dx, dy) || 0.01;
      const ideal = 160 + Math.min(140, Math.log2(1 + (link.weight || 1)) * 32);
      const force = (dist - ideal) * linkStrength;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      na.vx += fx;
      na.vy += fy;
      nb.vx -= fx;
      nb.vy -= fy;
    }

    for (const n of nodes) {
      n.vx += (cx - n.x) * gravity;
      n.vy += (cy - n.y) * gravity;
      // Soft walls keep outliers from flying off-canvas during warm-up.
      const margin = 48;
      if (n.x < margin) n.vx += (margin - n.x) * 0.04 * a;
      if (n.x > cssW - margin) n.vx -= (n.x - (cssW - margin)) * 0.04 * a;
      if (n.y < margin) n.vy += (margin - n.y) * 0.04 * a;
      if (n.y > cssH - margin) n.vy -= (n.y - (cssH - margin)) * 0.04 * a;
      n.vx *= 0.78;
      n.vy *= 0.78;
      if (drag !== n) {
        n.x += n.vx;
        n.y += n.vy;
      } else {
        n.vx = 0;
        n.vy = 0;
      }
      positions.set(n.id, { x: n.x, y: n.y });
    }
    alpha *= 0.985;
    if (needsFit && alpha < 0.35) {
      fitView({ animate: true });
      if (alpha < 0.08) needsFit = false;
    }
  }

  function labelPriority(n) {
    if (n.id === state.selectedEntity) return 1e9;
    if (n === hover) return 1e8;
    return n.degree || 1;
  }

  function pickLabels() {
    const ranked = [...nodes].sort((a, b) => labelPriority(b) - labelPriority(a));
    const maxLabels = Math.min(14, Math.max(7, Math.floor(nodes.length * 0.35)));
    const placed = [];
    const visible = new Map();
    ctx.font = LABEL_FONT;
    for (const n of ranked) {
      if (visible.size >= maxLabels && n !== hover && n.id !== state.selectedEntity) break;
      const text = truncateLabel(n.label);
      const r = nodeRadius(n);
      const tw = ctx.measureText(text).width;
      const th = 13;
      const candidates = [
        { x: n.x + r + 8, y: n.y + 4, align: "left" },
        { x: n.x - r - 8, y: n.y + 4, align: "right" },
        { x: n.x, y: n.y - r - 10, align: "center" },
        { x: n.x, y: n.y + r + 16, align: "center" },
        { x: n.x + r + 8, y: n.y - r - 6, align: "left" },
        { x: n.x - r - 8, y: n.y - r - 6, align: "right" },
      ];
      let chosen = null;
      for (const c of candidates) {
        const left = c.align === "right" ? c.x - tw : c.align === "center" ? c.x - tw / 2 : c.x;
        const top = c.y - th;
        const box = { left, top, right: left + tw, bottom: top + th + 2 };
        const hitNode = nodes.some((other) => {
          if (other === n) return false;
          const or = nodeRadius(other);
          const cx = Math.max(box.left, Math.min(other.x, box.right));
          const cy = Math.max(box.top, Math.min(other.y, box.bottom));
          return Math.hypot(other.x - cx, other.y - cy) < or + 4;
        });
        if (hitNode) continue;
        const overlap = placed.some(
          (p) => !(box.right < p.left - 8 || box.left > p.right + 8 || box.bottom < p.top - 4 || box.top > p.bottom + 4),
        );
        if (!overlap) {
          chosen = { ...c, text, box };
          break;
        }
      }
      if (!chosen && (n === hover || n.id === state.selectedEntity)) {
        const c = candidates[0];
        const left = c.x;
        chosen = { ...c, text, box: { left, top: c.y - th, right: left + tw, bottom: c.y + 2 } };
      }
      if (chosen) {
        placed.push(chosen.box);
        visible.set(n.id, chosen);
      }
    }
    return visible;
  }

  function draw() {
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = SURFACE;
    ctx.fillRect(0, 0, cssW, cssH);
    ctx.save();
    ctx.translate(panX, panY);
    ctx.scale(scale, scale);

    const focusId = state.selectedEntity || hover?.id || null;
    const neighborIds = new Set();
    if (focusId) {
      for (const link of links) {
        if (link.source.id === focusId) neighborIds.add(link.target.id);
        if (link.target.id === focusId) neighborIds.add(link.source.id);
      }
      neighborIds.add(focusId);
    }

    for (const link of links) {
      const typed = link.relation && link.relation !== "co_mentioned";
      const hot = !focusId || link.source.id === focusId || link.target.id === focusId;
      const selected = state.selectedEdgeKey && edgeKey(link) === state.selectedEdgeKey;
      ctx.beginPath();
      ctx.strokeStyle = selected ? "#8b6914" : typed
        ? hot
          ? "rgba(13,92,77,0.62)"
          : "rgba(13,92,77,0.14)"
        : hot
          ? "rgba(26,46,40,0.32)"
          : "rgba(26,46,40,0.1)";
      ctx.lineWidth = (selected ? 2.6 : Math.min(3.2, 0.85 + Math.log2(1 + (link.weight || 1)) * 0.55)) / scale;
      ctx.moveTo(link.source.x, link.source.y);
      ctx.lineTo(link.target.x, link.target.y);
      ctx.stroke();
    }

    const labels = pickLabels();
    const ordered = [...nodes].sort((a, b) => (a.degree || 1) - (b.degree || 1));
    for (const n of ordered) {
      const selected = n.id === state.selectedEntity;
      const r = nodeRadius(n);
      const dimmed = focusId && !neighborIds.has(n.id);
      ctx.beginPath();
      ctx.fillStyle = KIND_COLORS[n.kind] || KIND_COLORS.named_entity;
      ctx.globalAlpha = dimmed ? 0.3 : 1;
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = selected ? "#c5a35a" : n === hover ? "rgba(26,46,40,0.55)" : "rgba(243,245,243,0.9)";
      ctx.lineWidth = (selected ? 2.4 : n === hover ? 1.5 : 1) / scale;
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    ctx.font = LABEL_FONT;
    ctx.textBaseline = "alphabetic";
    for (const n of nodes) {
      const label = labels.get(n.id);
      if (!label) continue;
      const dimmed = focusId && !neighborIds.has(n.id) && n !== hover;
      ctx.globalAlpha = dimmed ? 0.4 : 1;
      ctx.textAlign = label.align;
      const padX = 4;
      const box = label.box;
      ctx.fillStyle = LABEL_BG;
      ctx.fillRect(box.left - padX, box.top - 1, box.right - box.left + padX * 2, box.bottom - box.top + 1);
      ctx.fillStyle = "#1a2e28";
      ctx.fillText(label.text, label.x, label.y);
      ctx.globalAlpha = 1;
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
    const x = (ev.clientX - rect.left - panX) / scale;
    const y = (ev.clientY - rect.top - panY) / scale;
    return { x, y };
  }

  function hit(ev) {
    const p = toWorld(ev);
    let best = null;
    let bestDist = Infinity;
    for (const n of nodes) {
      const d = Math.hypot(n.x - p.x, n.y - p.y);
      if (d <= nodeRadius(n) + 8 && d < bestDist) {
        best = n;
        bestDist = d;
      }
    }
    return best;
  }

  function pointSegDist(p, a, b) {
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const len2 = dx * dx + dy * dy || 1;
    const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2));
    return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
  }

  function hitEdge(ev) {
    const p = toWorld(ev);
    const limit = 12 / scale;
    let best = null;
    let bestDist = limit;
    for (const link of links) {
      const dist = pointSegDist(p, link.source, link.target);
      if (dist < bestDist) {
        best = link;
        bestDist = dist;
      }
    }
    return best;
  }

  canvas.addEventListener("pointerdown", (ev) => {
    const n = hit(ev);
    if (n) {
      drag = n;
      alpha = Math.max(alpha, 0.25);
      canvas.setPointerCapture(ev.pointerId);
      state.selectedEntity = n.id;
      state.selectedEdgeKey = "";
      if (onSelect) onSelect(n.id, { fromCanvas: true });
    } else {
      const edge = hitEdge(ev);
      if (edge && onEdgeSelect) {
        state.selectedEdgeKey = edgeKey(edge);
        onEdgeSelect(edge);
        return;
      }
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
      alpha = Math.max(alpha, 0.12);
    } else if (panning && panLast) {
      panX += ev.clientX - panLast.x;
      panY += ev.clientY - panLast.y;
      panLast = { x: ev.clientX, y: ev.clientY };
    } else {
      const next = hit(ev);
      const edge = next ? null : hitEdge(ev);
      if (next !== hover) hover = next;
      canvas.style.cursor = next || edge ? "pointer" : "grab";
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
      const rect = canvas.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      const factor = ev.deltaY < 0 ? 1.1 : 0.9;
      const next = Math.min(3.5, Math.max(0.35, scale * factor));
      const wx = (mx - panX) / scale;
      const wy = (my - panY) / scale;
      scale = next;
      panX = mx - wx * scale;
      panY = my - wy * scale;
    },
    { passive: false },
  );
  window.addEventListener("resize", () => {
    if (nodes.length) {
      resize();
      fitView();
    }
  });

  return {
    setData,
    highlight(id) {
      state.selectedEntity = id;
      alpha = Math.max(alpha, 0.08);
    },
    highlightEdge(link) {
      state.selectedEdgeKey = link ? edgeKey(link) : "";
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

function edgeKey(link) {
  const source = link.source?.id || link.subject || link.source || "";
  const target = link.target?.id || link.object || link.target || "";
  return `${source}|${target}|${link.relation || ""}|${link.sha || ""}`;
}

function setDossier(html) {
  const box = $("#graph-dossier");
  if (box) box.innerHTML = html;
}

function attrGrid(pairs) {
  return `<dl class="attrs">${pairs
    .map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`)
    .join("")}</dl>`;
}

function sourceCard(src) {
  const sha = src?.sha || "";
  if (!sha) return `<p class="meta">No archived document is attached.</p>`;
  const title = src.title || sha.slice(0, 12);
  const when = (src.fetched_at || "").slice(0, 10);
  const collection = agencyName(src.source_id);
  const url = src.url || "";
  return `<article class="source-card">
    <h4><a href="/d/${escapeHtml(sha)}">${escapeHtml(title)}</a></h4>
    <p class="meta">${[collection, when].filter(Boolean).map((part) => escapeHtml(part)).join(" · ")}</p>
    <div class="record-actions">
      <a href="/d/${escapeHtml(sha)}">Open record</a>
      ${url ? `<a href="${safeHref(url)}" target="_blank" rel="noopener">Government source</a>` : ""}
      <button type="button" class="linkish" data-correlate="${escapeHtml(sha)}">Related records</button>
    </div>
  </article>`;
}

function relationshipFromLink(link) {
  return {
    subject: link.source?.id || link.subject || link.source || "",
    object: link.target?.id || link.object || link.target || "",
    subject_label: link.subject_label || link.source?.label || "Unknown",
    object_label: link.object_label || link.target?.label || "Unknown",
    subject_kind: link.subject_kind || link.source?.kind || "",
    object_kind: link.object_kind || link.target?.kind || "",
    relation: link.relation || "co_mentioned",
    weight: link.weight || 1,
    evidence: link.evidence || "",
    sha: link.sha || "",
    title: link.title || "",
    url: link.url || "",
    fetched_at: link.fetched_at || "",
    source_id: link.source_id || "",
  };
}

function groupEdges(edges, focusId) {
  const groups = new Map();
  for (const edge of edges || []) {
    const focusIsSubject = edge.subject === focusId;
    const otherId = focusIsSubject ? edge.object : edge.subject;
    const key = `${otherId}|${edge.relation || "co_mentioned"}`;
    let group = groups.get(key);
    if (!group) {
      group = {
        otherId,
        otherLabel: focusIsSubject ? edge.object_label : edge.subject_label,
        otherKind: focusIsSubject ? edge.object_kind : edge.subject_kind,
        relation: edge.relation || "co_mentioned",
        count: 0,
        sample: edge,
      };
      groups.set(key, group);
    }
    group.count += 1;
  }
  return [...groups.values()].sort((a, b) => b.count - a.count);
}

function neighborhoodLinks(edges) {
  const groups = new Map();
  for (const edge of edges || []) {
    const key = `${edge.subject}|${edge.object}|${edge.relation || ""}`;
    let group = groups.get(key);
    if (!group) {
      group = { ...edge, source: edge.subject, target: edge.object, weight: 0 };
      groups.set(key, group);
    }
    group.weight += 1;
  }
  return [...groups.values()].sort((a, b) => b.weight - a.weight).slice(0, 80);
}

function renderEntityDossier(data) {
  const profile = data?.profile;
  if (!profile) {
    setDossier(`<p class="meta">This name is not in the graph.</p>`);
    return;
  }
  const groups = groupEdges(data.edges || [], profile.id);
  state.relSamples = groups.map((group) => ({ ...group.sample, weight: group.count }));
  const confidence =
    profile.confidence == null ? "not scored" : Number(profile.confidence).toFixed(2);
  const relations = (profile.relations || [])
    .slice(0, 4)
    .map((row) => `${relationLabel(row.relation)} ${fmtNum(row.count)}`)
    .join(" · ");
  setDossier(`
    <p class="eyebrow">${escapeHtml(kindLabel(profile.kind))}</p>
    <h3>${escapeHtml(profile.label)}</h3>
    <p class="meta">Inferred from archived sentences. Open a source to read the passage.</p>
    ${attrGrid([
      ["Kind", kindLabel(profile.kind)],
      ["Links", fmtNum(profile.degree)],
      ["Documents", fmtNum(profile.documents)],
      ["Mentions", fmtNum(profile.mentions)],
      ["Confidence", confidence],
      ["Relations", relations || "none yet"],
    ])}
    <h4>Source documents</h4>
    ${(profile.sources || []).map(sourceCard).join("") || `<p class="meta">No active document mentions this name.</p>`}
    <h4>Relationships</h4>
    ${
      groups
        .slice(0, 12)
        .map(
          (group, index) => `<button type="button" class="rel-row" data-rel="${index}">
            <span>${escapeHtml(relationLabel(group.relation))} ${escapeHtml(group.otherLabel || "Unknown")}</span>
            <small>${escapeHtml(kindLabel(group.otherKind))} · ${fmtNum(group.count)}</small>
          </button>`,
        )
        .join("") || `<p class="meta">No sentence-level relationships yet.</p>`
    }
  `);
}

function showRelationship(link) {
  const edge = relationshipFromLink(link);
  state.overviewToken += 1;
  state.selectedEdgeKey = edgeKey(edge);
  if (state.graphSim) state.graphSim.highlightEdge(edge);
  setDossier(`
    <p class="eyebrow">Relationship</p>
    <h3>${escapeHtml(edge.subject_label)} <em>${escapeHtml(relationLabel(edge.relation))}</em> ${escapeHtml(edge.object_label)}</h3>
    <p class="meta">A candidate inferred from one sentence. It is not an established fact.</p>
    ${attrGrid([
      ["Type", relationLabel(edge.relation)],
      ["Evidence count", fmtNum(edge.weight)],
      ["From", `${edge.subject_label} (${kindLabel(edge.subject_kind)})`],
      ["To", `${edge.object_label} (${kindLabel(edge.object_kind)})`],
    ])}
    <h4>Supporting sentence</h4>
    <blockquote>${escapeHtml(edge.evidence || "No sentence was stored for this link.")}</blockquote>
    <h4>Source document</h4>
    ${sourceCard(edge)}
    <div class="record-actions">
      <button type="button" class="linkish" data-entity="${escapeHtml(edge.subject)}">Open ${escapeHtml(edge.subject_label)}</button>
      <button type="button" class="linkish" data-entity="${escapeHtml(edge.object)}">Open ${escapeHtml(edge.object_label)}</button>
    </div>
  `);
}

async function showOverview() {
  const relation = $("#graph-relation")?.value || "";
  const data = await api(
    `/v1/graph/overview?limit=40${relation ? `&relation=${encodeURIComponent(relation)}` : ""}`,
  );
  if (!state.graphSim) state.graphSim = createForceGraph($("#graph-canvas"));
  const allLinks = data.links || [];
  const links = [...allLinks].sort((a, b) => (b.weight || 1) - (a.weight || 1)).slice(0, 120);
  state.selectedEdgeKey = "";
  state.graphSim.setData(
    { nodes: data.nodes || [], links },
    (id) => selectEntity(id, { mode: "neighborhood" }),
    { preserve: false, onEdge: showRelationship },
  );
  const focus = state.selectedEntity || data.nodes?.[0]?.id;
  if (!focus) {
    setDossier(`<p class="meta">The graph has no names yet. Ingest documents or run graph_rebuild.</p>`);
    return;
  }
  state.selectedEntity = focus;
  state.graphSim.highlight(focus);
  const token = ++state.overviewToken;
  try {
    const dossier = await api(`/v1/graph/entities/${focus}`);
    if (token !== state.overviewToken || state.selectedEdgeKey) return;
    renderEntityDossier(dossier);
  } catch {
    if (token === state.overviewToken) setDossier(`<p class="meta">Click a name or a line to see its sources.</p>`);
  }
}

async function selectEntity(entityId, opts = {}) {
  state.overviewToken += 1;
  state.selectedEntity = entityId;
  state.selectedEdgeKey = "";
  if (opts.mode && $("#graph-mode")) $("#graph-mode").value = opts.mode;
  $$(".entity-chip").forEach((el) => el.classList.toggle("is-active", el.dataset.entity === entityId));
  if (state.graphSim) state.graphSim.highlight(entityId);
  setDossier(`<p class="meta">Loading this name…</p>`);
  try {
    const data = await api(`/v1/graph/entities/${entityId}`);
    const nodes = new Map();
    if (data.profile) {
      nodes.set(entityId, {
        id: entityId,
        label: data.profile.label,
        kind: data.profile.kind,
        degree: data.profile.degree,
      });
    }
    for (const edge of data.edges || []) {
      if (!nodes.has(edge.subject)) {
        nodes.set(edge.subject, {
          id: edge.subject,
          label: edge.subject_label,
          kind: edge.subject_kind || "named_entity",
        });
      }
      if (!nodes.has(edge.object)) {
        nodes.set(edge.object, {
          id: edge.object,
          label: edge.object_label,
          kind: edge.object_kind || "named_entity",
        });
      }
    }
    for (const neighbor of data.profile?.neighbors || []) {
      if (nodes.has(neighbor.id)) nodes.get(neighbor.id).kind = neighbor.kind;
      else nodes.set(neighbor.id, { id: neighbor.id, label: neighbor.label, kind: neighbor.kind, degree: neighbor.weight });
    }
    if (!nodes.size) {
      nodes.set(entityId, {
        id: entityId,
        label: data.profile?.label || entityId.slice(0, 8),
        kind: data.profile?.kind || "named_entity",
      });
    }
    if (!state.graphSim) state.graphSim = createForceGraph($("#graph-canvas"));
    state.graphSim.setData(
      { nodes: [...nodes.values()], links: neighborhoodLinks(data.edges) },
      (id) => selectEntity(id, { mode: "neighborhood" }),
      { preserve: Boolean(opts.fromCanvas), onEdge: showRelationship },
    );
    renderEntityDossier(data);
    rememberLocation("graph");
  } catch (err) {
    setDossier(`<p class="meta">${escapeHtml(err.message)}</p>`);
  }
}

async function runPathFind() {
  const source = $("#graph-path-from").value;
  const target = $("#graph-path-to").value;
  if (!source || !target) return;
  setDossier(`<p class="meta">Searching for a path…</p>`);
  try {
    const data = await api(
      `/v1/graph/path?source=${encodeURIComponent(source)}&target=${encodeURIComponent(target)}&max_depth=6`,
    );
    if (!data.path?.length) {
      setDossier(`<p class="meta">${escapeHtml(data.note || "No path found")}</p>`);
      return;
    }
    if (!state.graphSim) state.graphSim = createForceGraph($("#graph-canvas"));
    const links = (data.edges || []).map((edge) => ({
      ...edge,
      source: edge.subject,
      target: edge.object,
      weight: 2,
    }));
    state.selectedEdgeKey = "";
    state.graphSim.setData(
      { nodes: data.nodes || [], links },
      (id) => selectEntity(id, { mode: "neighborhood" }),
      { preserve: false, onEdge: showRelationship },
    );
    state.selectedEntity = target;
    state.relSamples = data.edges || [];
    const hops = data.hops || links.length;
    setDossier(`
      <p class="eyebrow">Path</p>
      <h3>${hops} hop${hops === 1 ? "" : "s"}</h3>
      <p class="meta">Each hop is one inferred sentence. Open it to read the source.</p>
      ${(data.edges || [])
        .map(
          (edge, index) => `<button type="button" class="rel-row" data-rel="${index}">
            <span>${index + 1}. ${escapeHtml(edge.subject_label)} ${escapeHtml(relationLabel(edge.relation))} ${escapeHtml(edge.object_label)}</span>
            <small>${escapeHtml((edge.title || "source").slice(0, 42))}</small>
          </button>`,
        )
        .join("")}
    `);
  } catch (err) {
    setDossier(`<p class="meta">${escapeHtml(err.message)}</p>`);
  }
}

async function loadGraphAnalytics() {
  const analytics = await api("/v1/graph/analytics?hub_limit=12");
  $("#graph-stats").textContent =
    `${fmtNum(analytics.documents)} records in the graph · ${fmtNum(analytics.entities)} names · ${fmtNum(analytics.edges)} links · ${fmtNum(analytics.typed_edges || 0)} typed`;
  const relSel = $("#graph-relation");
  if (relSel) {
    const current = relSel.value;
    relSel.innerHTML =
      `<option value="">All relations</option>` +
      (analytics.relations || [])
        .map(
          (r) =>
            `<option value="${escapeHtml(r.relation)}">${escapeHtml(relationLabel(r.relation))} (${fmtNum(r.count)})</option>`,
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
            <small>${escapeHtml(kindLabel(e.kind))} · ${fmtNum(e.documents ?? e.degree ?? 0)}</small>
          </button>`,
        )
        .join("") ||
      "<p class='status'>No entities yet. Ingest documents or run job graph_rebuild.</p>";

    fillEntitySelect($("#graph-path-from"), entities, entities[0]?.id);
    fillEntitySelect($("#graph-path-to"), entities, entities[1]?.id || entities[0]?.id);

    if (!state.graphSim) state.graphSim = createForceGraph($("#graph-canvas"));

    if (mode === "path") {
      if (entities.length >= 2) await runPathFind();
      else setDossier(`<p class="meta">Choose two names, then find the path between them.</p>`);
      rememberLocation("graph");
      return;
    }
    if (q) {
      const focus = entities[0]?.id;
      if (focus) await selectEntity(focus, { mode: "neighborhood" });
      else setDossier(`<p class="meta">No names match that search.</p>`);
      return;
    }
    if (mode === "neighborhood") {
      const focus = state.selectedEntity || entities[0]?.id;
      if (focus) await selectEntity(focus);
      else setDossier(`<p class="meta">No names in the graph yet.</p>`);
      return;
    }
    await showOverview();
    rememberLocation("graph");
  } catch (err) {
    $("#graph-stats").textContent = err.message;
  }
}

async function loadSources() {
  try {
    const data = await api("/v1/sources");
    state.sources = data.sources || [];
    fillCollectionSelects(state.sources);
    $("#source-list").innerHTML = `<table class="sheet">
      <thead><tr><th>Collection</th><th>Documents</th><th>Crawl</th><th></th></tr></thead>
      <tbody>${state.sources
        .map(
          (source) => `<tr>
            <td>
              <strong>${escapeHtml(source.agency)}</strong>
              <div class="meta"><a href="${safeHref(source.url)}" target="_blank" rel="noopener">${escapeHtml(source.url || "")}</a></div>
            </td>
            <td>${fmtNum(source.document_count || 0)}</td>
            <td>${escapeHtml(collectionStatus(source))}${source.enabled ? "" : " · not selected"}</td>
            <td><button type="button" class="pager" data-open-collection="${escapeHtml(source.id)}">Browse</button></td>
          </tr>`,
        )
        .join("")}</tbody>
    </table>`;
  } catch (err) {
    $("#source-list").textContent = err.message;
  }
}

function collectionStatus(source) {
  const status = source.status;
  if (!status) return "Not in the crawl frontier yet";
  if (status.last_error) return `${status.status || "error"}: ${status.last_error}`;
  return status.status || "queued";
}

function galleryPreview(doc) {
  const mime = doc.mime || "";
  if (mime.startsWith("image/")) {
    return `<img class="gallery-thumb" src="/v1/raw/${escapeHtml(doc.source_sha256)}" alt="" loading="lazy" />`;
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

async function traceDocument(sha, statusEl) {
  setStatus(statusEl, "Finding names in this record…");
  try {
    const doc = await api(`/v1/graph/documents/${sha}`);
    const entities = (doc.entities || []).filter(
      (entity) => entity.kind !== "date" && (entity.label || "").trim().length >= 3,
    );
    if (!entities.length) {
      setStatus(statusEl, "No names were extracted from this record yet.");
      return;
    }
    entities.sort((a, b) => (b.confidence || 0) - (a.confidence || 0));
    state.selectedEntity = entities[0].id;
    if ($("#graph-mode")) $("#graph-mode").value = "neighborhood";
    const next = `#graph/${entities[0].id}`;
    if (location.hash === next) showView("graph");
    else location.hash = next;
  } catch (err) {
    setStatus(statusEl, err.message);
  }
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
  setStatus(status, "Pulling archived images…");
  try {
    const data = await api("/v1/documents?limit=48&offset=0&mime=image%2F");
    const docs = data.documents || [];
    $("#gallery-grid").innerHTML =
      docs
        .map(
          (d) => `<button type="button" class="gallery-item" data-sha="${escapeHtml(d.source_sha256)}">
          ${galleryPreview(d)}
          <strong>${escapeHtml((d.title || d.source_url || "").slice(0, 80))}</strong>
          <span class="meta">${escapeHtml(d.mime || "")} · ${(d.fetched_at || "").slice(0, 10)}</span>
        </button>`,
        )
        .join("") || "<p class='status'>No archived images on this shelf yet.</p>";
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
    const source_id = $("#browse-collection")?.value || "";
    const history = $("#browse-history")?.checked ? 1 : 0;
    const data = await api(
      `/v1/documents?limit=${state.browseLimit}&offset=${state.browseOffset}${q ? `&q=${encodeURIComponent(q)}` : ""}${source_id ? `&source_id=${encodeURIComponent(source_id)}` : ""}&history=${history}`,
    );
    state.browseTotal = data.total;
    const page = Math.floor(state.browseOffset / state.browseLimit) + 1;
    const pages = Math.max(1, Math.ceil(data.total / state.browseLimit));
    $("#browse-page").textContent = `Page ${page} / ${pages} · ${fmtNum(data.total)} docs`;
    $("#browse-prev").disabled = state.browseOffset <= 0;
    $("#browse-next").disabled = state.browseOffset + state.browseLimit >= data.total;
    $("#browse-list").innerHTML = `<table class="sheet">
      <thead><tr><th>Title</th><th>Collection</th><th>Format</th><th>Captured</th><th></th></tr></thead>
      <tbody>${(data.documents || [])
        .map((doc) => {
          const sha = doc.source_sha256 || "";
          const title = doc.title || doc.source_url || sha.slice(0, 12);
          return `<tr>
            <td><a href="/d/${escapeHtml(sha)}">${escapeHtml(title)}</a>${doc.current === false ? `<div class="meta">earlier capture</div>` : ""}</td>
            <td>${escapeHtml(doc.agency || agencyName(doc.source_id) || "—")}</td>
            <td>${escapeHtml(doc.mime || "—")}</td>
            <td>${escapeHtml((doc.fetched_at || "").slice(0, 10))}</td>
            <td><button type="button" class="linkish analyze-doc" data-sha="${escapeHtml(sha)}">Trace names</button></td>
          </tr>`;
        })
        .join("")}</tbody>
    </table>`;
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
  $("#browse-collection")?.addEventListener("change", () => {
    state.browseOffset = 0;
    if ($("#view-browse")?.classList.contains("is-active")) loadBrowse();
  });
  $("#browse-history")?.addEventListener("change", () => {
    state.browseOffset = 0;
    if ($("#view-browse")?.classList.contains("is-active")) loadBrowse();
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
  $("#graph-dossier")?.addEventListener("click", async (e) => {
    const entityBtn = e.target.closest("[data-entity]");
    if (entityBtn) {
      selectEntity(entityBtn.dataset.entity, { mode: "neighborhood" });
      return;
    }
    const relBtn = e.target.closest("[data-rel]");
    if (relBtn) {
      const sample = state.relSamples[Number(relBtn.dataset.rel)];
      if (sample) showRelationship(sample);
      return;
    }
    const correlate = e.target.closest("[data-correlate]");
    if (!correlate) return;
    const sha = correlate.dataset.correlate;
    try {
      const data = await api(`/v1/graph/correlations/${sha}?mode=entity&limit=8`);
      const out = document.createElement("div");
      out.className = "related-list";
      out.innerHTML =
        (data.results || [])
          .map(
            (row) => `<p><a href="/d/${escapeHtml(row.sha)}">${escapeHtml(row.title || row.sha.slice(0, 12))}</a>
              <span class="meta">shared ${escapeHtml(row.evidence_entities || "")}</span></p>`,
          )
          .join("") || `<p class="meta">No other records share these names.</p>`;
      correlate.replaceWith(out);
    } catch (err) {
      correlate.textContent = err.message;
    }
  });
  $("#graph-mode")?.addEventListener("change", () => {
    if ($("#graph-mode").value !== "neighborhood") state.selectedEntity = null;
    loadGraph();
  });
  $("#graph-relation")?.addEventListener("change", () => loadGraph());
  $("#graph-path-run")?.addEventListener("click", () => runPathFind());
  $("#catalog-viz")?.addEventListener("click", (e) => {
    const chip = e.target.closest("[data-collection]");
    if (!chip) return;
    const filter = $("#collection-filter");
    if (filter) filter.value = chip.dataset.collection;
    $("#q")?.focus();
  });
  $("#source-list")?.addEventListener("click", (e) => {
    const button = e.target.closest("[data-open-collection]");
    if (!button) return;
    const sel = $("#browse-collection");
    if (sel) sel.value = button.dataset.openCollection;
    state.browseOffset = 0;
    showView("browse");
  });
  document.body.addEventListener("click", (e) => {
    const button = e.target.closest(".analyze-doc");
    if (!button) return;
    const status = $("#view-browse")?.classList.contains("is-active") ? $("#browse-status") : $("#search-status");
    traceDocument(button.dataset.sha, status);
  });
  $("#gallery-refresh").addEventListener("click", loadGallery);
  $("#auth-form").addEventListener("submit", authLogin);
  $("#auth-register").addEventListener("click", authRegister);
  $("#auth-logout").addEventListener("click", authLogout);
  $("#key-create").addEventListener("click", mintKey);
}

wire();
refreshMeta();
loadFacets();
loadCatalogViz();
// Background tabs would otherwise exhaust the anonymous daily rate limit.
setInterval(() => {
  if (document.visibilityState === "visible") refreshMeta();
}, 60000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshMeta();
});
window.addEventListener("hashchange", applyHash);
applyHash();

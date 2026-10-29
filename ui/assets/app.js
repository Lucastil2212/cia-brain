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
  const agency = r.citation?.agency || r.agency;
  return `
    <article class="result" style="animation-delay:${index * 40}ms">
      <h3>${escapeHtml(title)}</h3>
      <div class="meta">
        <span class="score">RRF ${score}</span>
        ${agency ? `<span>${escapeHtml(agency)}</span>` : ""}
        <span>${escapeHtml(r.mime || "unknown")}</span>
        <span>chunk ${r.chunk_no ?? 0}</span>
        <span>${escapeHtml((r.fetched_at || "").slice(0, 10))}</span>
        ${mods}
        <a href="${safeHref(r.source_url)}" target="_blank" rel="noopener">source</a>
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
    renderBarChart(
      $("#modality-chart"),
      (facets.collections || []).map((row) => ({ label: row.value, value: row.count })),
    );
    fillCollectionSelects(state.sources);
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
  const status = $("#search-status");
  const box = $("#results");
  setStatus(status, "Searching the catalog…");
  box.innerHTML = "";
  try {
    const data = await api("/v1/search", {
      method: "POST",
      body: JSON.stringify({ query: q, top_k, mime, source_id }),
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

  function setData(payload, selectCb, { preserve = true } = {}) {
    onSelect = selectCb;
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
      ctx.beginPath();
      ctx.strokeStyle = typed
        ? hot
          ? "rgba(13,92,77,0.62)"
          : "rgba(13,92,77,0.14)"
        : hot
          ? "rgba(26,46,40,0.32)"
          : "rgba(26,46,40,0.1)";
      ctx.lineWidth = Math.min(3.2, 0.85 + Math.log2(1 + (link.weight || 1)) * 0.55) / scale;
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

  canvas.addEventListener("pointerdown", (ev) => {
    const n = hit(ev);
    if (n) {
      drag = n;
      alpha = Math.max(alpha, 0.25);
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
      alpha = Math.max(alpha, 0.12);
    } else if (panning && panLast) {
      panX += ev.clientX - panLast.x;
      panY += ev.clientY - panLast.y;
      panLast = { x: ev.clientX, y: ev.clientY };
    } else {
      const next = hit(ev);
      if (next !== hover) hover = next;
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
  const allLinks = (data.links || []).map((l) => ({
    source: l.source,
    target: l.target,
    relation: l.relation,
    weight: l.weight,
  }));
  // Cap dense overview edges so the canvas stays readable; keep strongest co-evidence first.
  const maxLinks = Math.min(120, allLinks.length);
  const links = [...allLinks].sort((a, b) => (b.weight || 1) - (a.weight || 1)).slice(0, maxLinks);
  state.graphSim.setData(
    {
      nodes: data.nodes || [],
      links,
    },
    (id) => selectEntity(id, { mode: "neighborhood" }),
    { preserve: false },
  );
  $("#graph-detail").innerHTML = `<p class="status">${fmtNum((data.nodes || []).length)} hubs · ${fmtNum(links.length)} of ${fmtNum(allLinks.length)} weighted links · ${escapeHtml(data.method || "")}</p>`;
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
        <p><a href="${safeHref(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.url)}</a></p>
        <p class="meta">${fmtNum(s.document_count || 0)} documents · ${escapeHtml(collectionStatus(s))}</p>
      </article>`,
      )
      .join("");
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
    const data = await api(
      `/v1/documents?limit=${state.browseLimit}&offset=${state.browseOffset}${q ? `&q=${encodeURIComponent(q)}` : ""}${source_id ? `&source_id=${encodeURIComponent(source_id)}` : ""}`,
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
        <span class="meta">${d.agency ? `${escapeHtml(d.agency)} · ` : ""}${escapeHtml(d.mime || "")} · ${fmtNum(d.text_chars)} chars · ${(d.fetched_at || "").slice(0, 10)}</span>
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
  $("#browse-collection")?.addEventListener("change", () => {
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

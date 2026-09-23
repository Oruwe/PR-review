/* The repo picture: a treemap, a dependency graph, and a language bar.
 *
 * Hand-rolled SVG rather than a charting library. A squarified treemap and a
 * small force layout are about a hundred lines each, and vendoring a library to
 * save them would add a megabyte to a page that has to work offline.
 *
 * Colour rules, held to throughout: magnitude uses one blue ramp light-to-dark;
 * identity uses the categorical slots in fixed order, never cycled; the two
 * never mix on one mark. Every fill carries a 2px surface gap so adjacent
 * rectangles read as separate, and every mark has a hover tooltip.
 */

import { showTip, hideTip } from "/static/js/app.js";

const SEQ = ["--seq-100", "--seq-200", "--seq-300", "--seq-400",
             "--seq-500", "--seq-600", "--seq-700"];
const CAT = ["--series-1", "--series-2", "--series-3", "--series-4",
             "--series-5", "--series-6", "--series-7", "--series-8"];

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const svgEl = (tag, attrs = {}) => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
};
const escapeHtml = (text) => String(text).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/** Map a 0..1 magnitude onto the sequential ramp. */
function seqColor(fraction) {
  const index = Math.min(SEQ.length - 1, Math.max(0, Math.round(fraction * (SEQ.length - 1))));
  return css(SEQ[index]);
}

/** Light fills need dark ink and vice versa; the ramp crosses over near step 400. */
const inkOn = (fraction) => (fraction > 0.5 ? "#fff" : css("--ink"));

// ---------------------------------------------------------------- treemap --

/** Squarified treemap: rows are packed to keep rectangles near-square. */
function squarify(items, x, y, width, height) {
  const out = [];
  const total = items.reduce((sum, item) => sum + item.value, 0) || 1;
  let remaining = items.map((item) => ({ ...item, area: (item.value / total) * width * height }));
  let [cx, cy, cw, ch] = [x, y, width, height];

  const worst = (row, side) => {
    const sum = row.reduce((s, r) => s + r.area, 0);
    const max = Math.max(...row.map((r) => r.area));
    const min = Math.min(...row.map((r) => r.area));
    return Math.max((side * side * max) / (sum * sum), (sum * sum) / (side * side * min));
  };

  while (remaining.length) {
    const vertical = cw >= ch;
    const side = vertical ? ch : cw;
    const row = [remaining[0]];
    let rest = remaining.slice(1);
    while (rest.length && worst([...row, rest[0]], side) <= worst(row, side)) {
      row.push(rest[0]);
      rest = rest.slice(1);
    }
    const sum = row.reduce((s, r) => s + r.area, 0);
    const thickness = sum / side || 0;
    let offset = vertical ? cy : cx;
    for (const cell of row) {
      const extent = (cell.area / sum) * side || 0;
      out.push(vertical
        ? { ...cell, x: cx, y: offset, w: thickness, h: extent }
        : { ...cell, x: offset, y: cy, w: extent, h: thickness });
      offset += extent;
    }
    if (vertical) { cx += thickness; cw -= thickness; } else { cy += thickness; ch -= thickness; }
    remaining = rest;
  }
  return out;
}

export function drawTreemap(atlas, host, mode = "churn") {
  if (!host) return;
  host.innerHTML = "";
  const modules = (atlas.modules || []).filter((m) => m.loc > 0).slice(0, 40);
  if (!modules.length) {
    host.innerHTML = '<p class="muted">Nothing to draw.</p>';
    return;
  }

  const width = host.clientWidth || 520;
  const height = 320;
  const svg = svgEl("svg", { class: "viz", viewBox: `0 0 ${width} ${height}`,
                             role: "img", "aria-label": "Modules sized by lines of code" });

  const metric = (module) => {
    if (mode === "untested") {
      return module.files ? 1 - module.test_files / module.files : 0;
    }
    if (mode === "symbols") return module.loc ? module.symbols / module.loc : 0;
    return module.commits;
  };
  const widest = Math.max(...modules.map(metric), 1);

  const cells = squarify(
    modules.map((m) => ({ value: m.loc, module: m })), 0, 0, width, height);

  for (const cell of cells) {
    const module = cell.module;
    const fraction = Math.min(1, metric(module) / widest);
    const group = svgEl("g");
    // 2px inset gives the surface gap between adjacent fills.
    const rect = svgEl("rect", {
      x: cell.x + 1, y: cell.y + 1,
      width: Math.max(0, cell.w - 2), height: Math.max(0, cell.h - 2),
      rx: 3, fill: seqColor(fraction), stroke: css("--surface"), "stroke-width": 1,
    });
    group.append(rect);

    // Direct-label anything with room; the rest is reachable by hover.
    if (cell.w > 62 && cell.h > 26) {
      const name = module.name.split("/").pop();
      const label = svgEl("text", {
        x: cell.x + 7, y: cell.y + 17, "font-size": 11, "font-weight": 550,
        fill: inkOn(fraction), "pointer-events": "none",
      });
      label.textContent = name.length > Math.floor(cell.w / 7)
        ? `${name.slice(0, Math.max(2, Math.floor(cell.w / 7) - 1))}…` : name;
      group.append(label);
      if (cell.h > 42) {
        const sub = svgEl("text", {
          x: cell.x + 7, y: cell.y + 31, "font-size": 10, "fill-opacity": 0.8,
          fill: inkOn(fraction), "pointer-events": "none",
        });
        sub.textContent = `${module.loc.toLocaleString()} lines`;
        group.append(sub);
      }
    }

    const metricLabel = mode === "untested" ? "Untested share"
      : mode === "symbols" ? "Symbols per line" : "Commits";
    const metricValue = mode === "churn" ? module.commits : `${(fraction * 100).toFixed(0)}%`;
    group.addEventListener("pointermove", (event) => showTip(
      `<div class="tt-title">${escapeHtml(module.name)}</div>` +
      `<div class="tt-row"><span>Lines</span><span>${module.loc.toLocaleString()}</span></div>` +
      `<div class="tt-row"><span>Files</span><span>${module.files}</span></div>` +
      `<div class="tt-row"><span>Symbols</span><span>${module.symbols}</span></div>` +
      `<div class="tt-row"><span>${metricLabel}</span><span>${metricValue}</span></div>` +
      `<div class="tt-row"><span>Test files</span><span>${module.test_files}</span></div>`,
      event));
    group.addEventListener("pointerleave", hideTip);
    svg.append(group);
  }
  host.append(svg);
  addRampLegend(host, mode);
}

function addRampLegend(host, mode) {
  const labels = { churn: ["fewer commits", "more commits"],
                   untested: ["well tested", "no tests"],
                   symbols: ["sparse", "dense"] };
  const [low, high] = labels[mode] || labels.churn;
  const legend = document.createElement("div");
  legend.className = "legend";
  legend.style.justifyContent = "space-between";
  legend.innerHTML =
    `<span class="item muted">${low}</span>` +
    `<span class="item">${SEQ.map((slot) =>
      `<span class="swatch" style="width:22px;height:9px;border-radius:2px;background:var(${slot})"></span>`
    ).join("")}</span>` +
    `<span class="item muted">${high}</span>`;
  host.append(legend);
}

// ------------------------------------------------------------------ graph --

export function drawGraph(atlas, host) {
  if (!host) return;
  host.innerHTML = "";
  const edges = atlas.edges || [];
  if (!edges.length) {
    host.innerHTML = '<p class="muted">No cross-module calls were resolved for this '
      + 'repository, so there is no dependency graph to draw.</p>';
    return;
  }

  const width = host.clientWidth || 520;
  const height = 320;
  const sizes = new Map((atlas.modules || []).map((m) => [m.name, m.loc]));
  const names = [...new Set(edges.flatMap((e) => [e.source, e.target]))];
  const widestLoc = Math.max(...names.map((n) => sizes.get(n) || 1), 1);

  const nodes = new Map(names.map((name, index) => {
    const angle = (index / names.length) * Math.PI * 2;
    return [name, {
      name,
      x: width / 2 + Math.cos(angle) * width * 0.28,
      y: height / 2 + Math.sin(angle) * height * 0.32,
      vx: 0, vy: 0,
      r: 5 + 13 * Math.sqrt((sizes.get(name) || 1) / widestLoc),
    }];
  }));

  // A short force relaxation: repulsion between every pair, springs along edges,
  // and a pull to the centre. Deterministic — same atlas, same layout.
  for (let step = 0; step < 240; step += 1) {
    const list = [...nodes.values()];
    for (let i = 0; i < list.length; i += 1) {
      for (let j = i + 1; j < list.length; j += 1) {
        const a = list[i]; const b = list[j];
        let dx = b.x - a.x; let dy = b.y - a.y;
        let distance = Math.hypot(dx, dy) || 0.01;
        const minimum = a.r + b.r + 16;
        const force = (2600 / (distance * distance)) + (distance < minimum ? 1.6 : 0);
        dx /= distance; dy /= distance;
        a.vx -= dx * force; a.vy -= dy * force;
        b.vx += dx * force; b.vy += dy * force;
      }
    }
    for (const edge of edges) {
      const a = nodes.get(edge.source); const b = nodes.get(edge.target);
      if (!a || !b) continue;
      const dx = b.x - a.x; const dy = b.y - a.y;
      const distance = Math.hypot(dx, dy) || 0.01;
      const pull = (distance - 92) * 0.012;
      a.vx += (dx / distance) * pull; a.vy += (dy / distance) * pull;
      b.vx -= (dx / distance) * pull; b.vy -= (dy / distance) * pull;
    }
    for (const node of nodes.values()) {
      node.vx += (width / 2 - node.x) * 0.004;
      node.vy += (height / 2 - node.y) * 0.004;
      node.x = Math.max(node.r + 3, Math.min(width - node.r - 3, node.x + node.vx * 0.5));
      node.y = Math.max(node.r + 3, Math.min(height - node.r - 3, node.y + node.vy * 0.5));
      node.vx *= 0.82; node.vy *= 0.82;
    }
  }

  const svg = svgEl("svg", { class: "viz", viewBox: `0 0 ${width} ${height}`,
                             role: "img", "aria-label": "Module dependency graph" });
  const heaviest = Math.max(...edges.map((e) => e.weight), 1);

  for (const edge of edges) {
    const a = nodes.get(edge.source); const b = nodes.get(edge.target);
    if (!a || !b) continue;
    svg.append(svgEl("line", {
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
      stroke: css("--axis"), "stroke-width": 0.6 + 2.6 * (edge.weight / heaviest),
      "stroke-opacity": 0.5, "stroke-linecap": "round",
    }));
  }

  for (const node of nodes.values()) {
    const group = svgEl("g", { style: "cursor:grab" });
    // 2px surface ring keeps overlapping nodes readable.
    group.append(svgEl("circle", {
      cx: node.x, cy: node.y, r: node.r,
      fill: css("--seq-400"), stroke: css("--surface"), "stroke-width": 2,
    }));
    if (node.r > 9) {
      const label = svgEl("text", {
        x: node.x, y: node.y + node.r + 11, "font-size": 9.5, "text-anchor": "middle",
        fill: css("--ink-muted"), "pointer-events": "none",
      });
      label.textContent = node.name.split("/").pop().slice(0, 14);
      group.append(label);
    }
    const inbound = edges.filter((e) => e.target === node.name).length;
    const outbound = edges.filter((e) => e.source === node.name).length;
    group.addEventListener("pointermove", (event) => showTip(
      `<div class="tt-title">${escapeHtml(node.name)}</div>` +
      `<div class="tt-row"><span>Lines</span><span>${(sizes.get(node.name) || 0).toLocaleString()}</span></div>` +
      `<div class="tt-row"><span>Depends on</span><span>${outbound}</span></div>` +
      `<div class="tt-row"><span>Depended on by</span><span>${inbound}</span></div>`,
      event));
    group.addEventListener("pointerleave", hideTip);
    svg.append(group);
  }
  host.append(svg);
}

// -------------------------------------------------------------- languages --

export function drawLanguages(atlas, bar, legend) {
  if (!bar) return;
  const entries = Object.entries(atlas.languages || {});
  if (!entries.length) { bar.innerHTML = ""; return; }
  const total = entries.reduce((sum, [, loc]) => sum + loc, 0) || 1;

  // Categorical identity, assigned in fixed order. Past eight, the tail folds
  // into "Other" rather than inventing a ninth hue.
  const shown = entries.slice(0, 7);
  const rest = entries.slice(7).reduce((sum, [, loc]) => sum + loc, 0);
  const slots = rest > 0 ? [...shown, ["Other", rest]] : shown;

  bar.innerHTML = "";
  legend.innerHTML = "";
  slots.forEach(([name, loc], index) => {
    const share = (loc / total) * 100;
    const colour = name === "Other" ? css("--ink-muted") : css(CAT[index % CAT.length]);
    bar.insertAdjacentHTML("beforeend",
      `<span style="width:${share}%;background:${colour}" title="${escapeHtml(name)}"></span>`);
    // Visible labels, not colour alone: three light-mode slots sit below 3:1
    // against the surface, so the text carries the identity.
    legend.insertAdjacentHTML("beforeend",
      `<span class="item"><span class="swatch" style="background:${colour}"></span>` +
      `${escapeHtml(name)} <span class="muted">${share.toFixed(1)}%</span></span>`);
  });
}

/* Charts read colour from CSS custom properties, so a theme change needs a repaint. */
window.addEventListener("pf:theme", () => {
  const data = document.getElementById("atlas-data");
  if (!data) return;
  const atlas = JSON.parse(data.textContent || "{}");
  if (!atlas.modules) return;
  const mode = document.getElementById("treemap-color")?.value || "churn";
  drawTreemap(atlas, document.getElementById("treemap"), mode);
  drawGraph(atlas, document.getElementById("depgraph"));
  drawLanguages(atlas, document.getElementById("langbar"),
                document.getElementById("langlegend"));
});

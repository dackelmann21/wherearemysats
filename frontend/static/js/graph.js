// Canvas-based directed graph renderer for the lineage view.
// No external libraries (REQ 34: no CDNs at runtime).
//
// Coordinate-system contract: ALL graph geometry (outpoint boxes, transaction squares, edges,
// arrowheads, fee/topology/provenance chips and labels) lives in one graph
// /world coordinate system and is transformed to the viewport by the single
// shared ctx transform applied in draw().  Edge endpoints and arrowheads are
// always computed from node geometry in world coordinates and are never
// derived from getBoundingClientRect()/screen positions, so zooming and
// panning can never make elements drift relative to each other.
//
// Horizontal layout: x(depth) = LEFT_MARGIN + depth * COLUMN_SPACING. Every
// distinct computed graph depth maps to its own column and the merge
// transaction sits strictly after the deepest tracked predecessor, so the
// graph flows left-to-right across depth instead of stacking node types.
window.LineageGraph = (function () {
  const NODE_W = 230;
  const NODE_H = 78;
  const TX_SIZE = 26;
  // Horizontal layout constants: every distinct graph depth gets its own column
  // spaced LEFT_MARGIN + depth * COLUMN_SPACING in world coordinates, so a
  // long trace grows wider than the viewport and stays a navigable canvas.
  const COLUMN_SPACING = 280;
  const LEFT_MARGIN = 40;

  // ------------------------------------------------------------------
  // Pure side-effect-free helpers (unit-tested by a test that
  // mirrors these exact algorithms).
  // ------------------------------------------------------------------

  // Classifies a pointer gesture: a click when total movement stays within
  // `threshold` CSS pixels, otherwise a drag/pan.
  function isClickGesture(dx, dy, threshold) {
    return Math.abs(dx) <= threshold && Math.abs(dy) <= threshold;
  }

  // One shared world<->screen viewport transform. scale + pan are the single
  // source of truth consumed by every visual graph layer.
  function worldToScreen(wx, wy, panX, panY, scale) {
    return { x: wx * scale + panX, y: wy * scale + panY };
  }
  function screenToWorld(sx, sy, panX, panY, scale) {
    return { x: (sx - panX) / scale, y: (sy - panY) / scale };
  }

  // Edge endpoints anchored to node boxes, in graph coordinates.
  //   source box  -> right-middle edge  ->  destination box <- left-middle edge
  // Returns null when either box is missing.
  function edgeAnchors(srcKey, dstKey, nodeMap, bounds) {
    const src = nodeMap(srcKey);
    const dst = nodeMap(dstKey);
    if (!src || !dst) return null;
    return {
      from: { x: src.x + bounds.nodeW, y: src.y },
      to: { x: dst.x, y: dst.y },
    };
  }

  // Bipartite layered DAG depth model.
  //
  // The trace is a layered graph of outpoints and transactions:
  //     start outpoint                    depth 0
  //     transaction (spending an outpoint) depth = max(depth of all TRACKED
  //                                          input outpoints) + 1
  //     output outpoint of a transaction   depth = transaction depth + 1
  //
  // A merge/re-convergence transaction therefore sits strictly AFTER the
  // deepest tracked predecessor branch (never at the same column as one),
  // and every txid maps to at most one node.  Custody bridges have an empty
  // spending_txid - they are logical user-confirmed links, not on-chain spend
  // edges - and never create transactions or feed tx/outpoint depth; they only
  // provide a fallback depth (+1) for an otherwise unplaced bridge destination.
  // `startKey` is the depth-0 anchor outpoint key, or an array of anchor keys
  // (an address-only start can resolve to several matching outpoints).
  function computeDepth(edges, startKey) {
    const producedBy = {};   // outpoint key -> producing txid
    const consumedBy = {};   // txid -> [consumed outpoint keys]  (tracked inputs)
    const bridges = [];      // { source, dest } logical links
    for (let i = 0; i < edges.length; i++) {
      const e = edges[i];
      const sk = e.source_txid + ":" + e.source_vout;
      const dk = e.destination_txid + ":" + e.destination_vout;
      if (!e.spending_txid) {
        bridges.push({ source: sk, dest: dk });
        continue;
      }
      producedBy[dk] = e.spending_txid;
      (consumedBy[e.spending_txid] = consumedBy[e.spending_txid] || []).push(sk);
    }

    const outDepth = {};
    const txDepth = {};
    // ONLY the start outpoint(s) anchor depth 0. Every edge source (spent
    // outpoint) and transaction depth is derived by the fixed-point
    // propagation below; a source is never pre-seeded at 0 - the propagation
    // only ever advances keys that are MISSING, so an incorrectly seeded
    // source would freeze the entire layout at depth 0 (the live regression:
    // ~all transactions collapsed to depth 1 because every seed source was a
    // dead end the "if (key in outDepth) return" guard could never replace).
    const startKeys = Array.isArray(startKey) ? startKey : [startKey];
    for (let i = 0; i < startKeys.length; i++) {
      if (startKeys[i]) outDepth[startKeys[i]] = 0;
    }

    let changed = true;
    while (changed) {
      changed = false;
      Object.keys(consumedBy).forEach(function (txid) {
        if (txid in txDepth) return;
        const preds = consumedBy[txid];
        let maxD = -1;
        let ready = true;
        for (let j = 0; j < preds.length; j++) {
          const d = outDepth[preds[j]];
          if (d === undefined) { ready = false; break; }
          if (d > maxD) maxD = d;
        }
        if (ready && maxD >= 0) { txDepth[txid] = maxD + 1; changed = true; }
      });
      Object.keys(producedBy).forEach(function (key) {
        if (key in outDepth) return;
        const t = producedBy[key];
        if (t in txDepth) { outDepth[key] = txDepth[t] + 1; changed = true; }
      });
    }

    for (let b = 0; b < bridges.length; b++) {
      const s = bridges[b].source;
      const d = bridges[b].dest;
      if ((s in outDepth) && !(d in outDepth)) outDepth[d] = outDepth[s] + 1;
    }

    return { outDepth: outDepth, txDepth: txDepth, producedBy: producedBy };
  }

  // Reverse-mode depth-0 anchors (REVERSE_PROVENANCE / BLOCKCHAIN_ANALYSIS_REVERSE):
  // the oldest discovered ancestor outpoints - edge sources that are never an
  // edge destination. Seeding these at depth 0 lets the existing chronological
  // edge propagation flow rightward through producing transactions to the
  // selected target at the deepest column, so reverse traces render as
  // "oldest ancestors on the left, newest target on the right" with arrows
  // keeping their real Bitcoin direction. Custody bridges (empty
  // spending_txid) never anchor depth - they are logical links and computeDepth
  // already ignores them.
  function reverseRoots(edges) {
    const sources = {};
    const destinations = {};
    for (let i = 0; i < edges.length; i++) {
      const e = edges[i];
      if (!e.spending_txid) continue;
      sources[e.source_txid + ":" + e.source_vout] = true;
      destinations[e.destination_txid + ":" + e.destination_vout] = true;
    }
    return Object.keys(sources).filter(function (key) { return !destinations[key]; });
  }

  function buildModel(traceData, opts) {
    opts = opts || {};
    // Collect outpoint nodes indexed by key.
    const outpoints = {};
    traceData.outpoints.forEach(function (o) {
      const key = o.txid + ":" + o.vout;
      if (!outpoints[key]) outpoints[key] = o;
    });

    // Heuristic custody suggestions / declined candidates, keyed by outpoint.
    const suggByKey = {};
    (traceData.possible_boundaries || []).forEach(function (s) {
      suggByKey[s.txid + ":" + s.vout] = s;
    });
    const dismByKey = {};
    (traceData.dismissed_candidates || []).forEach(function (d) {
      dismByKey[d.txid + ":" + d.vout] = d;
    });

    // Transactions nodes.
    const txns = {};
    traceData.transactions.forEach(function (t) { txns[t.txid] = t; });

    const model = { outpoints: outpoints, transactions: txns, edges: traceData.edges || [] };

    // Compute layered DAG depths (merge transactions sit after the deepest
    // tracked predecessor branch; every txid is rendered exactly once).
    // Resolve the depth-0 anchor key(s): with a vout the start outpoint is
    // exact; an address-only start (or a manual multi-outpoint start) anchors
    // every payload outpoint of the start transaction that matches - a single
    // ownership match resolves to that one exact outpoint at depth 0.
    let startKeys;
    if (opts.anchors) {
      // Reverse mode: the caller identifies the depth-0 set (oldest ancestor
      // roots via reverseRoots(), or a lone target fallback when the graph has
      // no edges). The selected target is NOT an anchor - it belongs on the
      // right alongside the newest outpoints.
      startKeys = Array.isArray(opts.anchors) ? opts.anchors.slice() : [opts.anchors];
    } else if (traceData.trace.start_vout !== null && traceData.trace.start_vout !== undefined) {
      startKeys = [traceData.trace.start_txid + ":" + traceData.trace.start_vout];
    } else {
      startKeys = [];
      traceData.outpoints.forEach(function (o) {
        const matches = o.txid === traceData.trace.start_txid &&
          (traceData.trace.start_address ? o.address === traceData.trace.start_address : o.ownership_status === "OWNED");
        if (matches) startKeys.push(o.txid + ":" + o.vout);
      });
      if (!startKeys.length) startKeys = [traceData.trace.start_txid + ":"];
    }

    const depth = computeDepth(traceData.edges || [], startKeys);
    const outDepth = depth.outDepth;
    const txDepth = depth.txDepth;

    const output = { nodes: [], edges: [] };

    // Transaction nodes (one per txid - a merge is rendered once).
    Object.keys(txDepth).forEach(function (txid) {
      output.nodes.push({
        type: "transaction", key: txid, depth: txDepth[txid], data: txns[txid],
      });
    });

    // Outputs of placed transactions that were not followed (EXTERNAL/UNKNOWN
    // siblings such as a small change output) stay on the canvas at the
    // transaction's output column.  Transaction nodes are keyed by txid, so a
    // merged transaction is rendered exactly once.
    Object.keys(txDepth).forEach(function (txid) {
      const t = txns[txid];
      if (!t || !t.outputs) return;
      t.outputs.forEach(function (o) {
        const key = o.txid + ":" + o.vout;
        if (!(key in outDepth)) outDepth[key] = txDepth[txid] + 1;
      });
    });

    traceData.edges.forEach(function (e) {
      const sk = e.source_txid + ":" + e.source_vout;
      const dk = e.destination_txid + ":" + e.destination_vout;
      output.edges.push({
        source: sk, tx: e.spending_txid, target: dk,
        rel: e.relationship_type, hop: e.hop_number, trace: true,
        bridge: e.relationship_type === "CUSTODY_BRIDGE",
      });
    });

    Object.keys(outDepth).forEach(function (key) {
      const o = outpoints[key];
      if (!o) return;
      output.nodes.push({
        type: "outpoint", key: key, depth: outDepth[key],
        data: o, txidx: key.split(":")[0], vout: parseInt(key.split(":")[1], 10),
        suggestion: suggByKey[key] || null, dismissed: dismByKey[key] || null,
        highlight: opts.highlight === key,
      });
    });

    // Any start outpoint without edges (terminal immediately) - include if owned.
    traceData.outpoints.forEach(function (o) {
      const key = o.txid + ":" + o.vout;
      if (o.txid === traceData.trace.start_txid && !(key in outDepth)) {
        const isStart = (traceData.trace.start_vout === null || o.vout === traceData.trace.start_vout ||
          (traceData.trace.start_address && o.address === traceData.trace.start_address));
        if (isStart || o.ownership_status === "OWNED") {
          outDepth[key] = 0;
          output.nodes.push({
            type: "outpoint", key: key, depth: 0, data: o,
            txidx: o.txid, vout: o.vout,
            suggestion: suggByKey[key] || null, dismissed: dismByKey[key] || null,
            highlight: opts.highlight === key,
          });
        }
      }
    });

    // Sort by depth then key for stable vertical ordering.
    output.nodes.sort(function (a, b) { return a.depth - b.depth || (a.key < b.key ? -1 : 1); });
    return output;
  }

  function layout(model, w, h) {
    // group nodes by depth
    const groups = {};
    model.nodes.forEach(function (n) {
      (groups[n.depth] = groups[n.depth] || []).push(n);
    });
    const depths = Object.keys(groups).map(Number).sort(function (a, b) { return a - b; });
    const xStep = COLUMN_SPACING;
    const minY = 30;
    const maxY = h - 30;

    depths.forEach(function (d) {
      const nodes = groups[d];
      const usable = Math.max(120, maxY - minY);
      const gap = nodes.length > 1 ? Math.min(110, usable / (nodes.length - 1)) : 0;
      const startY = (nodes.length - 1) * gap / 2;
      nodes.forEach(function (n, j) {
        // Every distinct computed graph depth maps to its own horizontal
        // column: x(depth) = leftMargin + depth * columnSpacing. The code uses
        // the depth VALUE, never an index over present depths and never a
        // type/parity offset, so outpoints and transactions can never collapse
        // into a few shared columns and gap magnitudes are preserved. The
        // required invariant holds by construction:
        //     if depth(A) < depth(B) then x(A) < x(B).
        n.x = LEFT_MARGIN + d * xStep;
        n.y = (maxY + minY) / 2 - startY + j * gap;
      });
    });
    return model;
  }

  // Viewport fit: computed as a pure function so the layout/tests can exercise
  // the exact zoom/pan math. `raw` fits the whole content (capped at 1.1, same
  // as the accepted v0.4.6 behavior); when a long trace would otherwise be
  // squeezed into the visible width, the scale is floored at `minScale` so the
  // world stays wider than the viewport (navigable canvas) and the pan jumps
  // to the START column instead of centering on a collapsed middle.
  function fitParams(x0, y0, x1, y1, visW, visH, minScale, pad) {
    const contentW = (x1 - x0) + 160;
    const contentH = (y1 - y0) + 120;
    const raw = Math.min((visW - 2 * pad) / contentW, (visH - 2 * pad) / contentH, 1.1);
    const floor = minScale === undefined || minScale === null ? 0 : minScale;
    const scale = Math.max(raw, floor);
    const floored = scale > raw;
    const panX = floored
      ? pad - x0 * scale
      : visW / 2 - (x0 + x1) / 2 * scale;
    const panY = visH / 2 - (y0 + y1) / 2 * scale;
    return { scale: scale, panX: panX, panY: panY, floored: floored };
  }

  function Renderer(canvas, model) {
    this.canvas = canvas;
    this.model = model;
    this.ctx = canvas.getContext("2d");
    this.scale = 1;
    this.offsetX = 0;
    this.offsetY = 0;
    this.panX = 0;
    this.panY = 0;
    this.visW = 1000;
    this.visH = 700;
    this.onOpenNode = null;
  }

  Renderer.prototype.backingSize = function () {
    const dpr = window.devicePixelRatio || 1;
    const cssW = this.canvas.clientWidth || 800;
    const cssH = this.canvas.clientHeight || 620;
    this.canvas.width = cssW * dpr;
    this.canvas.height = cssH * dpr;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.visW = this.canvas.width / dpr;
    this.visH = this.canvas.height / dpr;
  };

  Renderer.prototype.fit = function () {
    this.backingSize();
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const self = this;
    this.model.nodes.forEach(function (n) {
      if (n.type === "outpoint") { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y - NODE_H / 2); x1 = Math.max(x1, n.x + NODE_W); y1 = Math.max(y1, n.y + NODE_H / 2); }
      else { x0 = Math.min(x0, n.x - 30); y0 = Math.min(y0, n.y - 30); x1 = Math.max(x1, n.x + 30); y1 = Math.max(y1, n.y + 30); }
    });
    if (!isFinite(x0) || x1 === x0) { x0 = 20; x1 = 260; }
    if (!isFinite(y0) || y1 === y0) { y0 = 20; y1 = 200; }
    // Keep every column at least ~190 CSS px wide so 20+ depths are never
    // squeezed into the visible width; the trace then behaves like a wide,
    // navigable canvas (pan/zoom stay available).
    const minScale = 190 / COLUMN_SPACING;
    const fp = fitParams(x0, y0, x1, y1, this.visW, this.visH, minScale, 40);
    this.scale = fp.scale;
    this.panX = fp.panX;
    this.panY = fp.panY;
    this.draw();
  };

  // Single viewport transform: screen = worldToScreen(world, panX, panY, scale).
  Renderer.prototype.screen = function (x, y) {
    return worldToScreen(x, y, this.panX, this.panY, this.scale);
  };
  Renderer.prototype.world = function (sx, sy) {
    return screenToWorld(sx, sy, this.panX, this.panY, this.scale);
  };

  Renderer.prototype.draw = function () {
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.visW, this.visH);
    ctx.save();
    // ONE shared viewport transform for the entire world group: nodes, edges,
    // arrowheads and chips are all drawn in world coordinates below and move
    // together through this same transform - zoom/pan can never detach them.
    ctx.translate(this.panX, this.panY);
    ctx.scale(this.scale, this.scale);
    this.drawEdges();
    this.drawNodes();
    ctx.restore();
  };

  const bounds = { nodeW: NODE_W, nodeH: NODE_H, txSize: TX_SIZE };

  Renderer.prototype.drawEdges = function () {
    const ctx = this.ctx;
    const self = this;
    self.model.edges.forEach(function (e) {
      const src = self.nodePos(e.source);
      const dst = self.nodePos(e.target);
      // Custody bridges are logical, user-confirmed links: draw a dashed line
      // directly to the destination (no on-chain spending tx) so they can never
      // be read as exact Bitcoin lineage.
      if (e.bridge) {
        const anch = edgeAnchors(e.source, e.target, function (k) { return self.nodePos(k); }, bounds);
        if (!anch) return;
        ctx.save();
        ctx.strokeStyle = "#c586c0";
        ctx.lineWidth = 2;
        ctx.setLineDash([5, 4]);
        ctx.beginPath();
        ctx.moveTo(anch.from.x, anch.from.y);
        ctx.lineTo(anch.to.x, anch.to.y);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "#c586c0";
        const ang = Math.atan2(anch.to.y - anch.from.y, anch.to.x - anch.from.x);
        const hs = 5;
        ctx.beginPath();
        ctx.moveTo(anch.to.x, anch.to.y);
        ctx.lineTo(anch.to.x - hs * Math.cos(ang - 0.5), anch.to.y - hs * Math.sin(ang - 0.5));
        ctx.lineTo(anch.to.x - hs * Math.cos(ang + 0.5), anch.to.y - hs * Math.sin(ang + 0.5));
        ctx.closePath();
        ctx.fill();
        ctx.restore();
        return;
      }
      const txPos = self.txPos(e.tx);
      if (!src || !txPos || !dst) return;
      ctx.strokeStyle = "#4b5563";
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      // source outpoint right-middle -> transaction left -> transaction right ->
      // destination outpoint left-middle (all in world/graph coordinates).
      ctx.moveTo(src.x + NODE_W, src.y);
      ctx.lineTo(txPos.x - TX_SIZE / 2, txPos.y);
      ctx.moveTo(txPos.x + TX_SIZE / 2, txPos.y);
      ctx.lineTo(dst.x, dst.y);
      ctx.stroke();
      // arrow head into destination, aligned with the last segment.
      ctx.fillStyle = "#9ca3af";
      const ang = Math.atan2(dst.y - txPos.y, dst.x - txPos.x);
      const hs = 5;
      ctx.beginPath();
      ctx.moveTo(dst.x, dst.y);
      ctx.lineTo(dst.x - hs * Math.cos(ang - 0.5), dst.y - hs * Math.sin(ang - 0.5));
      ctx.lineTo(dst.x - hs * Math.cos(ang + 0.5), dst.y - hs * Math.sin(ang + 0.5));
      ctx.closePath();
      ctx.fill();
    });
  };

  Renderer.prototype.drawNodes = function () {
    const ctx = this.ctx;
    const self = this;
    this.model.nodes.forEach(function (n) {
      if (n.type === "transaction") self.drawTx(n);
      else self.drawOutpoint(n);
    });
  };

  Renderer.prototype.drawTx = function (n) {
    const ctx = this.ctx;
    // p is in graph/world coordinates; the shared ctx transform in draw()
    // maps it (and every edge/arrowhead/chip) to the viewport uniformly.
    const p = { x: n.x, y: n.y };
    ctx.fillStyle = "#1c2129";
    ctx.strokeStyle = "#6b7280";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    const s = TX_SIZE / 2;
    ctx.rect(p.x - s, p.y - s, TX_SIZE, TX_SIZE);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = "#9ca3af";
    ctx.font = "12px monospace";
    ctx.textAlign = "center";

    // Topology shape: N non-coinbase inputs -> M meaningful outputs. This is a
    // pure blockchain fact and never
    // a custody signal. A likely batch gets its own violet chip.
    const d = n.data || {};
    const ins = (d.inputs || []).filter(function (i) {
      return i.txid || (i.prev_outpoint && i.prev_outpoint !== "coinbase");
    }).length;
    const outs = (d.outputs || []).filter(function (o) {
      return (o.value_sats || 0) > 0 &&
        o.script_type !== "nulldata" && o.script_type !== "nonstandard";
    }).length;
    // Three lines above box: topology (top), txid (middle), fee (bottom,
    // only when fee_sats is known).  0 sats is rendered when the fee is
    // genuinely zero; null/undefined means the fee could not be computed.
    const hasFee = d.fee_sats != null;
    const txY = hasFee ? p.y - s - 18 : p.y - s - 5;
    const topoY = hasFee ? p.y - s - 30 : p.y - s - 17;
    ctx.font = "12px monospace";
    ctx.fillStyle = "#9ca3af";
    ctx.fillText(n.key.slice(0, 6), p.x, txY);
    ctx.font = "bold 10px monospace";
    ctx.fillStyle = "#8b949e";
    ctx.fillText(ins + "->" + outs, p.x, topoY);
    if (hasFee) {
      var feeLabel = "MINER FEE: " + Fmt.sats(d.fee_sats) + " sats";
      if (d.fee_rate != null) {
        feeLabel += " \u00b7 " + d.fee_rate + " sat/vB";
      }
      ctx.font = "bold 10px monospace";
      var fw = ctx.measureText(feeLabel).width + 10;
      var fx = p.x - fw / 2;
      var fy = p.y - s - 14;
      var fh = 14;
      ctx.fillStyle = "rgba(210,153,34,0.15)";
      roundRect(ctx, fx, fy, fw, fh, 3);
      ctx.fill();
      ctx.fillStyle = "#d29922";
      ctx.textAlign = "center";
      ctx.fillText(feeLabel, p.x, fy + 11);
    }
    if (d.likely_batch) {
      const chip = "batch";
      ctx.font = "bold 8px sans-serif";
      const cw = ctx.measureText(chip).width + 8;
      const cx = p.x + s + 4;
      const cy = p.y + s + 4;
      ctx.fillStyle = "rgba(78,59,222,0.25)";
      roundRect(ctx, cx, cy, cw, 12, 3);
      ctx.fill();
      ctx.fillStyle = "#a371f7";
      ctx.textAlign = "left";
      ctx.fillText(chip, cx + 4, cy + 10);
      ctx.textAlign = "center";
    }

    // tag
    if (d.tags && d.tags.length) {
      ctx.font = "10px sans-serif";
      ctx.fillStyle = "#8b949e";
      ctx.fillText(d.tags.join(","), p.x, p.y + s + 13);
    }
  };

  Renderer.prototype.drawOutpoint = function (n) {
    const ctx = this.ctx;
    // p is in graph/world coordinates (see drawTx note): box edges, labels and
    // the connecting edges all reference the same world space, so every
    // element translates+scales together through the single viewport transform.
    const p = { x: n.x, y: n.y };
    const d = n.data;
    const status = d.spend_status;
    const own = d.ownership_status;
    let fill = "#161b22";
    let border = "#6e7681";
    if (own === "OWNED") border = "#3fb950";
    else if (own === "LIKELY_OWNED") border = "#56d364";
    else if (own === "EXTERNAL") border = "#d29922";
    else if (own === "CUSTODIAL_DEPOSIT") border = "#c586c0";
    else border = "#8b949e";
    if (status === "UNSPENT") fill = "rgba(63,185,80,0.10)";
    if (status === "UNKNOWN") border = "#d29922";

    ctx.fillStyle = fill;
    ctx.strokeStyle = border;
    ctx.lineWidth = 2;
    const r = 6;
    ctx.beginPath();
    ctx.moveTo(p.x + r, p.y - NODE_H / 2);
    ctx.lineTo(p.x + NODE_W - r, p.y - NODE_H / 2);
    ctx.quadraticCurveTo(p.x + NODE_W, p.y - NODE_H / 2, p.x + NODE_W, p.y - NODE_H / 2 + r);
    ctx.lineTo(p.x + NODE_W, p.y + NODE_H / 2 - r);
    ctx.quadraticCurveTo(p.x + NODE_W, p.y + NODE_H / 2, p.x + NODE_W - r, p.y + NODE_H / 2);
    ctx.lineTo(p.x + r, p.y + NODE_H / 2);
    ctx.quadraticCurveTo(p.x, p.y + NODE_H / 2, p.x, p.y + NODE_H / 2 - r);
    ctx.lineTo(p.x, p.y - NODE_H / 2 + r);
    ctx.quadraticCurveTo(p.x, p.y - NODE_H / 2, p.x + r, p.y - NODE_H / 2);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();

    // Reverse-trace selected target ring (REVERSE_PROVENANCE /
    // BLOCKCHAIN_ANALYSIS_REVERSE): a bright accent ring + corner ticks make
    // the newest outpoint unmistakable next to its older ancestors without
    // changing any node geometry or layout internals.
    if (n.highlight) {
      ctx.save();
      ctx.strokeStyle = "#58a6ff";
      ctx.lineWidth = 3;
      ctx.setLineDash([7, 5]);
      ctx.beginPath();
      const hx = p.x + NODE_W / 2, hy = p.y;
      ctx.arc(hx, hy, Math.min(NODE_W, NODE_H) / 2 + 7, 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
      const tl = 14, g = 9;
      ctx.strokeStyle = "#58a6ff";
      ctx.beginPath();
      ctx.moveTo(p.x - g, p.y - NODE_H / 2 - g); ctx.lineTo(p.x + tl, p.y - NODE_H / 2 - g);
      ctx.moveTo(p.x - g, p.y - NODE_H / 2 - g); ctx.lineTo(p.x - g, p.y - NODE_H / 2 + tl);
      ctx.moveTo(p.x + NODE_W + g, p.y - NODE_H / 2 - g); ctx.lineTo(p.x + NODE_W - tl, p.y - NODE_H / 2 - g);
      ctx.moveTo(p.x + NODE_W + g, p.y - NODE_H / 2 - g); ctx.lineTo(p.x + NODE_W + g, p.y - NODE_H / 2 + tl);
      ctx.stroke();
      ctx.restore();
    }

    // top line: TXID:VOUT + status
    ctx.textAlign = "left";
    ctx.font = "bold 12px monospace";
    ctx.fillStyle = "#e6edf3";
    ctx.fillText(Fmt.shortTxid(d.txid, 10) + ":" + d.vout, p.x + 10, p.y - NODE_H / 2 + 16);

    ctx.font = "11px sans-serif";
    ctx.fillStyle = status === "UNSPENT" ? "#3fb950" : status === "UNKNOWN" ? "#d29922" : "#8b949e";
    const stText = status || "—";
    const tw = ctx.measureText(stText).width;
    ctx.fillText(stText, p.x + NODE_W - 10 - tw, p.y - NODE_H / 2 + 16);

    // address
    ctx.fillStyle = "#8b949e";
    ctx.fillText(Fmt.shortAddress(d.address), p.x + 10, p.y - 8);

    // amount: sats prominent (left, bold), BTC secondary (right, dim)
    // so even tiny change outputs (e.g. 5,000 sats) are visually distinct
    // UTXOs, not confused with miner fees.
    ctx.font = "bold 12px monospace";
    ctx.fillStyle = "#e6edf3";
    ctx.fillText(Fmt.sats(d.value_sats) + " sats", p.x + 10, p.y + 14);
    ctx.font = "10px monospace";
    ctx.fillStyle = "#8b949e";
    var btcLabel = Fmt.btc(d.value_sats) + " BTC";
    ctx.fillText(btcLabel, p.x + NODE_W - 10 - ctx.measureText(btcLabel).width, p.y + 14);

    // ownership label
    ctx.font = "10px sans-serif";
    ctx.fillStyle = own === "OWNED" ? "#3fb950" : own === "EXTERNAL" ? "#d29922" : own === "CUSTODIAL_DEPOSIT" ? "#c586c0" : "#8b949e";
    const ownLabel = own || "UNKNOWN";
    ctx.fillText(ownLabel, p.x + 10, p.y + NODE_H / 2 - 8);

    // provenance badge: attribution confidence for THIS output (EXACT / MIXED /
    // AMBIGUOUS). Prefixed "PROV:" so it is unambiguous next to ownership,
    // topology, and fee annotations.
    const prov = d.provenance_state || "UNKNOWN";
    ctx.font = "bold 9px sans-serif";
    const plabel = "PROV: " + prov;
    const pw = ctx.measureText(plabel).width + 12;
    const pbx = p.x + NODE_W - pw - 8;
    const pby = p.y + NODE_H / 2 - 17;
    const pbh = 13;
    const provColor = prov === "EXACT" ? "#3fb950" : prov === "MIXED" ? "#a371f7" : prov === "AMBIGUOUS" ? "#d29922" : "#8b949e";
    ctx.fillStyle = "rgba(0,0,0,0.25)";
    roundRect(ctx, pbx, pby, pw, pbh, 3);
    ctx.fill();
    ctx.fillStyle = provColor;
    ctx.textAlign = "left";
    ctx.fillText(plabel, pbx + 6, pby + 10);

    // custody badge: heuristic suggestion (or a declined candidate).
    if (n.suggestion) {
      const label = "Possible custody · " + (n.suggestion.confidence === "strong" ? "strong evidence" : "likely");
      ctx.font = "bold 10px sans-serif";
      const tw2 = ctx.measureText(label).width + 18;
      const bx = p.x + NODE_W - tw2 - 8;
      const by = p.y + NODE_H / 2 - 21;
      const bh = 15;
      ctx.fillStyle = "rgba(210,153,34,0.95)";
      roundRect(ctx, bx, by, tw2, bh, 4);
      ctx.fill();
      ctx.fillStyle = "#0d1117";
      ctx.textAlign = "left";
      ctx.fillText(label, bx + 9, by + 12);
      ctx.fillStyle = "#0d1117";
      ctx.font = "bold 11px sans-serif";
      ctx.fillText("!", bx + tw2 - 2 - ctx.measureText("!").width, by + 12);
      ctx.textAlign = "left";
    } else if (n.dismissed) {
      const label = "Possible custody · declined";
      ctx.font = "bold 10px sans-serif";
      const tw2 = ctx.measureText(label).width + 14;
      const bx = p.x + NODE_W - tw2 - 8;
      const by = p.y + NODE_H / 2 - 21;
      const bh = 15;
      ctx.fillStyle = "rgba(139,148,158,0.9)";
      roundRect(ctx, bx, by, tw2, bh, 4);
      ctx.fill();
      ctx.fillStyle = "#0d1117";
      ctx.textAlign = "left";
      ctx.fillText(label, bx + 7, by + 12);
    }
  };

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  Renderer.prototype.hitTest = function (sx, sy) {
    // Convert the screen-space pointer into world/graph coordinates through
    // the SAME shared viewport transform used for rendering, then test against
    // node boxes expressed in world coordinates.  This stays correct after any
    // sequence of zooms/pans because both sides use the one transform.
    const self = this;
    const w = this.world(sx, sy);
    // outpoints first
    for (let i = this.model.nodes.length - 1; i >= 0; i--) {
      const n = this.model.nodes[i];
      if (n.type === "transaction") continue;
      if (w.x >= n.x && w.x <= n.x + NODE_W && w.y >= n.y - NODE_H / 2 && w.y <= n.y + NODE_H / 2) return n;
    }
    for (let i = this.model.nodes.length - 1; i >= 0; i--) {
      const n = this.model.nodes[i];
      if (n.type !== "transaction") continue;
      if (Math.abs(w.x - n.x) <= TX_SIZE && Math.abs(w.y - n.y) <= TX_SIZE) return n;
    }
    return null;
  };

  Renderer.prototype.nodePos = function (key) {
    for (let i = 0; i < this.model.nodes.length; i++) {
      const n = this.model.nodes[i];
      if (n.key === key && n.type === "outpoint") return n;
    }
    return null;
  };
  Renderer.prototype.txPos = function (key) {
    for (let i = 0; i < this.model.nodes.length; i++) {
      const n = this.model.nodes[i];
      if (n.key === key && n.type === "transaction") return n;
    }
    return null;
  };

  function attach(canvas, model, opts) {
    const r = new Renderer(canvas, model);
    r.onOpenNode = opts.onOpenNode || null;
    layout(model, r.visW || 1000, r.visH || 700);
    r.fit();

    // Pointer gesture handling: a click is a pointerup with total movement within THRESH CSS
    // pixels of the pointerdown point; anything larger is a pan/drag. The node
    // under the pointer is captured at pointerdown (downHit) and opened on
    // click, so 1-2px of jitter never cancels a node click and the node that
    // WAS PRESSED is the node that opens - never a late re-hit-test at
    // pointerup.  Pan only starts once the movement EXCEEDS the threshold.  No
    // separate 'click' listener exists, so there is no double activation.
    const THRESH = 5;
    var dragging = false;
    var panned = false;
    var activePointerId = null;
    var downHit = null;
    var startX = 0, startY = 0, startX0 = 0, startY0 = 0;

    // Terminate the current gesture unconditionally and restore the idle
    // state. Called from EVERY path a gesture can end through (pointerup,
    // pointercancel, lostpointercapture, window blur) AND in a finally()
    // around onOpenNode(), so an exception thrown by the modal code releases
    // pointer capture and can never leave the canvas stuck to the mouse.
    function resetGesture(e) {
      if (e && activePointerId != null &&
          canvas.hasPointerCapture(activePointerId)) {
        try { canvas.releasePointerCapture(activePointerId); } catch (_) {}
      }
      dragging = false;
      panned = false;
      activePointerId = null;
      downHit = null;
    }

    function setCursor(sx, sy) {
      canvas.style.cursor = r.hitTest(sx, sy) ? "pointer" : "grab";
    }

    canvas.addEventListener("pointerdown", function (e) {
      // Only the primary button starts a node-click / pan gesture; right or
      // middle button presses must never hijack the canvas.
      if (e.button !== 0) return;
      const rect = canvas.getBoundingClientRect();
      dragging = true; panned = false;
      activePointerId = e.pointerId;
      startX = startX0 = e.clientX; startY = startY0 = e.clientY;
      downHit = r.hitTest(e.clientX - rect.left, e.clientY - rect.top);
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener("pointermove", function (e) {
      const rect = canvas.getBoundingClientRect();
      // Idle hover keeps updating the cursor for any pointer; once a gesture
      // is active, only the captured pointer drives it.
      if (activePointerId != null && e.pointerId !== activePointerId) return;
      if (!dragging) { setCursor(e.clientX - rect.left, e.clientY - rect.top); return; }
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      if (!panned && !isClickGesture(e.clientX - startX0, e.clientY - startY0, THRESH)) {
        panned = true;
      }
      if (panned) {
        r.panX += dx;
        r.panY += dy;
        startX = e.clientX; startY = e.clientY;
        r.draw();
      }
    });
    canvas.addEventListener("pointerup", function (e) {
      if (e.pointerId !== activePointerId) return;

      const click =
        dragging &&
        !panned &&
        isClickGesture(e.clientX - startX0,
                       e.clientY - startY0,
                       THRESH);

      const hit = click ? downHit : null;

      try {
        if (hit && r.onOpenNode) r.onOpenNode(hit);
      } finally {
        resetGesture(e);
      }
    });
    // A tap opens the dialog on pointerup; without this the browser's
    // compatibility click lands on the freshly shown dialog and opens the row
    // under the finger (FIX-UI-3). The canvas itself needs no click.
    canvas.addEventListener("touchend", function (e) {
      if (e.cancelable) e.preventDefault();
    }, { passive: false });
    canvas.addEventListener("pointercancel", function (e) {
      resetGesture(e);
    });
    canvas.addEventListener("lostpointercapture", function (e) {
      if (e.pointerId === activePointerId) resetGesture(e);
    });
    function onBlur() { resetGesture(null); }
    window.addEventListener("blur", onBlur);
    canvas.addEventListener("wheel", function (e) {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const sx = e.clientX - rect.left;
      const sy = e.clientY - rect.top;
      const ws = r.world(sx, sy);
      const factor = e.deltaY < 0 ? 1.12 : 0.89;
      r.scale = Math.min(3, Math.max(0.2, r.scale * factor));
      r.panX = sx - ws.x * r.scale;
      r.panY = sy - ws.y * r.scale;
      r.draw();
    }, { passive: false });

    function onResize() { r.fit(); }
    window.addEventListener("resize", onResize);
    // Remove the window-level listeners when the canvas is replaced or the
    // view is left, so detached canvases are not kept alive.
    r.detach = function () {
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("resize", onResize);
    };
    return r;
  }

  return {
    NODE_W: NODE_W,
    NODE_H: NODE_H,
    TX_SIZE: TX_SIZE,
    COLUMN_SPACING: COLUMN_SPACING,
    LEFT_MARGIN: LEFT_MARGIN,
    buildModel: buildModel,
    reverseRoots: reverseRoots,
    attach: attach,
    layout: layout,
    fitParams: fitParams,
    isClickGesture: isClickGesture,
    worldToScreen: worldToScreen,
    screenToWorld: screenToWorld,
    edgeAnchors: edgeAnchors,
    computeDepth: computeDepth,
  };
})();
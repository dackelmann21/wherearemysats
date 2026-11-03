// Application shell: view routing, node pill, detail modals.
window.App = (function () {

  const views = { reports: "view-reports", start: "view-start", traces: "view-traces", inventory: "view-inventory", acquisitions: "view-acquisitions", registry: "view-registry", status: "view-status", "node-settings": "view-node-settings", trace: "view-trace" };
  const reportViews = ['dashboard', 'current-wallet'];

  function showView(name, arg, routed) {
    if (!routed) {
      var next = '#' + name + (name === 'trace' ? '?id=' + encodeURIComponent(arg) : '');
      if (location.hash !== next) { location.hash = next; return; }
    }
    var requested = name;
    if (name === 'review') {
      var previousReview = new URLSearchParams(location.hash.split('?')[1] || '');
      var currentReview = new URLSearchParams({ coverage: 'UNRESOLVED' });
      if (previousReview.has('wallet')) currentReview.set('wallet', previousReview.get('wallet'));
      history.replaceState(null, '', '#current-wallet?' + currentReview.toString());
      name = requested = 'current-wallet';
    }
    if (name === 'records' || name === 'origins' || name === 'exits') name = 'acquisitions';
    if (reportViews.indexOf(name) >= 0) name = 'reports';
    if (!views[name] && name !== 'provenance') name = 'reports';
    if (name === "provenance") name = "acquisitions"; // compatible old view alias
    // Stop any background analysis-job polling before leaving a trace view so
    // no stale interval keeps firing into a detached DOM element.
    if (window.TraceView && window.TraceView.stop) window.TraceView.stop();
    if (window.StartPage) window.StartPage.stop();
    if (window.AcquisitionsPage && window.AcquisitionsPage.stopCoverage) window.AcquisitionsPage.stopCoverage();
    if (window.NodeSettingsPage) window.NodeSettingsPage.clearPassword();
    if (window.WalletDiscoveryPage) window.WalletDiscoveryPage.stop();
    if (window.InventoryPage) window.InventoryPage.stop();
    if (window.ProvenancePage) window.ProvenancePage.stop();
    if (window.ReportsPage) window.ReportsPage.stop();

    Object.keys(views).forEach(function (k) {
      document.getElementById(views[k]).classList.toggle("hidden", k !== name);
    });
    // v0.7.3: the acquisitions page uses a wider desktop container (scoped to
    // that view only; every other page keeps the compact 1200px layout).
    document.body.classList.toggle("acq-wide", name === "acquisitions");
    document.querySelectorAll(".tabs .tab").forEach(function (t) {
      if (name === "trace" && (t.dataset.view === "start" || t.dataset.view === "traces" || t.dataset.view === "registry" || t.dataset.view === "status")) {
        t.classList.toggle("active", false);
      } else {
        t.classList.toggle("active", t.dataset.view === requested);
      }
    });
    const el = document.getElementById(views[name]);
    if (name === 'reports') window.ReportsPage.render(el, requested, new URLSearchParams(location.hash.split('?')[1] || ''));
    if (name === "start") window.StartPage.render(el, new URLSearchParams(location.hash.split('?')[1] || ''));
    if (name === "traces") window.TracesPage.render(el);
    if (name === "acquisitions") window.ProvenancePage.render(el, new URLSearchParams(location.hash.split('?')[1] || ''), requested);
    if (name === "inventory") window.InventoryPage.render(el);
    if (name === "registry") window.RegistryPage.render(el);
    if (name === "status") window.StatusPage.render(el);
    if (name === "node-settings") window.NodeSettingsPage.render(el);
    if (name === "trace") window.TraceView.render(el, arg);
    window.scrollTo(0, 0);
  }

  function openTrace(id) {
    showView("trace", Number(id));
  }

  // ------------------------------------------------------ detail modals
  //
  // Modal UX (v0.5.3): modals are viewport-fixed centered dialogs that stay
  // fully on screen. The header doubles as a drag handle; scroll happens
  // INSIDE the dialog (.modal-scroll, capped at 85vh) so opening a long
  // UTXO/merge detail never requires scrolling the page behind it.

  function modalShell(title, bodyHtml) {
    return "" +
      '<div class="modal-header" title="Drag to move">' +
      "  <h2>" + title + "</h2>" +
      '  <button class="modal-close" title="Close">&times;</button>' +
      "</div>" +
      '<div class="modal-scroll">' + bodyHtml + "</div>";
  }

  // Pure geometry (mirrored by a unit test).
  // Clamp a desired top-left so the dialog can never be moved completely
  // outside the visible viewport; centered position is the same clamp applied
  // to the viewport midpoints.
  function clampModalPosition(x, y, w, h, vw, vh) {
    const maxX = Math.max(0, vw - w);
    const maxY = Math.max(0, vh - h);
    return {
      left: Math.min(Math.max(0, x), maxX),
      top: Math.min(Math.max(0, y), maxY),
    };
  }
  function centerModalPosition(w, h, vw, vh) {
    return clampModalPosition(Math.round((vw - w) / 2), Math.round((vh - h) / 2), w, h, vw, vh);
  }

  // Active header-drag gesture. Kept at module scope so a single window-blur
  // listener in init() can end a drag that left the window (same robustness as
  // the graph pointer handling).
  var modalDrag = null;
  var modalRestoreFocus = null;
  // One-time success notice for the next render of the same output dialog.
  // Confirming an already OWNED address re-renders an unchanged state, which
  // must not look like a dead click (BUG-UI-1).
  var ownershipNotice = null;
  function endModalDrag(e) {
    if (!modalDrag) return;
    if (e && modalDrag.pid != null && modalDrag.header.hasPointerCapture(modalDrag.pid)) {
      try { modalDrag.header.releasePointerCapture(modalDrag.pid); } catch (_) {}
    }
    modalDrag.header.classList.remove("dragging");
    modalDrag = null;
  }

  function wireModal(modalEl) {
    modalEl.querySelector(".modal-close").addEventListener("click", closeModal);
    modalEl.setAttribute('role', 'dialog');
    modalEl.setAttribute('aria-modal', 'true');
    modalEl.setAttribute('aria-label', modalEl.querySelector('h2').textContent);
    modalEl.querySelector('.modal-close').focus();
    const rect = modalEl.getBoundingClientRect();
    const pos = centerModalPosition(rect.width, rect.height, window.innerWidth, window.innerHeight);
    modalEl.style.left = pos.left + "px";
    modalEl.style.top = pos.top + "px";

    const header = modalEl.querySelector(".modal-header");
    if (!header) return;

    header.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;
      if (e.target.closest && e.target.closest(".modal-close")) return; // close is not a drag handle
      const r = modalEl.getBoundingClientRect();
      modalDrag = {
        pid: e.pointerId, header: header,
        startX: e.clientX, startY: e.clientY,
        baseLeft: r.left, baseTop: r.top,
      };
      header.classList.add("dragging");
      header.setPointerCapture(e.pointerId);
    });
    header.addEventListener("pointermove", function (e) {
      if (!modalDrag || e.pointerId !== modalDrag.pid) return;
      const pos = clampModalPosition(
        modalDrag.baseLeft + (e.clientX - modalDrag.startX),
        modalDrag.baseTop + (e.clientY - modalDrag.startY),
        modalEl.offsetWidth, modalEl.offsetHeight,
        window.innerWidth, window.innerHeight
      );
      modalEl.style.left = pos.left + "px";
      modalEl.style.top = pos.top + "px";
    });
    header.addEventListener("pointerup", endModalDrag);
    header.addEventListener("pointercancel", endModalDrag);
    header.addEventListener("lostpointercapture", endModalDrag);
  }

  function modal(title, bodyHtml) {
    const backdrop = document.getElementById("modal-backdrop");
    if (backdrop.classList.contains('hidden')) modalRestoreFocus = document.activeElement;
    const modalEl = document.getElementById("modal");
    modalEl.classList.remove("wide");
    modalEl.innerHTML = modalShell(title, bodyHtml);
    backdrop.classList.remove("hidden");
    wireModal(modalEl);
  }

  function closeModal() {
    endModalDrag(null);
    ownershipNotice = null;
    document.getElementById("modal-backdrop").classList.add("hidden");
    if (modalRestoreFocus && modalRestoreFocus.isConnected) modalRestoreFocus.focus();
    modalRestoreFocus = null;
  }

  function openTransaction(txid, full, ctx) {
    if (full) {
      renderTransactionModal(full, ctx || null);
      return;
    }
    modal("Transaction", '<div class="loading"><span class="spinner"></span>Loading…</div>');
    API.transaction(txid).then(function (tx) {
      renderTransactionModal(tx, null);
    }).catch(function (err) {
      modal("Transaction", '<div class="alert error">' + Fmt.esc(err.message) + "</div>");
    });
  }

  // Opens through modal() so the graph path (trace-payload transaction, no
  // loading step) also shows the backdrop and restores focus (FIX-UI-3).
  function renderTransactionModal(tx, ctx) {
    modal(
      "Transaction",
      '<div class="kv mono" style="font-size:12.5px">' +
      '  <div class="k">TXID</div><div class="v">' + Fmt.esc(tx.txid) + "</div>" +
      '  <div class="k">Block height</div><div class="v">' + (tx.block_height != null ? tx.block_height : "—") + "</div>" +
      '  <div class="k">Block hash</div><div class="v">' + Fmt.esc(tx.block_hash || "—") + "</div>" +
      '  <div class="k">Timestamp</div><div class="v">' + Fmt.date(tx.block_time) + "</div>" +
      '  <div class="k">Confirmations</div><div class="v">' + tx.confirmations + "</div>" +
      '  <div class="k">Input count</div><div class="v">' + (tx.inputs || []).length + "</div>" +
      '  <div class="k">Output count</div><div class="v">' + (tx.outputs || []).length + "</div>" +
      '  <div class="k">Input total</div><div class="v">' + (tx.input_total_sats != null ? Fmt.sats(tx.input_total_sats) + " sats" : "n/a") + "</div>" +
      '  <div class="k">Output total</div><div class="v">' + (tx.output_total_sats != null ? Fmt.sats(tx.output_total_sats) + " sats" : "n/a") + "</div>" +
      '  <div class="k">Fee</div><div class="v">' + (tx.fee_sats != null ? Fmt.sats(tx.fee_sats) + " sats" : "n/a") + "</div>" +
      '  <div class="k">VSIZE</div><div class="v">' + (tx.vsize != null ? tx.vsize : "n/a") + "</div>" +
      '  <div class="k">Fee rate</div><div class="v">' + (tx.fee_rate != null ? tx.fee_rate + " sat/vB" : "n/a") + "</div>" +
      '  <div class="k">Topology</div><div class="v">' + Fmt.esc(tx.transaction_topology || "UNKNOWN") + (tx.likely_batch ? " · <span style=\"color:#a371f7\">likely batch</span>" : "") + "</div>" +
      "</div>" +
      (ctx ? mergeContextHtml(tx, ctx) : '<h3>Inputs</h3><div class="io-list">' + (tx.inputs || []).map(inputRow).join("") + "</div>") +
      '<h3>Outputs</h3><div class="io-list">' +
      (tx.outputs || []).map(outputRowFull).join("") +
      "</div>"
    );
    wireModalRowClicks(document.getElementById("modal"), ctx);
  }

  function inputRow(i) {
    if (i.txid === "") {
      return '<div class="io-row"><div class="idx">coinbase</div><div class="hint">coinbase input</div><div class="amt">—</div></div>';
    }
    return '<div class="io-row clickable" data-open-outpoint data-otxid="' + Fmt.esc(i.txid) + '" data-ovout="' + i.vout + '">' +
      '<div class="idx">' + Fmt.esc(Fmt.shortTxid(i.txid, 10)) + ":" + i.vout + '</div>' +
      '<div class="hint">' + Fmt.esc(Fmt.shortAddress(i.address)) + "</div>" +
      '<div class="amt">' + (i.value_sats != null ? Fmt.sats(i.value_sats) + " sats" : "n/a") + "</div></div>";
  }

  // Merge / consolidation input context
  // (MERGE_INPUT_CONTEXT_AND_GRAPH_RENDERER_FIX_REQUIREMENTS.md Part A).
  // Rendered only when the transaction came from the trace payload (so the
  // role/related-trace fields are present): groups inputs into LINEAGE /
  // KNOWN_TRACE / CO buckets, shows the referenced-output ownership state,
  // the exact UTXOs consumed, and links to other stored traces that contain
  // the exact same outpoint. None of this alters edges or provenance - a
  // co-input is shown as transaction context, never as an ancestor.
  function mergeContextHtml(tx, ctx) {
    const inputs = tx.inputs || [];
    const lineage = [];
    const known = [];
    const co = [];
    inputs.forEach(function (i) {
      if (i.trace_role === "LINEAGE_INPUT") lineage.push(i);
      else if (i.trace_role === "KNOWN_TRACE_INPUT") known.push(i);
      else co.push(i);
    });
    const currentTraceId = ctx && ctx.traceId ? ctx.traceId : 0;
    let html =
      '<h3>Inputs · merge context</h3>' +
      '<p class="hint">Every input UTXO consumed together with the tracked lineage input(s). ' +
      "Co-inputs are transaction context only - they never become ancestors of this Acquisition trace, " +
      "and no sat-level attribution is implied. Click an input to inspect its referenced output.</p>" +
      mergeInputGroup("Lineage input", "the tracked outpoint through which this trace reached this transaction", lineage, currentTraceId) +
      mergeInputGroup("Known-trace co-inputs", "exact UTXOs already present in another stored trace", known, currentTraceId) +
      mergeInputGroup("Co-inputs", "additional inputs of this transaction (context only)", co, currentTraceId) +
      '<div class="merge-totals kv mono">' +
      '  <div class="k">Inputs consumed</div><div class="v">' + inputs.length + "</div>" +
      '  <div class="k">Tracked lineage inputs</div><div class="v">' + lineage.length + "</div>" +
      '  <div class="k">Input total</div><div class="v">' + (tx.input_total_sats != null ? Fmt.sats(tx.input_total_sats) + " sats" : "n/a") + "</div>" +
      '  <div class="k">Output total</div><div class="v">' + (tx.output_total_sats != null ? Fmt.sats(tx.output_total_sats) + " sats" : "n/a") + "</div>" +
      '  <div class="k">Fee</div><div class="v">' + (tx.fee_sats != null ? Fmt.sats(tx.fee_sats) + " sats" : "n/a") + "</div>" +
      "</div>";
    return html;
  }

  function mergeInputGroup(title, subtitle, items, currentTraceId) {
    if (!items.length) return "";
    let group =
      '<div class="merge-group">' +
      '  <div class="merge-group-title">' + Fmt.esc(title) + ' <span class="hint">· ' + Fmt.esc(subtitle) + "</span></div>" +
      '  <div class="io-list">';
    items.forEach(function (i) {
      let links = "";
      (i.related_trace_ids || []).forEach(function (tid) {
        if (tid !== currentTraceId) {
          links += '<span class="trace-chip" data-open-trace="1" data-tid="' + tid + '" title="Open trace #' + tid + '">trace #' + tid + "</span>";
        }
      });
      if (!links) links = '<span class="hint">—</span>';
      const own = i.ownership_status && i.ownership_status !== "UNKNOWN"
        ? '<span class="role-badge ' + (i.ownership_status === "OWNED" ? "own" : "other") + '">' + Fmt.esc(i.ownership_status) + (i.wallet_name ? " · " + Fmt.esc(i.wallet_name) : "") + "</span>"
        : "";
      group +=
        '<div class="io-row clickable" data-open-outpoint data-otxid="' + Fmt.esc(i.txid) + '" data-ovout="' + i.vout + '">' +
        '  <div class="idx">' + Fmt.esc(Fmt.shortTxid(i.txid, 10)) + ":" + i.vout + "</div>" +
        '  <div class="hint">' + Fmt.esc(Fmt.shortAddress(i.address)) + "</div>" +
        '  <div class="amt">' + (i.value_sats != null ? Fmt.sats(i.value_sats) + " sats" : "n/a") + "</div>" +
        "  <div>" + own + "</div>" +
        '  <div class="hint">' + links + "</div>" +
        "</div>";
    });
    return group + "</div></div>";
  }

  function wireModalRowClicks(modalEl, ctx) {
    modalEl.querySelectorAll("[data-open-outpoint]").forEach(function (tr) {
      tr.addEventListener("click", function () {
        openOutpoint(tr.dataset.otxid, Number(tr.dataset.ovout), ctx || null);
      });
    });
    modalEl.querySelectorAll("[data-open-trace]").forEach(function (el) {
      el.addEventListener("click", function (ev) {
        ev.stopPropagation();
        openTrace(Number(el.dataset.tid));
      });
    });
    modalEl.querySelectorAll("[data-open-spend]").forEach(function (el) {
      el.addEventListener("click", function (ev) {
        ev.stopPropagation();
        openTransaction(el.dataset.stxid);
      });
    });
    modalEl.querySelectorAll("[data-copy]").forEach(function (el) {
      el.addEventListener("click", function (ev) {
        ev.stopPropagation();
        try { navigator.clipboard.writeText(el.dataset.copy); } catch (e) { /* clipboard unavailable */ }
      });
    });
    modalEl.querySelectorAll(".more-traces").forEach(function (el) {
      el.addEventListener("click", function (ev) { ev.stopPropagation(); });
    });
  }

  function outputRowFull(o) {
    const st = o.spend_status === "UNSPENT" ? '<span style="color:#3fb950">unspent</span>'
      : o.spend_status === "SPENT" ? '<span style="color:#8b949e">spent</span>'
      : '<span style="color:#d29922">unknown</span>';
    return '<div class="io-row out clickable" data-open-outpoint data-otxid="' + Fmt.esc(o.txid) + '" data-ovout="' + o.vout + '">' +
      '<div class="idx">' + o.vout + "</div>" +
      '<div class="hint">' + Fmt.esc(Fmt.shortAddress(o.address)) + "</div>" +
      '<div class="amt">' + Fmt.sats(o.value_sats) + " sats</div>" +
      "<div>" + st + " · " + (o.ownership_status || "UNKNOWN") + "</div>" +
      '<div class="hint">' + (o.provenance_state || "UNKNOWN") + (o.likely_batch ? " · batch" : "") + "</div></div>";
  }

  function openOutpoint(txid, vout, ctx) {
    modal("Output / UTXO", '<div class="loading"><span class="spinner"></span>Loading…</div>');
    // P4-N1: inside a Blockchain Analysis the dialog shows the same
    // ownership-neutral provenance as the trace table and export.
    const mode = ctx && ctx.full && ctx.full.trace ? ctx.full.trace.trace_mode : null;
    const analysis = mode === "BLOCKCHAIN_ANALYSIS" || mode === "BLOCKCHAIN_ANALYSIS_REVERSE";
    (analysis ? API.transaction(txid, { provenance: "neutral" }) : API.transaction(txid)).then(function (tx) {
      const out = (tx.outputs || []).find(function (o) { return o.vout === vout; });
      if (!out) {
        document.getElementById("modal").innerHTML =
          modalShell("Output / UTXO", '<div class="alert error">Output not found on node.</div>');
        wireModal(document.getElementById("modal"));
        return;
      }
      // When this outpoint was spent, load the spending transaction so the
      // modal can surface the N-input merge context inline (the user never has
      // to hunt for a tiny transaction node in the graph to see where their
      // sats rode into a consolidation/batch). Prefer the trace payload copy,
      // which carries the per-input roles; fall back to a fresh node fetch.
      let spendPromise = null;
      if (out.spending_txid) {
        let found = null;
        if (ctx && ctx.full && ctx.full.transactions) {
          ctx.full.transactions.forEach(function (t) {
            if (t.txid === out.spending_txid) found = t;
          });
        }
        spendPromise = found ? Promise.resolve(found) : API.transaction(out.spending_txid);
      }
      const done = function (spendTx, spendError) {
        renderOutpointModal(tx, out, spendTx, ctx, txid, vout, spendError);
      };
      if (spendPromise) {
        // B5-04: a failed load is reported in the dialog, not dropped silently.
        spendPromise.then(function (spendTx) { done(spendTx); }).catch(function (err) { done(null, err || {}); });
      } else {
        done(null);
      }
    }).catch(function (err) {
      modal("Output / UTXO", '<div class="alert error">' + Fmt.esc(err.message) + "</div>");
    });
  }

  // "Spending transaction" block shown when a spent outpoint was consumed
  // together with N>=2 inputs (a merge/consolidation/batch), so the UTXO
  // detail flow exposes every exact input UTXO, its sats/address/ownership and
  // its role (LINEAGE_INPUT / CO_INPUT / KNOWN_TRACE_INPUT) without opening
  // the graph node.
  function copyBtn(value, what) {
    return value ? ' <button type="button" class="copy-btn" data-copy="' + Fmt.esc(value) + '" title="Copy ' + what + '" aria-label="Copy ' + what + '">⧉</button>' : "";
  }

  function mergeTraceCell(ids) {
    const parts = Fmt.splitTraceChips(ids, 3);
    function chip(tid) {
      return '<span class="trace-chip" data-open-trace="1" data-tid="' + tid + '" title="Open trace #' + tid + '">trace #' + tid + "</span>";
    }
    if (!ids || !ids.length) return '<span class="hint">—</span>';
    let html = parts.visible.map(chip).join("");
    if (parts.hidden.length) {
      html += '<details class="more-traces"><summary class="trace-chip more" title="Show all ' + ids.length + ' traces">+' + parts.hidden.length + "</summary>" +
        parts.hidden.map(chip).join("") + "</details>";
    }
    return html;
  }

  function spendMergeContextHtml(out, spendTx, ctx) {
    if (!spendTx || !spendTx.inputs || !spendTx.outputs) return "";
    const inputs = spendTx.inputs || [];
    const outputs = spendTx.outputs || [];
    if (inputs.length < 2) return "";
    const currentKey = out.txid + ":" + out.vout;
    const rows = Fmt.mergeInputRows(inputs, currentKey).map(function (r) {
      const i = r.input;
      const role = i.trace_role; // enriched by the trace payload when available
      const roleCls = r.isThis ? "lineage" : role === "KNOWN_TRACE_INPUT" ? "other" : "";
      const op = (i.txid || "") + ":" + (i.vout !== undefined ? i.vout : "");
      const own = Fmt.mergeOwnershipText(i.ownership_status, i.wallet_name);
      return '<div class="io-row mi-row' + (r.isThis ? " current" : "") + (i.txid ? " clickable" : "") + '"' +
        (i.txid ? ' data-open-outpoint data-otxid="' + Fmt.esc(i.txid) + '" data-ovout="' + i.vout + '"' : "") + ">" +
        '  <div class="mi-vin" data-label="vin" title="Input order in the transaction (vin index)">' + r._vin + "</div>" +
        '  <div class="mi-role" data-label="Role"><span class="role-badge ' + roleCls + '"' + (role ? ' title="' + Fmt.esc(role) + '"' : "") + ">" +
        Fmt.esc(Fmt.mergeRoleLabel(role, r.isThis)) + "</span></div>" +
        '  <div class="mi-op mono" data-label="Outpoint" title="' + Fmt.esc(op) + '">' + Fmt.esc(Fmt.shortTxid(i.txid || "", 10)) + ":" + i.vout + copyBtn(op, "outpoint") + "</div>" +
        '  <div class="mi-addr mono" data-label="Address" title="' + Fmt.esc(i.address || "") + '">' + Fmt.esc(Fmt.shortAddress(i.address)) + copyBtn(i.address, "address") + "</div>" +
        '  <div class="mi-amt mono" data-label="Amount">' + (i.value_sats != null ? Fmt.sats(i.value_sats) + " sats" : "n/a") + "</div>" +
        '  <div class="mi-own" data-label="Ownership">' + Fmt.esc(own) + "</div>" +
        '  <div class="mi-traces" data-label="Traces">' + mergeTraceCell(i.related_trace_ids) + "</div>" +
        "</div>";
    }).join("");
    const bal = Fmt.mergeBalance(inputs, outputs);
    function amt(v) { return v === null ? "n/a" : Fmt.sats(v) + " sats"; }
    const outLines = outputs.map(function (o) {
      return "<li>#" + o.vout + " · " + (o.value_sats != null ? Fmt.sats(o.value_sats) + " sats" : "n/a") +
        " · " + Fmt.esc(Fmt.mergeOwnershipText(o.ownership_status, o.wallet_name)) + "</li>";
    }).join("");
    return '<div class="custody-panel merge-spend">' +
      "<h3>Spent in merge transaction</h3>" +
      '<div class="cust-state"><span class="state">' + Fmt.plural(inputs.length, "input") + " → " + Fmt.plural(outputs.length, "output") + " </span>" +
      ' <span class="hint">This UTXO (' + Fmt.esc(Fmt.shortTxid(out.txid, 10)) + ":" + out.vout + ") was spent together with " + Fmt.plural(inputs.length - 1, "other input") + '. Co-inputs are transaction context only – they are never ancestors of this trace, and no sat-level attribution is implied.</span></div>' +
      '<div class="io-list mi-table">' +
      '<div class="io-row mi-head"><div title="vin index">vin</div><div>Role</div><div>Outpoint</div><div>Address</div><div class="mi-amt">Amount</div><div>Ownership</div><div>Traces</div></div>' +
      rows + "</div>" +
      '<div class="mi-balance"><div><span class="hint">Inputs total</span> <span class="mono">' + amt(bal.inputs) + "</span></div>" +
      '<div><span class="hint">Outputs total</span> <span class="mono">' + amt(bal.outputs) + "</span></div>" +
      '<div><span class="hint">Fee (inputs − outputs)</span> <span class="mono">' + amt(bal.fee) + "</span></div>" +
      '<ul class="mi-outputs">' + outLines + "</ul></div>" +
      '<div style="margin-top:10px"><button class="btn small" data-open-spend="1" data-stxid="' + Fmt.esc(out.spending_txid) + '">Open spending transaction ' + Fmt.esc(Fmt.shortTxid(out.spending_txid, 8)) + "</button></div>" +
      "</div>";
  }

  // B5-04: the spending transaction or the spender lookup was unavailable
  // (e.g. the node is not reachable); says why and offers a retry.
  function spendUnavailableHtml(out, spendError) {
    let text = "";
    if (spendError) {
      const reason = (spendError.data && spendError.data.reason) || spendError.message || "unknown error";
      text = "Spending transaction could not be loaded (" + reason + ").";
    } else if (out.spend_status === "SPENT" && !out.spending_txid && out.spending_lookup_error) {
      text = "Spending transaction could not be looked up (" + out.spending_lookup_error + ").";
    }
    if (!text) return "";
    return '<div class="alert warn" id="outpoint-spend-unavailable" aria-live="polite">' + Fmt.esc(text) +
      ' <button type="button" class="btn small" data-spend-retry>Retry</button></div>';
  }

  function renderOutpointModal(tx, out, spendTx, ctx, txid, vout, spendError) {
    const creation = Fmt.date(out.block_time);
    const spendState = out.spend_status === "UNSPENT"
      ? '<span class="status-tag COMPLETE">UNSPENT</span>'
      : out.spend_status === "SPENT"
        ? '<span class="status-tag IN_PROGRESS">SPENT</span>'
        : '<span class="status-tag NEEDS_CLASSIFICATION">UNKNOWN</span>';
    const modalEl = document.getElementById("modal");
    const notice = ownershipNotice && ownershipNotice.key === out.txid + ":" + out.vout ? ownershipNotice.text : "";
    ownershipNotice = null;
    modalEl.classList.toggle("wide", spendMergeContextHtml(out, spendTx, ctx) !== "");
    modalEl.innerHTML = modalShell(
      "Output / UTXO",
      '<p><button class="btn primary" id="outpoint-start-trace">Start trace from here</button></p>' +
      '<div class="kv mono" style="font-size:12.5px">' +
      '  <div class="k">TXID</div><div class="v">' + Fmt.esc(out.txid) + "</div>" +
      '  <div class="k">VOUT</div><div class="v">' + out.vout + "</div>" +
      '  <div class="k">Outpoint</div><div class="v">' + Fmt.esc(out.outpoint) + "</div>" +
      '  <div class="k">Address</div><div class="v">' + Fmt.esc(out.address || "—") + "</div>" +
      '  <div class="k">Amount (BTC)</div><div class="v">' + Fmt.btc(out.value_sats) + "</div>" +
      '  <div class="k">Amount (sats)</div><div class="v">' + Fmt.sats(out.value_sats) + "</div>" +
      '  <div class="k">Script type</div><div class="v">' + Fmt.esc(out.script_type || "—") + "</div>" +
      '  <div class="k">ScriptPubKey</div><div class="v">' + Fmt.esc(out.script_pubkey || "—") + "</div>" +
      '  <div class="k">Creation block</div><div class="v">' + (out.block_height != null ? out.block_height : "—") + "</div>" +
      '  <div class="k">Creation time</div><div class="v">' + creation + "</div>" +
      '  <div class="k">Status</div><div class="v">' + spendState + (out.spending_txid ? ' · spent by <span class="clickable" style="cursor:pointer;color:var(--accent)" data-open-spend="1" data-stxid="' + Fmt.esc(out.spending_txid) + '" title="Open spending transaction">' + Fmt.esc(Fmt.shortTxid(out.spending_txid, 10)) + "</span>" : out.spend_status === "SPENT" ? (out.spending_lookup_error ? " · spent by: not checked" : " · spent by: unknown") : "") + "</div>" +
      '  <div class="k">Ownership</div><div class="v">' + (out.ownership_status || "UNKNOWN") + (out.classification_source && out.classification_source !== "UNKNOWN" ? " (" + Fmt.esc(Fmt.sourceLabel(out.classification_source)) + ")" : "") + "</div>" +
      '  <div class="k">Wallet</div><div class="v">' + Fmt.esc(out.wallet_name || "—") + "</div>" +
      '  <div class="k">Provenance</div><div class="v">' + Fmt.esc(out.provenance_state || "UNKNOWN") + "</div>" +
      '  <div class="k">Provenance reasons</div><div class="v">' + Fmt.esc((out.provenance_reasons || []).join(" · ") || "—") + "</div>" +
      '  <div class="k">Topology</div><div class="v">' + Fmt.esc(out.transaction_topology || "UNKNOWN") + (out.likely_batch ? " · <span style=\"color:#a371f7\">likely batch</span>" : "") + "</div>" +
      '  <div class="k">Annotation</div><div class="v">' + Fmt.esc(out.user_annotation || "—") + "</div>" +
      "</div>" +
      spendUnavailableHtml(out, spendError) +
      spendMergeContextHtml(out, spendTx, ctx) +
      analysisFollowSectionHtml(ctx, out) +
      ownershipSectionHtml(ctx, out, notice) +
      custodySectionHtml(ctx, out) +
      '<div class="panel" id="outpoint-origin-resolution" aria-live="polite">Checking exact origin resolution…</div>' +
      '<div class="panel" id="outpoint-acquisition-provenance"></div>' +
      '<details class="panel" id="mark-acq-panel"><summary>Mark as Acquisition</summary>' +
      '<p class="hint">Attach user-supplied provenance to this exact output. This does not establish ownership or prove an acquisition. ' +
      'The amount, address and block facts are verified with your local node.</p>' +
      '<div class="acq-grid">' +
      '<label>Acquisition date<input type="date" id="mark-acq-date"></label>' +
      '<label>Source<input type="text" id="mark-acq-source"></label>' +
      '<label>Reference<input type="text" id="mark-acq-reference"></label>' +
      '<label>Notes<textarea id="mark-acq-notes"></textarea></label></div>' +
      '<p class="hint">All metadata is optional. Acquisition date is independent of block time.</p>' +
      '<button class="btn primary" id="mark-acq-save">Save Acquisition</button>' +
      '<div id="mark-acq-feedback" aria-live="polite"></div></details>' +
      '<div class="annotations meta">' +
      '  <h3>Annotation</h3>' +
      '  <p class="hint">Descriptive metadata only - it never changes ownership, custody or provenance. Use the OWNERSHIP / CUSTODY panel above to reclassify an output.</p>' +
      '  <div class="tag-list">' + ["INITIAL_ACQUISITION", "WALLET_MIGRATION", "CONSOLIDATION", "UTXO_SPLIT", "CHANGE", "PAYMENT", "SELF_TRANSFER", "UNKNOWN"].map(function (t) {
        return '<span class="tag-chip" data-a-tag="' + t + '">' + t + "</span>";
      }).join("") + "</div>" +
      '  <textarea id="anno-note-modal" placeholder="Free-text note"></textarea>' +
      '  <div style="margin-top:8px"><button class="btn small" id="btn-anno-modal">Save annotation</button></div>' +
      "</div>"
    );
    wireModal(modalEl);
    // SEC-19: the trace ID is set as text, never as markup.
    modalEl.querySelectorAll('[data-cust-trace-id]').forEach(function (n) { n.textContent = '#' + ctx.traceId; });
    const spendRetry = modalEl.querySelector('[data-spend-retry]');
    if (spendRetry) spendRetry.addEventListener('click', function () { openOutpoint(txid, vout, ctx); });
    modalEl.querySelector('#outpoint-start-trace').addEventListener('click', function () {
      closeModal();
      location.hash = '#start?txid=' + encodeURIComponent(txid) + '&vout=' + encodeURIComponent(vout);
    });
    wireModalRowClicks(modalEl, ctx);
    let acquisitionProvenanceRequest = 0;
    function refreshAcquisitionProvenance() {
      const request = ++acquisitionProvenanceRequest;
      const target = modalEl.querySelector('#outpoint-acquisition-provenance');
      target.innerHTML = '<details><summary>Legacy Acquisition path analysis</summary>' +
        '<p class="hint">Optional full-ancestry diagnostic. Origin review above remains the current review result. This separate analysis can reach its execution limits.</p>' +
        '<button class="btn small" data-load-legacy-provenance>Load legacy analysis</button>' +
        '<div data-legacy-result aria-live="polite"></div></details>';
      const result = target.querySelector('[data-legacy-result]');
      const button = target.querySelector('[data-load-legacy-provenance]');
      button.addEventListener('click', function () {
      button.disabled = true;
      result.textContent = 'Loading legacy analysis…';
      API.outpointProvenance(out.txid, out.vout).then(function (rows) {
        if (!target.isConnected || request !== acquisitionProvenanceRequest) return;
        result.innerHTML = rows.length ? Fmt.acquisitionBadge(rows[0].acquisition_provenance) +
          '<p class="hint">Derived Acquisition ancestry for this managed output; independent of ownership and topology. ' +
          'PARTIAL does not assign a sat-level fraction. Adding an anchor recomputes this state.</p>' :
          '<p class="hint">Acquisition provenance badges apply to evidence-backed managed outputs before an Exit boundary.</p>';
      }).catch(function () {
        if (target.isConnected && request === acquisitionProvenanceRequest)
          result.textContent = 'Legacy analysis could not be completed: execution limit or unavailable evidence. Origin review above is independent. You can retry this analysis.';
      }).finally(function () {
        if (target.isConnected && request === acquisitionProvenanceRequest) button.disabled = false;
      });
      });
    }
    refreshAcquisitionProvenance();
    let originRequest = 0;
    function refreshOriginResolution(attempt) {
      attempt = attempt || 1;
      const request = attempt === 1 ? ++originRequest : originRequest;
      const target = modalEl.querySelector('#outpoint-origin-resolution');
      API.originDetail(out.txid, out.vout).then(function (detail) {
        if (!target.isConnected || request !== originRequest) return;
        target.innerHTML = originResolutionHtml(detail);
        if (window.ReportsPage) window.ReportsPage.mountLineage(target.querySelector('[data-lineage-review]'), detail, function (txid, vout) { openOutpoint(txid, vout, ctx); });
        modalEl.querySelector('#mark-acq-panel').hidden = detail.origin_resolution.type !== 'UNRESOLVED';
        const comment = target.querySelector('[data-origin-comment]');
        if (comment) comment.value = detail.origin_resolution.comment || '';
        const sourceBox = target.querySelector('[data-anchor-source]');
        if (sourceBox) mountAnchorSource(sourceBox);
        target.querySelectorAll('[data-origin-action]').forEach(function (button) {
          button.addEventListener('click', function () {
            button.disabled = true;
            const action = button.dataset.originAction;
            if (action === 'view-acquisition') {
              API.getAcquisition(detail.origin_resolution.acquisition_id).then(function (a) {
                if (!target.isConnected) return;
                modal('Acquisition #' + a.id, '<p>Original outpoint: ' + Fmt.esc(a.origin_txid + ':' + a.origin_vout) + '</p>' +
                  '<p>Source: ' + Fmt.esc(a.source || '—') + ' · Date: ' + Fmt.esc(a.acquisition_date || '—') + '</p>' +
                  '<p>Original amount: ' + Fmt.btc(a.origin_amount_sats) + ' BTC</p><p>Reference: ' + Fmt.esc(a.reference || '—') +
                  '</p><p class="prov-note">Notes: ' + Fmt.esc(a.notes || '—') + '</p>');
              }).catch(function (err) { button.disabled = false; target.querySelector('[data-origin-feedback]').textContent = err.message; });
              return;
            }
            const operation = action === 'remove' ? API.removeOriginResolution(out.txid, out.vout) :
              API.resolveOrigin(out.txid, out.vout, comment.value || null);
            operation.then(function () {
              if (target.isConnected) refreshOriginResolution();
              if (window.InventoryPage) window.InventoryPage.reload();
              if (window.ProvenancePage) window.ProvenancePage.reload();
              if (window.ReportsPage) window.ReportsPage.reload();
            }).catch(function (err) {
              if (target.isConnected) { button.disabled = false; target.querySelector('[data-origin-feedback]').textContent = err.message; }
            });
          });
        });
      }).catch(function (err) {
        if (!target.isConnected || request !== originRequest) return;
        // FIX-OWN-1: show the reason of a 503 and retry it a few times on its own.
        const reason = err && ((err.data && err.data.reason) || err.message);
        if (err && err.status === 503 && attempt < 3) {
          target.textContent = 'Origin resolution temporarily unavailable (' + reason + '). Retrying automatically…';
          setTimeout(function () {
            if (target.isConnected && request === originRequest) refreshOriginResolution(attempt + 1);
          }, ((err.data && err.data.retry_after) || 2) * 1000);
          return;
        }
        target.innerHTML = '<p>Origin resolution unavailable' + (reason ? ' (' + Fmt.esc(reason) + ')' : '') + '.</p>' +
          '<button class="btn small" data-origin-retry>Retry</button>';
        target.querySelector('[data-origin-retry]').addEventListener('click', function () { refreshOriginResolution(); });
      });
    }
    // B6: the user-provided Source of a No-Acquisition anchor uses the existing
    // annotation GET/PATCH API. The resolution comment stays a separate field.
    function mountAnchorSource(box) {
      API.provenanceAnchor(out.txid, out.vout).then(function (anchor) {
        if (!box.isConnected) return;
        box.innerHTML = '<label>Source (user-provided, optional)<input type="text" data-anchor-source-input maxlength="200"></label> ' +
          '<button class="btn small" data-anchor-source-save>Save source</button>' +
          '<p data-anchor-source-feedback aria-live="polite"></p>';
        const input = box.querySelector('[data-anchor-source-input]');
        const feedback = box.querySelector('[data-anchor-source-feedback]');
        input.value = anchor.source || '';
        const button = box.querySelector('[data-anchor-source-save]');
        button.addEventListener('click', function () {
          button.disabled = true;
          feedback.textContent = 'Saving source…';
          API.provenanceAnchorEdit(out.txid, out.vout, { revision: anchor.revision, source: input.value.trim() || null }).then(function (saved) {
            if (!box.isConnected) return;
            anchor = saved;
            input.value = saved.source || '';
            button.disabled = false;
            feedback.textContent = 'Source saved.';
            if (window.ProvenancePage) window.ProvenancePage.reload();
            if (window.ReportsPage) window.ReportsPage.reload();
          }).catch(function (err) {
            if (!box.isConnected) return;
            button.disabled = false;
            feedback.textContent = 'Source not saved: ' + err.message;
          });
        });
      }).catch(function (err) {
        if (box.isConnected) box.textContent = 'Source unavailable: ' + err.message + ' Reopen the output to retry.';
      });
    }
    refreshOriginResolution();
    modalEl.querySelector("#mark-acq-save").addEventListener("click", function () {
      const button = this;
      button.disabled = true;
      const feedback = modalEl.querySelector("#mark-acq-feedback");
      API.createAcquisition({
        txid: out.txid, vout: out.vout,
        acquisition_date: modalEl.querySelector("#mark-acq-date").value || null,
        source: modalEl.querySelector("#mark-acq-source").value.trim() || null,
        reference: modalEl.querySelector("#mark-acq-reference").value.trim() || null,
        notes: modalEl.querySelector("#mark-acq-notes").value.trim() || null,
      }).then(function () {
        feedback.textContent = "Acquisition anchor saved. View it in the Provenance workspace.";
        refreshAcquisitionProvenance();
        refreshOriginResolution();
        if (window.InventoryPage) window.InventoryPage.reload();
        if (window.ProvenancePage) window.ProvenancePage.reload();
        if (window.ReportsPage) window.ReportsPage.reload();
      }).catch(function (err) {
        button.disabled = false;
        feedback.textContent = err.message;
      });
    });
    let aTag = "UNKNOWN";
    modalEl.querySelectorAll(".tag-chip").forEach(function (c) {
      c.addEventListener("click", function () {
        modalEl.querySelectorAll(".tag-chip").forEach(function (x) { x.style.borderColor = ""; x.style.color = ""; });
        aTag = c.dataset.aTag;
        c.style.borderColor = "var(--accent)"; c.style.color = "var(--accent)";
      });
    });
    modalEl.querySelector("#btn-anno-modal").addEventListener("click", function () {
      API.addAnnotation({
        entity_type: "outpoint", entity_id: out.outpoint,
        tag: aTag, note: modalEl.querySelector("#anno-note-modal").value,
      }).then(function () { openOutpoint(txid, vout, ctx); });
    });
    wireCustodyActions(modalEl, ctx, out, txid, vout);
  }

  function originResolutionHtml(detail) {
    const r = detail.origin_resolution;
    let html = '';
    if (r.type === 'ACQUISITION') {
      html = '<h3>Acquisition Origin</h3><p>Acquisition #' + r.acquisition_id + '</p><p>Source: ' + Fmt.esc(r.source || '—') +
        ' · Date: ' + Fmt.esc(r.date || '—') + '</p><p>Original amount: ' + Fmt.btc(r.amount_sats) + ' BTC</p>' +
        '<button class="btn small" data-origin-action="view-acquisition">View Acquisition</button>';
    } else if (r.type === 'NO_ACQUISITION') {
      html = '<h3>No Acquisition · Reviewed</h3><p>No documented Acquisition is required for this origin.</p>';
    } else {
      html = '<h3>' + (detail.can_resolve_no_acquisition ? 'Unresolved Origin' : 'Lineage origins') + '</h3>' +
        '<p>No exact Acquisition anchor or explicit review decision is recorded at this output.</p>';
    }
    if (detail.can_resolve_no_acquisition || r.type === 'NO_ACQUISITION') {
      html += '<p class="hint">This decision applies only to this exact origin. It is not ownership evidence, external classification or a Disposal.</p>' +
        '<label>Comment (optional)<textarea data-origin-comment maxlength="4000"></textarea></label>' +
        '<button class="btn small" data-origin-action="save">' + (r.type === 'NO_ACQUISITION' ? 'Save resolution' : 'Mark as No Acquisition') + '</button> ' +
        (r.type === 'NO_ACQUISITION' ? '<button class="btn small" data-origin-action="remove">Remove resolution</button>' +
          '<div data-anchor-source>Loading source…</div>' : '');
    }
    html += '<div data-lineage-review></div>';
    return html + '<p data-origin-feedback aria-live="polite"></p>';
  }

  function analysisFollowSectionHtml(ctx, out) {
    // Blockchain Analysis action (BLOCKCHAIN_ANALYSIS_MODE_REQUIREMENTS.md):
    // deliberately extend the walk by following the spending transaction of a
    // spent output. Only surfaced for spent outputs that carry one. Following
    // never classifies the output and never changes ownership.
    if (!ctx || !ctx.traceId || !ctx.full) return "";
    const mode = ctx.full.trace.trace_mode;
    // Reverse analysis action (REVERSE_BLOCKCHAIN_ANALYSIS_REQUIREMENTS_v0.6.0
    // .md "Job operations"): deliberately continue the ancestry backward from
    // a discovered ancestor outpoint. Unlike forward analysis there is no
    // spent-output requirement - the reverse walk targets a NEWER outpoint and
    // continues from any old input it already surfaced. Wording is
    // reverse-specific and never says "Follow spending transaction".
    if (mode === "BLOCKCHAIN_ANALYSIS_REVERSE") {
      const inTrace = (ctx.full.outpoints || []).some(function (o) {
        return o.txid === out.txid && o.vout === out.vout;
      });
      if (!inTrace) return "";
      return '<div class="custody-panel analysis">' +
        "<h3>Reverse analysis</h3>" +
        '<div class="cust-state"><span class="state">Ownership: ' + Fmt.esc(out.ownership_status || "UNKNOWN") + '</span></div>' +
        '<p class="hint">Continue the reverse ancestry backward from this input. ' +
        "This is the reverse-specific <b>continue</b> action - it never classifies the output and never implies ownership. " +
        "Use it to push past reverse analysis safety limits deliberately.</p>" +
        '<div class="cust-actions">' +
        '  <button class="btn small btn-own" id="btn-analysis-follow">Continue backward from this input</button>' +
        "</div>" +
        "</div>";
    }
    if (mode !== "BLOCKCHAIN_ANALYSIS") return "";
    if (out.spend_status !== "SPENT" || !out.spending_txid) return "";
    return '<div class="custody-panel analysis">' +
      "<h3>Blockchain analysis</h3>" +
      '<div class="cust-state"><span class="state">Ownership: ' + Fmt.esc(out.ownership_status || "UNKNOWN") + ' · Status: SPENT</span></div>' +
      '<p class="hint">Follow the spending transaction to extend the analysis from this output. ' +
      "This is the analysis-specific <b>follow</b> action - it never classifies the output and never implies ownership. " +
      "Use it to push past analysis safety limits deliberately.</p>" +
      '<div class="cust-actions">' +
      '  <button class="btn small btn-own" id="btn-analysis-follow">Follow spending transaction ' +
        Fmt.esc(Fmt.shortTxid(out.spending_txid, 8)) + "</button>" +
      "</div>" +
      "</div>";
  }

  // Same wording as the B4 getting-started checklist.
  const OWNED_SCOPE_HINT = "Mark as owned only for your own on-chain wallets. Do not mark outputs from a Lightning wallet, " +
    "an exchange or another service as owned; record them where they enter as an acquisition or " +
    "no-acquisition origin.";

  function ownershipSectionHtml(ctx, out, notice) {
    const status = out.ownership_status || "UNKNOWN";
    // A custodial deposit is governed by the OWNERSHIP/CUSTODY panel below
    // (keep boundary / change classification); do not duplicate controls here.
    if (status === "CUSTODIAL_DEPOSIT") return "";
    const src = out.classification_source && out.classification_source !== "UNKNOWN"
      ? out.classification_source
      : "";
    const current = Fmt.esc(status) + (src ? " (" + Fmt.esc(Fmt.sourceLabel(src)) + ")" : "");

    let actions;
    if (status === "UNKNOWN") {
      actions = [
        { act: "owned", label: out.address ? "Mark address as OWNED" : "Mark as OWNED" },
        { act: "external", label: "Mark as EXTERNAL" },
      ];
    } else if (status === "OWNED" || status === "LIKELY_OWNED") {
      actions = [
        ...(out.address ? [{ act: "owned", label: "Confirm address ownership" }] : []),
        { act: "external", label: "Mark as EXTERNAL" },
        { act: "reset", label: "Reset to automatic" },
      ];
    } else if (status === "EXTERNAL") {
      actions = [
        { act: "owned", label: out.address ? "Mark address as OWNED" : "Mark as OWNED" },
        { act: "reset", label: "Reset to automatic" },
      ];
    } else {
      actions = [];
    }

    return '<div class="custody-panel ownership">' +
      "<h3>Ownership</h3>" +
      '<div class="cust-state"><span class="state">Current: ' + current + "</span></div>" +
      (notice ? '<p class="alert ok" role="status">' + Fmt.esc(notice) + "</p>" : "") +
      // FIX-OWN-1: a stored manual decision the address confirmation outranks.
      (out.overridden_classification
        ? '<p class="alert warn" data-own-override>Stored classification: ' + Fmt.esc(out.overridden_classification) +
          " (manual) — saved, but overridden by the address confirmation in Known Addresses.</p>"
        : "") +
      '<div data-own-confirm hidden></div>' +
      '<p class="hint">OWNED also confirms this address in Known Addresses for its other outputs. Existing external or custody decisions remain subject to conflict review. EXTERNAL and reset apply only to this output; reset does not remove a confirmed address. Acquisition review remains separate.</p>' +
      // FIX-OWN-1: when OWNED applies and when not.
      '<p class="hint" data-own-scope-hint>' + OWNED_SCOPE_HINT + "</p>" +
      // REG-1: ownership and key access (custody) are separate choices.
      (out.address && actions.some(function (a) { return a.act === "owned"; })
        ? '<div class="cust-actions"><label class="hint">Key access for this address ' +
          '<select data-own-key-access>' +
          '<option value="key" selected>Key available (counts in holdings)</option>' +
          '<option value="none">No key access (documented only)</option>' +
          "</select></label></div>" +
          '<p class="hint" data-own-key-hint>Without key access the address stays ownership evidence but is not scanned and never counts in holdings; documented outputs on it appear under Not in current wallets. A later OWNED never resets this; change it in Known Addresses.</p>'
        : "") +
      '<div class="cust-actions">' +
      actions.map(function (a) {
        return '<button class="btn small" data-own-act="' + a.act + '">' + a.label + "</button>";
      }).join("") +
      "</div>" +
      // Address evidence is not part of the output payload, so the reset
      // hint is shown with every reset action (REL-0.18.1).
      (actions.some(function (a) { return a.act === "reset"; })
        ? '<p class="hint" data-reset-hint>Resets the classification only. If you confirmed this address, the confirmation stays in Known Addresses and must be removed there.</p>'
        : "") +
      "</div>";
  }

  function custodySectionHtml(ctx, out) {
    if (!ctx || !ctx.traceId || !ctx.full) return "";
    const key = out.txid + ":" + out.vout;
    const sug = (ctx.full.possible_boundaries || []).find(function (s) { return s.txid + ":" + s.vout === key; });
    const dis = (ctx.full.dismissed_candidates || []).find(function (d) { return d.txid + ":" + d.vout === key; });
    const bnd = (ctx.full.custodial_boundaries || []).find(function (b) { return b.txid === out.txid && b.vout === out.vout; });
    const conf = (ctx.full.ownership_custody_conflicts || []).find(
      function (c) { return c.txid === out.txid && c.vout === out.vout && c.status === "PENDING"; });
    const custDeposit = out.ownership_status === "CUSTODIAL_DEPOSIT";

    // An explicit ownership/custody conflict must be surfaced here: the user
    // decides "keep the boundary" or "remove only the custody classification".
    if (conf) {
      return '<div class="custody-panel conflict">' +
        "<h3>Ownership / custody</h3>" +
        '<div class="cust-state"><span class="cust-badge cust-badge-conflict">Ownership / custody conflict</span></div>' +
        "<p class=\"hint\">" + Fmt.esc(conf.detail) + " Nothing is changed automatically.</p>" +
        '<div class="cust-actions">' +
        '  <button class="btn small" id="cust-modal-occ-keep" data-cid="' + conf.id + '">Keep custody boundary</button>' +
        '  <button class="btn small danger" id="cust-modal-occ-remove" data-cid="' + conf.id + '">Remove custody boundary and continue the trace</button>' +
        "</div>" +
        "</div>";
    }

    // Already a custodial deposit -> offer removal + optional reclassification,
    // never "Mark as custodial boundary" again.
    if (custDeposit || bnd) {
      return '<div class="custody-panel marked">' +
        "<h3>Ownership / custody</h3>" +
        '<div class="cust-state"><span class="cust-badge cust-badge-marked">Custodial deposit' + (bnd ? " · boundary" : "") + '</span>' +
        '<span class="hint">Exact lineage ends at this deposit.</span></div>' +
        '<div><button class="btn small danger" id="cust-modal-remove-class">Remove custodial boundary</button>' +
        ' <button class="btn small" id="cust-modal-reclass-toggle">Change ownership classification</button>' +
        "</div>" +
        '<div class="cust-reclass" id="cust-reclass-box" style="display:none">' +
        '  <div class="classify-bar">' +
        '    <button class="btn small reclass" data-reclass="OWNED">Owned</button>' +
        '    <button class="btn small reclass" data-reclass="UNKNOWN">Unknown</button>' +
        '    <button class="btn small reclass" data-reclass="EXTERNAL">External</button>' +
        "  </div>" +
        "</div>" +
        "</div>";
    }
    if (sug) {
      return '<div class="custody-panel suggested">' +
        "<h3>Ownership / custody</h3>" +
        '<div class="cust-state"><span class="cust-badge cust-badge-sug">Possible custody · ' + Fmt.esc(sug.confidence) + "</span></div>" +
        '<p class="hint">Heuristic only — not cryptographic proof. Review the reasons, then accept (exact lineage stops here) or decline.</p>' +
        '<ul class="cust-reasons">' + (sug.reasons || []).map(function (r) { return "<li>" + Fmt.esc(r) + "</li>"; }).join("") + "</ul>" +
        '<div class="cust-actions">' +
        '  <button class="btn small btn-own" id="cust-modal-accept">Accept as boundary</button>' +
        '  <button class="btn small" id="cust-modal-decline">Decline</button>' +
        "</div>" +
        "</div>";
    }
    if (dis) {
      return '<div class="custody-panel declined">' +
        "<h3>Ownership / custody</h3>" +
        '<div class="cust-state"><span class="cust-badge cust-badge-dis">Possible custody · declined</span></div>' +
        '<p class="hint">You declined this suggestion; it will not be offered again until you reset the decision.</p>' +
        '<div><button class="btn small" id="cust-modal-reset">Reset decision</button></div>' +
        "</div>";
    }
    return '<div class="custody-panel">' +
      "<h3>Ownership / custody</h3>" +
      '<p class="hint">Mark this output as a custodial deposit so exact lineage stops here. Applies to trace <span data-cust-trace-id></span>.</p>' +
      '<div><button class="btn small" id="cust-modal-mark">Mark as custodial boundary</button></div>' +
      "</div>";
  }

  function custodyError(modalEl, err) {
    modalEl.querySelectorAll(".custody-panel .btn").forEach(function (b) {
      b.disabled = false;
    });
    const panel = modalEl.querySelector(".custody-panel");
    if (panel) {
      panel.insertAdjacentHTML("beforeend", '<div class="alert error">' + Fmt.esc(err.message) + "</div>");
    } else {
      modalEl.insertAdjacentHTML("beforeend", '<div class="alert error">' + Fmt.esc(err.message) + "</div>");
    }
  }

  function refreshOutpointAfterCustody(ctx, txid, vout) {
    if (window.ReportsPage) window.ReportsPage.reload();
    if (window.InventoryPage) window.InventoryPage.reload();
    if (window.ProvenancePage) window.ProvenancePage.reload();
    API.getTrace(ctx.traceId).then(function (fresh) {
      const next = { traceId: ctx.traceId, full: fresh, onChanged: ctx.onChanged };
      openOutpoint(txid, vout, next);
      if (ctx.onChanged) ctx.onChanged(fresh);
    }).catch(function (err) {
      const modalEl = document.getElementById("modal");
      if (modalEl) custodyError(modalEl, err);
    });
  }

  function wireCustodyActions(modalEl, ctx, out, txid, vout) {

    const doAction = function (p) {
      p.catch(function (err) { custodyError(modalEl, err); });
    };

    const refresh = function () {
      if (ctx && ctx.traceId && ctx.full) { refreshOutpointAfterCustody(ctx, txid, vout); return; }
      if (window.ReportsPage) window.ReportsPage.reload();
      if (window.ProvenancePage) window.ProvenancePage.reload();
      if (window.InventoryPage) window.InventoryPage.reload();
      openOutpoint(txid, vout, null);
    };

    // Outpoint-level OWNERSHIP classification (graph/classify modal section).
    const classify = function (act, removeConfirmation, button) {
      const reset = act === "reset";
      const payload = {
        ownership_status: reset ? "UNKNOWN" : act === "owned" ? "OWNED" : "EXTERNAL",
        classification_source: reset ? "UNKNOWN" : "USER",
        wallet_name: null,
        confidence: null,
        register_owned_address: act === "owned" && !!out.address,
      };
      if (removeConfirmation) payload.remove_address_confirmation = true;
      const keyChoice = modalEl.querySelector("[data-own-key-access]");
      if (payload.register_owned_address && keyChoice && keyChoice.value === "none") payload.key_access = false;
      (ctx && ctx.traceId && ctx.full ? API.classify(ctx.traceId, txid, vout, payload) :
        API.classifyOutpoint(txid, vout, payload)).then(function (result) {
        const effective = result && result.effective_ownership;
        if (payload.register_owned_address) {
          ownershipNotice = { key: out.txid + ":" + out.vout,
            text: payload.key_access === false
              ? "Address ownership confirmed in Known Addresses (USER_CONFIRMED) without key access: documented only, not counted in holdings. This output is OWNED (manual)."
              : "Address ownership confirmed in Known Addresses (USER_CONFIRMED). This output is OWNED (manual)." };
        } else if (removeConfirmation) {
          ownershipNotice = { key: out.txid + ":" + out.vout,
            text: "Address confirmation removed from Known Addresses. This output is " +
              (reset ? "reset to automatic." : "EXTERNAL (manual).") };
        } else if (effective && effective.address_confirmed && effective.ownership_status === "OWNED") {
          // Never a silent no-op: the saved decision is outranked (FIX-OWN-1).
          ownershipNotice = { key: out.txid + ":" + out.vout,
            text: (reset ? "Reset saved" : "EXTERNAL saved") +
              ", but the address confirmation in Known Addresses keeps this output OWNED." };
        }
        refresh();
      }).catch(function (err) {
        if (button) button.disabled = false;
        custodyError(modalEl, err);
      });
    };

    // FIX-OWN-1: EXTERNAL and reset do not change an output whose address is
    // confirmed in Known Addresses. Ask first and offer the combined action.
    const confirmRemoval = function (act) {
      const box = modalEl.querySelector("[data-own-confirm]");
      const label = act === "reset" ? "reset this output to automatic" : "mark this output as EXTERNAL";
      box.innerHTML = '<p class="alert warn">This address is confirmed as your own in Known Addresses. ' +
        "That confirmation outranks this output's classification, and it applies to every output on the address. " +
        "Remove the confirmation and " + label + "?</p>" +
        '<div class="cust-actions">' +
        '<button class="btn small danger" data-own-combined="' + act + '">Remove confirmation and ' + label + "</button>" +
        '<button class="btn small" data-own-registry>Open in Known Addresses</button>' +
        '<button class="btn small" data-own-cancel>Cancel</button></div>';
      box.hidden = false;
      box.querySelector("[data-own-combined]").addEventListener("click", function () {
        this.disabled = true;
        classify(act, true, this);
      });
      box.querySelector("[data-own-registry]").addEventListener("click", function () {
        closeModal();
        if (window.RegistryPage && window.RegistryPage.focusAddress) window.RegistryPage.focusAddress(out.address);
        showView("registry");
      });
      box.querySelector("[data-own-cancel]").addEventListener("click", function () {
        box.hidden = true;
        box.innerHTML = "";
        modalEl.querySelectorAll("[data-own-act]").forEach(function (other) { other.disabled = false; });
      });
    };

    modalEl.querySelectorAll("[data-own-act]").forEach(function (b) {
      b.addEventListener("click", function () {
        b.disabled = true;
        const act = b.dataset.ownAct;
        // A reset falls back to any registry entry; a stored EXTERNAL is only
        // outranked by a confirmation with address evidence (FIX-OWN-1b).
        if ((act === "external" && out.confirmation_outranks_external) || (act === "reset" && out.address_confirmed)) {
          confirmRemoval(act);
          return;
        }
        classify(act, false, null);
      });
    });

    if (!ctx || !ctx.traceId || !ctx.full) return;
    const sug = (ctx.full.possible_boundaries || []).find(function (s) { return s.txid + ":" + s.vout === out.txid + ":" + out.vout; });
    // Blockchain Analysis "follow spending transaction" (analysis only).
    // Never classifies; re-walks with the followed output re-anchored at hop 0.
    const analysisFollow = modalEl.querySelector("#btn-analysis-follow");
    if (analysisFollow) {
      analysisFollow.addEventListener("click", function () {
        analysisFollow.disabled = true;
        doAction(API.follow(ctx.traceId, txid, vout).then(refresh));
      });
    }

    const mark = modalEl.querySelector("#cust-modal-mark");
    if (mark) {
      mark.addEventListener("click", function () {
        mark.disabled = true;
        doAction(API.markCustodialBoundary(ctx.traceId, txid, vout, "", null).then(refresh));
      });
    }
    const accept = modalEl.querySelector("#cust-modal-accept");
    if (accept) {
      accept.addEventListener("click", function () {
        accept.disabled = true;
        doAction(API.markCustodialBoundary(ctx.traceId, txid, vout, "", null).then(refresh));
      });
    }
    const decline = modalEl.querySelector("#cust-modal-decline");
    if (decline) {
      decline.addEventListener("click", function () {
        decline.disabled = true;
        doAction(API.declineCustodialSuggestion(ctx.traceId, txid, vout, {
          reasons: (sug && sug.reasons) || [],
          confidence: (sug && sug.confidence) || "likely",
        }).then(refresh));
      });
    }
    const reset = modalEl.querySelector("#cust-modal-reset");
    if (reset) {
      reset.addEventListener("click", function () {
        reset.disabled = true;
        doAction(API.resetCustodialSuggestion(ctx.traceId, txid, vout).then(refresh));
      });
    }
    // "Remove custodial boundary" - works with or without a boundary record.
    const removeClass = modalEl.querySelector("#cust-modal-remove-class");
    if (removeClass) {
      removeClass.addEventListener("click", function () {
        if (!confirm("Remove the custodial classification? The boundary is deleted, but the UTXO, tx graph and wallet evidence stay; ownership is re-evaluated from the remaining evidence and tracing continues.")) return;
        removeClass.disabled = true;
        doAction(API.deleteOutpointCustody(ctx.traceId, txid, vout).then(refresh));
      });
    }
    // Optional explicit reclassification of the same outpoint.
    const reclassToggle = modalEl.querySelector("#cust-modal-reclass-toggle");
    const reclassBox = modalEl.querySelector("#cust-reclass-box");
    if (reclassToggle && reclassBox) {
      reclassToggle.addEventListener("click", function () {
        reclassBox.style.display = reclassBox.style.display === "none" ? "block" : "none";
      });
    }
    modalEl.querySelectorAll("#cust-reclass-box .reclass").forEach(function (b) {
      b.addEventListener("click", function () {
        b.disabled = true;
        doAction(API.classify(ctx.traceId, txid, vout, {
          ownership_status: b.dataset.reclass,
          classification_source: "USER",
          wallet_name: null,
          confidence: null,
        }).then(refresh));
      });
    });
    // Explicit ownership/custody conflict resolution.
    modalEl.querySelectorAll("#cust-modal-occ-keep, #cust-modal-occ-remove").forEach(function (b) {
      b.addEventListener("click", function () {
        const resolution = b.id === "cust-modal-occ-keep" ? "KEEP_BOUNDARY" : "REMOVE_BOUNDARY";
        if (resolution === "REMOVE_BOUNDARY" &&
            !confirm("Remove only the custody classification? The boundary is deleted, but the UTXO, tx graph and wallet evidence stay; ownership evidence resumes exact tracing.")) return;
        b.disabled = true;
        doAction(API.resolveOwnershipCustodyConflict(
          ctx.traceId, Number(b.dataset.cid), resolution).then(refresh));
      });
    });
  }

  // ------------------------------------------------------------ init

  function init() {
    document.querySelectorAll(".tabs .tab").forEach(function (t) {
      t.addEventListener("click", function () {
        var scope = new URLSearchParams(location.hash.split('?')[1] || '').get('wallet');
        if (reportViews.indexOf(t.dataset.view) >= 0 && scope !== null) {
          location.hash = '#' + t.dataset.view + '?wallet=' + encodeURIComponent(scope);
        } else showView(t.dataset.view);
      });
    });
    document.getElementById("brand-link").addEventListener("click", function () { showView("dashboard"); });
    document.getElementById('central-export').addEventListener('click', function () { window.ReportsPage.exportMenu(); });
    // Close only when the press also started on the backdrop, so a text
    // selection dragged out of the dialog does not dismiss it (R-06).
    var backdropPress = false;
    document.getElementById("modal-backdrop").addEventListener("pointerdown", function (e) {
      backdropPress = e.target.id === "modal-backdrop";
    });
    document.getElementById("modal-backdrop").addEventListener("click", function (e) {
      if (e.target.id === "modal-backdrop" && backdropPress) closeModal();
      backdropPress = false;
    });
    window.addEventListener("blur", function () { endModalDrag(null); });
    document.addEventListener('keydown', function (e) {
      if (document.getElementById('modal-backdrop').classList.contains('hidden')) return;
      if (e.key === 'Escape') { e.preventDefault(); closeModal(); }
      if (e.key === 'Tab') {
        var items = Array.from(document.getElementById('modal').querySelectorAll('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary'))
          .filter(function (el) { return el.getClientRects().length > 0; });
        if (!items.length) return;
        var first = items[0], last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });

    // default view
    function route() {
      var parts = location.hash.slice(1).split('?'), params = new URLSearchParams(parts[1] || '');
      closeModal();
      // SEC-19: only a positive decimal trace ID leaves the hash; anything
      // else reaches the trace view as null and shows the error page.
      showView(parts[0] || 'dashboard', Fmt.traceId(params.get('id')), true);
    }
    window.addEventListener('hashchange', route);
    route();

    // Node pill
    const pill = document.getElementById("node-pill");
    function refreshPill() {
      API.health().then(function (s) {
        const state = nodePillState(s);
        pill.textContent = state.text;
        pill.className = "node-pill " + state.cls;
      }).catch(function () {
        pill.textContent = "Node unreachable";
        pill.className = "node-pill err";
      });
    }
    refreshPill();
    setInterval(refreshPill, 30000);
  }

  // Node pill state from /api/health. The endpoint answers HTTP 200 even when
  // Bitcoin Core is unreachable (reachable:false); that and a network mismatch
  // are errors, not "syncing"/"ok".
  function nodePillState(s) {
    if (!s || s.reachable !== true) return { text: "Node unreachable", cls: "err" };
    const version = s.core_version ? "v" + s.core_version : "?";
    if (s.network_match === false) {
      return { text: "Node: " + version + " · network mismatch (" + (s.network || "?") + ", expected " + (s.expected_network || "?") + ")", cls: "err" };
    }
    return {
      text: "Node: " + version + " · " + (s.block_height != null ? "#" + s.block_height : "?") + " · " + (s.blockchain_synced ? "synced" : "syncing"),
      cls: s.blockchain_synced && s.txospenderindex_synced ? "ok" : "warn",
    };
  }

  return {
    init: init, nodePillState: nodePillState, showView: showView, openTrace: openTrace,
    modal: modal,
    openTransaction: openTransaction, openOutpoint: openOutpoint, closeModal: closeModal,
  };
})();

document.addEventListener("DOMContentLoaded", function () { window.App.init(); });

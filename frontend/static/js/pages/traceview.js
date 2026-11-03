// Trace detail: graph view, timeline view, classification, annotations, export.
window.TraceView = (function () {

  const tags = ["INITIAL_ACQUISITION", "WALLET_MIGRATION", "CONSOLIDATION", "UTXO_SPLIT", "CHANGE", "PAYMENT", "SELF_TRANSFER", "UNKNOWN"];

  // Reverse modes: discovered ancestors
  // run backward toward older on-chain inputs while the graph stays
  // chronological. Reverse Provenance is ownership-gated (follows OWNED
  // predecessors only, UNKNOWN stops) and Reverse Blockchain Analysis is
  // ownership-neutral (every resolvable predecessor is followed).
  function isReverseMode(full) {
    const m = full.trace && full.trace.trace_mode;
    return m === "REVERSE_PROVENANCE" || m === "BLOCKCHAIN_ANALYSIS_REVERSE";
  }

  function isAnalysisMode(full) {
    const m = full.trace && full.trace.trace_mode;
    return m === "BLOCKCHAIN_ANALYSIS" || m === "BLOCKCHAIN_ANALYSIS_REVERSE";
  }

  // Explicit trace-mode dispatch (v0.6.5): BLOCKCHAIN_ANALYSIS is the canonical
  // FORWARD Blockchain Analysis mode. Its running/empty-state wording must
  // describe a forward spend walk and must NEVER be derived from a broad
  // "anything that is not forward provenance" check (which mis-labelled live
  // in-progress forward analyses as "Reverse analysis is running"). Reverse
  // wording is selected only by these narrow mode sets.
  function isReverseProvenance(full) {
    const m = full.trace && full.trace.trace_mode;
    return m === "REVERSE_PROVENANCE";
  }

  function isForwardBlockchainAnalysis(full) {
    const m = full.trace && full.trace.trace_mode;
    return m === "BLOCKCHAIN_ANALYSIS";
  }

  function isReverseBlockchainAnalysis(full) {
    const m = full.trace && full.trace.trace_mode;
    return m === "BLOCKCHAIN_ANALYSIS_REVERSE";
  }

  // Background analysis job polling:
  // walks run
  // on the backend worker; the view shows a QUEUED / RUNNING banner with live
  // progress counters and polls until a terminal state. On COMPLETE /
  // LIMIT_REACHED / FAILED the view auto-refreshes once (guard: job id) so the
  // finished graph appears without a manual reload. Only one poller is ever
  // armed. Every render/leave bumps `generation`; a response that belongs to
  // an older trace or a left view is dropped and never re-arms the poller.
  let activePoll = null;
  let autoRefreshedJobId = null;
  let generation = 0;
  let graphRenderer = null;
  // Hop limit of the latest analysis-job response ({ traceId, hops }); the
  // job arrives independently of the trace payload the hop view renders.
  let analysisHopLimit = null;

  function clearPollTimer() {
    if (activePoll) { clearInterval(activePoll.timer); activePoll = null; }
  }

  function stopPolling() {
    generation++;
    clearPollTimer();
  }

  function detachGraph() {
    if (graphRenderer) { graphRenderer.detach(); graphRenderer = null; }
  }

  // Called by the router when leaving the view.
  function stop() {
    stopPolling();
    detachGraph();
  }

  function showActionError(el, err) {
    const old = el.querySelector("#trace-action-error");
    if (old) old.remove();
    const toolbar = el.querySelector(".trace-toolbar");
    if (toolbar) toolbar.insertAdjacentHTML("afterend", '<div class="alert error" id="trace-action-error">' + Fmt.esc(err.message) + "</div>");
  }

  function analysisJobBannerHtml(job, traceMode) {
    if (!job) return "";
    // Explicit mode dispatch (v0.6.5): BLOCKCHAIN_ANALYSIS is the FORWARD
    // analysis mode, so its queued/running/failed banner says "Blockchain
    // analysis" with forward spend-traversal wording - never "Reverse analysis".
    // Reverse wording (REVERSE_PROVENANCE, BLOCKCHAIN_ANALYSIS_REVERSE) is
    // preserved verbatim.
    const isReverseProvenance = traceMode === "REVERSE_PROVENANCE";
    const isReverseBlockchainAnalysis = traceMode === "BLOCKCHAIN_ANALYSIS_REVERSE";
    const reverse = isReverseProvenance || isReverseBlockchainAnalysis;
    const head = reverse ? "Reverse analysis" : "Blockchain analysis";
    const opLabel = job.operation === "FOLLOW"
      ? (reverse ? "Continuing backward from selected input" : "Following spending transaction")
      : job.operation === "CONTINUE" ? "Re-checking analysis"
      : (reverse ? "Tracing ancestry" : "Building analysis");
    if (job.state === "QUEUED") {
      return '<div class="alert info" id="analysis-job-banner"><span class="spinner"></span><b>' + head + " queued.</b>" +
        " <span class=\"hint\">" + Fmt.esc(opLabel) +
        " - another analysis is currently using the background worker. This trace will start automatically.</span></div>";
    }
    if (job.state === "RUNNING") {
      const p = job.progress || {};
      const l = job.limits || {};
      const body = reverse
        ? 'Ancestry is being traversed backward through predecessor inputs (' + Fmt.esc(opLabel) + '). The trace will update automatically when it finishes.'
        : 'Spend relationships are being traversed forward through successor outputs (' + Fmt.esc(opLabel) + '). The trace will update automatically when the current analysis finishes.';
      return '<div class="alert info" id="analysis-job-banner"><span class="spinner"></span><b>' + head + " is running.</b>" +
        ' <span class="hint">' + body + '</span>' +
        ' <span class="mono">' + (p.transactions != null ? p.transactions : 0) + " / " + (l.transactions != null ? l.transactions : "?") +
        " txs · " + (p.outpoints != null ? p.outpoints : 0) + " / " + (l.outpoints != null ? l.outpoints : "?") +
        " outpoints · hop " + (p.hop != null ? p.hop : 0) + " / " + (l.hops != null ? l.hops : "?") + "</span></div>";
    }
    if (job.state === "FAILED") {
      const msg = job.error_summary || "unknown internal error";
      return '<div class="alert error" id="analysis-job-banner"><b>' + head + " stopped because of an internal error.</b>" +
        " <span class=\"hint\">Partial trace data was preserved. You can retry with Re-check / continue.</span>" +
        ' <span class="hint">(' + Fmt.esc(opLabel) + ' failed: ' + Fmt.esc(msg) + ")</span></div>";
    }
    return "";
  }

  function applyJobBanner(el, job) {
    const old = el.querySelector("#analysis-job-banner");
    if (old) old.remove();
    const html = analysisJobBannerHtml(job, el.dataset.traceMode);
    if (html) {
      const toolbar = el.querySelector(".trace-toolbar");
      if (toolbar) toolbar.insertAdjacentHTML("afterend", html);
    }
    // Guard expensive actions while this trace has an active job (§15): the
    // backend also returns 409, but the controls should reflect it immediately.
    const active = job && (job.state === "QUEUED" || job.state === "RUNNING");
    const btn = el.querySelector("#btn-continue");
    if (btn) btn.disabled = !!active;
  }

  // Blockchain Analysis hop view (owner decision, browser check 39): columns
  // are graph positions, so a merge can place outputs beyond the hop limit,
  // which bounds the search depth. Text only.
  function applyHopLimitHint(root) {
    const old = root.querySelector("#hop-limit-hint");
    if (old) old.remove();
    const groups = root.querySelector(".timeline-groups[data-max-hop]");
    if (!groups || !analysisHopLimit || analysisHopLimit.traceId !== Number(groups.dataset.traceId)) return;
    const limit = analysisHopLimit.hops;
    if (limit == null || Number(groups.dataset.maxHop) <= limit) return;
    groups.insertAdjacentHTML("beforebegin", '<p class="hint" id="hop-limit-hint">Column numbers show the position in the graph. ' +
      "The hop limit (" + Fmt.esc(limit) + ") applies to the search depth, so merges can place outputs beyond it.</p>");
  }

  function currentTab(el) {
    const a = el.querySelector(".subtabs .tab.active");
    return a && a.dataset.sub ? a.dataset.sub : "graph";
  }

  function pollAnalysisJob(el, traceId, token) {
    clearPollTimer();
    API.analysisJob(traceId).then(function (job) {
      if (token !== generation) return;
      applyJobBanner(el, job);
      analysisHopLimit = { traceId: Number(traceId), hops: (job.limits || {}).hops };
      applyHopLimitHint(el);
      if (job.state === "QUEUED") {
        activePoll = { timer: setInterval(function () { pollAnalysisJob(el, traceId, token); }, 2500), traceId: traceId };
      } else if (job.state === "RUNNING") {
        activePoll = { timer: setInterval(function () { pollAnalysisJob(el, traceId, token); }, 1500), traceId: traceId };
      } else if (job.state === "COMPLETE" || job.state === "LIMIT_REACHED" || job.state === "FAILED") {
        // Terminal: stop polling and refresh the view with the finished data.
        if (autoRefreshedJobId !== job.job_id) {
          autoRefreshedJobId = job.job_id;
          render(el, traceId, currentTab(el));
        }
      }
    }).catch(function () {
      // No analysis job for this trace (forward provenance) / transient error:
      // stop; the static view already reflects the trace state.
      if (token === generation) clearPollTimer();
    });
  }

  function render(el, traceId, activeTab) {
    stop();
    // SEC-19: never request or display an ID that is not a positive integer.
    traceId = Fmt.traceId(traceId);
    if (traceId === null) {
      el.innerHTML = '<div class="alert error">Invalid trace link: the trace ID must be a positive whole number.</div>';
      return;
    }
    const token = generation;
    el.innerHTML = '<div class="loading"><span class="spinner"></span>Loading trace…</div>';
    API.getTrace(traceId).then(function (full) {
      if (token !== generation) return;
      const reverse = isReverseMode(full);
      const analysisNoClassify = isAnalysisMode(full);
      el.innerHTML =
        '<div class="trace-toolbar">' +
        '  <div style="flex:1;min-width:240px">' +
        '    <h1 style="margin:0">' + Fmt.esc(full.trace.name || ("Trace #" + full.trace.id)) + "</h1>" +
        '    <div class="hint mono">start ' + Fmt.esc(Fmt.shortTxid(full.trace.start_txid, 12)) +
          (full.trace.start_address ? " · " + Fmt.esc(Fmt.shortAddress(full.trace.start_address)) : "") +
          (full.trace.start_vout !== null && full.trace.start_vout !== undefined ? " · vout " + full.trace.start_vout : "") + "</div>" +
        "  </div>" +
        '  <span class="status-tag ' + full.trace.status + '">' + full.trace.status + "</span>" +
        (full.trace.trace_mode === "REVERSE_PROVENANCE"
          ? '<span class="badge" title="Ownership-aware reverse walk: traces the ancestry of your current UTXO back to earlier owned coins. Starts from an exact TXID:VOUT and never classifies the target.">REVERSE PROVENANCE</span>'
          : full.trace.trace_mode === "BLOCKCHAIN_ANALYSIS_REVERSE"
            ? '<span class="badge" title="Ownership-neutral backward ancestry walk. Following an output never classifies it.">BLOCKCHAIN ANALYSIS — REVERSE</span>'
            : full.trace.trace_mode === "BLOCKCHAIN_ANALYSIS"
              ? '<span class="badge" title="Ownership-independent spend-state walk. Following an output never classifies it.">BLOCKCHAIN ANALYSIS</span>'
              : "") +
        '  <button class="btn small" id="btn-continue">Re-check / continue</button>' +
        '  <button class="btn small" id="btn-export-json">Export JSON</button>' +
        '  <button class="btn small" id="btn-export-csv">Export CSV</button>' +
        '  <button class="btn small danger" id="btn-delete">Delete</button>' +
        "</div>" +
        (full.trace.status === "FORWARD_LIMIT_REACHED"
          ? '<div class="alert warn">Forward Provenance stopped at MAX_TRACE_DEPTH — this graph is partial. Increase the configured limit, then use <b>Re-check / continue</b>.</div>'
          : full.forward_stop_reason === "CONTINUATION_UNAVAILABLE"
            ? '<div class="alert warn">Forward continuation is still unresolved after re-checking. This graph is incomplete; retry with <b>Re-check / continue</b>.</div>'
          : full.trace.status === "REVERSE_LIMIT_REACHED"
          ? '<div class="alert warn">Reverse Provenance stopped at a safety limit — this graph is intentionally partial. Add Known Addresses or classify an input as Owned to extend that branch, then use <b>Re-check / continue</b>.</div>'
          : full.trace.status === "ANALYSIS_LIMIT_REACHED"
            ? reverse
              ? '<div class="alert warn">Reverse analysis stopped at a safety limit — this graph is intentionally partial. Open an ancestor outpoint and use <b>Continue backward from this input</b> to deliberately extend the walk.</div>'
              : '<div class="alert warn">Blockchain analysis stopped at a safety limit — this graph is intentionally partial. Open a spent output and use <b>Follow spending transaction</b> to deliberately extend the analysis.</div>'
            : "") +
        (full.unresolved_count > 0
          ? '<div class="alert info" id="trace-unresolved-notice">' + full.unresolved_count +
            (full.unresolved_count === 1 ? " predecessor" : " predecessors") +
            " without available history — ancestry cannot be followed further there.</div>"
          : "") +
        (full.trace.trace_mode === "REVERSE_PROVENANCE" && full.summary && full.summary.unknown_predecessors > 0
          ? '<div class="alert info">Unclassified predecessor inputs are shown as transaction context. Only confirmed owned inputs are followed automatically. You can optionally classify an input as owned to extend that branch.</div>'
          : full.needs_classification && !analysisNoClassify
            ? '<div class="alert warn">Some outputs still need classification. Mark owned outputs to continue the trace.</div>'
            : "") +
        (full.needs_review && !analysisNoClassify
          ? '<div class="alert warn">An outpoint is both a confirmed custodial deposit and owned per registry evidence. Review it in the <b>Custody</b> tab: keep the boundary, or remove only the custody classification so ownership evidence restores exact tracing.</div>'
          : "") +
        '<details class="panel"><summary>Managed output Acquisition provenance</summary>' +
        '<div id="trace-acquisition-provenance">Checking provenance…</div></details>' +
        '<div class="tabs subtabs">' +
        '  <button class="tab active" data-sub="graph">Graph</button>' +
        '  <button class="tab" data-sub="timeline">Timeline / Table</button>' +
        '  <button class="tab" data-sub="summary">Summary</button>' +
        '  <button class="tab" data-sub="classify">Classify</button>' +
        '  <button class="tab" data-sub="custody">Custody</button>' +
        "</div>" +
        '<div id="trace-sub"></div>';

      el.dataset.traceMode = full.trace.trace_mode;

      // Analysis / reverse traces run their spend walk on the backend worker
      // (v0.5.6 + v0.6.0): fetch the latest job state and arm the poller.
      if (full.trace.trace_mode !== "FORWARD_PROVENANCE") {
        pollAnalysisJob(el, traceId, token);
      }

      const sub = el.querySelector("#trace-sub");
      const reload = function () {
        var activeTab = "graph";
        var activeEl = el.querySelector(".subtabs .tab.active");
        if (activeEl && activeEl.dataset.sub) activeTab = activeEl.dataset.sub;
        render(el, traceId, activeTab);
      };
      const custodyCtx = function () { return { traceId: traceId, full: full, onChanged: reload }; };
      const provenanceTarget = el.querySelector('#trace-acquisition-provenance');
      const loadProvenance = function (attempt) {
        API.traceProvenance(traceId).then(function (outputs) {
          if (!provenanceTarget.isConnected) return;
          function show(filter) {
            provenanceTarget.innerHTML = '<p class="hint">Historical and current evidence-backed managed outputs. ' +
              'These states are independent of ownership and topology; no sat-level allocation is implied.</p>' +
              '<label><input type="checkbox" id="trace-unanchored-only"' + (filter ? ' checked' : '') + '> UNANCHORED only</label>' +
              outputs.filter(function (o) { return !filter || o.acquisition_provenance === 'UNANCHORED'; }).map(function (o) {
                return '<div><button class="btn small" data-provenance-output="' + Fmt.esc(o.outpoint) + '">' + Fmt.esc(o.outpoint) +
                  '</button> ' + Fmt.acquisitionBadge(o.acquisition_provenance) + '</div>';
              }).join('');
            provenanceTarget.querySelector('#trace-unanchored-only').addEventListener('change', function () { show(this.checked); });
            provenanceTarget.querySelectorAll('[data-provenance-output]').forEach(function (button) {
              button.addEventListener('click', function () {
                var key = button.dataset.provenanceOutput.split(':');
                window.App.openOutpoint(key[0], Number(key[1]), custodyCtx());
              });
            });
          }
          show(false);
        }).catch(function (err) {
          const reason = err && ((err.data && err.data.reason) || err.message);
          const unavailable = function () {
            if (provenanceTarget.isConnected) provenanceTarget.textContent = 'Acquisition provenance unavailable' +
              (reason ? ' (' + reason + ')' : '') + '; reload to retry.';
          };
          // FIX-OWN-1: a 503 states its reason; retry it a few times on its own.
          const retry = function () {
            if (!provenanceTarget.isConnected) return;
            if (attempt >= 3) return unavailable();
            provenanceTarget.textContent = 'Acquisition provenance temporarily unavailable (' + reason + '). Retrying automatically…';
            setTimeout(function () {
              if (provenanceTarget.isConnected) loadProvenance(attempt + 1);
            }, ((err.data && err.data.retry_after) || 2) * 1000);
          };
          if (!err || err.status !== 503) return unavailable();
          if (full.trace.trace_mode === "FORWARD_PROVENANCE") return retry();
          // A running walk keeps writing evidence, so the read reports a change
          // (503). Not an error: the terminal job re-renders and loads it again.
          API.analysisJob(traceId).then(function (job) {
            if (!provenanceTarget.isConnected) return;
            if (job.state === "QUEUED" || job.state === "RUNNING") {
              provenanceTarget.textContent = 'Acquisition provenance is shown when the running walk finishes.';
            } else retry();
          }).catch(unavailable);
        });
      };
      loadProvenance(1);

      function switchTab(name) {
        detachGraph();
        el.querySelectorAll(".subtabs .tab").forEach(function (t) {
          t.classList.toggle("active", t.dataset.sub === name);
        });
        // Pass the custodyCtx FUNCTION to the renderers (one slot per visited
        // tab is re-used, so the closure must stay a factory) - never the
        // eagerly-evaluated object. The renderers invoke custodyCtx() lazily
        // when a node / timeline row is clicked to build the popup context, so
        // passing a plain object here regressed into "custodyCtx is not a
        // function" and broke every graph-node and timeline-row popup.
        if (name === "graph") renderGraph(sub, full, custodyCtx);
        else if (name === "timeline") renderTimeline(sub, full, custodyCtx);
        else if (name === "summary") renderSummary(sub, full);
        else if (name === "classify") renderClassify(sub, full, traceId, full);
        else if (name === "custody") renderCustody(sub, full, traceId, custodyCtx);
      }

      el.querySelectorAll(".subtabs .tab").forEach(function (t) {
        t.addEventListener("click", function () { switchTab(t.dataset.sub); });
      });
      switchTab(typeof activeTab === "string" ? activeTab : "graph");

      el.querySelector("#btn-continue").addEventListener("click", function () {
        sub.innerHTML = '<div class="loading"><span class="spinner"></span>Re-checking spend state…</div>';
        API.continueTrace(traceId).then(function () { if (token === generation) render(el, traceId); })
          .catch(function (err) { if (token === generation) sub.innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>"; });
      });
      el.querySelector("#btn-export-json").addEventListener("click", function () {
        API.exportJson(traceId).then(function (data) {
          const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a"); a.href = url; a.download = "trace-" + traceId + ".json";
          document.body.appendChild(a); a.click();
          setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 100);
        }).catch(function (err) { showActionError(el, err); });
      });
      el.querySelector("#btn-export-csv").addEventListener("click", function () {
        const a = document.createElement("a");
        a.href = "/api/traces/" + traceId + "/export.csv"; a.download = "";
        document.body.appendChild(a); a.click(); a.remove();
      });
      el.querySelector("#btn-delete").addEventListener("click", function () {
        if (!confirm("Delete trace #" + traceId + "?")) return;
        API.deleteTrace(traceId).then(function () { window.App.showView("traces"); })
          .catch(function (err) { showActionError(el, err); });
      });
    }).catch(function (err) {
      if (token !== generation) return;
      el.innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>";
    });
  }

  // ---------------------------------------------------------------- Graph
  function renderGraph(el, full, custodyCtx) {
    const reverse = isReverseMode(full);
    if (!full.edges || !full.edges.length) {
      // Explicit trace-mode empty-state wording (v0.6.5): Blockchain Analysis
      // traversal is ownership-neutral - classification NEVER gates it - so its
      // no-graph state must not suggest classification as a requirement.
      // BLOCKCHAIN_ANALYSIS (forward) gets forward spend wording; the reverse
      // modes get ancestry wording. Forward Provenance keeps its legacy text.
      let emptyText;
      if (isReverseProvenance(full)) {
        emptyText = "This reverse trace has no resolved ancestry yet. The selected target outpoint is rendered below (a target whose producing transaction is unspendable or alone on-chain has no predecessor inputs to follow).";
      } else if (isReverseBlockchainAnalysis(full)) {
        emptyText = "This reverse Blockchain Analysis trace has no resolved ancestry yet. A target whose producing transaction is unspendable or alone on-chain has no predecessor inputs to follow.";
      } else if (isForwardBlockchainAnalysis(full)) {
        if (full.trace.status === "IN_PROGRESS") {
          // Analysis (re)walk is still running and no graph data exists yet.
          emptyText = "Blockchain analysis is still running. The graph will appear as spend relationships are resolved.";
        } else if ((full.outpoints || []).some(function (o) {
          return o.txid === full.trace.start_txid && o.vout === full.trace.start_vout && o.spend_status === "UNSPENT";
        })) {
          // The selected start output is unspent: terminal, nothing to follow.
          emptyText = "The selected start output is currently unspent, so there is no forward transaction path to follow.";
        } else {
          emptyText = "This Blockchain Analysis trace has no spend relationships to show yet. A terminal (unspent) start output has no forward transaction path to follow.";
        }
      } else {
        emptyText = "This trace has no confirmed walk yet. Either the start output is still unspent (a terminal UTXO) or outputs still need classification.";
      }
      el.innerHTML =
        '<div class="panel">' +
        '  <h2>No lineage graph yet</h2>' +
        '  <p class="hint">' + emptyText + "</p>" +
        (full.outpoints.length ? "<div>" + full.outpoints.map(function (o) {
          return '<div class="mono">' + Fmt.esc(o.txid) + ":" + o.vout + " — " + Fmt.sats(o.value_sats) + " sats — " + o.spend_status + " — " + o.ownership_status + "</div>";
        }).join("") + "</div>" : "") +
        "</div>";
      return;
    }
    // Reverse traces anchor the OLDEST ancestor roots at depth 0 (left) and let
    // chronological edge propagation place the target at the deepest column
    // (right), then highlight the target with a ring. Forward/analysis traces
    // keep the existing single-start-anchor model unchanged.
    let model;
    if (reverse) {
      const roots = LineageGraph.reverseRoots(full.edges || []);
      const hasVout = full.trace.start_vout !== null && full.trace.start_vout !== undefined;
      const targetKey = hasVout ? full.trace.start_txid + ":" + full.trace.start_vout : null;
      model = LineageGraph.buildModel(full, {
        anchors: roots.length ? roots : (targetKey ? [targetKey] : undefined),
        highlight: targetKey,
      });
    } else {
      model = LineageGraph.buildModel(full);
    }
    el.innerHTML =
      '<div class="graph-wrap">' +
      '  <div class="graph-legend">' +
      '    <div><span class="dot" style="background:#3fb950"></span>Owned output</div>' +
      '    <div><span class="dot" style="background:#8b949e"></span>Unknown</div>' +
      '    <div><span class="dot" style="background:#d29922"></span>External / uncertain</div>' +
      '    <div><span class="dot" style="background:#c586c0"></span>Custodial boundary</div>' +
      '    <div><span class="dot" style="background:#a371f7"></span>PROV: MIXED (output attribution confidence)</div>' +
      '    <div><span class="dot" style="background:#a371f7;opacity:0.6"></span>batch chip (transaction topology)</div>' +
      '    <div><span class="line-dash"></span>Custody bridge (user-confirmed, not on-chain)</div>' +
      '    <div><span class="dot" style="background:#6e7681"></span>Transaction (shown as inputs→outputs)</div>' +
      (reverse ? '    <div><span class="dot" style="background:#58a6ff"></span>Selected reverse target (newest)</div>' : "") +
      "  </div>" +
      '  <div class="graph-hint">drag to pan · scroll to zoom · click a node for details</div>' +
      '  <canvas id="graph-canvas"></canvas>' +
      "</div>" +
      '<div class="hint">Edge relationship types: ' +
        full.edges.map(function (e) {
          return '<span style="color:var(--' + (e.relationship_type === "DIRECT" ? "ok" : e.relationship_type === "SPLIT" ? "info" : e.relationship_type === "MERGED" || e.relationship_type === "MIXED" ? "warn" : "unknown") + ')">' + e.relationship_type + "</span>";
        }).filter(function (v, i, arr) { return arr.indexOf(v) === i; }).join(" · ") +
      "</div>";
    graphRenderer = LineageGraph.attach(el.querySelector("#graph-canvas"), model, {
      onOpenNode: function (node) {
        if (node.type === "outpoint") {
          if (custodyCtx) window.App.openOutpoint(node.data.txid, node.data.vout, custodyCtx());
          else window.App.openOutpoint(node.data.txid, node.data.vout);
        } else {
          // Pass the full trace-payload transaction so the modal renders the
          // merge/consolidation input context (input roles, referenced-output
          // ownership, related-trace links) without a second API fetch.
          window.App.openTransaction(node.key, node.data, custodyCtx ? custodyCtx() : null);
        }
      }
    });
  }

  // ------------------------------------------------------------- Timeline
  // Pure, side-effect-free hop/table model
  // Mirrored
  // 1:1 by a unit test.
  //
  // A hop is graph depth, not evidence the chain is linear. Every outpoint
  // belongs to exactly one column: an edge source sits at `hop_number`, an
  // edge destination at `hop_number + 1`. Identical TXID:VOUT rows inside a
  // hop collapse into ONE entry carrying aggregated relationship chips, so
  // the table never repeats an outpoint just because several edges touch it.
  //
  // Hop summary labels are derived from tracked graph structure only (never a
  // semantics change): START on hop 0, CURRENT on the deepest hop still
  // holding an UNSPENT outpoint, MERGE where an output of a transaction that
  // consumes >= 2 tracked edges lands, SPLIT where outputs of a transaction
  // that produces >= 2 tracked outputs land, CONSOLIDATION where the
  // producing transaction's native topology says CONSOLIDATION.
  function buildTimelineModel(full) {
    const byKey = {};
    full.outpoints.forEach(function (o) { byKey[o.txid + ":" + o.vout] = o; });

    const suggByKey = {};
    (full.possible_boundaries || []).forEach(function (s) { suggByKey[s.txid + ":" + s.vout] = s; });
    const dismByKey = {};
    (full.dismissed_candidates || []).forEach(function (d) { dismByKey[d.txid + ":" + d.vout] = d; });

    // Whole-transaction topology/batch;
    // a pure blockchain fact, never a custody signal.
    const txMeta = {};
    (full.transactions || []).forEach(function (t) { txMeta[t.txid] = t; });

    // Per-outpoint aggregation across the whole edge set.
    const agg = {};   // key -> { cols:{}, rels:{}, producer }
    function get(key) {
      if (!agg[key]) agg[key] = { cols: {}, rels: {}, producer: null };
      return agg[key];
    }
    (full.edges || []).forEach(function (e) {
      const sk = e.source_txid + ":" + e.source_vout;
      const dk = e.destination_txid + ":" + e.destination_vout;
      const rel = e.relationship_type || "UNKNOWN";
      const spend = e.spending_txid;
      const s = get(sk); s.cols[e.hop_number] = true; s.rels[rel] = true;
      const d = get(dk); d.cols[e.hop_number + 1] = true; d.rels[rel] = true;
      if (spend) d.producer = spend;
    });

    // Tracked fan-in / fan-out of each spending transaction. These are the
    // structural merge/split signals (counted over the tracked edge set;
    // custody bridges - empty spending_txid - never contribute).
    const trackedInputs = {};
    const trackedOutputs = {};
    (full.edges || []).forEach(function (e) {
      if (!e.spending_txid) return;
      (trackedInputs[e.spending_txid] = trackedInputs[e.spending_txid] || {})[e.source_txid + ":" + e.source_vout] = true;
      (trackedOutputs[e.spending_txid] = trackedOutputs[e.spending_txid] || {})[e.destination_txid + ":" + e.destination_vout] = true;
    });

    // Terminal / start-only trace (no edges yet): put every outpoint on hop 0.
    if (!Object.keys(agg).length) {
      full.outpoints.forEach(function (o) { get(o.txid + ":" + o.vout).cols[0] = true; });
    }

    // One stable graph column per outpoint (Math.max is only a fallback; a
    // well-formed trace places every outpoint at exactly one column).
    const group = {};
    Object.keys(agg).forEach(function (key) {
      const cols = Object.keys(agg[key].cols).map(Number);
      const col = cols.length ? Math.max.apply(null, cols) : 0;
      (group[col] = group[col] || []).push(key);
    });
    const hops = Object.keys(group).map(Number).sort(function (a, b) { return a - b; });

    // Deepest hop still holding an UNSPENT outpoint = the current/final hop.
    let currentCol = null;
    hops.forEach(function (col) {
      group[col].forEach(function (key) {
        const o = byKey[key];
        if (o && o.spend_status === "UNSPENT" && (currentCol === null || col > currentCol)) currentCol = col;
      });
    });

    const model = [];
    const lastCol = hops.length ? hops[hops.length - 1] : 0;
    hops.forEach(function (col) {
      const keys = group[col].slice().sort();
      const rels = [];
      const seen = {};
      let merge = false;
      let split = false;
      let consol = false;
      keys.forEach(function (key) {
        const a = agg[key];
        Object.keys(a.rels).forEach(function (r) {
          if (!seen[r]) { seen[r] = true; rels.push(r); }
        });
        const o = byKey[key];
        const producer = (o && o.txid) || a.producer || null;
        if (producer) {
          if (trackedInputs[producer] && Object.keys(trackedInputs[producer]).length >= 2) merge = true;
          if (trackedOutputs[producer] && Object.keys(trackedOutputs[producer]).length >= 2) split = true;
          const tm = txMeta[producer];
          if (tm && tm.transaction_topology === "CONSOLIDATION") consol = true;
        }
      });
      let label = null;
      if (col === 0) label = "START";
      else if (col === currentCol) label = "CURRENT";
      else if (merge) label = "MERGE";
      else if (split) label = "SPLIT";
      else if (consol) label = "CONSOLIDATION";
      model.push({
        hop: col,
        count: keys.length,
        label: label,
        defaultOpen: col === 0 || col === currentCol || col === lastCol,
        keys: keys,
        rels: rels,
      });
    });

    return { hops: model, byKey: byKey, txMeta: txMeta, suggByKey: suggByKey, dismByKey: dismByKey };
  }

  // Reverse-trace timeline model.
  //
  // Reverse edges store `hop_number` = the reverse discovery hop of the edge
  // DESTINATION (selected target = 0, its producing-transaction predecessor
  // inputs = 1, the predecessors of those = 2, ...). So a destination belongs
  // to reverse hop `hop_number`, and an edge source sits one hop further back
  // (`hop_number + 1`). Shared TXID:VOUT rows collapse onto ONE column exactly
  // like the forward model, because every outpoint that is both a traced
  // output and a predecessor input lands on the SAME hop from both roles:
  //     e.g. edge (hop 1) D:0 -> E -> E:0 and edge (hop 0) E:0 -> F -> F:0
  //          E:0 = dest at hop 1 AND source at hop 0 + 1 = 1  -> one column.
  // Hop labels are reverse-specific: TARGET / PREDECESSORS / EARLIEST ANCESTRY.
  function buildReverseTimelineModel(full) {
    const byKey = {};
    full.outpoints.forEach(function (o) { byKey[o.txid + ":" + o.vout] = o; });

    const suggByKey = {};
    (full.possible_boundaries || []).forEach(function (s) { suggByKey[s.txid + ":" + s.vout] = s; });
    const dismByKey = {};
    (full.dismissed_candidates || []).forEach(function (d) { dismByKey[d.txid + ":" + d.vout] = d; });

    const txMeta = {};
    (full.transactions || []).forEach(function (t) { txMeta[t.txid] = t; });

    const agg = {};   // key -> { cols:{}, rels:{} }
    function get(key) {
      if (!agg[key]) agg[key] = { cols: {}, rels: {} };
      return agg[key];
    }
    (full.edges || []).forEach(function (e) {
      const sk = e.source_txid + ":" + e.source_vout;
      const dk = e.destination_txid + ":" + e.destination_vout;
      const rel = e.relationship_type || "UNKNOWN";
      const d = get(dk); d.cols[e.hop_number] = true; d.rels[rel] = true;
      const s = get(sk); s.cols[(e.hop_number || 0) + 1] = true; s.rels[rel] = true;
    });

    // Terminal / target-only reverse trace (no edges yet): put every outpoint
    // on reverse hop 0.
    if (!Object.keys(agg).length) {
      full.outpoints.forEach(function (o) { get(o.txid + ":" + o.vout).cols[0] = true; });
    }

    const group = {};
    Object.keys(agg).forEach(function (key) {
      const cols = Object.keys(agg[key].cols).map(Number);
      // Prefer the discovery-hop column (the destination role) so a node is
      // grouped where the walk REACHED it, not where it feeds onward.
      const col = cols.length ? Math.min.apply(null, cols) : 0;
      (group[col] = group[col] || []).push(key);
    });
    const hops = Object.keys(group).map(Number).sort(function (a, b) { return a - b; });

    const model = [];
    const lastCol = hops.length ? hops[hops.length - 1] : 0;
    hops.forEach(function (col) {
      const keys = group[col].slice().sort();
      const rels = [];
      const seen = {};
      keys.forEach(function (key) {
        Object.keys(agg[key].rels).forEach(function (r) {
          if (!seen[r]) { seen[r] = true; rels.push(r); }
        });
      });
      let label = null;
      if (col === 0) label = "TARGET";
      else if (col === 1) label = "PREDECESSORS";
      else if (col === lastCol) label = "EARLIEST ANCESTRY";
      model.push({
        hop: col,
        count: keys.length,
        label: label,
        defaultOpen: col === 0 || col === lastCol,
        keys: keys,
        rels: rels,
      });
    });

    return { hops: model, byKey: byKey, txMeta: txMeta, suggByKey: suggByKey, dismByKey: dismByKey };
  }

  function renderTimeline(el, full, custodyCtx) {
    const reverse = isReverseMode(full);
    const model = reverse ? buildReverseTimelineModel(full) : buildTimelineModel(full);
    const openWithCtx = function (txid, vout) {
      window.App.openOutpoint(txid, vout, custodyCtx ? custodyCtx() : null);
    };
    if (!model.hops.length) {
      el.innerHTML = '<div class="panel hint">No outpoints in this trace yet.</div>';
      return;
    }

    const feeSeen = {};
    let html = full.trace.trace_mode === "BLOCKCHAIN_ANALYSIS"
      ? '<div class="timeline-groups" data-trace-id="' + full.trace.id + '" data-max-hop="' +
        Math.max.apply(null, model.hops.map(function (g) { return g.hop; })) + '">'
      : '<div class="timeline-groups">';
    model.hops.forEach(function (g) {
      const chevron = g.defaultOpen ? "▼" : "▶";
      const labelChip = g.label ? '<span class="hop-label ' + g.label + '">' + g.label + "</span>" : "";
      html +=
        '<section class="hop-group' + (g.defaultOpen ? " open" : "") + '">' +
        '  <button class="hop-header" aria-expanded="' + (g.defaultOpen ? "true" : "false") + '">' +
        '    <span class="hop-chevron">' + chevron + "</span>" +
        '    <span class="hop-title">' + (reverse ? "Reverse Hop " : "Hop ") + g.hop + "</span>" +
        '    <span class="hop-count">' + g.count + " outpoint" + (g.count === 1 ? "" : "s") + "</span>" +
        labelChip +
        "  </button>" +
        '  <div class="hop-body"' + (g.defaultOpen ? "" : ' style="display:none"') + ">" +
        '    <table class="data">' +
        "    <thead><tr><th>Date</th><th>TXID</th><th>VOUT</th><th>Address</th><th>Amount</th><th>Status</th><th>Ownership</th><th>Provenance</th><th>Topology</th><th>Fee</th><th>Relationship</th></tr></thead><tbody>";
      g.keys.forEach(function (key) {
        const o = model.byKey[key] || {};
        const sug = model.suggByKey[key];
        const dis = model.dismByKey[key];
        const title = sug
          ? "Possible custody · " + Fmt.esc(sug.confidence) + " — " + Fmt.esc((sug.reasons || []).join(" · "))
          : dis ? "Possible custody · declined — reset the decision in the Custody tab" : "";
        const badge = sug
          ? '<span class="cust-badge cust-badge-sug" title="' + title + '">Possible custody · ' + Fmt.esc(sug.confidence) + "</span>"
          : dis
            ? '<span class="cust-badge cust-badge-dis" title="' + title + '">declined</span>'
            : "";
        const own = o.ownership_status || "UNKNOWN";
        const prov = o.provenance_state || "UNKNOWN";
        const tm = model.txMeta[o.txid] || {};
        const topo = (tm.transaction_topology || "UNKNOWN") + (tm.likely_batch ? " · batch" : "");
        var feeCell;
        if (feeSeen[o.txid]) {
          feeCell = "";
        } else {
          feeSeen[o.txid] = true;
          if (tm.fee_sats != null) {
            var feeText = Fmt.sats(tm.fee_sats) + " sats";
            if (tm.fee_rate != null) {
              feeText += " \u00b7 " + tm.fee_rate + " sat/vB";
            }
            feeCell = '<td class="amt">' + feeText + "</td>";
          } else {
            feeCell = '<td class="hint">\u2014</td>';
          }
        }
        const chips = g.rels.map(function (r) {
          return '<span class="rel-chip ' + Fmt.relClass(r) + '">' + Fmt.esc(r) + "</span>";
        }).join(" ");
        html +=
          '<tr class="clickable" data-txid="' + Fmt.esc(o.txid) + '" data-vout="' + o.vout + '">' +
          "<td>" + Fmt.dateOnly(o.block_time) + "</td>" +
          '<td class="mono">' + Fmt.esc(Fmt.shortTxid(o.txid, 10)) + "</td>" +
          "<td>" + o.vout + badge + "</td>" +
          '<td class="mono">' + Fmt.esc(Fmt.shortAddress(o.address)) + "</td>" +
          '<td class="amt">' + (o.value_sats != null ? Fmt.sats(o.value_sats) : "—") + "</td>" +
          "<td>" + (o.spend_status === "UNSPENT" ? '<span style="color:#3fb950">unspent</span>' : o.spend_status === "SPENT" ? '<span style="color:#8b949e">spent</span>' : "unknown") + "</td>" +
          '<td style="color:' + (own === "OWNED" || own === "LIKELY_OWNED" ? "#3fb950" : own === "EXTERNAL" ? "#d29922" : own === "CUSTODIAL_DEPOSIT" ? "#c586c0" : "#8b949e") + '">' + Fmt.esc(own) + "</td>" +
          '<td style="color:' + (prov === "EXACT" ? "#3fb950" : prov === "MIXED" ? "#a371f7" : prov === "AMBIGUOUS" ? "#d29922" : "#8b949e") + '">' + Fmt.esc(prov) + "</td>" +
          '<td class="hint">' + Fmt.esc(topo) + "</td>" +
          feeCell +
          "<td>" + chips + "</td>" +
          "</tr>";
      });
      html += "</tbody></table></div></section>";
    });
    html += "</div>";

    el.innerHTML = html;
    applyHopLimitHint(el);

    // Collapse / expand whole hop groups (chevron + aria-expanded state).
    el.querySelectorAll(".hop-header").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const section = btn.closest(".hop-group");
        const body = section.querySelector(".hop-body");
        const open = section.classList.toggle("open");
        body.style.display = open ? "" : "none";
        btn.setAttribute("aria-expanded", open ? "true" : "false");
        btn.querySelector(".hop-chevron").textContent = open ? "▼" : "▶";
      });
    });
    el.querySelectorAll("tr[data-txid]").forEach(function (tr) {
      tr.addEventListener("click", function () {
        openWithCtx(tr.dataset.txid, Number(tr.dataset.vout));
      });
    });
    el.querySelectorAll(".cust-badge").forEach(function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        const tr = b.closest("tr[data-txid]");
        if (tr) openWithCtx(tr.dataset.txid, Number(tr.dataset.vout));
      });
    });
  }

  // -------------------------------------------------------------- Summary
  function renderSummary(el, full) {
    const s = full.summary;
    if (!s) { el.innerHTML = '<div class="panel hint">No summary available.</div>'; return; }
    const reverse = isReverseMode(full);
    const oldest = function () {
      if (s.oldest_block_height != null) return "block " + s.oldest_block_height;
      if (s.oldest_block_time != null) return Fmt.dateOnly(s.oldest_block_time);
      return "—";
    };
    const cells = reverse
      ? [
          ["Target outpoint", s.target_outpoint || "—"],
          ["Transactions followed", s.transactions_followed],
          // The reverse summary API reports discovered ancestor outpoints
          // (edge sources, coinbase origins included) as tracked_branches.
          ["Ancestor outpoints", s.tracked_branches],
          ["Owned ancestry branches", s.owned_ancestors],
          ["Unknown context inputs", s.unknown_predecessors],
          ["External context inputs", s.external_predecessors],
          ["Merges", s.merges],
          ["Splits", s.splits],
          ["Oldest ancestry", oldest()],
        ]
      : [
          ["Start amount", s.start_amount_sats != null ? Fmt.sats(s.start_amount_sats) + " sats" : "—"],
          ["Transactions followed", s.transactions_followed],
          ["Tracked branches", s.tracked_branches],
          ["Current UTXOs", s.current_utxos],
          ["Unknown outputs", s.unknown_outputs],
          ["Merges", s.merges],
          ["Splits", s.splits],
        ];
    let html =
      '<div class="panel"><h2>Trace Summary</h2>' +
      '<div class="kv" style="margin-bottom:14px">' +
      '  <div class="k">Status</div><div>' + full.trace.status + "</div>" +
      '  <div class="k">Start TX</div><div class="mono">' + Fmt.esc(full.trace.start_txid) + "</div>" +
      (full.trace.start_vout !== null && full.trace.start_vout !== undefined
        ? '  <div class="k">Target VOUT</div><div>' + full.trace.start_vout + "</div>" : "") +
      (full.trace.start_address ? '<div class="k">Start address</div><div class="mono">' + Fmt.esc(full.trace.start_address) + "</div>" : "") +
      "</div>" +
      '<div class="summary-grid">';
    cells.forEach(function (c) {
      html += '<div class="summary-cell"><div class="num">' + Fmt.esc(c[1]) + '</div><div class="lbl">' + Fmt.esc(c[0]) + "</div></div>";
    });
    html += "</div></div>";

    if (reverse) {
      html += '<p class="hint">' +
        (full.trace.trace_mode === "REVERSE_PROVENANCE"
          ? "Unclassified (unknown) and external inputs are shown as context only and never block this trace's completeness. Classifying an input as Owned is optional - it just extends that branch on the next Re-check."
          : "Ownership is display-only metadata on a reverse analysis: every resolvable predecessor is followed regardless of classification.") +
        "</p>";
    }

    html += '<div class="panel"><h2>Annotations</h2><div id="annotations">' +
      full.annotations.filter(function (a) { return a.entity_type === "trace" && a.entity_id === String(full.trace.id); })
        .map(function (a) { return '<div class="hint mono">[' + Fmt.esc(a.tag) + "] " + Fmt.esc(a.note || "") + "</div>"; }).join("") +
      "</div>" +
      '<div class="annotations">' +
      '  <div class="tag-list">' + tags.map(function (t) { return '<span class="tag-chip" data-tag="' + t + '">' + t + "</span>"; }).join("") + "</div>" +
      '  <textarea id="anno-note" placeholder="Free-text note (e.g. Wallet migration from Cold Wallet A to Cold Wallet B)"></textarea>' +
      '  <div style="margin-top:8px"><button class="btn small" id="btn-anno">Add trace annotation</button></div>' +
      "</div></div>";

    el.innerHTML = html;
    let selTag = "UNKNOWN";
    el.querySelectorAll(".tag-chip").forEach(function (c) {
      c.addEventListener("click", function () {
        el.querySelectorAll(".tag-chip").forEach(function (x) { x.style.borderColor = ""; x.style.color = ""; });
        selTag = c.dataset.tag;
        c.style.borderColor = "var(--accent)"; c.style.color = "var(--accent)";
      });
    });
    el.querySelector("#btn-anno").addEventListener("click", function () {
      API.addAnnotation({
        entity_type: "trace", entity_id: String(full.trace.id),
        tag: selTag, note: el.querySelector("#anno-note").value,
      }).then(function () { render(el, full.trace.id); });
    });
  }

  // ------------------------------------------------------------ Classify
  function renderClassify(el, full, traceId, latest) {
    // Blockchain Analysis never classifies (spec section 15): ownership is
    // display-only metadata and "following" (the outpoint modal action) is the
    // only deliberate-extension control. Both directions behave identically.
    if (isAnalysisMode(full)) {
      const isReverse = full.trace.trace_mode === "BLOCKCHAIN_ANALYSIS_REVERSE";
      el.innerHTML = '<div class="panel"><h2>No classification</h2>' +
        '<p class="hint">This is a ' + (isReverse ? 'Reverse Blockchain Analysis' : 'Blockchain Analysis') + ' trace. It walks ancestry ' + (isReverse ? 'backward through predecessor inputs' : 'spent outputs by spend state') + ' and <b>never classifies</b> outputs as owned or external. ' +
        "Ownership you see on the graph is display-only metadata and is not modified by the analysis - it never gates traversal. " +
        "To extend a limit-truncated analysis, " +
        (isReverse ? "open an ancestor outpoint and use <b>Continue backward from this input</b>."
          : "open a spent output and use <b>Follow spending transaction</b>.") +
        "</p></div>";
      return;
    }
    // Reverse Provenance classifies exactly like forward provenance: UNKNOWN
    // predecessor inputs decide whether the personal lineage continues, so the
    // manual controls here are the sanctioned continuation workflow.
    const reverse = isReverseMode(full);
    // Build a list of every output across trace transactions that has a
    // manual classification control.
    const byKey = {};
    latest.outpoints.forEach(function (o) { byKey[o.txid + ":" + o.vout] = o; });

    let html = '<div class="panel"><p class="hint">' +
      (reverse
        ? "For each predecessor input of this reverse trace, mark whether it belongs to you (OWNED), belongs to someone else (EXTERNAL), or is unknown. Only confirmed OWNED ancestors are followed automatically; UNKNOWN and EXTERNAL inputs are shown as context and never block the trace's completeness. Classifying an input as OWNED is optional - it simply extends that branch on the next Re-check."
        : "For each output of a transaction in this trace, mark whether it belongs to you (OWNED), belongs to someone else (EXTERNAL), or is unknown.") +
      "<br>Marked OWNED outputs continue the trace automatically. The trace is a watch-only analysis: no keys, no signing.</p></div>";

    const grouped = {};
    latest.transactions.forEach(function (t) {
      grouped[t.txid] = t;
    });
    latest.outpoints.forEach(function (o) {
      (grouped[o.txid] = grouped[o.txid] || { outputs: [] });
    });

    Object.keys(grouped).forEach(function (txid) {
      const t = grouped[txid];
      const outs = latest.outpoints.filter(function (o) { return o.txid === txid; });
      if (!outs.length) return;
      html += '<div class="panel"><h2>TX ' + Fmt.esc(Fmt.shortTxid(txid, 10)) + "</h2>";
      outs.forEach(function (o) {
        const state = Fmt.sourceLabel(o.classification_source) + (o.wallet_name ? " · " + o.wallet_name : "");
        html +=
          '<div class="output-card" data-txid="' + txid + '" data-vout="' + o.vout + '" data-address="' + Fmt.esc(o.address || '') + '">' +
          '  <div class="head">' +
          '    <div class="mono">Output ' + o.vout + " · " + Fmt.sats(o.value_sats) + " sats · " +
            (o.spend_status === "UNSPENT" ? '<span style="color:#3fb950">unspent</span>' : o.spend_status === "SPENT" ? '<span style="color:#8b949e">spent</span>' : '<span style="color:#d29922">unknown</span>') + "</div>" +
          '    <div class="hint mono">' + Fmt.esc(o.address || "no address") + "</div>" +
          "  </div>" +
          '  <div class="classify-bar">' +
          '    <span class="state">' + Fmt.esc(o.ownership_status || "UNKNOWN") + (state && state !== "UNKNOWN" ? " (" + Fmt.esc(state) + ")" : "") + "</span>" +
          '    <button class="btn small btn-own' + (o.ownership_status === "OWNED" ? " active" : "") + '" data-own="OWNED">' + (o.address ? 'Address owned' : 'Owned') + '</button>' +
          '    <button class="btn small btn-ext' + (o.ownership_status === "EXTERNAL" ? " active" : "") + '" data-own="EXTERNAL">External</button>' +
          '    <button class="btn small btn-unk' + (o.ownership_status === "UNKNOWN" ? " active" : "") + '" data-own="UNKNOWN">Unknown</button>' +
          // REG-1: key access applies to the address confirmed by "Address owned".
          (o.address ? '    <select data-own-key-access title="Key access for the confirmed address. Without key access it is not scanned and never counts in holdings; a later OWNED never resets it (change it in Known Addresses).">' +
            '<option value="key" selected>Key available (counts in holdings)</option>' +
            '<option value="none">No key access (documented only)</option></select>' : "") +
          "  </div>" +
          "</div>";
      });
      html += "</div>";
    });

    if (!latest.transactions.length) {
      html += '<div class="panel hint">No transactions in this trace yet.</div>';
    }

    el.innerHTML = html;
    el.querySelectorAll(".classify-bar .btn").forEach(function (b) {
      b.addEventListener("click", function () {
        const card = b.closest(".output-card");
        const txid = card.dataset.txid;
        const vout = Number(card.dataset.vout);
        const own = b.dataset.own;
        b.disabled = true;
        const payload = {
          ownership_status: own,
          classification_source: "USER",
          register_owned_address: own === "OWNED" && !!card.dataset.address,
          wallet_name: null,
          confidence: null,
        };
        const keyChoice = card.querySelector("[data-own-key-access]");
        if (payload.register_owned_address && keyChoice && keyChoice.value === "none") payload.key_access = false;
        API.classify(traceId, txid, vout, payload).then(function () {
          render(el, traceId);
        }).catch(function (err) {
          b.disabled = false;
          el.querySelector(".panel").insertAdjacentHTML("beforeend", '<div class="alert error">' + Fmt.esc(err.message) + "</div>");
        });
      });
    });
  }

  // -------------------------------------------------------------- Custody
  function renderCustody(el, full, traceId, custodyCtx) {
    const poss = full.possible_boundaries || [];
    const intro =
      '<div class="panel"><h2>Custodial boundaries</h2>' +
      '<p class="hint">Exact on-chain lineage is only provable up to the point funds enter a pooled custodial service. ' +
      "Custody is <b>never detected automatically</b>: multi-input, merged, batched or pooled-looking flows are stable facts of the <b>topology</b> and shown as <b>MIXED/AMBIGUOUS provenance</b> on the Graph and Timeline - they never produce custody suggestions by themselves. " +
      "To record custody you explicitly mark a deposit as a <b>custodial boundary</b> (exact tracing stops there), " +
      "then search for known-owned continuation outputs and <b>confirm a custody bridge</b> so tracing resumes from the withdrawal. " +
      "<b>Custody bridges are logical, user-confirmed links - never cryptographic proof.</b>" +
      " Declined suggestions are remembered and will not be re-offered until you reset the decision.</p>" +
      '<div class="kv" style="margin-bottom:8px">' +
      '  <div class="k">Detected</div><div>' + poss.length + "</div>" +
      '  <div class="k">Confirmed boundaries</div><div>' + full.custodial_boundaries.length + "</div>" +
      '  <div class="k">Confirmed bridges</div><div>' + full.custody_bridges.length + "</div>" +
      '  <div class="k">Declined</div><div>' + (full.dismissed_candidates || []).length + "</div>" +
      '  <div class="k">Ownership/custody conflicts</div><div>' +
        (full.ownership_custody_conflicts || []).filter(function (c) { return c.status === "PENDING"; }).length + "</div>" +
      "</div></div>";

    // ----- Boundaries already confirmed
    let html = intro +
      '<div class="panel"><h2>Confirmed boundaries</h2>' +
      '<table class="data"><thead><tr><th>Outpoint</th><th>Service</th><th>Note</th><th></th></tr></thead><tbody>';
    if (!full.custodial_boundaries.length) {
      html += '<tr><td colspan="4" class="hint">No custody boundaries confirmed yet.</td></tr>';
    }
    full.custodial_boundaries.forEach(function (b) {
      html += "<tr>" +
        '<td class="mono">' + Fmt.esc(Fmt.shortTxid(b.txid, 10)) + ":" + b.vout + "</td>" +
        "<td>" + Fmt.esc(b.service_name || "") + "</td>" +
        "<td>" + Fmt.esc(b.note || "") + "</td>" +
        '<td><button class="btn small danger" data-del-boundary="' + b.id + '">Delete</button></td>' +
        "</tr>";
    });
    html += "</tbody></table></div>";

    // ----- Ownership / custody conflicts (real-node discovery reviews)
    const occ = full.ownership_custody_conflicts || [];
    html += '<div class="panel"><h2>Ownership / custody conflicts</h2>' +
      '<p class="hint">Direct ownership evidence (XPUB_DISCOVERY / MANUAL_IMPORT / USER_CONFIRMED) now applies to an outpoint marked as a custodial deposit. Nothing is deleted automatically - keep the boundary, or remove only the custody classification. A resolved decision stays resolved.</p>' +
      '<table class="data"><thead><tr><th>Outpoint</th><th>Evidence</th><th>Service</th><th>Status</th><th></th></tr></thead><tbody>';
    if (!occ.length) {
      html += '<tr><td colspan="5" class="hint">No ownership/custody conflicts. An own address that is also a confirmed custodial deposit is the only signal that creates one.</td></tr>';
    }
    occ.forEach(function (c) {
      html += "<tr>" +
        '<td class="mono">' + Fmt.esc(Fmt.shortTxid(c.txid, 10)) + ":" + c.vout +
          '<div class="hint">' + Fmt.esc(Fmt.shortAddress(c.address)) + "</div></td>" +
        '<td>' + Fmt.esc(c.wallet_name || "") + " · " + Fmt.esc(c.evidence_source || "") + "</td>" +
        '<td>' + Fmt.esc(c.service_name || "") + "</td>" +
        '<td><span class="status-tag ' + (c.status === "PENDING" ? "NEEDS_REVIEW" : "COMPLETE") + '">' + Fmt.esc(c.status) + "</span></td>" +
        '<td class="cust-actions">' +
          (c.status === "PENDING"
            ? '<button class="btn small" data-occ-keep="' + c.id + '">Keep boundary</button> ' +
              '<button class="btn small danger" data-occ-remove="' + c.id + '">Remove custody only</button>'
            : "") +
          "</td>" +
        "</tr>";
    });
    html += "</tbody></table></div>";

    // ----- Possible boundaries detected by heuristics
    if (poss.length) {
      html += '<div class="panel"><h2>Detected · possible boundaries (suggestion)</h2>' +
        '<p class="hint">Heuristic signals (<b>' + poss.length +
        "</b>): never treated as fact. Review the reasons, then accept (stops exact lineage) or decline (remembered).</p>" +
        '<table class="data"><thead><tr><th>Outpoint</th><th>Confidence</th><th>Reasons</th><th></th></tr></thead><tbody>';
      poss.forEach(function (p) {
        const reasons = Fmt.esc((p.reasons || []).join(" · "));
        html += "<tr>" +
          '<td class="mono">' + Fmt.esc(Fmt.shortTxid(p.txid, 10)) + ":" + p.vout + "</td>" +
          '<td class="' + (p.confidence === "strong" ? "warn" : "") + '"><span class="cust-badge cust-badge-sug">' + Fmt.esc(p.confidence) + "</span></td>" +
          '<td><span class="hint" title="' + reasons + '">' + reasons + "</span></td>" +
          '<td class="cust-actions">' +
          '  <button class="btn small" data-possible-review=\'' + JSON.stringify({ txid: p.txid, vout: p.vout }) + "'>Review</button> " +
          '  <button class="btn small btn-own" data-possible-accept=\'' + JSON.stringify({ txid: p.txid, vout: p.vout }) + "'>Accept</button> " +
          '  <button class="btn small" data-possible-decline=\'' + JSON.stringify({ txid: p.txid, vout: p.vout, reasons: p.reasons || [], confidence: p.confidence }) + "'>Decline</button>" +
          "</td>" +
          "</tr>";
      });
      html += "</tbody></table></div>";
    } else {
      html += '<div class="panel"><h2>Detected · possible boundaries</h2>' +
        '<p class="hint">No heuristic custody detections right now. Batches, merges and multi-input flows are shown as MIXED/AMBIGUOUS provenance (Graph/Timeline) but are never flagged as custody automatically. Custody is only recorded here when you confirm a boundary manually.</p></div>';
    }

    // ----- Declined suggestions (user decisions, resettable)
    const declined = full.dismissed_candidates || [];
    html += '<div class="panel"><h2>Declined suggestions</h2>' +
      '<table class="data"><thead><tr><th>Outpoint</th><th>Confidence</th><th>Reasons</th><th></th></tr></thead><tbody>';
    if (!declined.length) {
      html += '<tr><td colspan="4" class="hint">No declined suggestions. A decline is remembered so the same candidate is not offered again on every re-check.</td></tr>';
    }
    declined.forEach(function (d) {
      const reasons = Fmt.esc((d.reasons || []).join(" · "));
      html += "<tr>" +
        '<td class="mono">' + Fmt.esc(Fmt.shortTxid(d.txid, 10)) + ":" + d.vout + "</td>" +
        '<td><span class="cust-badge cust-badge-dis">' + Fmt.esc(d.confidence) + "</span></td>" +
        '<td><span class="hint" title="' + reasons + '">' + reasons + "</span></td>" +
        '<td class="cust-actions">' +
        '  <button class="btn small" data-declined-review=\'' + JSON.stringify({ txid: d.txid, vout: d.vout }) + "'>Review</button> " +
        '  <button class="btn small" data-declined-reset=\'' + JSON.stringify({ txid: d.txid, vout: d.vout }) + "'>Reset decision</button>" +
        "</td>" +
        "</tr>";
    });
    html += "</tbody></table></div>";

    // ----- Mark boundary manually
    html += '<div class="panel"><h2>Mark a custodial deposit</h2>' +
      '<div class="row">' +
      '  <input id="cust-txid" placeholder="txid" class="mono" style="flex:3;min-width:220px">' +
      '  <input id="cust-vout" placeholder="vout" type="number" min="0" value="0" style="flex:1">' +
      '  <input id="cust-service" placeholder="service name (e.g. exchange)" style="flex:2">' +
      '  <button class="btn small" id="btn-mark-boundary">Mark boundary</button>' +
      "</div>" +
      '<div id="cust-boundary-msg"></div>' +
      "</div>";

    // ----- Bridges + candidate search per boundary
    html += '<div class="panel"><h2>Custody bridges</h2>';
    if (!full.custodial_boundaries.length) {
      html += '<p class="hint">Confirm a boundary first to search for continuation candidates.</p>';
    } else {
      html += '<table class="data"><thead><tr><th>From</th><th>To</th><th>Provenance</th><th>Score</th><th></th></tr></thead><tbody>';
      (full.custody_bridges || []).forEach(function (br) {
        html += "<tr>" +
          '<td class="mono">' + Fmt.esc(Fmt.shortTxid(br.boundary_txid || "", 8)) + ":" + (br.boundary_vout !== undefined && br.boundary_vout !== null ? br.boundary_vout : "?") + "</td>" +
          '<td class="mono">' + Fmt.esc(Fmt.shortTxid(br.destination_txid, 8)) + ":" + br.destination_vout + "</td>" +
          "<td>" + br.provenance + "</td>" +
          "<td>" + (br.confidence_score != null ? br.confidence_score : "—") + "</td>" +
          '<td><button class="btn small danger" data-del-bridge="' + br.id + '">Delete</button></td>' +
          "</tr>";
      });
      html += "</tbody></table>";
      html += '<div id="cust-candidates"></div>';
      html += full.custodial_boundaries.map(function (b) {
        return '<button class="btn small" data-find-candidates="' + b.id + '" data-outpoint="' + Fmt.esc(b.txid + ":" + b.vout) + '">Find candidates • ' + Fmt.esc(Fmt.shortTxid(b.txid, 8)) + ":" + b.vout + "</button>";
      }).join(" ");
    }
    html += "</div>";

    el.innerHTML = html;

    el.querySelector("#btn-mark-boundary").addEventListener("click", function () {
      const txid = el.querySelector("#cust-txid").value.trim();
      const vout = Number(el.querySelector("#cust-vout").value);
      const service = el.querySelector("#cust-service").value.trim();
      if (!txid) { el.querySelector("#cust-boundary-msg").innerHTML = '<div class="alert error">txid is required</div>'; return; }
      API.markCustodialBoundary(traceId, txid, vout, service, null).then(function () {
        render(el, traceId);
      }).catch(function (err) {
        el.querySelector("#cust-boundary-msg").innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>";
      });
    });

    el.querySelectorAll("[data-del-boundary]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (!confirm("Remove this custody boundary? Tracing resumes from here as unknown.")) return;
        API.deleteCustodialBoundary(traceId, Number(btn.dataset.delBoundary)).then(function () { render(el, traceId); })
          .catch(function (err) { el.querySelector("#cust-boundary-msg").innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>"; });
      });
    });

    const openWithCtx = function (txid, vout) {
      window.App.openOutpoint(txid, vout, custodyCtx ? custodyCtx() : null);
    };

    el.querySelectorAll("[data-possible-review]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const p = JSON.parse(btn.dataset.possibleReview);
        openWithCtx(p.txid, p.vout);
      });
    });

    el.querySelectorAll("[data-occ-keep], [data-occ-remove]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const resolution = btn.dataset.occKeep ? "KEEP_BOUNDARY" : "REMOVE_BOUNDARY";
        if (resolution === "REMOVE_BOUNDARY" &&
            !confirm("Remove only the custody classification? The boundary is deleted, but the UTXO, tx graph and wallet evidence stay; tracing resumes with the ownership evidence.")) return;
        btn.disabled = true;
        API.resolveOwnershipCustodyConflict(traceId, Number(btn.dataset.occKeep || btn.dataset.occRemove), resolution)
          .then(function () { render(el, traceId); })
          .catch(function (err) {
            btn.disabled = false;
            el.innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>";
          });
      });
    });

    el.querySelectorAll("[data-possible-accept]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const p = JSON.parse(btn.dataset.possibleAccept);
        btn.disabled = true;
        API.markCustodialBoundary(traceId, p.txid, p.vout, "", null).then(function () { render(el, traceId); })
          .catch(function (err) { btn.disabled = false; el.querySelector("#cust-boundary-msg").innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>"; });
      });
    });

    el.querySelectorAll("[data-possible-decline]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const p = JSON.parse(btn.dataset.possibleDecline);
        btn.disabled = true;
        API.declineCustodialSuggestion(traceId, p.txid, p.vout, {
          reasons: p.reasons || [],
          confidence: p.confidence || "likely",
        }).then(function () { render(el, traceId); })
          .catch(function (err) { btn.disabled = false; el.querySelector("#cust-boundary-msg").innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>"; });
      });
    });

    el.querySelectorAll("[data-declined-review]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const p = JSON.parse(btn.dataset.declinedReview);
        openWithCtx(p.txid, p.vout);
      });
    });

    el.querySelectorAll("[data-declined-reset]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const p = JSON.parse(btn.dataset.declinedReset);
        btn.disabled = true;
        API.resetCustodialSuggestion(traceId, p.txid, p.vout)
          .then(function () { render(el, traceId); })
          .catch(function (err) { btn.disabled = false; el.querySelector("#cust-boundary-msg").innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>"; });
      });
    });

    el.querySelectorAll("[data-del-bridge]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (!confirm("Delete this custody bridge? The on-chain trace data is NOT removed.")) return;
        API.deleteCustodyBridge(traceId, Number(btn.dataset.delBridge)).then(function () { render(el, traceId); })
          .catch(function (err) { el.querySelector("#cust-candidates").innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>"; });
      });
    });

    el.querySelectorAll("[data-find-candidates]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const boundaryId = Number(btn.dataset.findCandidates);
        const outpoint = btn.dataset.outpoint;
        const box = el.querySelector("#cust-candidates");
        box.innerHTML = '<div class="loading"><span class="spinner"></span>Searching registry + UTXO set…</div>';
        API.custodyCandidates(traceId, boundaryId).then(function (cands) {
          box.innerHTML = "<h3>Candidates after " + Fmt.esc(outpoint) + "</h3>" +
            '<p class="hint">Ranked suggestions (provenance <b>INFERRED</b>). Confirming one resumes exact tracing from that output.</p>' +
            '<table class="data"><thead><tr><th>Score</th><th>Outpoint</th><th>Amount</th><th>Address</th><th>Reasons</th><th></th></tr></thead><tbody>' +
            cands.map(function (c, i) {
              return "<tr>" +
                "<td>" + c.confidence_score + "</td>" +
                '<td class="mono">' + Fmt.esc(Fmt.shortTxid(c.txid, 10)) + ":" + c.vout + "</td>" +
                '<td class="amt">' + Fmt.sats(c.amount_sats) + "</td>" +
                '<td class="mono">' + Fmt.esc(Fmt.shortAddress(c.address)) + "</td>" +
                "<td><span class=\"hint\">" + Fmt.esc((c.reasons || []).join(" · ")) + "</span></td>" +
                '<td><button class="btn small btn-own" data-confirm-bridge="' + boundaryId + '" data-dtx="' + c.txid + '" data-dvout="' + c.vout + '" data-score="' + c.confidence_score + '">Confirm</button></td>' +
                "</tr>";
            }).join("") +
            (cands.length ? "" : '<tr><td colspan="6" class="hint">No continuation candidates found.</td></tr>') +
            "</tbody></table>";
          box.querySelectorAll("[data-confirm-bridge]").forEach(function (cb) {
            cb.addEventListener("click", function () {
              API.confirmCustodyBridge(traceId, Number(cb.dataset.confirmBridge), cb.dataset.dtx, Number(cb.dataset.dvout), Number(cb.dataset.score)).then(function () {
                render(el, traceId);
              }).catch(function (err) { box.insertAdjacentHTML("beforeend", '<div class="alert error">' + Fmt.esc(err.message) + "</div>"); });
            });
          });
        }).catch(function (err) {
          box.innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>";
        });
      });
    });
  }

  return { render: render, buildTimelineModel: buildTimelineModel, buildReverseTimelineModel: buildReverseTimelineModel, stopPolling: stopPolling, stop: stop };
})();

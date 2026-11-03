// Start screen: TXID + optional address + optional VOUT, analysis output.
window.StartPage = (function () {

  // Persistent address-discovery scan lifecycle (backend polls scanblocks):
  // the browser polls the durable job; while one is active a new scan is never
  // started and the app-owned scan can be stopped without deleting anything.
  var SCAN_POLL_MS = 2000;
  var pollTimer = null;
  var generation = 0;
  var state = { scanActive: false, job: null, mode: null, foreign: false };

  function isReverse(mode) {
    return mode === "REVERSE_PROVENANCE" || mode === "BLOCKCHAIN_ANALYSIS_REVERSE";
  }

  function isForwardAnalysis(mode) {
    return mode === "BLOCKCHAIN_ANALYSIS";
  }

  // Reverse modes and Blockchain Analysis — Forward accept a TXID OR a mainnet
  // Bitcoin address as the Start Trace input; Forward Provenance keeps the
  // TXID + optional address form.
  function supportsAddressDiscovery(mode) {
    return isReverse(mode) || isForwardAnalysis(mode);
  }

  // Lightweight mainnet-address shape check (base58 P2PKH "1…" / P2SH "3…",
  // bech32 "bc1…"). The backend scan endpoint remains the authoritative
  // validator; this only blocks obviously-malformed input with a clean message.
  function looksLikeMainnetAddress(s) {
    if (!s || typeof s !== "string") return false;
    if (/^bc1[023456789acdefghjklmnpqrstuvwxyz]{11,71}$/.test(s)) return true;
    if (/^[13][1-9A-HJ-NP-Za-km-z]{25,34}$/.test(s)) return true;
    return false;
  }

  function modeHintText(mode) {
    if (isForwardAnalysis(mode)) {
      return "Blockchain Analysis — Forward: enter a TXID (64 hex) to load its outputs, or a mainnet Bitcoin address to scan for candidate outputs.";
    }
    if (isReverse(mode)) {
      return "Reverse modes: enter a TXID (64 hex) to load its outputs, or a mainnet Bitcoin address to discover candidate outputs.";
    }
    return "Exactly one of address or VOUT is enough to start. With neither, you can pick outputs manually.";
  }

  function render(el, params) {
    stop();
    state = { scanActive: false, job: null, mode: null, foreign: false };
    el.innerHTML =
      '<h1>Bitcoin Lineage Tracker</h1>' +
      '<p class="hint">Reconstruct and visualize the on-chain path of a Bitcoin UTXO ' +
      'starting from a known acquisition or withdrawal transaction. ' +
      'Runs entirely against your local Bitcoin Core node.</p>' +
      '<div class="panel start-form">' +
      '  <label>Trace mode</label>' +
      '  <div class="mode-select">' +
      '    <label class="mode-opt"><input type="radio" name="trace-mode" value="FORWARD_PROVENANCE" checked /><span class="mode-title">Forward Provenance</span><span class="mode-desc">Follow your own coins forward through time.</span></label>' +
      '    <label class="mode-opt"><input type="radio" name="trace-mode" value="REVERSE_PROVENANCE" /><span class="mode-title">Reverse Provenance</span><span class="mode-desc">Trace the ancestry of your current UTXOs back to earlier owned coins, acquisitions, migrations, and consolidations. Starts from an exact TXID:VOUT.</span></label>' +
      '    <label class="mode-opt"><input type="radio" name="trace-mode" value="BLOCKCHAIN_ANALYSIS" /><span class="mode-title">Blockchain Analysis — Forward</span><span class="mode-desc">Investigate arbitrary transactions and outputs forward through spend relationships. Ownership is metadata only.</span></label>' +
      '    <label class="mode-opt"><input type="radio" name="trace-mode" value="BLOCKCHAIN_ANALYSIS_REVERSE" /><span class="mode-title">Blockchain Analysis — Reverse</span><span class="mode-desc">Investigate arbitrary UTXOs backward through transaction ancestry. Ownership is metadata only. Starts from an exact TXID:VOUT.</span></label>' +
      '  </div>' +
      '  <label id="lbl-txid">Transaction ID <span class="badge">required</span></label>' +
      '  <input type="text" id="in-txid" placeholder="ffff000011112222... (64 hex characters)" spellcheck="false" />' +
      '  <div id="addr-block">' +
      '    <label>Bitcoin Address <span style="color:#3fb950">(optional)</span> <span class="badge">auto-matches output</span></label>' +
      '    <input type="text" id="in-address" placeholder="bc1q..." spellcheck="false" />' +
      "  </div>" +
      '  <details class="advanced"><summary>Advanced</summary>' +
      '    <label>VOUT <span style="color:#3fb950">(optional)</span></label>' +
      '    <input type="number" id="in-vout" placeholder="1" min="0" style="width:160px" />' +
      '  </details>' +
      '  <div style="margin-top:18px">' +
      '    <button class="btn primary" id="btn-analyze">ANALYZE</button>' +
      '    <span style="margin-left:10px" class="hint" id="hint-analyze">Exactly one of address or VOUT is enough to start. With neither, you can pick outputs manually.</span>' +
      '  </div>' +
      '  <div id="addr-scan-status"></div>' +
      '</div>' +
      '<div id="analyze-result"></div>';

    const btn = el.querySelector("#btn-analyze");
    const txidEl = el.querySelector("#in-txid");
    const addrEl = el.querySelector("#in-address");
    const voutEl = el.querySelector("#in-vout");
    if (params && /^[0-9a-f]{64}$/i.test(params.get('txid') || '') &&
        /^\d+$/.test(params.get('vout') || '') && Number.isSafeInteger(Number(params.get('vout')))) {
      txidEl.value = params.get('txid');
      voutEl.value = params.get('vout');
      el.querySelector('details.advanced').open = true;
    }

    function selectedMode() {
      const r = el.querySelector('input[name="trace-mode"]:checked');
      return r ? r.value : "FORWARD_PROVENANCE";
    }

    function refreshModeUI() {
      const mode = selectedMode();
      const discovery = supportsAddressDiscovery(mode);
      const lbl = el.querySelector("#lbl-txid");
      const addrBlock = el.querySelector("#addr-block");
      const hint = el.querySelector("#hint-analyze");
      if (discovery) {
        lbl.innerHTML = 'Transaction ID or Bitcoin address <span class="badge">required</span>';
        txidEl.placeholder = "ffff000011112222… or bc1q… (64 hex or a mainnet address)";
        if (addrBlock) addrBlock.style.display = "none";
        hint.textContent = modeHintText(mode);
      } else {
        lbl.innerHTML = 'Transaction ID <span class="badge">required</span>';
        txidEl.placeholder = "ffff000011112222... (64 hex characters)";
        if (addrBlock) addrBlock.style.display = "";
        hint.textContent = modeHintText(mode);
      }
    }

    function doAnalyze() {
      const txid = txidEl.value.trim();
      const address = addrEl.value.trim();
      const vout = voutEl.value === "" ? null : Number(voutEl.value);
      const mode = selectedMode();
      if (isReverse(mode)) {
        if (state.scanActive && state.job) {
          renderScanRunning(el, state.job);
          return;
        }
        if (!/^[0-9a-fA-F]{64}$/.test(txid)) {
          beginAddressScan(el, txid || address, mode);
          return;
        }
      } else if (isForwardAnalysis(mode)) {
        // Blockchain Analysis — Forward accepts either a TXID (64 hex) to load
        // its outputs or a mainnet Bitcoin address to start the durable
        // discovery lifecycle. Malformed input fails cleanly here before any
        // scan starts (the backend remains the authority on address validity).
        if (state.scanActive && state.job) {
          renderScanRunning(el, state.job);
          return;
        }
        if (!/^[0-9a-fA-F]{64}$/.test(txid)) {
          const candidate = txid || address;
          if (!looksLikeMainnetAddress(candidate)) {
            showError(el, "Enter a 64-hex TXID or a mainnet Bitcoin address.");
            return;
          }
          beginAddressScan(el, candidate, mode);
          return;
        }
      }
      if (!/^[0-9a-fA-F]{64}$/.test(txid)) {
        showError(el, "The supplied transaction ID is invalid. Expected 64 hex characters.");
        return;
      }
      const out = el.querySelector("#analyze-result");
      out.innerHTML = '<div class="loading"><span class="spinner"></span>Loading transaction from Bitcoin Core…</div>';
      API.analyze(txid, supportsAddressDiscovery(mode) ? null : address, vout).then(function (res) {
        renderAnalyzed(el, res, selectedMode());
      }).catch(function (err) {
        showError(el, err.message);
      });
    }

    btn.addEventListener("click", doAnalyze);
    txidEl.addEventListener("keydown", function (e) { if (e.key === "Enter") doAnalyze(); });
    txidEl.addEventListener("input", function () { clearStaleScanUI(el); });
    addrEl.addEventListener("input", function () { clearStaleScanUI(el); });
    el.querySelectorAll('input[name="trace-mode"]').forEach(function (r) {
      r.addEventListener("change", function () { clearStaleScanUI(el); refreshModeUI(); });
    });
    refreshModeUI();

    // Browser reconnect / revisit: resume an in-flight scan so a page reload
    // never starts a duplicate Core scan. A completed/historical job is never
    // re-hydrated as current UI state (the backend returns the active job,
    // foreign-scan info, or nothing - terminal jobs are not returned).
    checkActiveScan(el);
  }

  function showError(el, msg) {
    const out = el.querySelector("#analyze-result");
    out.innerHTML = '<div class="alert error">' + Fmt.esc(sanitizeError(msg)) + "</div>";
  }

  function currentMode(el) {
    const r = el.querySelector('input[name="trace-mode"]:checked');
    return r ? r.value : "FORWARD_PROVENANCE";
  }

  // Short user-facing error text only; a Python traceback in a JSON "message"
  // is never rendered raw into the page.
  function sanitizeError(msg) {
    if (typeof msg !== "string" || msg === "") {
      return "The address scan failed. See the container logs for details.";
    }
    if (/Traceback|  File "|line \d+, in /.test(msg)) {
      return "The address scan failed. See the container logs for details.";
    }
    return msg;
  }

  function resetModeHint(el, mode) {
    const hint = el.querySelector("#hint-analyze");
    if (!hint) return;
    hint.textContent = modeHintText(mode);
  }

  function updateScanButtonStates(el) {
    const btn = el.querySelector("#btn-analyze");
    if (btn) { btn.disabled = !!state.scanActive; btn.classList.toggle("disabled", !!state.scanActive); }
    const hint = el.querySelector("#hint-analyze");
    if (hint && state.scanActive) {
      hint.textContent = "An address scan is already running — it will be shown above. Wait for it to finish before starting another.";
    }
  }

  function stopScanPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // Called by the router when leaving the view: late scan responses are
  // dropped and never re-arm the poller.
  function stop() {
    generation++;
    stopScanPolling();
  }

  // Terminal discovery jobs are historical, not current UI state: when the user
  // starts a new interaction (new address/TXID, mode change, another scan) an
  // old completion banner / candidate list must not remain on the page. Only
  // genuinely active scan UI (QUEUED/RUNNING) is preserved across reconnections.
  function clearStaleScanUI(el) {
    if (!state.job) return;
    var jstate = state.job.state;
    if (jstate !== "COMPLETE" && jstate !== "CANCELLED" && jstate !== "FAILED") return;
    stopScanPolling();
    state.job = null;
    state.scanActive = false;
    var st = el.querySelector("#addr-scan-status");
    if (st) st.innerHTML = "";
    var out = el.querySelector("#analyze-result");
    if (out) out.innerHTML = "";
    updateScanButtonStates(el);
  }

  function renderScanRunning(el, job) {
    const st = el.querySelector("#addr-scan-status");
    if (!st) return;
    const p = job.progress || {};
    const pct = (typeof p.progress === "number" && !isNaN(p.progress)) ? Math.round(p.progress) : null;
    let range = "";
    if (p.from_height != null && p.to_height != null) {
      range = " · Block " + (p.current_height == null ? "?" : p.current_height) + " / " + p.to_height;
    }
    st.innerHTML =
      '<div class="panel scan-status">' +
      '  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">' +
      '    <span class="spinner"></span>' +
      '    <strong>Address scan running…</strong>' +
      (pct !== null ? '<span class="status-tag IN_PROGRESS">' + pct + "%</span>" : "") +
      '    <span class="hint mono">' + Fmt.esc(job.address || "") + Fmt.esc(range) + "</span>" +
      '    <button class="btn small" id="btn-scan-stop">Stop scan</button>' +
      "  </div>" +
      "</div>";
    const stop = st.querySelector("#btn-scan-stop");
    if (stop) stop.addEventListener("click", function () { stopScan(el); });
  }

  function renderForeignScan(el, status) {
    // A Core-wide scan we did NOT start. We never offer Stop Scan for a foreign
    // scan and never associate an address with it (neutral-language handling).
    const out = el.querySelector("#analyze-result");
    const st = el.querySelector("#addr-scan-status");
    const core = status.core_scan || {};
    const pct = (typeof core.progress === "number" && !isNaN(core.progress)) ? Math.round(core.progress) : null;
    const msg = status.foreign_scan_message || "Bitcoin Core already has a block scan in progress.";
    let html = '<div class="alert info">' + Fmt.esc(msg) +
      (pct !== null ? " (" + pct + "%)" : "") + "</div>";
    if (st) st.innerHTML = html;
    if (out) {
      out.innerHTML = '<div class="alert info">No new scan was started, so no addresses were associated with the in-progress scan. Wait for it to finish, then try again.</div>';
    }
  }

  function renderTerminalScan(el, job) {
    stopScanPolling();
    const st = el.querySelector("#addr-scan-status");
    if (!st) return;
    const mode = currentMode(el);
    resetModeHint(el, mode);
    if (job.state === "COMPLETE") {
      // v0.6.5: neutral completion messaging. The scan lifecycle is unchanged;
      // a finished scan never instructs an artificial mode-switch workflow (the
      // user may already be in a reverse mode). Candidates, when found, reuse
      // the existing candidate-output UI.
      const outputs = job.result && job.result.outputs && job.result.outputs.length ? job.result.outputs : [];
      if (outputs.length) {
        st.innerHTML = '<div class="alert info">Address scan complete' +
          (job.address ? " for <b>" + Fmt.esc(job.address) + "</b>" : "") + ".</div>";
        renderAddressCandidates(el, job.result, mode);
      } else {
        st.innerHTML = '<div class="panel"><h2 style="margin:0">No outputs found</h2>' +
          '<p class="hint">Address scan complete. No on-chain output paying to <b>' + Fmt.esc(job.address) + "</b> was found.</p></div>";
      }
    } else if (job.state === "CANCELLED") {
      st.innerHTML = '<div class="alert info">' + Fmt.esc(sanitizeError(job.error_summary || "Scan stopped.")) + "</div>";
    } else {
      st.innerHTML = '<div class="alert error">' + Fmt.esc(sanitizeError(job.error_summary || "The address scan failed.")) + "</div>";
    }
  }

  function handleScanJob(el, job) {
    state.job = job;
    const active = job.state === "QUEUED" || job.state === "RUNNING";
    state.scanActive = active;
    updateScanButtonStates(el);
    if (active) {
      renderScanRunning(el, job);
      startScanPolling(el);
    } else {
      renderTerminalScan(el, job);
    }
  }

  function startScanPolling(el) {
    stopScanPolling();
    var token = generation;
    pollTimer = setInterval(function () {
      if (!state.job) return;
      API.addressScanJob(state.job.id).then(function (job) {
        if (token !== generation) return;
        handleScanJob(el, job);
      }).catch(function () {
        if (token !== generation) return;
        // Transient backend / Core unreachability: keep the running panel and
        // retry on the next tick; never surface a scary failure mid-scan.
        const st = el.querySelector("#addr-scan-status");
        if (st && state.scanActive) {
          if (!st.querySelector(".scan-transient")) {
            st.querySelector(".scan-status").insertAdjacentHTML("beforeend",
              '<div class="hint scan-transient">Waiting for Bitcoin Core…</div>');
          }
        }
      });
    }, SCAN_POLL_MS);
  }

  function stopScan(el) {
    if (!state.job) return;
    API.addressScanAbort(state.job.id).then(function (job) {
      state.job = job;
      state.scanActive = false;
      updateScanButtonStates(el);
      renderTerminalScan(el, job);
    }).catch(function (err) {
      const st = el.querySelector("#addr-scan-status");
      if (st) st.innerHTML = '<div class="alert error">' + Fmt.esc(sanitizeError(err.message)) + "</div>";
    });
  }

  // Start the persistent address-discovery scan. A duplicate start under an
  // active job resumes the running scan instead of issuing a second Core
  // scanblocks (the backend also protects against duplicates).
  function beginAddressScan(el, input, mode) {
    clearStaleScanUI(el);
    const out = el.querySelector("#analyze-result");
    if (state.scanActive && state.job) {
      renderScanRunning(el, state.job);
      return;
    }
    state.mode = mode;
    state.foreign = false;
    out.innerHTML = '<div class="loading"><span class="spinner"></span>Starting local address scan…</div>';
    var token = generation;
    API.addressScanStart(input).then(function (res) {
      if (token !== generation) return;
      if (res.foreign_scan) {
        state.foreign = true;
        renderForeignScan(el, res);
      } else if (res.job) {
        handleScanJob(el, res.job);
      } else {
        showError(el, "The scan returned no status.");
      }
    }).catch(function (err) {
      if (token !== generation) return;
      showError(el, err.message);
    });
  }

  // Browser reconnect / revisit: resume a durable active scan (QUEUED/RUNNING),
  // surface foreign-scan / no-scan states. Terminal COMPLETE/CANCELLED/FAILED
  // jobs returned by the reconnect endpoint are defensive-fallback ignored: a
  // fresh Start Trace page always starts visually clean.
  function checkActiveScan(el) {
    var token = generation;
    API.addressScanActive().then(function (res) {
      if (token !== generation) return;
      if (res.foreign_scan) {
        state.foreign = true;
        renderForeignScan(el, res);
      } else if (res.job && (res.job.state === "QUEUED" || res.job.state === "RUNNING")) {
        // Reconnect hydrates ONLY genuinely active scans: terminal
        // COMPLETE/CANCELLED/FAILED jobs are historical, so a fresh Start Trace
        // page starts clean instead of restoring an old completion banner or
        // candidate list.
        handleScanJob(el, res.job);
      }
    }).catch(function () { /* no scan info available; ignore silently on page load */ });
  }

  function renderAddressCandidates(el, res, mode) {
    const out = el.querySelector("#analyze-result");
    const outputs = res.outputs || [];
    const forward = isForwardAnalysis(mode);
    const sourceLabel = res.discovery_source === "electrs"
      ? "local Electrs index"
      : "Bitcoin Core block filter scan";
    if (!outputs.length) {
      out.innerHTML =
        '<div class="panel">' +
        '  <h2 style="margin:0">No outputs found</h2>' +
        '  <p class="hint">No on-chain output paying to <b>' + Fmt.esc(res.address) +
        "</b> was found via " + sourceLabel + ".</p>" +
        "</div>";
      return;
    }

    // Forward Blockchain Analysis prefers SPENT candidates on top; ordering is
    // otherwise untouched (stable sort, discovery result semantics unchanged).
    const ordered = forward
      ? outputs.slice().sort(function (a, b) {
          const sa = a.spend_status === "SPENT" ? 0 : 1;
          const sb = b.spend_status === "SPENT" ? 0 : 1;
          return sa - sb;
        })
      : outputs;
    const instruction = forward
      ? "Choose exactly one target TXID:VOUT. Forward analysis follows spending transactions from the selected output."
      : "Choose exactly one target TXID:VOUT. Reverse modes trace the ancestry of a specific target outpoint.";
    const createLabel = forward ? "Trace forward from selected target" : "Trace ancestry from selected target";
    const unspentNote = forward && outputs.some(function (o) { return o.spend_status !== "SPENT"; })
      ? '<p class="hint">Unspent outputs are valid targets: tracing one completes as a terminal UTXO (no forward spending path).</p>'
      : "";

    let cards = "";
    ordered.forEach(function (o) {
      const isUnspent = o.spend_status === "UNSPENT";
      const dateStr = o.block_height === null || o.block_height === undefined
        ? "unconfirmed"
        : Fmt.date(o.block_time);
      const heightStr = o.block_height === null || o.block_height === undefined ? "—" : o.block_height;
      cards +=
        '<div class="output-card" data-outpoint="' + Fmt.esc(o.outpoint) + '">' +
        '  <div class="head">' +
        '    <div>' +
        '      <div class="mono"><strong>' + Fmt.esc(o.outpoint) + "</strong> " +
        '        <span class="status-tag ' + (isUnspent ? "COMPLETE" : "SPENT") + '">' +
        (isUnspent ? "UNSPENT" : "SPENT") + "</span></div>" +
        '      <div class="hint">' + Fmt.btc(o.value_sats) + " BTC (" + Fmt.sats(o.value_sats) + " sats)</div>" +
        '      <div class="mono" style="margin-top:4px">Block ' + heightStr + " · " + dateStr + "</div>" +
        "    </div>" +
        '    <button class="btn primary small track-btn" data-outpoint="' + Fmt.esc(o.outpoint) + '">Choose target</button>' +
        "  </div>" +
        "</div>";
    });

    out.innerHTML =
      '<div class="panel">' +
      '  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px">' +
      '    <h2 style="margin:0">Outputs paying to ' + Fmt.esc(Fmt.shortAddress(res.address)) + "</h2>" +
      '    <span class="status-tag IN_PROGRESS">' + sourceLabel + "</span>" +
      '    <button class="btn small" id="btn-addr-create">' + createLabel + "</button>" +
      "  </div>" +
      '  <h3 style="margin-top:6px">' + instruction + "</h3>" +
      unspentNote +
      '  <div id="output-list">' + cards + "</div>" +
      '  <div class="sel-outputs" style="margin-top:10px">' +
      '    <button class="btn small" id="btn-addr-clear">Clear</button>' +
      "  </div>" +
      "</div>";

    const selected = new Set();
    if (outputs.length === 1) selected.add(outputs[0].outpoint);

    function refreshButtons() {
      out.querySelectorAll(".track-btn").forEach(function (b) {
        const k = b.dataset.outpoint;
        if (selected.has(k)) { b.classList.add("active"); b.textContent = "Selected target"; }
        else { b.classList.remove("active"); b.textContent = "Choose target"; }
      });
    }
    refreshButtons();

    out.querySelectorAll(".track-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        const k = b.dataset.outpoint;
        if (selected.has(k)) { selected.delete(k); }
        else { selected.clear(); selected.add(k); }
        refreshButtons();
      });
    });
    out.querySelector("#btn-addr-clear").addEventListener("click", function () {
      selected.clear();
      refreshButtons();
    });
    out.querySelector("#btn-addr-create").addEventListener("click", function () {
      if (selected.size !== 1) {
        out.querySelector(".panel").insertAdjacentHTML("beforeend",
          '<div class="alert warn">' + (forward
            ? "Forward analysis starts from exactly one target output — select one and try again."
            : "Reverse traces start from exactly one target output — select one and try again.") + "</div>");
        return;
      }
      const parts = Array.from(selected)[0].split(":");
      API.createTrace({
        name: "",
        start_txid: parts[0],
        start_address: null,
        start_vout: Number(parts[1]),
        trace_mode: mode,
      }).then(function (trace) {
        window.App.openTrace(trace.id);
      }).catch(function (err) { showError(el, err.message); });
    });
  }

  function outputCard(out, matched, label) {
    const state = out.spend_status === "UNSPENT" ? '<span class="st" style="color:#3fb950">UNSPENT</span>'
      : out.spend_status === "SPENT" ? '<span class="st" style="color:#8b949e">SPENT</span>'
      : '<span class="st" style="color:#d29922">UNKNOWN</span>';
    return (
      '<div class="output-card" data-vout="' + out.vout + '">' +
      '  <div class="head">' +
      '    <div>' +
      '      <div class="mono"><strong>Output ' + out.vout + '</strong> ' + state + (matched ? ' <span class="status-tag COMPLETE">matched</span>' : "") + "</div>" +
      '      <div class="hint">' + Fmt.esc(out.address || "no address") + "</div>" +
      '      <div class="mono" style="margin-top:4px">' + Fmt.btc(out.value_sats) + " BTC (" + Fmt.sats(out.value_sats) + " sats) " +
      '        <span class="badge">' + Fmt.esc(out.script_type || "unknown") + "</span></div>" +
      '    </div>' +
      '    <button class="btn primary small track-btn" data-vout="' + out.vout + '">' + label + "</button>" +
      '  </div>' +
      "</div>"
    );
  }

  function renderAnalyzed(el, res, mode) {
    const tx = res.transaction;
    const out = el.querySelector("#analyze-result");
    if (!tx) { showError(el, "Analysis returned no transaction data."); return; }
    const matched = res.matched_vout !== null && res.matched_vout !== undefined ? res.matched_vout : null;
    const isAnalysis = mode === "BLOCKCHAIN_ANALYSIS";
    const isReverse = mode === "REVERSE_PROVENANCE" || mode === "BLOCKCHAIN_ANALYSIS_REVERSE";
    const singleLabel = isAnalysis ? "Investigate this output" : isReverse ? "Set as target" : "Trace this output";
    const selectLabel = isAnalysis ? "Investigate" : isReverse ? "Choose target" : "Track this output";
    const createLabel = isAnalysis ? "Investigate selected output(s)"
      : isReverse ? "Trace ancestry from selected target"
      : "Start trace from selected output(s)";
    const heading = isAnalysis
      ? "Outputs — choose the output(s) to investigate (ownership is display-only)"
      : isReverse
        ? "Outputs — choose exactly one target output. Reverse modes trace the ancestry of a specific TXID:VOUT."
        : "Outputs — choose the one(s) that belong to you";

    let cards = "";
    (tx.outputs || []).forEach(function (o) {
      cards += outputCard(o, o.vout === matched, singleLabel);
    });

    out.innerHTML =
      '<div class="panel">' +
      '  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px">' +
      '    <h2 style="margin:0">Transaction ' + Fmt.esc(Fmt.shortTxid(tx.txid, 10)) + "</h2>" +
      '    <span class="status-tag ' + (tx.confirmations > 0 ? "COMPLETE" : "IN_PROGRESS") + '">' +
          (tx.confirmations > 0 ? "confirmed" : "unconfirmed") + "</span>" +
      '    <button class="btn small" id="btn-create-trace">' + createLabel + "</button>" +
      '  </div>' +
      '  <div class="kv mono" style="font-size:12.5px">' +
      '    <div class="k">TXID</div><div class="v">' + Fmt.esc(tx.txid) + '</div>' +
      '    <div class="k">Block height</div><div class="v">' + (tx.block_height ?? "—") + '</div>' +
      '    <div class="k">Block time</div><div class="v">' + Fmt.date(tx.block_time) + '</div>' +
      '    <div class="k">Confirmations</div><div class="v">' + tx.confirmations + '</div>' +
      '    <div class="k">Inputs</div><div class="v">' + ((tx.inputs || []).length) + '</div>' +
      '    <div class="k">Outputs</div><div class="v">' + ((tx.outputs || []).length) + '</div>' +
      '    <div class="k">Output total</div><div class="v">' + Fmt.sats(tx.output_total_sats) + ' sats</div>' +
      '    <div class="k">Input total</div><div class="v">' + (tx.input_total_sats != null ? Fmt.sats(tx.input_total_sats) + " sats" : "n/a") + '</div>' +
      '    <div class="k">Fee</div><div class="v">' + (tx.fee_sats != null ? Fmt.sats(tx.fee_sats) + " sats" : "n/a") + '</div>' +
      '    <div class="k">VSIZE</div><div class="v">' + (tx.vsize ?? "n/a") + '</div>' +
      '    <div class="k">Fee rate</div><div class="v">' + (tx.fee_rate != null ? tx.fee_rate + " sat/vB" : "n/a") + '</div>' +
      '  </div>' +
      '  <h3 style="margin-top:16px">' + heading + "</h3>" +
      '  <div id="output-list">' + cards + "</div>" +
      '  <div class="sel-outputs" style="margin-top:10px">' +
      '    <button class="btn small" id="btn-select-all">Select all</button>' +
      '    <button class="btn small" id="btn-clear">Clear</button>' +
      '  </div>' +
      '</div>';

    let selected = new Set();
    if (matched !== null) selected.add(matched);

    function refreshButtons() {
      out.querySelectorAll(".track-btn").forEach(function (b) {
        const v = Number(b.dataset.vout);
        if (selected.has(v)) { b.classList.add("active"); b.textContent = "Selected"; }
        else { b.classList.remove("active"); b.textContent = selectLabel; }
      });
    }
    refreshButtons();

    out.querySelectorAll(".track-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        const v = Number(b.dataset.vout);
        if (selected.has(v)) selected.delete(v); else selected.add(v);
        refreshButtons();
      });
    });
    out.querySelector("#btn-select-all").addEventListener("click", function () {
      (tx.outputs || []).forEach(function (o) { selected.add(o.vout); });
      refreshButtons();
    });
    out.querySelector("#btn-clear").addEventListener("click", function () {
      selected.clear();
      refreshButtons();
    });

    out.querySelector("#btn-create-trace").addEventListener("click", function () {
      // Reverse modes require EXACTLY one target outpoint: the exact start
      // VOUT makes the reverse walk concrete (the backend rejects a missing /
      // invalid / unspendable VOUT). A multi-selection is meaningless for a
      // reverse trace, so it is refused here.
      if (isReverse) {
        if (selected.size !== 1) {
          out.querySelector(".panel").insertAdjacentHTML("beforeend",
            '<div class="alert warn">Reverse traces start from exactly one target output — select one (or clear your selection) and try again.</div>');
          return;
        }
        API.createTrace({
          name: "",
          start_txid: tx.txid,
          start_address: null,
          start_vout: Array.from(selected)[0],
          trace_mode: mode,
        }).then(function (trace) {
          window.App.openTrace(trace.id);
        }).catch(function (err) { showError(el, err.message); });
        return;
      }
      // Blockchain Analysis has no ownership classification: a single
      // selection pins the start VOUT, anything else starts the analysis on
      // the whole transaction. It never calls the forward "select outputs"
      // endpoint (which would write OWNED rows).
      if (isAnalysis) {
        const start_vout = selected.size === 1 ? Array.from(selected)[0] : null;
        API.createTrace({
          name: "",
          start_txid: tx.txid,
          start_address: null,
          start_vout: start_vout,
          trace_mode: "BLOCKCHAIN_ANALYSIS",
        }).then(function (trace) {
          window.App.openTrace(trace.id);
        }).catch(function (err) { showError(el, err.message); });
        return;
      }
      if (selected.size === 0) {
        out.querySelector(".panel").insertAdjacentHTML("beforeend",
          '<div class="alert warn">Please select at least one output to trace.</div>');
        return;
      }
      const payload = {
        name: "",
        start_txid: tx.txid,
        start_address: null,
        start_vout: selected.size === 1 ? Array.from(selected)[0] : null,
      };
      if (payload.start_vout === null && selected.size > 0) {
        // Multi-output selection: create trace first without vout, then select.
        API.createTrace({ name: "", start_txid: tx.txid, start_address: null, start_vout: null })
          .then(function (trace) {
            return API.selectOutputs(trace.id, tx.txid, Array.from(selected)).then(function () { return trace; });
          })
          .then(function (trace) {
            window.App.openTrace(trace.id);
          })
          .catch(function (err) { showError(el, err.message); });
        return;
      }
      API.createTrace(payload).then(function (trace) {
        window.App.openTrace(trace.id);
      }).catch(function (err) { showError(el, err.message); });
    });
  }

  function openTxDetail(txid) {
    window.App.openTransaction(txid);
  }

  return { render: render, stop: stop };
})();

// Node status / health screen.
// Status semantics (REQ-UMB-021/022):
//   - Informational + correct  -> check mark (pass)
//   - Genuine warning          -> warning symbol (e.g. index still syncing)
//   - Error                    -> cross symbol  (e.g. unreachable / mismatch)
window.StatusPage = (function () {
  // The one place for the minimum Bitcoin Core requirement (also used by the
  // B4 getting-started checklist on the dashboard).
  var CORE_REQUIREMENT_HTML = 'Bitcoin Core 31.1 or newer with <code>txindex=1</code> and <code>txospenderindex=1</code>.';

  function checkSymbol(ok) {
    if (ok === true) return '<span class="status-icon ok">&#10003;</span>';
    if (ok === false) return '<span class="status-icon err">&#10007;</span>';
    return '<span class="status-icon warn">&#9888;</span>';
  }

  // pass: true -> check, false -> warning/cross, null/undefined -> info-neutral
  function row(label, value, ok) {
    const tri = ok === undefined ? value : ok;
    const sym = tri === true ? checkSymbol(true) : tri === false ? checkSymbol(false) : checkSymbol(null);
    return '<div class="status-row"><div class="status-row-head">' + sym + "<span>" + Fmt.esc(label) +
      ":</span> <span class=\"mono\">" + Fmt.esc(String(value)) + "</span></div></div>";
  }

  function progressBar(p) {
    if (p == null) return "";
    const pct = Math.max(0, Math.min(100, Math.round(p * 100)));
    return '<div class="progress"><div class="progress-fill" style="width:' + pct + '%"></div></div>';
  }

  function indexRow(s, idx) {
    const progress = s[idx.name + "_progress"];
    const indexed = s[idx.name + "_indexed_height"];
    const target = s[idx.name + "_target_height"];
    const synced = idx.synced === true;

    if (synced) {
      return row(idx.name + " synced", "yes", true);
    }
    let detail = "still synchronizing";
    if (indexed != null && target != null) {
      detail = indexed + " / " + target + " (" +
        (progress != null ? Math.round(progress * 100) : 0) + " %)";
    }
    return '<div class="status-row"><div class="status-row-head">' + checkSymbol(null) +
      "<span>" + Fmt.esc(idx.name) + ":</span> <span class=\"mono\">" +
      Fmt.esc(detail) + "</span></div>" +
      progressBar(progress) + "</div>";
  }

  function render(el) {
    el.innerHTML = '<div class="loading"><span class="spinner"></span>Checking Bitcoin Core…</div>';
    // Settings are optional context: a failing /api/settings must not hide the
    // node status, so both requests settle independently.
    Promise.allSettled([API.health(), API.settings()]).then(function (results) {
      if (results[0].status === "rejected") throw results[0].reason;
      const s = results[0].value;
      const settings = (results[1].status === "fulfilled" && results[1].value) || {};
      let rows = "";
      let overall = null;

      if (s.reachable === true) {
        rows += row("RPC reachable", "yes", true);
        rows += row("Bitcoin Core version", s.core_version + (s.subversion ? " (" + s.subversion + ")" : ""), true);
        if (s.network_match === true) {
          rows += row("Network", s.network + " (expected match)", true);
        } else if (s.network_match === false) {
          rows += row("Network", s.network + " (MISMATCH, expected " + s.expected_network + ")", false);
        } else {
          rows += row("Network", s.network, true);
        }
        if (s.blockchain_synced === true) {
          rows += row("Blockchain synced", "yes", true);
        } else {
          rows += row("Blockchain synced", "no – initial block download", null);
        }
        rows += row("Block height", s.block_height, true);
        rows += row("txindex synced", s.txindex_synced === true ? "yes" : "no", s.txindex_synced === true);
        (s.indexes || []).forEach(function (idx) {
          if (idx.name === "txindex") return;
          rows += indexRow(s, idx);
        });

        const readiness = s.network_match !== false && s.blockchain_synced &&
          s.txindex_synced === true && s.txospenderindex_synced === true;
        overall = readiness;
      } else {
        overall = "error";
        rows += row("RPC reachable", "no", false);
        rows += row("Last error", s.rpc_error || "unknown", null);
      }

      const settingsHtml = Object.keys(settings).length
        ? '<div class="panel"><h2>Application Settings</h2>' +
          '<div class="kv mono" style="font-size:12.5px">' +
          Object.keys(settings).map(function (k) {
            return '<div class="k">' + Fmt.esc(k) + "</div><div class=\"v\">" + Fmt.esc(settings[k]) + "</div>";
          }).join("") +
          "</div></div>"
        : "";

      let alertHtml;
      if (overall === true) {
        alertHtml = '<div class="alert ok">All checks passed. Bitcoin Core is ready for tracing.</div>';
      } else if (overall === "error") {
        alertHtml = '<div class="alert error">Bitcoin Core is currently unavailable. The app keeps running and retries on each request.</div>';
      } else {
        alertHtml = '<div class="alert warn">Bitcoin Core is reachable but not fully ready. Some tracing features may be blocked.</div>';
      }

      el.innerHTML =
        '<h1>Node Status</h1>' +
        alertHtml +
        '<div class="panel"><h2>Bitcoin Core</h2>' +
        '<div class="status-list">' + rows + "</div></div>" +
        settingsHtml +
        '<div class="panel">' +
        '  <h2>Requirements</h2>' +
        '  <ul class="hint" style="margin:0;padding-left:20px;line-height:1.9">' +
        '    <li>' + CORE_REQUIREMENT_HTML + '</li>' +
        '    <li>Tracing that needs historical spender info is blocked until the txospenderindex has finished syncing.</li>' +
        '    <li>No external blockchain APIs are used.</li>' +
        '  </ul>' +
        "</div>";
    }).catch(function (err) {
      el.innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>";
    });
  }

  return { render: render, CORE_REQUIREMENT_HTML: CORE_REQUIREMENT_HTML };
})();
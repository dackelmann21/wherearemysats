// Independent snapshot + the shared provenance report. GET never starts a scan.
window.InventoryPage = (function () {
  var root = null, report = null, epoch = 0, timer = null, controller = null;
  var wallet = "", provenance = "";
  var coverageLabels = { COVERED: 'Covered', MIXED: 'Mixed', UNCOVERED: 'No acquisition (verified)', UNRESOLVED: 'Needs Review' };
  function esc(value) { return Fmt.esc(String(value == null ? "—" : value)); }
  function evidenceHistory(u) {
    var history = u.historical_wallet_evidence || [];
    if (!history.length) return '';
    return '<details><summary>Historical evidence</summary>' + history.map(function (e) {
      return '<div>' + esc(e.wallet_name) + ' · ' + esc(e.source) +
        (e.derivation_path ? ' · ' + esc(e.derivation_path) : '') + '</div>';
    }).join('') + '</details>';
  }
  function stop() {
    ++epoch; clearTimeout(timer);
    if (controller) controller.abort();
    controller = null; root = null;
  }
  function table() {
    var items = report.utxos.filter(function (u) {
      return (!provenance || u.coverage_status === provenance) &&
        (!wallet || u.wallet_matches.some(function (m) { return m.wallet_id === wallet; }));
    });
    var html = '<p>' + items.length + ' matching UTXOs · ' + Fmt.btc(items.reduce(function (n, u) { return n + u.amount_sats; }, 0)) + ' BTC</p>' +
      '<div style="overflow-x:auto"><table class="acq-table"><thead><tr><th>Wallet / derivation</th><th>TXID:VOUT</th>' +
      '<th>BTC</th><th>Address / script</th><th>Block / confirmations</th><th>Acquisition provenance</th><th>Actions</th></tr></thead><tbody>';
    items.forEach(function (u) {
      html += '<tr><td>' + (u.wallet_matches.map(function (m) {
        return esc(m.wallet_label) + (m.derivation_path ? '<br><small>' + esc(m.derivation_path) + '</small>' : '');
      }).join('<br>') || 'Assignment unresolved') + evidenceHistory(u) + '</td><td class="mono">' + esc(u.outpoint) + '</td><td>' + Fmt.btc(u.amount_sats) +
        '</td><td class="mono">' + esc(u.address || u.script_pubkey) + '</td><td>' + esc(u.block_height) + ' / ' + esc(u.confirmations) +
        '<br><small>' + esc(u.block_time == null ? null : new Date(u.block_time * 1000).toISOString()) + '</small></td><td>' +
        '<strong>' + esc(coverageLabels[u.coverage_status]) + '</strong><br><small>' +
        esc(u.acquisition_origin_count) + ' Acquisition origins · ' + esc(u.no_acquisition_origin_count) +
        ' No-Acquisition origins · ' + esc(u.unresolved_origin_count) + ' unresolved origins</small><br>Review: ' + esc(u.review_status) +
        '<br><small>Acquisitions: ' + esc(u.acquisition_ids.join(', ') || '—') +
        '</small><br><small>Ownership: ' + esc(u.ownership_status) + '</small></td><td>' +
        '<button class="btn small" data-output="' + esc(u.outpoint) + '">Review lineage</button> ' +
        '<button class="btn small" data-reverse="' + esc(u.outpoint) + '">Trace ancestry</button></td></tr>';
    });
    if (!items.length) html += '<tr><td colspan="7">No matching current UTXOs in the scanned inventory.</td></tr>';
    root.querySelector('#inventory-rows').innerHTML = html + '</tbody></table></div>';
    root.querySelectorAll('[data-output]').forEach(function (b) {
      b.onclick = function () { var k = b.dataset.output.split(':'); App.openOutpoint(k[0], Number(k[1])); };
    });
    root.querySelectorAll('[data-reverse]').forEach(function (b) {
      b.onclick = function () {
        var k = b.dataset.reverse.split(':'), generation = epoch;
        b.disabled = true;
        API.createTrace({ name: '', start_txid: k[0], start_vout: Number(k[1]), trace_mode: 'REVERSE_PROVENANCE' }).then(function (t) {
          if (generation === epoch) App.openTrace(t.id);
        }).catch(function (err) { if (generation === epoch) { b.disabled = false; root.querySelector('#inventory-message').textContent = err.message; } });
      };
    });
  }
  function show() {
    var meta = report.inventory;
    root.querySelector('#inventory-summary').innerHTML =
      '<p>Current managed BTC: <strong>' + Fmt.btc(report.current_managed_sats) + '</strong> · ' + report.utxo_count + ' UTXOs · all current wallets</p>' +
      '<p>' + Object.keys(coverageLabels).map(function (c) {
        var b = report.coverage_totals[c];
        return coverageLabels[c] + ': ' + b.utxo_count + ' UTXOs · ' + Fmt.btc(b.amount_sats) + ' BTC';
      }).join(' | ') + '</p><p class="hint">Whole current UTXO values by lineage coverage; no per-origin sat allocation. No acquisition (verified): all origins are reviewed No-Acquisition anchors, not a deficiency. Needs Review: unresolved chain.</p>' +
      '<p>Documented — not in current wallets: <strong>' + Fmt.btc(report.documented_not_in_current_wallet_sats) + '</strong> BTC · ' +
      report.documented_not_in_current_wallet_utxo_count + ' UTXOs · excluded from Current managed BTC</p>' +
      '<p>Provenance-linked BTC: ' + Fmt.btc(report.provenance_linked_sats) + ' · Path coverage: ' +
      (report.provenance_coverage_percentage_bps == null ? '— (empty inventory)' : (report.provenance_coverage_percentage_bps / 100).toFixed(2) + '%') + '</p>' +
      '<p class="hint">' + esc(meta.scope) + '</p><p>Scanned addresses: ' + meta.scanned_address_count + ' / ' + meta.configured_address_count +
      ' · Oldest snapshot: ' + esc(meta.oldest_scan_at) + '</p>' +
      (meta.refresh_required ? '<div class="alert warn">Inventory refresh required. These totals cover only the available snapshot, not all configured addresses.</div>' : '') +
      '<details><summary>Discovery limits</summary>' + meta.discoveries.map(function (d) {
        var p = d.scan_policy;
        return '<p>' + esc(d.wallet_name) + ' · ' + esc(d.script_type) + ' · blocks ' + esc(d.start_height) + '–' + esc(d.end_height) +
          ' · gap ' + d.gap_limit + ' · receive/change scanned through ' + esc(p.branch0_scanned_through) + ' / ' + esc(p.branch1_scanned_through) +
          (p.limited ? ' · Derivation limit reached' : '') + (p.utxo_only ? ' · UTXO-only discovery' : '') + '</p>';
      }).join('') + '</details>';
    var select = root.querySelector('#inventory-wallet');
    var wallets = meta.wallets.concat(report.utxos.reduce(function (a, u) {
      return a.concat(u.wallet_matches.map(function (m) { return m.wallet_id; }));
    }, [])).filter(function (w, i, a) { return a.indexOf(w) === i; }).sort();
    select.innerHTML = '<option value="">All wallets</option>' + wallets.map(function (w) { return '<option value="' + esc(w) + '">' + esc(w) + '</option>'; }).join('');
    if (wallets.indexOf(wallet) === -1) wallet = '';
    select.value = wallet;
    showOutsidePositions();
    table();
  }
  function showOutsidePositions() {
    var positions = report.acquisition_wallet_positions || [];
    var html = '<h3>Documented — not in current wallets</h3>' +
      '<p class="hint">These Acquisition-linked on-chain positions are verified unspent, but are not found in the current configured wallet inventory. ' +
      'This does not establish private-key loss, a sale or a custody change. The relevant wallet may not have been imported yet. ' +
      'Documentation quality is unchanged. Values count each outpoint once, without assigning exact sats to individual Acquisitions after a merge. ' +
      'This separate section is not affected by the current-wallet table filters.</p>';
    if (!report.acquisition_wallet_positions_complete) {
      html += '<p class="alert warn">Some Acquisition positions remain unresolved. This is the verified subtotal, not a complete total for unresolved positions.</p>';
    }
    var outputs = report.documented_not_in_current_wallet_utxos || [];
    html += '<div style="overflow-x:auto"><table class="acq-table"><thead><tr><th>State</th><th>TXID:VOUT</th><th>BTC</th>' +
      '<th>Node status</th><th>Acquisition provenance</th><th>Review</th></tr></thead><tbody>';
    outputs.forEach(function (u) {
      var traceIds = positions.filter(function (p) { return u.acquisition_ids.indexOf(p.acquisition_id) !== -1; })
        .map(function (p) { return p.trace_id; }).filter(function (id, i, all) { return id != null && all.indexOf(id) === i; });
      html += '<tr><td><span class="position-badge">Not in current wallets</span></td><td class="mono">' + esc(u.outpoint) +
        '<br><small>' + esc(u.address || u.script_pubkey) + '</small></td><td>' + Fmt.btc(u.amount_sats) +
        '</td><td>' + esc(u.spend_status) + ' · ' + esc(u.confirmations) + ' confirmations</td><td>' +
        Fmt.acquisitionBadge(u.acquisition_provenance) + '<br>Acquisitions: ' + esc(u.acquisition_ids.join(', ')) +
        evidenceHistory(u) + '</td><td><button class="btn small" data-output="' + esc(u.outpoint) + '">Output details</button> ' +
        traceIds.map(function (id) { return '<button class="btn small" data-position-trace="' + id + '">View trace #' + id + '</button>'; }).join(' ') + '</td></tr>';
    });
    if (!outputs.length) html += '<tr><td colspan="6">No verified documented positions outside current wallets.</td></tr>';
    html += '</tbody></table></div>';
    var unresolved = positions.filter(function (p) { return p.state === 'NEEDS_REVIEW'; });
    if (unresolved.length) {
      html += '<details><summary>Unresolved Acquisition positions (' + unresolved.length + ')</summary>' + unresolved.map(function (p) {
        return '<p>Acquisition #' + p.acquisition_id + ': ' + esc(p.reasons.join(' ') || 'Current wallet representation could not be established.') +
          (p.trace_id == null ? '' : ' <button class="btn small" data-position-trace="' + p.trace_id + '">View trace</button>') + '</p>';
      }).join('') + '</details>';
    }
    html += '<button class="btn" id="inventory-review-acquisitions">Review Acquisitions</button>';
    root.querySelector('#inventory-outside').innerHTML = html;
    root.querySelector('#inventory-review-acquisitions').onclick = function () { App.showView('acquisitions'); };
    root.querySelectorAll('[data-position-trace]').forEach(function (b) {
      b.onclick = function () { App.openTrace(Number(b.dataset.positionTrace)); };
    });
  }
  function reload() {
    if (!root) return;
    if (controller) controller.abort();
    var generation = epoch, c = controller = new AbortController();
    var timeout = setTimeout(function () { c.abort(); }, 45000);
    API.inventory(c.signal).then(function (r) {
      if (generation === epoch && c === controller) { report = r; show(); }
    }).catch(function (err) {
      if (generation === epoch && c === controller) root.querySelector('#inventory-summary').textContent = 'Inventory unavailable. ' + (err.message || 'Request timed out; retry.');
    }).finally(function () { clearTimeout(timeout); });
  }
  function status(s) {
    root.querySelector('#inventory-message').textContent = s.message || s.state;
    root.querySelector('#inventory-refresh').disabled = s.state === 'RUNNING';
    if (s.state === 'RUNNING') {
      var generation = epoch;
      timer = setTimeout(function () {
        API.inventoryRefreshStatus().then(function (next) {
          if (generation !== epoch) return;
          status(next); if (next.state === 'COMPLETE') reload();
        }).catch(function (err) {
          if (generation === epoch) { root.querySelector('#inventory-message').textContent = err.message; root.querySelector('#inventory-refresh').disabled = false; }
        });
      }, 1500);
    }
  }
  function render(el) {
    stop(); root = el; report = null;
    root.innerHTML = '<div class="panel"><h2>Wallet Inventory</h2>' +
      '<p>Update saved wallet observations and discover additional addresses here. <a href="/#current-wallet">Open Current Wallet</a> for the saved holdings report and combined filters.</p>' +
      '<p class="hint">Wallet match, ownership and Acquisition provenance are separate facts. Updating known scripts does not discover every possible wallet address.</p>' +
      '<button class="btn primary" id="inventory-refresh">Update known wallet scripts</button> ' +
      '<button class="btn" id="inventory-reload">Reload provenance checks</button> ' +
      '<button class="btn" id="inventory-discovery">Discover wallet / new addresses</button> ' +
      '<button class="btn" id="inventory-export">Export filtered inventory CSV</button>' +
      '<p id="inventory-message" aria-live="polite"></p><div id="inventory-summary">Loading inventory…</div>' +
      '<p class="hint">PARTIAL retains mixed documented/undocumented ancestry. Path coverage counts the whole value of each linked UTXO once; it is not a documented-sat fraction. ' +
      'Use Trace ancestry to inspect historical outputs and explicitly select the appropriate Acquisition anchor. Scans never create Acquisitions.</p>' +
      '<h3>Current wallet UTXOs</h3><label>Wallet <select id="inventory-wallet"></select></label> <label>Provenance <select id="inventory-provenance">' +
      [''].concat(Object.keys(coverageLabels)).map(function (p) { return '<option value="' + p + '">' + (coverageLabels[p] || 'ALL') + '</option>'; }).join('') +
      '</select></label><div id="inventory-rows"></div></div><div class="panel" id="inventory-outside"></div>';
    root.querySelector('#inventory-provenance').value = provenance;
    root.querySelector('#inventory-provenance').onchange = function () { provenance = this.value; if (report) table(); };
    root.querySelector('#inventory-wallet').onchange = function () { wallet = this.value; if (report) table(); };
    root.querySelector('#inventory-reload').onclick = reload;
    root.querySelector('#inventory-discovery').onclick = function () { App.showView('registry'); };
    root.querySelector('#inventory-refresh').onclick = function () {
      var generation = epoch; this.disabled = true;
      API.inventoryRefresh().then(function (s) { if (generation === epoch) status(s); }).catch(function (err) {
        if (generation === epoch) { root.querySelector('#inventory-message').textContent = err.message; root.querySelector('#inventory-refresh').disabled = false; }
      });
    };
    root.querySelector('#inventory-export').onclick = function () {
      var generation = epoch;
      var params = [];
      if (provenance) params.push('coverage_status=' + encodeURIComponent(provenance));
      if (wallet) params.push('wallet=' + encodeURIComponent(wallet));
      API.inventoryCsv(params.length ? '?' + params.join('&') : '').then(function (blob) {
        var url = URL.createObjectURL(blob), a = document.createElement('a');
        a.href = url; a.download = 'wallet-inventory.csv'; a.click(); setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      }).catch(function (err) { if (generation === epoch) root.querySelector('#inventory-message').textContent = err.message; });
    };
    var generation = epoch;
    API.inventoryRefreshStatus().then(function (s) { if (generation === epoch) status(s); }).catch(function () {});
    reload();
  }
  return { render: render, stop: stop, reload: reload };
})();

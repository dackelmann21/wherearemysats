// Shared report API owns classification, membership, counts and filters.
window.ReportsPage = (function () {
  var root, view, params, controller, generation = 0, dashboard = null;
  var exportJob = null, exportTimer = null, exportGeneration = 0;
  var titles = { dashboard: 'Dashboard', 'current-wallet': 'Current Wallet' };
  var datasets = { 'current-wallet': 'current-wallet' };
  var labels = { COVERED: 'Covered', MIXED: 'Mixed', UNCOVERED: 'No acquisition (verified)', UNRESOLVED: 'Needs Review',
    CURRENT: 'Current', NEEDS_REVIEW: 'Needs Review', PARTIALLY_DISPOSED: 'Partial', DISPOSED: 'Disposed', TRACING: 'Tracing',
    ACQUISITION_NOT_IN_CURRENT_WALLET: 'Not in current wallets (overlapping)',
    acquisition_records: 'All Acquisition records', no_acquisition_anchors: 'All saved No-Acquisition origins',
    known_current_acquisition_references: 'Acquisition records linked to current UTXOs',
    known_current_anchor_references: 'No-Acquisition origins linked to current UTXOs',
    unresolved_current_origins: 'Current UTXOs with unresolved origin', CURRENT_UTXO: 'Current UTXO reviews', ACQUISITION: 'Acquisition reviews' };
  function esc(v) { return Fmt.esc(v == null ? '' : String(v)); }
  function href(target, values) {
    var q = new URLSearchParams(values || {});
    if (params && params.has('wallet')) q.set('wallet', params.get('wallet'));
    return '/#' + target + (q.toString() ? '?' + q.toString() : '');
  }
  function link(url, text) { return '<a href="' + esc(url) + '">' + esc(text) + '</a>'; }
  function stop() { generation++; if (controller) controller.abort(); controller = null; }
  function filterSelect(key, title, choices) {
    return '<label>' + title + '<select data-report-filter="' + key + '"><option value="">All</option>' + choices.map(function (c) {
      return '<option value="' + c[0] + '"' + (params.get(key) === c[0] ? ' selected' : '') + '>' + c[1] + '</option>';
    }).join('') + '</select></label>';
  }
  function filtersHtml() {
    if (view === 'current-wallet') return '<div class="report-filters">' +
      filterSelect('coverage', 'Coverage', [['COVERED', 'Covered'], ['MIXED', 'Mixed'], ['UNCOVERED', labels.UNCOVERED], ['UNRESOLVED', 'Needs Review']]) +
      filterSelect('origin_type', 'Contains origin type', [['ACQUISITION', 'Acquisition'], ['NO_ACQUISITION', 'No Acquisition'], ['UNRESOLVED', 'Unresolved']]) +
      '<details class="report-advanced"' + (params.has('provenance_class') ? ' open' : '') + '><summary>Additional classification filter' + (params.has('provenance_class') ? ' · active' : '') + '</summary>' +
      filterSelect('provenance_class', 'Exclusive provenance class', ['ACQUISITION', 'NO_ACQUISITION', 'MIXED', 'UNRESOLVED'].map(function (c) { return [c, c]; })) +
      '<p class="hint">Contains origin type includes known references in Mixed and Unresolved UTXOs. All selected filters combine.</p></details></div>';
    if (view === 'records') return filterSelect('position', 'Acquisition position / membership',
      ['CURRENT', 'NEEDS_REVIEW', 'PARTIALLY_DISPOSED', 'DISPOSED', 'TRACING', 'ACQUISITION_NOT_IN_CURRENT_WALLET'].map(function (c) { return [c, labels[c]]; }));
    return '';
  }
  function help() {
    return '<details class="panel"><summary>How to read these reports</summary>' +
      '<p>No acquisition (verified) means all origins are reviewed No-Acquisition anchors; this is not a deficiency or a lineage error. Needs Review means an unresolved chain: required evidence or origin review remains incomplete. Mixed means Acquisition and No-Acquisition ancestry; it gives no sat shares.</p>' +
      '<p>Origin type is overlapping membership: Acquisition and No Acquisition include Mixed and Unresolved with a known reference. Coverage, class and wallet filters combine with AND. Full UTXO values and all known origins remain visible.</p>' +
      '<p>Acquisition positions count records. Not in current wallets is an overlapping location filter, not Sold or Disposed. Tracing remains a separate state. Several review types may concern related evidence; their counts are not independent problem totals.</p>' +
      '<p>Reports read the last saved Inventory and cached blockchain observations; opening this page does not scan or call Bitcoin Core. Data as of is the oldest saved scan, not the page-load time. ' +
      link('/#inventory', 'Open Wallet Inventory to refresh receipts and mempool spends') + '. Finite scans do not prove complete wallet discovery. Shared wallet totals are not additive.</p></details>';
  }
  function stamp(r) {
    return '<span>Saved wallet data as of: ' + esc(r.data_as_of_utc || 'No saved scan') + '</span>' +
      '<span class="report-data-note">' + (r.stale ? 'Inventory update required. ' : '') +
      link('/#inventory', 'Update wallet observations') + ' to check for new receipts and spends.</span>' +
      '<details class="report-revision"><summary>Report revision</summary><code>' + esc(r.snapshot_id) + '</code></details>';
  }
  function metricHtml(m) {
    var units = { UTXO: 'UTXOs', ACQUISITION_RECORD: 'records', ACQUISITION_ID: 'IDs', ANCHOR_OUTPOINT: 'anchors', CURRENT_UTXO: 'UTXOs', ACQUISITION: 'records' };
    return '<p>' + link(m.detail_path, (labels[m.metric_key] || m.metric_key) + ': ' + m.count + ' ' + (units[m.unit] || m.unit)) +
      (m.value_btc != null ? '<br><span class="report-amount">' + esc(m.value_btc) + ' BTC</span>' : '') + '</p>';
  }
  function cards(r) {
    var cards = [['holdings', 'Current Self-Custody'], ['coverage', 'Current Wallet Coverage'], ['attention', 'Needs Attention'],
      ['origins', 'Origins'], ['positions', 'Acquisition Positions']];
    return '<div class="dashboard-grid">' + cards.map(function (c) {
      return '<section class="panel dashboard-card dashboard-' + c[0] + '"><h2>' + c[1] + '</h2>' + r.metrics.filter(function (m) { return m.dimension === c[0]; }).map(function (m) {
        if (c[0] === 'holdings') return '<p class="report-amount">' + esc(m.value_btc) + ' BTC</p><p>' +
          link(m.detail_path, m.count + ' current UTXOs · Open Current Wallet') + '</p>' +
          (m.count === 0 ? '<p>' + link('/#registry', 'Configure known wallet addresses') + '</p>' : '');
        return metricHtml(m);
      }).join('') + (c[0] === 'attention' ? '<ul>' + r.review_preview.map(function (q) {
        return '<li>' + link(q.detail_path, reviewTitle(q)) + '<br>' + reviewDetail(q) + '</li>';
      }).join('') + '</ul>' : '') +
      (c[0] === 'coverage' ? '<p>Each bucket counts whole UTXOs and their full values, without origin allocation.</p>' : '') +
      (c[0] === 'positions' ? '<p>Counts are Acquisition records. Not in current wallets overlaps the primary states.</p>' : '') + '</section>';
    }).join('') + '</div>';
  }
  function outputAction(key) { return '<button class="btn small" data-report-output="' + esc(key) + '">Open output / origin review</button>'; }
  function shortOutpoint(key) {
    var parts = String(key).split(':');
    return parts[0].slice(0, 10) + '…' + parts[0].slice(-6) + ':' + parts[1];
  }
  // FIX-UI-7 point 4: an Acquisition review names its first open movement
  // (the backend omits the generic review sentence when movements exist);
  // remaining reasons, and entries without movement data, render as before.
  function reviewDetail(q) {
    var parts = q.reasons && q.reasons.length ? [esc(q.reasons.join(' '))] : [];
    if (q.open_movement) {
      var line = Fmt.openMovementText(q.open_movement);
      if (q.open_movement_count > 1) line += ' + ' + (q.open_movement_count - 1) + ' more';
      parts.push(esc(line));
    }
    return parts.join('<br>');
  }
  function reviewTitle(r) {
    return r.review_item_type === 'CURRENT_UTXO' ? 'Review UTXO origins · ' + shortOutpoint(r.object_id) :
      r.review_item_type === 'ACQUISITION' ? 'Review Acquisition #' + r.object_id : r.review_item_type + ' · ' + r.object_id;
  }
  function originDetails(r) {
    return '<div data-lineage-review></div>' +
      '<p>Known lineage references, not exact sat shares.</p>';
  }
  function mountLineage(target, r, openOutput) {
    var origins = r.origins || [], selected = origins.some(function (o) { return o.review_status === 'UNRESOLVED'; }) ? 'needs_review' : 'all';
    var annotations = {};
    (r.anchors || []).forEach(function (a) { annotations[a.outpoint] = a; });
    var groups = {
      all: origins,
      needs_review: origins.filter(function (o) { return o.review_status === 'UNRESOLVED'; }),
      reviewed: origins.filter(function (o) { return o.review_status === 'REVIEWED'; })
    };
    function draw() {
      target.innerHTML = '<h3>Lineage origins</h3><div role="group" aria-label="Origin review filter">' +
        [['all', 'All'], ['needs_review', 'Needs Review'], ['reviewed', 'Reviewed']].map(function (f) {
          return '<button class="btn small" data-lineage-filter="' + f[0] + '" aria-pressed="' + (selected === f[0]) + '">' + f[1] + ' (' + groups[f[0]].length + ')</button>';
        }).join(' ') + '</div>' +
        (groups.needs_review.length === 0 ? '<p>No open origin reviews.' +
          (r.review_status === 'UNRESOLVED' ? ' Other evidence still requires review: ' + esc((r.unresolved_reasons || []).join(' ') || 'Report evidence is incomplete; reload the saved report.') : '') + '</p>' : '') +
        (groups[selected].length ? groups[selected].map(function (o) {
          return '<p>' + esc(o.origin_resolution.type) + ' · ' + esc(o.outpoint) +
            (o.origin_resolution.acquisition_id != null ? '<br>' + link('/#records?acquisition_id=' + encodeURIComponent(o.origin_resolution.acquisition_id), 'Acquisition #' + o.origin_resolution.acquisition_id) : '') +
            (o.origin_resolution.comment ? '<br>' + esc(o.origin_resolution.comment) : '') +
            (annotations[o.outpoint] && annotations[o.outpoint].source ? '<br>Source: ' + esc(annotations[o.outpoint].source) : '') +
            (annotations[o.outpoint] && annotations[o.outpoint].note ? '<br>Note: ' + esc(annotations[o.outpoint].note) : '') +
            '<br>Acquisition review: ' + esc(o.acquisition_review_status || 'Unavailable') + ' · Ownership review: ' + esc(o.ownership_review_status || 'Unavailable') +
            (o.review_reasons || []).map(function (reason) { return '<br>' + esc(reason.dimension + ': ' + reason.reason); }).join('') +
            '<br><button class="btn small" data-prov-output="' + esc(o.outpoint) + '" data-origin-output="' + esc(o.outpoint) + '">Open output / origin review</button></p>';
        }).join('') : '<p>No origins in this filter.</p>');
      target.querySelectorAll('[data-lineage-filter]').forEach(function (b) { b.addEventListener('click', function () { selected = b.dataset.lineageFilter; draw(); }); });
      target.querySelectorAll('[data-origin-output]').forEach(function (b) { b.addEventListener('click', function () { var k = b.dataset.originOutput.split(':'); openOutput(k[0], Number(k[1])); }); });
    }
    draw();
  }
  function wireOutputs(target) {
    target.querySelectorAll('[data-report-output]').forEach(function (b) { b.addEventListener('click', function () {
      var k = b.dataset.reportOutput.split(':'); App.openOutpoint(k[0], Number(k[1]));
    }); });
    // The Output ID opens the same output / origin review; a link to this
    // filtered view would show the unchanged row and look dead (BUG-UI-1).
    target.querySelectorAll('[data-report-output-id]').forEach(function (b) { b.addEventListener('click', function () {
      var k = b.dataset.reportOutputId.split(':'); App.openOutpoint(k[0], Number(k[1]));
    }); });
  }
  function row(r) {
    if (view === 'current-wallet') return '<div class="acq-row report-wallet-row" role="row">' +
      '<div role="cell">' + esc((r.wallet_matches || []).map(function (w) { return w.wallet_label; }).filter(function (v, i, a) { return a.indexOf(v) === i; }).join(', ') || r.address || 'No label') + '</div>' +
      '<div class="mono report-wallet-amount" role="cell">' + esc(Fmt.btc(r.amount_sats)) + ' BTC</div>' +
      '<div class="mono report-wallet-output" role="cell"><button type="button" class="link-button" data-report-output-id="' + esc(r.outpoint) + '" title="Open output / origin review">' + esc(r.outpoint) + '</button></div>' +
      '<div role="cell"><span class="report-coverage report-coverage-' + esc(r.coverage_status) + '">' + esc(labels[r.coverage_status]) + '</span></div>' +
      '<div role="cell"><details data-load-origins="' + esc(r.outpoint) + '"><summary>Known origins: ' + (r.known_acquisition_count == null ? r.acquisition_ids.length : r.known_acquisition_count) +
      ' Acquisition IDs · ' + (r.known_no_acquisition_anchor_count == null ? r.no_acquisition_anchor_outpoints.length : r.known_no_acquisition_anchor_count) +
      ' No-Acquisition origins</summary><div class="report-origin-scroll"></div></details>' +
      '<details class="report-technical"><summary>Output identifier &amp; classification</summary><p class="mono">' + esc(r.outpoint) + '</p><p>Class: ' + esc(r.provenance_class) + ' · Review: ' + esc(r.review_status) + '</p><p>Class describes the resolved origin composition. Review remains unresolved when required Acquisition, ownership or other evidence is incomplete. Current ownership and transaction topology are separate.</p>' +
      (r.unresolved_reasons.length ? '<p class="report-review-reason">' + esc(r.unresolved_reasons.join(' ')) + '</p>' : '') + '</details></div>' +
      '<div class="acq-cell-actions" role="cell">' + outputAction(r.outpoint) + '</div></div>';
    return '';
  }
  function load() {
    var token = ++generation;
    if (controller) controller.abort();
    controller = new AbortController();
    var target = root.querySelector('#report-body');
    target.innerHTML = '<p role="status">Loading report…</p>';
    var q = new URLSearchParams(params.toString());
    if (view !== 'dashboard') q.set('dataset', datasets[view]);
    API.reportRequest((view === 'dashboard' ? '/api/dashboard' : '/api/reports/view') + '?' + q.toString(), 'GET', undefined, controller.signal).then(function (r) {
      if (token !== generation) return;
      root.querySelector('#report-stamp').innerHTML = stamp(r);
      var scopeSelect = root.querySelector('#report-wallet');
      scopeSelect.innerHTML = '<option value="">All wallets</option>' + r.wallets.map(function (w) { return '<option value="wallet:' + esc(w) + '">' + esc(w || '(unlabelled)') + '</option>'; }).join('');
      scopeSelect.value = params.has('wallet') ? 'wallet:' + params.get('wallet') : '';
      if (view === 'dashboard') {
        dashboard = r;
        target.innerHTML = cards(r);
      } else {
        target.innerHTML = '<p>' + r.total + ' ' + (view === 'current-wallet' ? 'current UTXOs · full value of displayed UTXOs (no origin allocation): ' + Fmt.btc(r.value_sats) + ' BTC' : 'records / objects') + '</p>' +
          (r.records.length ? '<div class="acq-list-wrap report-wallet-table-wrap" tabindex="0" role="region" aria-label="Current UTXOs table"><div class="acq-grid-table report-wallet-table" role="table" aria-label="Current UTXOs"><div class="acq-table-head" role="row">' +
            ['Wallet', 'Full BTC amount', 'Output ID (txid:vout)', 'Coverage', 'Origins & classification', 'Actions'].map(function (title) { return '<span role="columnheader">' + title + '</span>'; }).join('') +
            '</div>' + r.records.map(row).join('') + '</div></div>' : '<p>No matching records in this scope.</p>') + '<nav aria-label="Report pagination">' +
          (r.offset > 0 ? link(pageLink(Math.max(0, r.offset - r.limit)), 'Previous page') : '') + ' ' +
          (r.offset + r.limit < r.total ? link(pageLink(r.offset + r.limit), 'Next page') : '') + '</nav>';
        wireOutputs(target);
        target.querySelectorAll('[data-load-origins]').forEach(function (details) {
          var loaded = false, busy = false, panel = details.querySelector('.report-origin-scroll');
          function origins() {
            if (loaded || busy || !details.open) return;
            busy = true; panel.textContent = 'Loading origin details…';
            var query = new URLSearchParams({ dataset: 'current-wallet', outpoint: details.dataset.loadOrigins });
            if (params.has('wallet')) query.set('wallet', params.get('wallet'));
            API.reportRequest('/api/reports/view?' + query.toString(), 'GET', undefined, controller.signal).then(function (detail) {
              if (token !== generation) return;
              if (detail.snapshot_id !== r.snapshot_id || detail.records.length !== 1) throw new Error('Revision changed. Refresh the list before loading origins.');
              panel.innerHTML = originDetails(detail.records[0]); wireOutputs(panel);
              mountLineage(panel.querySelector('[data-lineage-review]'), detail.records[0], App.openOutpoint);
              loaded = true; busy = false;
            }).catch(function (e) {
              if (token !== generation) return;
              busy = false; panel.innerHTML = '<p role="alert">' + esc(e.message) + '</p><button class="btn small">Retry origin details</button>';
              panel.querySelector('button').addEventListener('click', origins);
            });
          }
          details.addEventListener('toggle', origins);
        });
      }
    }).catch(function (e) {
      if (token !== generation) return;
      root.querySelector('#report-stamp').textContent = 'Unavailable — no replacement balance';
      target.innerHTML = '<p role="alert">' + esc(e.message) + '</p><button class="btn" id="report-retry">Retry</button>';
      target.querySelector('#report-retry').addEventListener('click', load);
    });
  }
  // B4: getting-started checklist above the dashboard cards. Loaded next to
  // the report, never before it; a failure only affects this panel.
  var GS_KEY = 'wams.gettingStarted.hidden', gsToken = 0;
  // Same wording as the ownership help in the output dialog (FIX-OWN-1b).
  var OWNED_SCOPE = 'Mark as owned only for your own on-chain wallets. Do not mark outputs from a Lightning wallet, ' +
    'an exchange or another service as owned; record them where they enter as an acquisition or ' +
    'no-acquisition origin.';
  var gsTitles = { node: 'Node connected', blockchain: 'Blockchain synced', txindex: 'txindex enabled and synced',
    txospenderindex: 'txospenderindex enabled and synced', wallet: 'Wallet added', acquisition: 'Acquisition recorded',
    exit: 'Exchange exit recorded' };
  var gsStates = { done: 'Done', todo: 'To do', in_progress: 'In progress', unknown: 'Unknown' };
  var NOT_CONNECTED = '<p>Unknown until the node is connected.</p>';
  function gsHidden() {
    try { return window.localStorage.getItem(GS_KEY) === '1'; } catch (e) { return false; }
  }
  function gsRemember(hidden) {
    try {
      if (hidden) window.localStorage.setItem(GS_KEY, '1'); else window.localStorage.removeItem(GS_KEY);
    } catch (e) { /* no browser storage: the choice lasts for this page only */ }
  }
  function gsIndexNotes() {
    return '<p>Requirement: ' + window.StatusPage.CORE_REQUIREMENT_HTML + '</p>' +
      '<p>Optional, for historical wallet discovery: <code>blockfilterindex=basic</code>.</p>' +
      "<p>On Umbrel or Start9, set these options in the Bitcoin node app's settings or custom configuration.</p>";
  }
  function gsIndexDetail(s, notes) {
    if (s.state === 'unknown') return NOT_CONNECTED;
    if (s.state === 'done') return '<p>Enabled and synced.</p>';
    if (s.state === 'in_progress') return '<p>Indexing: ' +
      (s.progress != null ? (s.progress * 100).toFixed(1) + '% (block ' + esc(s.indexed_height) + '/' + esc(s.target_height) + ')' : 'progress not reported yet') +
      '. Tracing with historical spends is complete only after the index is synced.</p>';
    return '<p>Add this line to <code>bitcoin.conf</code>:</p><pre class="gs-code"><code>' + esc(s.key) + '=1</code></pre>' +
      '<p>Restart Bitcoin Core. The index then builds in the background; its progress is shown here and under ' +
      link('/#status', 'Node Status') + '.</p>' + (notes ? gsIndexNotes() : '');
  }
  function gsDetail(s, firstMissingIndex) {
    switch (s.key) {
      case 'node':
        if (s.state === 'done') return '<p>Connected to Bitcoin Core (' + esc(s.network) + ').</p>';
        return (s.reachable && s.network_match === false
          ? '<p>Bitcoin Core runs on network ' + esc(s.network) + ', but Node Settings expect ' + esc(s.expected_network) + '.</p>'
          : '<p>Not connected' + (s.reason ? ': ' + esc(s.reason) : '.') + '</p>') + '<p>' + link('/#node-settings', 'Open Node Settings') + '</p>';
      case 'blockchain':
        if (s.state === 'unknown') return NOT_CONNECTED;
        return s.state === 'done' ? '<p>Synced (block ' + esc(s.block_height) + ').</p>'
          : '<p>Bitcoin Core is still syncing the blockchain (block ' + esc(s.block_height) + ').</p>';
      case 'txindex': case 'txospenderindex':
        return gsIndexDetail(s, s.key === firstMissingIndex);
      case 'wallet':
        return (s.state === 'done' ? '<p>Addresses in your own custody are in Known Addresses.</p>'
          : '<p>Run wallet discovery or import your addresses in ' + link('/#registry', 'Known Addresses') + '.</p>') +
          '<p class="hint">' + OWNED_SCOPE + '</p>';
      case 'acquisition':
        return s.state === 'done' ? '<p>At least one acquisition is recorded.</p>'
          : '<p>Record where your bitcoin entered: + Add Acquisition in ' + link('/#acquisitions', 'Records & Origins') + '.</p>';
      default:
        return '<p>Optional. ' + (s.state === 'done' ? 'At least one exchange exit is recorded.'
          : 'When bitcoin went to an exchange, record it with Record Exit at the acquisition in ' +
            link('/#acquisitions', 'Records & Origins') + '.') + '</p>';
    }
  }
  function gsPanel(r) {
    var missing = r.steps.filter(function (s) { return (s.key === 'txindex' || s.key === 'txospenderindex') && s.state === 'todo'; })[0];
    var required = r.steps.filter(function (s) { return s.required; });
    var done = required.filter(function (s) { return s.state === 'done'; }).length;
    return '<details class="panel getting-started"' + (r.all_set ? '' : ' open') + '><summary><strong>Getting started</strong> · ' +
      (r.all_set ? 'All set up' : done + ' of ' + required.length + ' required steps done') + '</summary><ol class="gs-steps">' +
      r.steps.map(function (s) {
        return '<li class="gs-step gs-' + esc(s.state) + '" data-gs-step="' + esc(s.key) + '" data-gs-state="' + esc(s.state) + '">' +
          '<span class="gs-state">' + esc(gsStates[s.state] || s.state) + '</span> <strong>' + esc(gsTitles[s.key] || s.key) + '</strong>' +
          (s.required ? '' : ' <span class="hint">(optional)</span>') +
          '<div class="gs-detail">' + gsDetail(s, missing && missing.key) + '</div></li>';
      }).join('') + '</ol><button class="btn small" data-gs-hide>Hide</button></details>';
  }
  function gsShow(hidden) {
    var panel = root.querySelector('#getting-started'), again = root.querySelector('#getting-started-show');
    panel.hidden = hidden; again.hidden = !hidden;
  }
  function loadChecklist() {
    var token = ++gsToken, panel = root.querySelector('#getting-started');
    var unavailable = function () { if (token === gsToken) panel.innerHTML = '<p class="panel hint">Checklist unavailable.</p>'; };
    panel.innerHTML = '<p class="hint" role="status">Loading getting-started checklist…</p>';
    try {
      API.gettingStarted().then(function (r) {
        if (token !== gsToken) return;
        panel.innerHTML = gsPanel(r);
        panel.querySelector('[data-gs-hide]').addEventListener('click', function () { gsRemember(true); gsShow(true); });
      }).catch(unavailable);
    } catch (e) { unavailable(); }
  }
  function checklist() {
    root.querySelector('[data-gs-show]').addEventListener('click', function (e) {
      if (e && e.preventDefault) e.preventDefault();
      gsRemember(false); gsShow(false); loadChecklist();
    });
    if (gsHidden()) { gsToken++; gsShow(true); return; }
    gsShow(false);
    loadChecklist();
  }
  function pageLink(offset) { var q = new URLSearchParams(params.toString()); q.set('offset', offset); return '/#' + view + '?' + q.toString(); }
  function render(el, name, query) {
    root = el; view = name; params = query;
    if (!titles[view]) { root.innerHTML = '<p role="alert">Unknown page.</p>' + link('/#dashboard', 'Dashboard'); return; }
    root.innerHTML = '<h1 tabindex="-1">' + titles[view] + '</h1><div class="report-toolbar">' + link(href('dashboard'), 'Dashboard') +
      ' · ' + link(href('current-wallet'), 'Current Wallet') + ' · ' + link(href('acquisitions'), 'Records & Origins') +
      '<label>Scope <select id="report-wallet"><option value="">All wallets</option>' +
      ((dashboard && dashboard.wallets) || (params.has('wallet') ? [params.get('wallet')] : [])).map(function (w) { return '<option value="wallet:' + esc(w) + '">' + esc(w || '(unlabelled)') + '</option>'; }).join('') +
      '</select></label><button class="btn" id="report-refresh" title="Reloads saved observations; does not scan the wallet">Reload saved report</button></div><div id="report-stamp" class="report-stamp" role="status">Loading data revision…</div>' +
      filtersHtml() + (view === 'dashboard' ? '<div id="getting-started" class="getting-started-slot"></div>' : '') +
      '<div id="report-body"></div>' + help() +
      (view === 'dashboard' ? '<p class="hint" id="getting-started-show"><a href="/#dashboard" data-gs-show>Show the getting-started checklist</a></p>' : '');
    root.querySelector('#report-wallet').value = params.has('wallet') ? 'wallet:' + params.get('wallet') : '';
    root.querySelector('#report-wallet').addEventListener('change', function () {
      var q = new URLSearchParams(params.toString()); q.delete('offset');
      if (this.value) q.set('wallet', this.value.slice(7)); else q.delete('wallet');
      location.hash = '#' + view + '?' + q.toString();
    });
    root.querySelectorAll('[data-report-filter]').forEach(function (s) { s.addEventListener('change', function () {
      var q = new URLSearchParams(params.toString()); q.delete('offset'); q.delete('outpoint'); q.delete('acquisition_id');
      if (s.value) q.set(s.dataset.reportFilter, s.value); else q.delete(s.dataset.reportFilter);
      location.hash = '#' + view + '?' + q.toString();
    }); });
    root.querySelector('#report-refresh').addEventListener('click', load);
    load();
    if (view === 'dashboard') checklist();
  }
  // Preselect the previous calendar year (UTC) when it has Exits, otherwise every year.
  function defaultExportYears(years, thisYear) {
    var previous = thisYear - 1;
    return years.some(function (y) { return y.year === previous; }) ? [previous] : 'all';
  }
  function exportFilterText(selection) {
    if (selection === 'all') return 'Filters: Years: all';
    if (!selection.length) return 'Filters: none selected';
    return 'Filters: Years: ' + selection.join(', ');
  }
  function reportUrl(wallet, selection, includeFreeText) {
    var q = ['years=' + (selection === 'all' ? 'all' : selection.join(','))];
    if (wallet !== null) q.push('wallet=' + encodeURIComponent(wallet));
    if (includeFreeText) q.push('include_free_text=1');
    return '/api/reports/blockchain-history?' + q.join('&');
  }
  function exportMenu() {
    var oldFocus = document.activeElement, dialog = document.createElement('dialog');
    var token = ++exportGeneration;
    var q = new URLSearchParams(location.hash.split('?')[1] || ''), wallet = q.has('wallet') ? q.get('wallet') : null;
    var years = [], yearsLoaded = false, selection = 'all', unconfirmed = 0;
    dialog.className = 'report-export-dialog';
    dialog.innerHTML = '<h2>Export Exchange Exits (CSV)</h2>' +
      '<p>One row per exit and origin anchor. Blockchain history, not a tax report.</p>' +
      '<p>Scope: ' + esc(wallet === null ? 'All wallets' : wallet || '(unlabelled)') + '</p>' +
      '<fieldset id="export-years"><legend>Years (exit = deposit to exchange, by block time, UTC)</legend>' +
      '<label><input type="checkbox" id="export-all-years"> All years</label>' +
      '<div id="export-year-list"><p id="export-years-state">Loading years…</p></div></fieldset>' +
      '<p id="export-filter-info"></p><p id="export-unconfirmed-note" class="hidden"></p><label class="export-text-label"><input type="checkbox" id="export-text"> Include sensitive free text: Notes, Broker, customer references and review comments</label>' +
      '<p>Wallet labels and addresses are sensitive inventory data. Reports use saved observations; this export is neither a backup nor a tax report.</p>' +
      '<p id="export-status" role="status">Prepare to obtain the exact data revision and row count before download.</p>' +
      '<button class="btn primary" id="export-prepare" disabled>Prepare export</button> <button class="btn" id="export-report" disabled title="Opens a printable page; save it as PDF with Ctrl+P">Blockchain History Report</button> <a class="btn hidden" id="export-download">Download</a> <button class="btn" id="export-close">Cancel / Close</button>';
    document.body.appendChild(dialog); dialog.showModal();
    var all = dialog.querySelector('#export-all-years'), list = dialog.querySelector('#export-year-list');
    var prepare = dialog.querySelector('#export-prepare'), status = dialog.querySelector('#export-status');
    function boxes() { return list.querySelectorAll('[data-export-year]'); }
    function update() {
      all.checked = selection === 'all';
      boxes().forEach(function (box) { box.checked = selection !== 'all' && selection.indexOf(Number(box.dataset.exportYear)) >= 0; });
      dialog.querySelector('#export-filter-info').textContent = exportFilterText(selection);
      // FX-8: exits still without block time are only in "All years".
      var note = dialog.querySelector('#export-unconfirmed-note');
      note.textContent = unconfirmed + ' exit(s) not yet confirmed on-chain are not included in year filters.';
      note.classList.toggle('hidden', !(unconfirmed > 0 && selection !== 'all'));
      prepare.disabled = !yearsLoaded || (selection !== 'all' && !selection.length);
      dialog.querySelector('#export-report').disabled = prepare.disabled;
      dialog.querySelector('#export-download').classList.add('hidden');
    }
    all.addEventListener('change', function () { selection = all.checked ? 'all' : []; update(); });
    function renderYears() {
      list.innerHTML = years.length ? years.map(function (y) {
        return '<label><input type="checkbox" data-export-year="' + y.year + '"> ' + y.year + ' \u00b7 ' + y.exit_count + (y.exit_count === 1 ? ' exit output' : ' exit outputs') + '</label> ';
      }).join('') : '<p id="export-years-state">No confirmed exits with a block time yet.</p>';
      boxes().forEach(function (box) {
        box.addEventListener('change', function () {
          var year = Number(box.dataset.exportYear), chosen = selection === 'all' ? [] : selection.slice();
          if (box.checked && chosen.indexOf(year) < 0) chosen.push(year);
          if (!box.checked) chosen = chosen.filter(function (v) { return v !== year; });
          selection = chosen.sort(function (a, b) { return a - b; }); update();
        });
      });
    }
    API.reportRequest('/api/reports/exit-years' + (wallet === null ? '' : '?wallet=' + encodeURIComponent(wallet))).then(function (result) {
      if (token !== exportGeneration) return;
      years = result.years || []; unconfirmed = result.unconfirmed_exits || 0; yearsLoaded = true; selection = defaultExportYears(years, new Date().getUTCFullYear());
      renderYears(); update();
    }).catch(function (e) {
      if (token !== exportGeneration) return;
      list.innerHTML = '<p id="export-years-state">' + esc((e && e.message) || 'Years unavailable.') + ' Only all years can be exported.</p>';
      yearsLoaded = true; selection = 'all'; update();
    });
    update();
    function cancelJob() {
      if (exportTimer) clearTimeout(exportTimer);
      if (exportJob) API.reportRequest('/api/exports/' + exportJob, 'DELETE').catch(function () {});
      exportJob = null;
    }
    dialog.addEventListener('close', function () { exportGeneration++; cancelJob(); dialog.remove(); if (oldFocus) oldFocus.focus(); });
    dialog.querySelector('#export-close').addEventListener('click', function () { dialog.close(); });
    dialog.querySelector('#export-report').addEventListener('click', function () {
      // Same selection as the CSV export; the page is rendered server-side and opens in its own tab.
      window.open(reportUrl(wallet, selection, dialog.querySelector('#export-text').checked), '_blank', 'noopener');
    });
    prepare.addEventListener('click', function () {
      cancelJob(); prepare.disabled = true; all.disabled = true;
      boxes().forEach(function (box) { box.disabled = true; });
      dialog.querySelector('#export-text').disabled = true;
      dialog.querySelector('#export-download').classList.add('hidden');
      status.textContent = 'Preparing snapshot-consistent export…';
      var payload = { dataset: 'exit-provenance-v2', wallet: wallet, years: selection, include_free_text: !!dialog.querySelector('#export-text').checked };
      function unlock() {
        all.disabled = false; boxes().forEach(function (box) { box.disabled = false; });
        dialog.querySelector('#export-text').disabled = false; update();
      }
      function fail(e) { if (token !== exportGeneration) return; status.textContent = e.message || 'Export failed. Retry.'; unlock(); }
      function poll(job) {
        if (token !== exportGeneration) { API.reportRequest('/api/exports/' + job.id, 'DELETE').catch(function () {}); return; }
        exportJob = job.id;
        if (job.state === 'FAILED') { fail({ message: job.error }); return; }
        if (job.state === 'READY') {
          var rows = Object.keys(job.row_counts || {}).reduce(function (sum, k) { return sum + job.row_counts[k]; }, 0);
          status.textContent = 'Data as of: ' + (job.data_as_of_utc || 'No scan') + ' · Export time: ' + job.exported_at_utc + ' · Revision: ' + job.snapshot_id +
            (dashboard && dashboard.snapshot_id !== job.snapshot_id ? ' (different revision from Dashboard)' : '') + ' · Rows: ' + rows;
          var download = dialog.querySelector('#export-download'); download.href = '/api/exports/' + job.id + '/download'; download.download = job.filename;
          unlock(); download.classList.remove('hidden'); download.focus(); return;
        }
        exportTimer = setTimeout(function () { API.reportRequest('/api/exports/' + job.id).then(poll).catch(fail); }, 400);
      }
      API.reportRequest('/api/exports', 'POST', payload).then(poll).catch(fail);
    });
  }
  return { render: render, stop: stop, exportMenu: exportMenu, defaultExportYears: defaultExportYears, reportUrl: reportUrl, mountLineage: mountLineage,
    reload: function () { if (root && root.isConnected && !root.classList.contains('hidden')) load(); } };
})();

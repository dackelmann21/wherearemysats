// Acquisitions page.
// Record user-supplied Acquisition metadata anchored to an exact on-chain
// outpoint (TXID:VOUT). Saving one automatically creates + runs a normal
// forward provenance trace from that outpoint. This page is NOT a tax engine:
// no FIFO, cost-basis or gain/loss is computed or displayed.
// v0.10.0: Acquisition Review & Disposals - record sales/disposals against
// acquisitions (backend-controlled position states, no tax math).
window.AcquisitionsPage = (function () {

  var EDITING_ID = null;
  var PENDING_VOUT = null;

  // Position and wallet membership are separate filter dimensions.
  var CURRENT_FILTER = null; // null = All; otherwise a FILTERS key.
  var OUTSIDE_WALLETS = "ACQUISITION_NOT_IN_CURRENT_WALLET";
  var walletPositions = null;
  var walletPositionsStatus = "loading";
  var loadedAcquisitions = null;
  var positionCounts = {};
  var listRequest = 0;
  var countsRequest = 0;
  var DISPOSAL_EDITOR = null; // { mode, disposalId, acqId, acq, containerEl, el, candidates }
  var EXIT_EDITOR = null; // { mode, exitId, acqId, acq, containerEl, el, candidates, candidate }
  var coverageRequest = 0;
  var coverageController = null;
  var COVERAGE_FILTER = "";
  // Provenance composes positions; this component owns the existing record
  // forms, table cells, action handlers and inline Exit/Disposal editors.
  var WORKSPACE = null;
  var activeActionRow = null;

  var FILTERS = [
    { key: null, label: "All" },
    { key: "CURRENT", label: "Current" },
    { key: "NEEDS_REVIEW", label: "Needs Review" },
    { key: "PARTIALLY_DISPOSED", label: "Partial" },
    { key: "DISPOSED", label: "Disposed" },
    { key: OUTSIDE_WALLETS, label: "Not in current wallets", dimension: "wallet_membership" },
  ];

  function formRow(label, inputHtml, hintText) {
    return '<div class="acq-field"><label>' + label + "</label>" + inputHtml +
      (hintText ? '<span class="hint">' + hintText + "</span>" : "") + "</div>";
  }

  function formHtml() {
    return '<details class="panel" id="acq-add-panel"><summary>+ Add Acquisition</summary>' +
      '<p class="hint">Select an exact output from your local Bitcoin node. Date, source, reference and notes are user assertions, ' +
      'not blockchain proof of acquisition or ownership.</p>' +
      '<div class="acq-grid" id="acq-form">' +
      '<div class="acq-field span-3"><label>Transaction ID</label>' +
      '<input type="text" id="acq-txid" placeholder="64 hex characters" required />' +
      '<button class="btn small" id="acq-load-outputs">Load outputs</button></div>' +
      '<div class="acq-field span-3" id="acq-output-facts"></div>' +
      formRow("Acquisition date", '<input type="date" id="acq-date" />', "Optional user-supplied date, independent of block time.") +
      formRow("Source", '<input type="text" id="acq-source" placeholder="optional" />') +
      formRow("Reference", '<input type="text" id="acq-reference" placeholder="optional" />') +
      formRow("Notes", '<textarea id="acq-notes"></textarea>') +
      '<div class="acq-actions">' +
      '  <div class="acq-preview" id="acq-preview"></div>' +
      '  <div class="acq-actions-btns">' +
      '    <button class="btn primary" id="acq-submit">Add Acquisition</button>' +
      '    <button class="btn" id="acq-cancel" style="display:none">Cancel edit</button>' +
      "  </div>" +
      "</div>" +
      "</div>" +
      '<div id="acq-select-wrap" class="acq-select-box hidden"></div>' +
      "</details>";
  }

  function listHtml() {
    return (WORKSPACE ? WORKSPACE.filtersHtml() : '') +
      '<div id="acq-filter-bar"></div><div id="acq-list"></div>';
  }

  // --- Destructive maintenance: reset the Acquisition dataset ---------------
  // Deliberately a separate, quiet section at the very bottom of the page so it
  // never competes with Add Acquisition / Import CSV. It is a dataset reset for
  // development, testing and re-importing a corrected CSV - NOT a database reset.
  function dangerZoneHtml() {
    return '<section class="panel" id="acq-danger-zone" aria-label="Destructive maintenance actions">' +
      '<h2>Destructive maintenance</h2>' +
      '<p class="hint">Maintenance action for development and for re-importing an Acquisition CSV. ' +
      'It removes the Acquisition dataset only; it is not a database reset.</p>' +
      '<div style="margin:8px 0">' +
      '  <button class="btn small danger" id="btn-delete-all-acquisitions">Delete all Acquisitions</button>' +
      "</div>" +
      '<div id="acq-danger-feedback" aria-live="polite"></div>' +
      "</section>";
  }

  function setDangerFeedback(el, kind, html) {
    var box = el.querySelector("#acq-danger-feedback");
    if (box) box.innerHTML = '<div class="alert ' + kind + '">' + html + "</div>";
  }

  // --- CSV import (one CSV row = one Acquisition) ---------------------------
  // Canonical CSV identity is the exact outpoint; amount comes from the node.
  var CSV_HEADER =
    "acquisition_date;txid;vout;source;reference;notes";
  var CSV_EXAMPLE =
    "2025-01-08;<TXID>;2;Example source;SYNTHETIC-001;Example notes";
  var CSV_BULLETS = [
    "Amount, address and block metadata are resolved from your local Bitcoin node",
    "Legacy bitcoin_address may replace vout; ambiguous matches require output selection",
    "one CSV row = one Acquisition",
    "import runs in two steps: Preview (never writes anything), then Import the ready rows",
    "Source for all rows (optional) fills rows without their own source value",
  ];

  function csvTemplateText() {
    return CSV_HEADER + "\n" + CSV_EXAMPLE;
  }

  // innerHTML would swallow the <TXID> placeholder as an HTML tag, so the
  // example block escapes the angle brackets (they still render as <TXID>).
  function escAngleBrackets(s) {
    return String(s).split("<").join("&lt;").split(">").join("&gt;");
  }

  function infoBoxHtml() {
    var html =
      '<div class="acq-import-info">' +
      '<div class="acq-import-info-title">CSV format</div>' +
      '<div class="acq-import-cols">' +
      '<div><span class="ec">Required columns:</span> ' +
      "<code>txid</code><code>vout</code></div>" +
      '<div><span class="ec">Optional columns:</span> ' +
      "<code>acquisition_date</code><code>source</code>" +
      "<code>reference</code><code>notes</code></div>" +
      "</div>" +
      '<ul class="acq-import-preferred">' +
      "<li>UTF-8</li>" +
      "<li>semicolon-separated</li>" +
      "<li>acquisition_date: YYYY-MM-DD</li>" +
      "</ul>" +
      '<div class="ec">Recommended header</div>' +
      '<div class="csv-block">' + escAngleBrackets(CSV_HEADER) + "</div>" +
      '<div class="ec">Example row</div>' +
      '<div class="csv-block">' + escAngleBrackets(CSV_EXAMPLE) + "</div>" +
      '<ul class="acq-import-bullets">';
    for (var i = 0; i < CSV_BULLETS.length; i++) {
      html += "<li>" + CSV_BULLETS[i] + "</li>";
    }
    html += "</ul></div>";
    return html;
  }

  function importHtml() {
    return '<div class="panel acq-import">' +
      '<div class="acq-import-head">' +
      '<h3>Import Acquisitions from CSV</h3>' +
      '<button class="btn small" id="btn-acq-template">Download CSV Template</button>' +
      "</div>" +
      infoBoxHtml() +
      '<div class="acq-import-controls">' +
      '<textarea id="acq-csv-paste" placeholder="Paste the CSV text header + rows here, or choose a .csv file…"></textarea>' +
      // FIX-UI-4: optional Source for rows without their own source cell.
      '<label class="acq-import-default-source">Source for all rows ' +
      '<input type="text" id="acq-csv-default-source" placeholder="optional, e.g. exchange name" /></label>' +
      '<div class="hint">A non-empty <code>source</code> column value in the CSV takes precedence. ' +
      'Existing Acquisitions are not changed.</div>' +
      '<div class="acq-import-btns">' +
      '<input type="file" id="acq-csv-file" accept=".csv,text/csv" style="display:none">' +
      '<button class="btn" id="btn-acq-csv-file">Choose .csv file</button>' +
      '<button class="btn primary" id="btn-acq-csv-preview">Preview</button>' +
      "</div>" +
      "</div>" +
      '<div id="acq-csv-result" class="acq-import-result"></div>' +
      "</div>";
  }

  // Two-phase import state (per paste-revision). selections = line -> chosen
  // vout for ambiguous (REVIEW_REQUIRED) rows; unchecked = lines the user
  // deliberately excluded from the "import ready rows" batch.
  var IMPORT_STATE = { prevContent: "", selections: {}, unchecked: {} };

  function statusChip(status) {
    var cls = "chip";
    if (status === "READY") cls += " chip-ready";
    else if (status === "DUPLICATE") cls += " chip-duplicate";
    else if (status === "INVALID") cls += " chip-invalid";
    else if (status === "REVIEW_REQUIRED") cls += " chip-review";
    else if (status === "IMPORTED") cls += " chip-imported";
    else if (status === "IMPORT_FAILED") cls += " chip-failed";
    return '<span class="' + cls + '">' + status + "</span>";
  }

  function summaryChip(kind, count) {
    var label = kind;
    if (kind === "review-required") label = "review required";
    return '<span class="chip chip-' + kind + '">' + count + " " + label + "</span>";
  }

  function voutSelect(row) {
    var wasChosen = IMPORT_STATE.selections[row.line];
    var opts = '<option value=""' + (wasChosen == null ? " selected" : "") +
      " disabled>choose vout…</option>";
    var chosen = (wasChosen == null) ? -1 : wasChosen;
    for (var i = 0; i < row.candidates.length; i++) {
      var c = row.candidates[i];
      var sel = (chosen === c.vout) ? " selected" : "";
      opts += '<option value="' + c.vout + '"' + sel + ">vout " + c.vout +
        " · " + Fmt.btc(c.amount_sats) + " BTC</option>";
    }
    return '<select class="acq-vout" data-line="' + row.line + '">' + opts + "</select>";
  }

  function readyLineNumbers(r) {
    var out = [];
    for (var i = 0; i < r.rows.length; i++) {
      var row = r.rows[i];
      if (row.status === "READY" && !IMPORT_STATE.unchecked[row.line]) out.push(row.line);
    }
    return out;
  }

  function renderPreview(el, r) {
    var box = el.querySelector("#acq-csv-result");
    var html = '<div class="acq-preview-sum">' +
      summaryChip("ready", r.ready) + summaryChip("duplicate", r.duplicates) +
      summaryChip("invalid", r.invalid) + summaryChip("review-required", r.review_required);
    if (r.ignored_columns && r.ignored_columns.length) {
      html += '<span class="chip chip-ignored">ignored: ' +
        Fmt.esc(r.ignored_columns.join(", ")) + "</span>";
    }
    html += "</div>";
    html += '<div class="acq-preview-scroll"><table class="acq-preview-table">' +
      "<thead><tr>" +
      '<th class="sel">Include</th><th>Line</th><th>Date</th><th>BTC</th>' +
      "<th>Original outpoint</th><th>Source</th><th>Status</th><th>Detail</th>" +
      "</tr></thead><tbody>";
    for (var i = 0; i < r.rows.length; i++) {
      var row = r.rows[i];
      var include = "";
      var detail = "";
      if (row.status === "READY") {
        var check = IMPORT_STATE.unchecked[row.line] ? "" : "checked";
        include = '<label class="acq-include"><input type="checkbox" data-line="' +
          row.line + '" ' + check + "> include</label>";
      } else if (row.status === "REVIEW_REQUIRED") {
        include = voutSelect(row);
        detail = "ambiguous output – pick which vout to import";
      } else if (row.status === "DUPLICATE") {
        detail = "already exists (acq #" +
          (row.existing_acquisition_id != null ? row.existing_acquisition_id : "?") + ")";
      } else if (row.status === "INVALID" || row.status === "IMPORT_FAILED") {
        detail = '<span class="acq-import-err">' + Fmt.esc(row.error || "") + "</span>";
      } else if (row.status === "IMPORTED") {
        detail = "imported";
      }
      html += "<tr>" +
        '<td class="sel">' + include + "</td>" +
        '<td class="idx mono">' + row.line + "</td>" +
        "<td>" + Fmt.esc(row.acquisition_date || "") + "</td>" +
        "<td>" + Fmt.esc(row.btc_amount || "") + "</td>" +
        "<td>" + Fmt.esc(row.txid) + ":" + (row.origin_vout == null ? "?" : row.origin_vout) + "</td>" +
        "<td>" + Fmt.esc(row.source || "—") + "</td>" +
        "<td>" + statusChip(row.status) + "</td>" +
        '<td class="act">' + detail + "</td>" +
        "</tr>";
    }
    html += "</tbody></table></div>";
    var lines = readyLineNumbers(r);
    html += '<div class="acq-import-btns">' +
      '<button class="btn primary" id="btn-acq-csv-import-ready" style="display:none"' +
      ">Import ready rows (0)</button>" +
      '<button class="btn" id="btn-acq-csv-repreview" ' +
      (r.ready + r.review_required ? "" : 'style="display:none"') +
      ">Re-preview</button>" +
      "</div>";
    box.innerHTML = html;
    wirePreview(el, r, lines);
  }

  function wirePreview(el, r, readyLines) {
    var checks = el.querySelectorAll("#acq-csv-result input[type=checkbox]");
    for (var i = 0; i < checks.length; i++) {
      checks[i].addEventListener("change", function () {
        var line = Number(this.getAttribute("data-line"));
        if (this.checked) delete IMPORT_STATE.unchecked[line];
        else IMPORT_STATE.unchecked[line] = true;
        updateImportButton(el);
      });
    }
    var selects = el.querySelectorAll("#acq-csv-result select.acq-vout");
    for (var j = 0; j < selects.length; j++) {
      selects[j].addEventListener("change", function () {
        var line = Number(this.getAttribute("data-line"));
        if (this.value === "") delete IMPORT_STATE.selections[line];
        else IMPORT_STATE.selections[line] = Number(this.value);
        doCsvPreview(el, true);
      });
    }
    el.querySelector("#btn-acq-csv-import-ready").addEventListener("click", function () {
      doCsvImport(el);
    });
    el.querySelector("#btn-acq-csv-repreview").addEventListener("click", function () {
      doCsvPreview(el, true);
    });
    updateImportButton(el, readyLines);
  }

  function updateImportButton(el, readyLines) {
    var box = el.querySelector("#acq-csv-result");
    var btn = box.querySelector("#btn-acq-csv-import-ready");
    if (!btn) return;
    var list = readyLines || [];
    var count = 0;
    var checks = box.querySelectorAll("input[type=checkbox]:checked");
    for (var i = 0; i < checks.length; i++) count++;
    if (list.length && count > list.length) count = list.length;
    btn.textContent = "Import ready rows (" + count + ")";
    btn.style.display = count ? "" : "none";
  }

  function doCsvPreview(el, keepSelections) {
    var box = el.querySelector("#acq-csv-result");
    var content = el.querySelector("#acq-csv-paste").value;
    if (!content.trim()) {
      box.innerHTML = '<div class="alert warn">Nothing to import: paste the CSV text or choose a .csv file.</div>';
      return;
    }
    if (!keepSelections && content !== IMPORT_STATE.prevContent) {
      IMPORT_STATE.prevContent = content;
      IMPORT_STATE.selections = {};
      IMPORT_STATE.unchecked = {};
    } else {
      IMPORT_STATE.prevContent = content;
    }
    box.innerHTML = '<div class="loading"><span class="spinner"></span>Checking…</div>';
    API.importAcquisitionsPreview({
      content: content,
      selections: IMPORT_STATE.selections,
      default_source: defaultSource(el),
    }).then(function (r) {
      renderPreview(el, r);
    }).catch(function (err) {
      box.innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>";
    });
  }

  function renderImportResult(el, r) {
    var box = el.querySelector("#acq-csv-result");
    var cls = r.failed ? "warn" : "ok";
    var note = "Imported <b>" + r.imported + "</b>, duplicates skipped <b>" + r.duplicates +
      "</b>, invalid <b>" + r.invalid + "</b>, review required <b>" + r.review_required +
      "</b>, failed <b>" + r.failed + "</b>.";
    if (r.ignored_columns && r.ignored_columns.length) {
      note += " Unknown column(s) ignored: <b>" + Fmt.esc(r.ignored_columns.join(", ")) + "</b>.";
    }
    var html = '<div class="alert ' + cls + '">' + note + "</div>";
    var list = [];
    for (var i = 0; i < r.rows.length; i++) {
      if (r.rows[i].status === "IMPORT_FAILED") list.push(r.rows[i]);
    }
    if (list.length) {
      html += '<div class="io-list">';
      for (var j = 0; j < list.length; j++) {
        var x = list[j];
        html += '<div class="io-row">' +
          '<div class="idx">line ' + x.line + "</div>" +
          '<div class="hint">' + Fmt.esc(x.txid ? Fmt.shortTxid(x.txid, 12) : "") + "</div>" +
          '<div class="amt" style="color:var(--warn)">' + Fmt.esc(x.error || "") + "</div>" +
          "</div>";
      }
      html += "</div>";
    }
    box.innerHTML = html;
    // A finished import invalidates the previous preview's selections.
    IMPORT_STATE = { prevContent: "", selections: {}, unchecked: {} };
  }

  function doCsvImport(el) {
    var box = el.querySelector("#acq-csv-result");
    var content = el.querySelector("#acq-csv-paste").value;
    var lines = [];
    var checks = box.querySelectorAll("input[type=checkbox]:checked");
    for (var i = 0; i < checks.length; i++) {
      lines.push(Number(checks[i].getAttribute("data-line")));
    }
    if (!lines.length) return;
    box.innerHTML = '<div class="loading"><span class="spinner"></span>Importing…</div>';
    API.importAcquisitions({
      content: content,
      lines: lines,
      selections: IMPORT_STATE.selections,
      default_source: defaultSource(el),
    }).then(function (r) {
      renderImportResult(el, r);
      loadList(el);
      loadPositionCounts(el);
    }).catch(function (err) {
      box.innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>";
    });
  }

  function defaultSource(el) {
    var input = el.querySelector("#acq-csv-default-source");
    return input ? input.value.trim() : "";
  }

  function downloadCsvTemplate() {
    var blob = new Blob([csvTemplateText()], { type: "text/csv;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = "acquisitions-import-template.csv";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  function wireImport(el) {
    var fileInput = el.querySelector("#acq-csv-file");
    el.querySelector("#btn-acq-template").addEventListener("click", function () {
      downloadCsvTemplate();
    });
    el.querySelector("#btn-acq-csv-file").addEventListener("click", function () {
      fileInput.click();
    });
    fileInput.addEventListener("change", function (e) {
      var f = e.target.files && e.target.files[0];
      if (!f) return;
      var reader = new FileReader();
      reader.onload = function () {
        el.querySelector("#acq-csv-paste").value = reader.result;
        doCsvPreview(el, false);
      };
      reader.readAsText(f);
    });
    el.querySelector("#btn-acq-csv-preview").addEventListener("click", function () {
      doCsvPreview(el, false);
    });
    // A preview shows the effective Source per row; a changed dialog value
    // makes it stale, so the user must preview again before importing.
    el.querySelector("#acq-csv-default-source").addEventListener("input", function () {
      el.querySelector("#acq-csv-result").innerHTML = "";
    });
  }

  function render(el, workspace) {
    WORKSPACE = workspace || null;
    if (WORKSPACE) CURRENT_FILTER = WORKSPACE.initialFilter ? WORKSPACE.initialFilter() : null;
    activeActionRow = null;
    ++listRequest;
    ++countsRequest;
    loadedAcquisitions = null;
    positionCounts = {};
    walletPositions = null;
    walletPositionsStatus = "loading";
    // The single write to the page container. Everything below only mutates
    // elements INSIDE this markup, so the list can refresh independently
    // without ever removing the maintenance section at the bottom.
    el.innerHTML = (WORKSPACE ? WORKSPACE.headerHtml() : "<h2>Acquisition records</h2>") +
      (WORKSPACE ? '' : '<details class="panel" id="acq-export-panel"><summary>Export</summary><button class="btn" id="export-acquisitions">Export Acquisitions CSV</button>' +
      '<h3>Exit Provenance evidence</h3><div class="acq-grid">' +
      formRow('From (inclusive, UTC)', '<input type="date" id="export-exits-from">') +
      formRow('To (inclusive, UTC)', '<input type="date" id="export-exits-to">') +
      '</div><p class="hint">Filters use confirmed transaction block time, not the editable Exit date. ' +
      'Leave both empty for all time, including unconfirmed/unknown-time records. One row per on-chain path hop. ' +
      'Shared output amounts repeat; do not sum rows or infer per-Acquisition allocations.</p>' +
      '<button class="btn" id="export-exits">Export Exit Provenance CSV</button>' +
      '<details><summary>Legacy / machine-readable export</summary><button class="btn small" id="export-exits-legacy">Export Recorded Exits CSV</button></details>' +
      '<div id="export-feedback" aria-live="polite"></div></details>') +
      (WORKSPACE ? '' :
      '<section class="panel" aria-label="Provenance Coverage">' +
      '<h2>Provenance Coverage</h2>' +
      '<button class="btn small" id="coverage-refresh">Refresh Coverage</button>' +
      '<div id="documentation-coverage" aria-live="polite"></div></section>') +
      '<div id="acq-feedback" aria-live="polite"></div>' + listHtml() + formHtml() +
      '<details class="panel" id="acq-import-panel"><summary>Import Acquisitions from CSV</summary>' + importHtml() + '</details>' +
      (WORKSPACE ? '<details class="panel"><summary>Maintenance</summary>' + dangerZoneHtml() + '</details>' : dangerZoneHtml()) +
      (WORKSPACE ? WORKSPACE.advancedHtml() : '');
    if (WORKSPACE) WORKSPACE.wire(el);
    else el.querySelector("#coverage-refresh").addEventListener("click", function () { loadCoverage(el); });
    // Bind the maintenance action defensively. It is part of the markup above,
    // but an unguarded lookup would throw here and abort the rest of render(),
    // taking the forms, the list and the position counters down with it - a
    // missing maintenance section must never be able to break the whole page.
    var dangerBtn = el.querySelector("#btn-delete-all-acquisitions");
    if (dangerBtn) {
      dangerBtn.addEventListener("click", function () {
        deleteAllAcquisitions(el);
      });
    }
    wireForm(el);
    if (!WORKSPACE) {
    el.querySelector('#export-acquisitions').addEventListener('click', function () {
      downloadExport(el, this, API.exportAcquisitions(), 'wams-acquisitions.csv');
    });
    el.querySelector('#export-exits').addEventListener('click', function () {
      downloadExport(el, this, API.exportExitProvenance(el.querySelector('#export-exits-from').value,
        el.querySelector('#export-exits-to').value), 'wams-exit-provenance-v1.csv');
    });
    el.querySelector('#export-exits-legacy').addEventListener('click', function () {
      downloadExport(el, this, API.exportRecordedExits(el.querySelector('#export-exits-from').value,
        el.querySelector('#export-exits-to').value), 'wams-recorded-exits.csv');
    });
    }
    wireImport(el);
    loadList(el);
    loadPositionCounts(el);
  }

  // "Delete all Acquisitions" - a dataset reset, never a database reset.
  //
  // One accidental click must never destroy the dataset, so this is a two-step
  // gate: a plain confirm that states exactly what is removed and what is
  // preserved, then a TYPED "DELETE" prompt before the request is ever sent.
  // The dialogs carry no addresses, TXIDs or amounts.
  //
  // One backend call performs the whole cleanup as a single atomic operation
  // (never N single deletes). The button is disabled while the request is in
  // flight so a double click cannot submit twice.
  function downloadExport(el, button, request, filename) {
    button.disabled = true;
    var feedback = el.querySelector('#export-feedback');
    feedback.textContent = 'Preparing CSV…';
    request.then(function (blob) {
      var url = URL.createObjectURL(blob);
      var link = document.createElement('a');
      link.href = url; link.download = filename;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      feedback.textContent = 'CSV ready.';
    }).catch(function (error) { feedback.textContent = error.message; })
      .finally(function () { button.disabled = false; });
  }

  function deleteAllAcquisitions(el) {
    var btn = el.querySelector("#btn-delete-all-acquisitions");
    var ok = confirm(
      "Delete all Acquisitions?\n\n" +
      "This removes all Acquisition anchors and " +
      "Acquisition-specific derived data (their forward provenance traces and " +
      "their Disposal / Exchange Exit allocations).\n\n" +
      "Independent blockchain analysis and unrelated application data will be " +
      "preserved: Exchange Exits, Disposals, ownership-neutral Blockchain " +
      "Analysis traces, the Known Address Registry, wallet discovery, node " +
      "settings and cached blockchain facts.\n\n" +
      "This action cannot be undone."
    );
    if (!ok) return;
    if (prompt("Type DELETE to confirm removing every Acquisition.") !== "DELETE") {
      setDangerFeedback(el, "warn", "Cancelled &ndash; nothing was deleted.");
      return;
    }
    btn.disabled = true;
    API.deleteAllAcquisitions().then(function (r) {
      btn.disabled = false;
      resetPageStateAfterBulkDelete(el);
      setDangerFeedback(el, "ok",
        "Deleted " + (r.deleted_acquisitions || 0) + " acquisition(s), " +
        (r.deleted_acquisition_traces || 0) + " acquisition trace(s); removed " +
        (r.removed_exchange_exit_links || 0) + " Exchange Exit link(s) and " +
        (r.removed_disposal_links || 0) + " Disposal link(s). Preserved " +
        (r.preserved_exchange_exits || 0) + " Exchange Exit(s), " +
        (r.preserved_disposals || 0) + " Disposal(s) and " +
        (r.preserved_traces || 0) + " independent trace(s).");
    }).catch(function (err) {
      btn.disabled = false;
      setDangerFeedback(el, "error",
        "Nothing was deleted. " + Fmt.esc(err.message));
    });
  }

  // Return the whole page to a clean "no acquisitions" state so Import CSV can
  // recreate the dataset immediately: no stale filter, no selected/editing
  // record, no open Sale/Exit editor, refreshed list + counters + coverage.
  function resetPageStateAfterBulkDelete(el) {
    CURRENT_FILTER = null;
    EDITING_ID = null;
    PENDING_VOUT = null;
    EXIT_EDITOR = null;
    DISPOSAL_EDITOR = null;
    closeForm(el);
    loadList(el);
    loadPositionCounts(el);
  }

  function wireForm(el) {
    el.querySelector("#acq-submit").addEventListener("click", function () {
      submitForm(el);
    });
    el.querySelector("#acq-cancel").addEventListener("click", function () {
      closeForm(el);
    });
    el.querySelector("#acq-txid").addEventListener("input", function () {
      PENDING_VOUT = null;
      hideSelection(el);
      el.querySelector("#acq-output-facts").textContent = "";
    });
    el.querySelector("#acq-load-outputs").addEventListener("click", function () {
      var txid = el.querySelector("#acq-txid").value.trim();
      PENDING_VOUT = null;
      hideSelection(el);
      el.querySelector("#acq-output-facts").textContent = "Loading outputs…";
      API.acquisitionOutputs(txid).then(function (outputs) {
        if (el.querySelector("#acq-txid").value.trim() !== txid || EDITING_ID !== null) return;
        el.querySelector("#acq-output-facts").textContent = "";
        showSelection(el, outputs);
      }).catch(function (err) { setFeedback(el, "error", Fmt.esc(err.message)); });
    });
  }

  // The acquisition inputs live inside a <div id="acq-form">, NOT a real
  // form element, so there is no form-level reset to call: clearing the
  // inputs and the attached page state must be explicit (v0.7.0 invoked the
  // element's reset method on the div, which threw "reset is not a function"
  // after Add / Cancel).
  function clearForm(el) {
    ["#acq-date", "#acq-source", "#acq-reference", "#acq-notes", "#acq-txid"].forEach(function (sel) {
      var input = el.querySelector(sel);
      if (input) {
        input.value = "";
      }
    });
  }

  // Return the page to "add a new acquisition" state: blank inputs, no edit
  // target, no pending VOUT, no stale warning/error, form actions restored.
  // The list below the form is untouched and stays rendered.
  function closeForm(el) {
    EDITING_ID = null;
    PENDING_VOUT = null;
    clearForm(el);
    el.querySelector("#acq-add-panel").open = false;
    el.querySelector("#acq-txid").disabled = false;
    el.querySelector("#acq-load-outputs").disabled = false;
    el.querySelector("#acq-output-facts").textContent = "";
    var preview = el.querySelector("#acq-preview");
    if (preview) {
      preview.innerHTML = "";
    }
    el.querySelector("#acq-cancel").style.display = "none";
    el.querySelector("#acq-submit").textContent = "Add Acquisition";
    el.querySelector("#acq-feedback").innerHTML = "";
    hideSelection(el);
  }

  function readForm(el) {
    return {
      acquisition_date: el.querySelector("#acq-date").value || null,
      source: el.querySelector("#acq-source").value.trim() || null,
      reference: el.querySelector("#acq-reference").value.trim() || null,
      notes: el.querySelector("#acq-notes").value.trim() || null,
    };
  }

  function submitPayload(el) {
    var p = readForm(el);
    if (EDITING_ID === null) {
      p.txid = el.querySelector("#acq-txid").value.trim();
      p.vout = PENDING_VOUT;
    }
    return p;
  }

  // Integer-string arithmetic retained for the separate legacy Disposal editor.
  function decParts(s) {
    var neg = s.charAt(0) === "-";
    var t = neg ? s.slice(1) : s;
    var dot = t.indexOf(".");
    return {
      neg: neg,
      whole: dot === -1 ? t : t.slice(0, dot),
      frac: dot === -1 ? "" : t.slice(dot + 1),
    };
  }

  function stripLeadingZeros(s) {
    var i = 0;
    while (i < s.length - 1 && s.charAt(i) === "0") i++;
    return s.slice(i);
  }

  function padZeros(s, n) {
    while (s.length < n) s = "0" + s;
    return s;
  }

  function addOne(digits) {
    var arr = digits.split("");
    for (var i = arr.length - 1; i >= 0; i--) {
      if (arr[i] === "9") {
        arr[i] = "0";
      } else {
        arr[i] = String(Number(arr[i]) + 1);
        return arr.join("");
      }
    }
    return "1" + arr.join("");
  }

  function mulDigits(a, b) {
    var out = [];
    var i, j, carry, k, v, pos;
    for (i = a.length - 1; i >= 0; i--) {
      carry = 0;
      var ai = a.charCodeAt(i) - 48;
      var rightPosI = a.length - 1 - i;
      for (j = b.length - 1; j >= 0; j--) {
        var rightPosJ = b.length - 1 - j;
        pos = out[rightPosI + rightPosJ] || 0;
        v = pos + ai * (b.charCodeAt(j) - 48) + carry;
        out[rightPosI + rightPosJ] = v % 10;
        carry = Math.floor(v / 10);
      }
      k = rightPosI + b.length;
      if (carry) {
        v = (out[k] || 0) + carry;
        out[k] = v % 10;
        carry = Math.floor(v / 10);
        while (carry) {
          k++;
          v = (out[k] || 0) + carry;
          out[k] = v % 10;
          carry = Math.floor(v / 10);
        }
      }
    }
    var s = "";
    for (var m = out.length - 1; m >= 0; m--) s += out[m];
    return stripLeadingZeros(s) || "0";
  }

  function decimalProduct(a, b) {
    var pa = decParts(a);
    var pb = decParts(b);
    var neg = pa.neg !== pb.neg;
    var da = stripLeadingZeros(pa.whole + pa.frac);
    var db = stripLeadingZeros(pb.whole + pb.frac);
    var scale = pa.frac.length + pb.frac.length;
    var prod = mulDigits(da, db);
    var sign = neg && prod !== "0" ? "-" : "";
    if (scale === 0) return sign + prod;
    if (prod.length > scale) {
      return sign + prod.slice(0, prod.length - scale) + "." + prod.slice(prod.length - scale);
    }
    return sign + "0." + padZeros(prod, scale);
  }

  // Round a non-negative decimal string to a whole number, halves up, purely
  // by inspecting its first fractional digit - no division or float.
  function roundHalfUpToInt(decStr) {
    var neg = decStr.charAt(0) === "-";
    var t = neg ? decStr.slice(1) : decStr;
    var dot = t.indexOf(".");
    var whole = dot === -1 ? t : t.slice(0, dot);
    var frac = dot === -1 ? "" : t.slice(dot + 1);
    var digits = (whole === "" ? "0" : whole);
    if (frac !== "" && frac.charAt(0) >= "5") digits = addOne(digits);
    return (neg && digits !== "0" ? "-" : "") + digits;
  }

  // Integer cents ("421") to the German money rendering "4,21".
  function centsToDe(centsStr) {
    var neg = centsStr.charAt(0) === "-";
    var t = neg ? centsStr.slice(1) : centsStr;
    t = stripLeadingZeros(t) || "0";
    var whole = t.length <= 2 ? "0" : t.slice(0, t.length - 2);
    var frac = t.length === 1 ? "0" + t : t.slice(t.length - 2);
    return (neg && whole !== "0" ? "-" : "") + whole + "," + frac;
  }

  function feeCentsStr(eurStr, pctStr) {
    return roundHalfUpToInt(decimalProduct(eurStr, pctStr));
  }

  // Integer subtraction of two non-negative digit strings. Returns a possibly
  // sign-prefixed result (no division, no floats).
  function subInts(a, b) {
    a = stripLeadingZeros(a || "0");
    b = stripLeadingZeros(b || "0");
    if (a === b) return "0";
    var neg = false;
    if (a.length < b.length || (a.length === b.length && a < b)) {
      neg = true;
      var tmp = a;
      a = b;
      b = tmp;
    }
    var out = [];
    var borrow = 0;
    var i = a.length - 1;
    var j = b.length - 1;
    while (i >= 0) {
      var av = a.charCodeAt(i) - 48 - borrow;
      var bv = j >= 0 ? b.charCodeAt(j) - 48 : 0;
      if (av < bv) {
        av += 10;
        borrow = 1;
      } else {
        borrow = 0;
      }
      out.push(String(av - bv));
      i--;
      j--;
    }
    while (out.length > 1 && out[out.length - 1] === "0") out.pop();
    return (neg ? "-" : "") + out.reverse().join("");
  }

  function grossCentsStr(eurStr) {
    return roundHalfUpToInt(decimalProduct(eurStr, "100"));
  }

  function netCentsStr(eurStr, pctStr) {
    return subInts(grossCentsStr(eurStr), feeCentsStr(eurStr, pctStr));
  }

  function feeEurosPreview(eurStr, pctStr) {
    return centsToDe(feeCentsStr(eurStr, pctStr));
  }

  function netEurosPreview(eurStr, pctStr) {
    return centsToDe(netCentsStr(eurStr, pctStr));
  }

  function setFeedback(el, kind, html) {
    el.querySelector("#acq-feedback").innerHTML = '<div class="alert ' + kind + '">' + html + "</div>";
  }

  function hideSelection(el) {
    el.querySelector("#acq-select-wrap").classList.add("hidden");
    el.querySelector("#acq-select-wrap").innerHTML = "";
  }

  function showSelection(el, candidates) {
    var opts = candidates.map(function (c) {
      return '<option value="' + c.vout + '">VOUT ' + c.vout + " · " +
        Fmt.btc(c.amount_sats) + " BTC · " + Fmt.esc(c.address || "Non-address output") + "</option>";
    }).join("");
    el.querySelector("#acq-select-wrap").innerHTML =
      '<div class="acq-select-box">' +
      "<p class=\"hint\">Choose the exact output to mark as an Acquisition anchor.</p>" +
      '<select id="acq-select-vout"><option value="" selected disabled>Select output…</option>' + opts + "</select> " +
      '<button class="btn small primary" id="acq-select-apply">Use this output</button>' +
      "</div>";
    el.querySelector("#acq-select-wrap").classList.remove("hidden");
    el.querySelector("#acq-select-apply").addEventListener("click", function () {
      if (el.querySelector("#acq-select-vout").value === "") return;
      PENDING_VOUT = Number(el.querySelector("#acq-select-vout").value);
      var output = candidates.find(function (c) { return c.vout === PENDING_VOUT; });
      el.querySelector("#acq-output-facts").textContent =
        "Original outpoint: " + el.querySelector("#acq-txid").value.trim() + ":" + PENDING_VOUT +
        " · " + Fmt.btc(output.amount_sats) + " BTC · " + (output.address || "Non-address output") +
        " · Block: " + (output.block_height == null ? "Unconfirmed" : output.block_height) +
        " · Block time: " + (output.block_time == null ? "Unknown" : new Date(output.block_time * 1000).toISOString());
      hideSelection(el);
    });
  }

  function submitForm(el) {
    var payload = submitPayload(el);
    if (EDITING_ID === null && (!payload.txid || payload.vout === null)) {
      setFeedback(el, "error", "Load the transaction and select an output first.");
      return;
    }
    el.querySelector("#acq-submit").disabled = true;
    var done = function () {
      el.querySelector("#acq-submit").disabled = false;
    };
    var save;
    if (EDITING_ID === null) {
      save = API.createAcquisition(payload);
    } else {
      save = API.updateAcquisition(EDITING_ID, payload);
    }
    save.then(function () {
      closeForm(el);
      loadList(el);
      loadPositionCounts(el);
      done();
    }).catch(function (err) {
      done();
      hideSelection(el);
      // The backend nests the multi-match candidates in the response body's
      // "detail" ({detail: {detail, candidates}}); api.js puts the whole body
      // in err.data. A top-level data.candidates stays accepted as fallback.
      var nested = err.data && err.data.detail && typeof err.data.detail === "object" ? err.data.detail : null;
      var candidates = (nested && nested.candidates) || (err.data && err.data.candidates) || null;
      if (err.status === 409 && candidates && candidates.length) {
        showSelection(el, candidates);
      } else {
        setFeedback(el, err.status === 409 ? "warn" : "error", Fmt.esc(err.message));
      }
    });
  }

  // --- v0.10.0 Acquisition Review & Disposals ------------------------------

  function btcStringToSats(btcStr) {
    var s = String(btcStr || "0").trim();
    var neg = s.charAt(0) === "-";
    var t = neg ? s.slice(1) : s;
    var dot = t.indexOf(".");
    var whole = dot === -1 ? t : t.slice(0, dot);
    var frac = dot === -1 ? "" : t.slice(dot + 1);
    frac = (frac + "00000000").slice(0, 8);
    var intStr = stripLeadingZeros(whole + frac) || "0";
    return (neg ? "-" : "") + intStr;
  }

  function compareInts(a, b) {
    a = stripLeadingZeros(a || "0");
    b = stripLeadingZeros(b || "0");
    if (a.length !== b.length) return a.length > b.length ? 1 : -1;
    if (a === b) return 0;
    return a > b ? 1 : -1;
  }

  function divideInts(a, b) {
    a = stripLeadingZeros(a || "0");
    b = stripLeadingZeros(b || "0");
    if (b === "0") return "";
    var neg = false;
    if (a.charAt(0) === "-") { neg = !neg; a = a.slice(1); }
    if (b.charAt(0) === "-") { neg = !neg; b = b.slice(1); }
    a = stripLeadingZeros(a) || "0";
    b = stripLeadingZeros(b) || "0";
    var quotient = "";
    var remainder = "0";
    for (var i = 0; i < a.length; i++) {
      remainder = stripLeadingZeros(remainder + a.charAt(i)) || "0";
      var q = 0;
      while (compareInts(remainder, b) >= 0) {
        remainder = subInts(remainder, b);
        q++;
      }
      quotient += String(q);
    }
    quotient = stripLeadingZeros(quotient) || "0";
    return (neg && quotient !== "0" ? "-" : "") + quotient;
  }

  function eurPerBtc(eurStr, btcStr) {
    var eurCents = grossCentsStr(eurStr);
    var sats = btcStringToSats(btcStr);
    if (!eurCents || sats === "0" || eurCents.charAt(0) === "-" || sats.charAt(0) === "-") return "";
    var priceCents = divideInts(decimalProduct(eurCents, "100000000"), sats);
    return centsToDe(priceCents);
  }

  function remainingSats(a) {
    return subInts(btcStringToSats(a.btc_amount), String(a.allocated_sats || 0));
  }

  function remainingBtc(a) {
    return Fmt.btc(remainingSats(a));
  }

  function disposedSats(a) {
    var total = "0";
    (a.disposals || []).forEach(function (d) {
      total = sumInts(total, String(d.allocated_sats || 0));
    });
    return total;
  }

  function sumInts(a, b) {
    a = stripLeadingZeros(a || "0");
    b = stripLeadingZeros(b || "0");
    var out = [];
    var carry = 0;
    var i = a.length - 1;
    var j = b.length - 1;
    while (i >= 0 || j >= 0 || carry) {
      var av = i >= 0 ? a.charCodeAt(i) - 48 : 0;
      var bv = j >= 0 ? b.charCodeAt(j) - 48 : 0;
      var v = av + bv + carry;
      out.push(String(v % 10));
      carry = Math.floor(v / 10);
      i--;
      j--;
    }
    return out.reverse().join("");
  }

  function disposedBtc(a) {
    return Fmt.btc(disposedSats(a));
  }

  function positionBadgeClass(state) {
    if (state === "CURRENT") return "pos-current";
    if (state === "NEEDS_REVIEW") return "pos-needs-review";
    if (state === "PARTIALLY_DISPOSED") return "pos-partial";
    if (state === "DISPOSED") return "pos-disposed";
    if (state === "TRACING") return "pos-tracing";
    return "pos-unknown";
  }

  function positionBadgeHtml(a) {
    var state = a.position_state || "CURRENT";
    var label = state === "PARTIALLY_DISPOSED" ? "PARTIAL" : state;
    return '<span class="position-badge ' + positionBadgeClass(state) + '">' + label + "</span>";
  }

  function positionStateLabel(state) {
    return state === "PARTIALLY_DISPOSED" ? "PARTIAL" : state;
  }

  function filterLabelFor(key) {
    for (var i = 0; i < FILTERS.length; i++) {
      if (FILTERS[i].key === key) return FILTERS[i].label;
    }
    return "All";
  }

  function filterTabsHtml() {
    return '<div class="acq-filter-bar" id="acq-filter-bar-inner"></div>';
  }

  function isMembershipFilter(key) {
    return FILTERS.some(function (f) { return f.key === key && f.dimension === "wallet_membership"; });
  }

  function membershipBadgeHtml(a) {
    if (!walletPositions || walletPositions[a.id] !== OUTSIDE_WALLETS) return "";
    return '<span class="position-badge" title="Wallet membership, separate from Acquisition position state">' +
      filterLabelFor(OUTSIDE_WALLETS) + '</span>';
  }

  function updateWalletPositions(el, report, status) {
    walletPositions = null;
    walletPositionsStatus = status;
    if (report && Array.isArray(report.acquisition_wallet_positions)) {
      walletPositions = Object.create(null);
      report.acquisition_wallet_positions.forEach(function (p) {
        walletPositions[p.acquisition_id] = p.state;
      });
      walletPositionsStatus = "ready";
    }
    renderFilterTabs(el, positionCounts);
    if (!WORKSPACE && isMembershipFilter(CURRENT_FILTER)) {
      renderLoadedAcquisitions(el);
    } else {
      // Coverage may finish after the user opens an inline Exit/Disposal editor.
      // Update only its badge slot; do not replace position-filter rows/editors.
      el.querySelectorAll('[data-acq-membership]').forEach(function (slot) {
        slot.innerHTML = membershipBadgeHtml({ id: Number(slot.dataset.acqMembership) });
      });
    }
  }

  function renderLoadedAcquisitions(el) {
    if (loadedAcquisitions === null) return;
    var visible = loadedAcquisitions;
    if (isMembershipFilter(CURRENT_FILTER)) {
      if (!walletPositions) {
        el.querySelector("#acq-list").innerHTML = '<div class="panel hint">' +
          (walletPositionsStatus === "loading" ? "Loading wallet membership…" :
            "Wallet membership unavailable. Refresh Coverage to retry.") + '</div>';
        return;
      }
      visible = visible.filter(function (a) { return walletPositions[a.id] === OUTSIDE_WALLETS; });
    }
    renderList(el, visible);
  }

  function renderFilterTabs(el, counts) {
    counts = counts || {};
    var box = el.querySelector("#acq-filter-bar");
    var html = (WORKSPACE ? '<span class="prov-filter-label" title="Counts are Acquisition records, not UTXOs. Not in current wallets is a membership predicate, not a position state.">Acquisitions</span>' : '') + '<div class="acq-filter-tabs">';
    for (var i = 0; i < FILTERS.length; i++) {
      var f = FILTERS[i];
      var active = (CURRENT_FILTER === f.key && (!WORKSPACE || WORKSPACE.acquisitionView())) ? " active" : "";
      var count = f.key == null ? (counts.total || 0) : (counts[f.key] || 0);
      if (f.dimension === "wallet_membership" && !WORKSPACE) {
        count = walletPositions ? Object.keys(walletPositions).filter(function (id) {
          return walletPositions[id] === f.key;
        }).length : (walletPositionsStatus === "loading" ? "…" : "—");
      }
      html += '<button class="btn small acq-filter-tab' + active + '" data-filter="' + (f.key || "") + '">' +
        f.label + " (" + count + ")</button>";
    }
    html += "</div>";
    box.innerHTML = html;
    box.querySelectorAll(".acq-filter-tab").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var key = btn.getAttribute("data-filter");
        CURRENT_FILTER = key === "" ? null : key;
        if (WORKSPACE) {
          closeExitEditor(el);
          closeDisposalEditor(el);
          WORKSPACE.filter(el, CURRENT_FILTER);
          renderFilterTabs(el, positionCounts);
          return;
        }
        loadList(el);
        loadPositionCounts(el);
      });
    });
  }

  function loadPositionCounts(el) {
    var request = ++countsRequest;
    API.positionCounts(!!WORKSPACE, WORKSPACE && WORKSPACE.walletScope ? WORKSPACE.walletScope() : null).then(function (counts) {
      if (request !== countsRequest) return;
      positionCounts = counts;
      renderFilterTabs(el, positionCounts);
    }).catch(function () {
      if (request !== countsRequest) return;
      positionCounts = {};
      renderFilterTabs(el, positionCounts);
    });
  }

  function loadList(el) {
    if (WORKSPACE) { WORKSPACE.load(el, CURRENT_FILTER); return; }
    var request = ++listRequest;
    loadedAcquisitions = null;
    loadCoverage(el);
    el.querySelector("#acq-list").innerHTML = '<div class="loading"><span class="spinner"></span>Loading acquisitions…</div>';
    API.listAcquisitions(isMembershipFilter(CURRENT_FILTER) ? null : CURRENT_FILTER).then(function (acqs) {
      if (request !== listRequest) return;
      loadedAcquisitions = acqs;
      renderLoadedAcquisitions(el);
    }).catch(function (err) {
      if (request !== listRequest) return;
      el.querySelector("#acq-list").innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>";
    });
  }

  function coverageHtml(report) {
    var linkLabels = {
      PROVENANCE_LINKED: "Established",
      UNANCHORED: "UNANCHORED",
      NEEDS_REVIEW: "Unresolved",
    };
    function percent(bps) { return bps == null ? "—" : (bps / 100).toFixed(2) + "%"; }
    var html = report.total_sats === 0
      ? '<p><strong>No current self-custody holdings.</strong></p>'
      : '<p><strong>' + percent(report.provenance_coverage_percentage_bps) +
        ' of current BTC is held in UTXOs with a documented acquisition provenance path.</strong></p>' +
        '<p>Provenance-linked BTC: <strong>' + Fmt.btc(report.provenance_linked_sats) +
        '</strong> · Current managed BTC: <strong>' + Fmt.btc(report.total_sats) + '</strong>' +
        ' · Unanchored BTC: <strong>' + Fmt.btc(report.unanchored_sats) + '</strong></p>' +
        '<p>UTXOs with provenance path: ' + report.provenance_linked_utxo_count + ' / ' + report.utxo_count +
        ' · UNANCHORED UTXOs: ' + report.unanchored_utxo_count +
        ' · Unresolved: ' + report.provenance_unresolved_utxo_count + '</p>';
    html += '<p class="hint">A linked UTXO has at least one on-chain path to an Acquisition anchor with user-supplied provenance. ' +
      'Its current value counts once, even after consolidation. This does not prove acquisition or legal ownership, or that every satoshi has documented origins; no within-UTXO allocation is made.</p>';
    html += '<p class="hint">UNANCHORED means no documented Acquisition anchor for this managed lineage. It says nothing about origin, ownership, legality or taxes. ' +
      'PARTIAL identifies mixed documented/undocumented ancestry; a linked path does not make the entire output fully acquisition-backed.</p>';
    if (report.provenance_unresolved_utxo_count) {
      html += '<p class="hint">Linkage is unresolved for ' + Fmt.btc(report.provenance_unresolved_sats) +
        ' BTC. These holdings remain in current BTC but are not counted as provenance-linked.</p>';
    }
    html += '<p class="hint">Independent Wallet Inventory, counted once by TXID:VOUT and checked against Bitcoin Core, including mempool spends. ' +
      'This is not a claim that wallet discovery is complete. Refresh known scripts or discover new addresses on the Wallet Inventory page.</p>';
    html += '<p>Documented — not in current wallets: <strong>' + Fmt.btc(report.documented_not_in_current_wallet_sats) +
      '</strong> BTC. This separate amount is excluded from Current managed BTC and its coverage denominator. ' +
      'Acquisition documentation is unchanged; absence from configured wallets does not establish private-key loss.</p>' +
      '<p class="hint">Acquisition CURRENT position status is separate from current configured wallet membership.</p>' +
      '<button class="btn small" id="coverage-wallet-positions">Review wallet positions</button>';
    if (report.inventory && report.inventory.refresh_required) {
      html += '<p class="alert warn">Inventory refresh required: totals cover only the available snapshot, not all configured addresses.</p>';
    }
    if (report.utxos.length) {
      html += '<label>Filter managed UTXOs <select id="coverage-filter">' +
        [['', 'All'], ['UNANCHORED', 'UNANCHORED'], ['ANCHORED', 'Anchored'], ['PARTIAL', 'Partial'], ['NEEDS_REVIEW', 'Unresolved']].map(function (option) {
          return '<option value="' + option[0] + '"' + (COVERAGE_FILTER === option[0] ? ' selected' : '') + '>' + option[1] + '</option>';
        }).join('') + '</select></label>';
      html += '<details open><summary>Current UTXO provenance details</summary><div style="overflow-x:auto">' +
        '<table class="acq-table"><thead><tr><th>TXID:VOUT</th><th>Sats</th><th>Provenance path</th>' +
        '<th>Acquisition ancestry</th><th>Provenance structure</th><th>Connected Acquisitions</th></tr></thead><tbody>';
      var visible = report.utxos.filter(function (u) { return !COVERAGE_FILTER || u.acquisition_provenance === COVERAGE_FILTER; });
      visible.forEach(function (u) {
        html += '<tr><td class="mono"><button class="btn small" data-coverage-output="' + Fmt.esc(u.outpoint) + '">' + Fmt.esc(u.outpoint) + '</button></td><td>' + Fmt.sats(u.amount_sats) +
          '</td><td>' + linkLabels[u.provenance_link_state] + '</td><td>' + Fmt.acquisitionBadge(u.acquisition_provenance) +
          '</td><td>' + Fmt.esc(u.provenance_state) +
          '</td><td>' + Fmt.esc(u.acquisition_ids.join(", ") || "—") + '</td></tr>';
      });
      if (!visible.length) html += '<tr><td colspan="6">No matching managed UTXOs.</td></tr>';
      html += '</tbody></table></div></details>';
    }
    return html;
  }

  function renderCoverage(target, report) {
    target.innerHTML = coverageHtml(report);
    target.querySelector('#coverage-wallet-positions').addEventListener('click', function () { window.App.showView('inventory'); });
    var filter = target.querySelector('#coverage-filter');
    if (filter) filter.addEventListener('change', function () {
      COVERAGE_FILTER = this.value;
      renderCoverage(target, report);
    });
    target.querySelectorAll('[data-coverage-output]').forEach(function (button) {
      button.addEventListener('click', function () {
        var key = button.dataset.coverageOutput.split(':');
        window.App.openOutpoint(key[0], Number(key[1]));
      });
    });
  }

  function stopCoverage() {
    ++coverageRequest;
    if (coverageController) coverageController.abort();
    coverageController = null;
  }

  function loadCoverage(el) {
    if (WORKSPACE) { loadList(el); return; }
    var target = el.querySelector("#documentation-coverage");
    if (!target) return;
    stopCoverage();
    var request = ++coverageRequest;
    var controller = coverageController = new AbortController();
    // Transport safeguard; the backend has its own overall execution budget.
    var timer = setTimeout(function () { controller.abort(); }, 45000);
    updateWalletPositions(el, null, "loading");
    target.innerHTML = '<p class="hint">Checking current self-custody holdings…</p>';
    API.documentationCoverage(controller.signal).then(function (report) {
      if (request === coverageRequest) {
        renderCoverage(target, report);
        updateWalletPositions(el, report, "unavailable");
      }
    }).catch(function (err) {
      if (request === coverageRequest) {
        updateWalletPositions(el, null, "unavailable");
        target.innerHTML = '<p>Documentation Coverage unavailable. ' +
          Fmt.esc(err.name === "AbortError" ? "Request timed out. Please retry." : err.message) + '</p>';
      }
    }).finally(function () {
      clearTimeout(timer);
      if (coverageController === controller) coverageController = null;
    });
  }

  function disposalEditorHtml() {
    return '<div class="panel disposal-editor" id="disposal-editor">' +
      '<h3 id="disp-editor-title">Record Sale</h3>' +
      '<div class="disp-grid">' +
      formRow("Sale date", '<input type="date" id="disp-date" required />') +
      formRow("BTC sold", '<input type="text" id="disp-btc" placeholder="0.00100000" required />') +
      formRow("Gross proceeds EUR", '<input type="text" id="disp-gross" placeholder="1000,00" required />') +
      formRow("Sale price EUR/BTC", '<input type="text" id="disp-price" placeholder="optional" />') +
      formRow("Fee %", '<input type="text" id="disp-fee-pct" placeholder="1,00" />') +
      formRow("Broker", '<input type="text" id="disp-broker" placeholder="optional" />') +
      formRow("Reference", '<input type="text" id="disp-reference" placeholder="optional" />') +
      '<div class="acq-field span-3"><label>Notes</label><input type="text" id="disp-notes" placeholder="optional" /></div>' +
      "</div>" +
      '<div class="disp-preview" id="disp-preview"></div>' +
      '<div class="disp-allocations" id="disp-allocations"></div>' +
      '<div class="disp-evidence" id="disp-evidence"></div>' +
      '<div class="disp-actions">' +
      '<button class="btn primary" id="disp-save">Save Sale</button>' +
      '<button class="btn" id="disp-cancel">Cancel</button>' +
      "</div>" +
      '<div id="disp-feedback"></div>' +
      "</div>";
  }

  function setDisposalFeedback(editorEl, kind, html) {
    editorEl.querySelector("#disp-feedback").innerHTML = '<div class="alert ' + kind + '">' + html + "</div>";
  }

  function updateDisposalPreview(editorEl) {
    var box = editorEl.querySelector("#disp-preview");
    if (!box) return;
    var gross = Fmt.normalizeEconomicDecimal(editorEl.querySelector("#disp-gross").value);
    var pct = Fmt.normalizeFeePercent(editorEl.querySelector("#disp-fee-pct").value);
    var btc = editorEl.querySelector("#disp-btc").value.trim();
    if (!gross || gross.charAt(0) === "-" || !btc) {
      box.innerHTML = "";
      return;
    }
    var fee = pct && pct.charAt(0) !== "-" ? feeEurosPreview(gross, pct) : null;
    var netStr = pct && pct.charAt(0) !== "-" ? netEurosPreview(gross, pct) : centsToDe(grossCentsStr(gross));
    var price = eurPerBtc(gross, btc);
    var html = '<div class="disp-preview-line"><span class="ec">Gross proceeds</span> €' + centsToDe(grossCentsStr(gross)) + "</div>";
    if (fee) html += '<div class="disp-preview-line"><span class="ec">Fee EUR</span> €' + fee + "</div>";
    html += '<div class="disp-preview-line"><span class="ec">Net proceeds</span> €' + netStr + "</div>";
    if (price) html += '<div class="disp-preview-line"><span class="ec">Effective BTC price</span> €' + price + "/BTC</div>";
    box.innerHTML = html;
  }

  function renderAllocationRow(editorEl, a, allocatedBtc) {
    var remaining = remainingBtc(a);
    var box = editorEl.querySelector("#disp-allocations");
    box.innerHTML = '<div class="disp-alloc-title">Allocation</div>' +
      '<table class="disp-alloc-table">' +
      "<thead><tr><th>Acquisition</th><th>Total BTC</th><th>Remaining BTC</th><th>Allocate BTC</th></tr></thead>" +
      '<tbody><tr data-acq-id="' + a.id + '">' +
      "<td>#" + a.id + " " + Fmt.esc(a.source || "") + "</td>" +
      "<td>" + Fmt.esc(a.btc_amount) + "</td>" +
      "<td>" + remaining + "</td>" +
      '<td><input type="text" class="disp-alloc-btc" value="' + Fmt.esc(allocatedBtc || remaining) + '" /></td>' +
      "</tr></tbody></table>";
  }

  function renderDisposalCandidates(editorEl, candidates, selectedEvidence) {
    var box = editorEl.querySelector("#disp-evidence");
    var html = '<div class="disp-evidence-title">Blockchain evidence (optional)</div>' +
      '<label class="disp-evidence-none"><input type="radio" name="disp-evidence" value="" checked /> No blockchain evidence</label>';
    candidates.forEach(function (c, idx) {
      var sourceTxid = c.txid || c.source_txid;
      var sourceVout = c.vout != null ? c.vout : c.source_vout;
      var checked = selectedEvidence && selectedEvidence.source_txid === sourceTxid && selectedEvidence.source_vout === sourceVout ? " checked" : "";
      html += '<div class="disp-evidence-item">' +
        '<label><input type="radio" name="disp-evidence" value="' + idx + '"' + checked + " /> " +
        "Source " + Fmt.esc(Fmt.shortTxid(sourceTxid, 12)) + ":" + sourceVout +
        " · Spent by " + Fmt.esc(Fmt.shortTxid(c.spending_txid, 12)) +
        " · " + Fmt.btc(c.amount_sats) + " BTC</label>" +
        "</div>";
    });
    box.innerHTML = html;
  }

  function buildDisposalPayload(editorEl, acq) {
    var gross = Fmt.normalizeEconomicDecimal(editorEl.querySelector("#disp-gross").value);
    var pct = Fmt.normalizeFeePercent(editorEl.querySelector("#disp-fee-pct").value) || null;
    var price = Fmt.normalizeEconomicDecimal(editorEl.querySelector("#disp-price").value) || null;
    var btc = editorEl.querySelector("#disp-btc").value.trim();
    var allocatedSats = btcStringToSats(btc);
    var remaining = remainingSats(acq);
    if (compareInts(allocatedSats, remaining) > 0) allocatedSats = remaining;

    var evidence = [];
    var selected = editorEl.querySelector('input[name="disp-evidence"]:checked');
    if (selected && selected.value !== "" && DISPOSAL_EDITOR && DISPOSAL_EDITOR.candidates) {
      var c = DISPOSAL_EDITOR.candidates[Number(selected.value)];
      if (c) {
        evidence.push({
          source_txid: c.txid || c.source_txid,
          source_vout: c.vout != null ? c.vout : c.source_vout,
          spending_txid: c.spending_txid,
          destination_vout: c.destination_vout != null ? c.destination_vout : null,
          destination_address: c.destination_address || null,
          amount_sats: c.amount_sats,
        });
      }
    }

    return {
      disposal_date: editorEl.querySelector("#disp-date").value,
      disposal_time: null,
      btc_amount: btc,
      gross_proceeds_eur: gross,
      sale_price_eur_per_btc: price,
      fee_percent: pct,
      broker: editorEl.querySelector("#disp-broker").value.trim() || null,
      reference: editorEl.querySelector("#disp-reference").value.trim() || null,
      notes: editorEl.querySelector("#disp-notes").value.trim() || null,
      allocations: [{ acquisition_id: acq.id, allocated_sats: Number(allocatedSats) }],
      blockchain_evidence: evidence,
    };
  }

  function saveDisposal(editorEl) {
    if (!DISPOSAL_EDITOR) return;
    var acq = DISPOSAL_EDITOR.acq;
    var payload = buildDisposalPayload(editorEl, acq);

    if (!payload.disposal_date || !payload.btc_amount || !payload.gross_proceeds_eur) {
      setDisposalFeedback(editorEl, "error", "Please fill every required field.");
      return;
    }
    if (payload.allocations[0].allocated_sats <= 0) {
      setDisposalFeedback(editorEl, "error", "BTC sold must be greater than zero.");
      return;
    }

    var savePromise;
    if (DISPOSAL_EDITOR.mode === "edit") {
      savePromise = API.updateDisposal(DISPOSAL_EDITOR.disposalId, payload);
    } else {
      savePromise = API.createDisposal(payload);
    }
    savePromise.then(function (r) {
      if (r && r.warning) {
        setDisposalFeedback(editorEl, "warn", Fmt.esc(r.warning));
        return;
      }
      var containerEl = DISPOSAL_EDITOR.containerEl;
      closeDisposalEditor(containerEl);
      loadList(containerEl);
      loadPositionCounts(containerEl);
    }).catch(function (err) {
      setDisposalFeedback(editorEl, err.status === 409 ? "warn" : "error", Fmt.esc(err.message));
    });
  }

  function closeDisposalEditor(el) {
    var editor = el.querySelector("#disposal-editor");
    if (editor) editor.parentNode.removeChild(editor);
    var detail = el.querySelector("#disposal-detail");
    if (detail) detail.parentNode.removeChild(detail);
    DISPOSAL_EDITOR = null;
  }

  function openDisposalEditor(containerEl, acq, mode, disposal) {
    closeDisposalEditor(containerEl);
    var row = actionRow(containerEl, acq.id);
    if (!row) return;
    var wrapper = document.createElement("div");
    wrapper.innerHTML = disposalEditorHtml();
    var editor = wrapper.firstElementChild;
    row.parentNode.insertBefore(editor, row.nextSibling);

    var editorEl = containerEl.querySelector("#disposal-editor");
    DISPOSAL_EDITOR = {
      mode: mode || "create",
      disposalId: disposal ? disposal.disposal_id : null,
      acqId: acq.id,
      acq: acq,
      containerEl: containerEl,
      el: editorEl,
      candidates: [],
    };

    editorEl.querySelector("#disp-editor-title").textContent = mode === "edit" ? "Edit Sale" : "Record Sale";

    if (mode === "edit" && disposal) {
      editorEl.querySelector("#disp-date").value = disposal.disposal_date;
      editorEl.querySelector("#disp-btc").value = Fmt.btc(disposal.allocated_sats);
      editorEl.querySelector("#disp-gross").value = disposal.gross_proceeds_eur;
      editorEl.querySelector("#disp-price").value = disposal.sale_price_eur_per_btc || "";
      editorEl.querySelector("#disp-fee-pct").value = disposal.fee_percent || "";
      editorEl.querySelector("#disp-broker").value = disposal.broker || "";
      editorEl.querySelector("#disp-reference").value = disposal.reference || "";
      editorEl.querySelector("#disp-notes").value = disposal.notes || "";
    } else {
      editorEl.querySelector("#disp-date").value = acq.acquisition_date;
      editorEl.querySelector("#disp-btc").value = remainingBtc(acq);
    }

    renderAllocationRow(editorEl, acq, editorEl.querySelector("#disp-btc").value);

    editorEl.querySelector("#disp-gross").addEventListener("input", function () { updateDisposalPreview(editorEl); });
    editorEl.querySelector("#disp-fee-pct").addEventListener("input", function () { updateDisposalPreview(editorEl); });
    editorEl.querySelector("#disp-btc").addEventListener("input", function () {
      renderAllocationRow(editorEl, acq, editorEl.querySelector("#disp-btc").value);
      updateDisposalPreview(editorEl);
    });

    editorEl.querySelector("#disp-save").addEventListener("click", function () { saveDisposal(editorEl); });
    editorEl.querySelector("#disp-cancel").addEventListener("click", function () { closeDisposalEditor(containerEl); });

    updateDisposalPreview(editorEl);

      editorEl.querySelector("#disp-evidence").innerHTML = '<div class="disp-evidence-title">Blockchain evidence (optional)</div><div class="loading"><span class="spinner"></span>Loading candidates…</div>';
      API.disposalCandidates(acq.id).then(function (candidates) {
        if (!DISPOSAL_EDITOR || DISPOSAL_EDITOR.acqId !== acq.id) return;
        DISPOSAL_EDITOR.candidates = candidates || [];
        var selectedEvidence = null;
        if (mode === "edit" && disposal && disposal.blockchain_evidence && disposal.blockchain_evidence.length) {
          selectedEvidence = disposal.blockchain_evidence[0];
        }
        renderDisposalCandidates(editorEl, DISPOSAL_EDITOR.candidates, selectedEvidence);
      }).catch(function (err) {
        if (!DISPOSAL_EDITOR || DISPOSAL_EDITOR.acqId !== acq.id) return;
        editorEl.querySelector("#disp-evidence").innerHTML = '<div class="disp-evidence-title">Blockchain evidence (optional)</div><div class="alert error">' + Fmt.esc(err.message) + "</div>";
      });
  }

  function disposalDetailHtml(acq) {
    var html = '<div class="panel disposal-detail" id="disposal-detail">' +
      '<h3>Sale records for acquisition #' + acq.id + "</h3>" +
      '<div class="disp-detail-list">';
    (acq.disposals || []).forEach(function (d) {
      html += '<div class="disp-detail-row" data-did="' + d.disposal_id + '">' +
        "<div><strong>" + Fmt.esc(d.disposal_date) + "</strong> · " + Fmt.btc(d.allocated_sats) + " BTC</div>" +
        "<div>Gross €" + Fmt.deMoney(d.gross_proceeds_eur) + " · Net €" + Fmt.deMoney(d.net_proceeds_eur) +
        (d.broker ? " · " + Fmt.esc(d.broker) : "") + "</div>" +
        '<div class="disp-detail-actions">' +
        '<button class="btn small edit-disposal-btn" data-did="' + d.disposal_id + '">Edit</button>' +
        '<button class="btn small danger delete-disposal-btn" data-did="' + d.disposal_id + '">Delete</button>' +
        "</div></div>";
    });
    html += '</div><div class="disp-actions"><button class="btn" id="disp-detail-close">Close</button></div></div>';
    return html;
  }

  function openDisposalDetail(containerEl, acq) {
    closeDisposalEditor(containerEl);
    var row = actionRow(containerEl, acq.id);
    if (!row) return;
    var wrapper = document.createElement("div");
    wrapper.innerHTML = disposalDetailHtml(acq);
    var detail = wrapper.firstElementChild;
    row.parentNode.insertBefore(detail, row.nextSibling);

    detail.querySelectorAll(".edit-disposal-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        var did = Number(b.getAttribute("data-did"));
        var disposal = (acq.disposals || []).find(function (d) { return d.disposal_id === did; });
        if (disposal) openDisposalEditor(containerEl, acq, "edit", disposal);
      });
    });
    detail.querySelectorAll(".delete-disposal-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        var did = Number(b.getAttribute("data-did"));
        if (!confirm("Delete this sale record? The acquisition will be updated to reflect the remaining amount.")) return;
        API.deleteDisposal(did).then(function () {
          closeDisposalEditor(containerEl);
          loadList(containerEl);
          loadPositionCounts(containerEl);
        }).catch(function (err) {
          setFeedback(containerEl, "error", Fmt.esc(err.message));
        });
      });
    });
    detail.querySelector("#disp-detail-close").addEventListener("click", function () {
      closeDisposalEditor(containerEl);
    });
  }

  function exitDetailHtml(acq) {
    var html = '<div class="panel exit-detail" id="exit-detail">' +
      '<h3>Confirmed Exchange Exit records for acquisition #' + acq.id + "</h3>" +
      '<p class="hint">An Exchange Exit is the confirmed custody boundary where BTC left this ' +
      'acquisition. Removing it releases the source(s) from this Exit only - the Acquisition, ' +
      'its provenance trace and all other records are preserved and the position state is ' +
      're-derived from the remaining evidence.</p>' +
      '<div class="exit-detail-list">';
    (acq.exchange_exits || []).forEach(function (x) {
      var fullFee = x.fee_sats != null ? x.fee_sats : (x.network_fee_sats || 0);
      var unattributed = Number(x.unattributed_inputs || 0);
      // FX-2: involvement through inputs attributed to no single Acquisition
      // is shown without an amount; it reduces no remaining amount.
      var amounts = Number(x.allocated_sats || 0) > 0 || !unattributed ?
        '<div><span class="ec">SOURCE</span> ' + Fmt.btc(x.allocated_sats) + " BTC</div>" +
        '<div><span class="ec">TRANSFERRED</span> ' + Fmt.btc(x.disposed_sats) + " BTC</div>" +
        (unattributed ? '<div class="hint">Also involved through ' + unattributed + " input(s) not attributed to a single Acquisition (no amount).</div>" : "") :
        '<div><span class="ec">INVOLVED</span> Involved (not attributed) <span class="hint">through ' + unattributed + " input(s) not attributed to a single Acquisition; no amount.</span></div>";
      html += '<div class="exit-detail-row" data-xid="' + x.exit_id + '">' +
        '<div><span class="ec">EXIT DATE</span> ' + Fmt.esc(x.exit_date) + "</div>" +
        amounts +
        '<div><span class="ec">MINER FEE</span> ' + Fmt.btc(fullFee) + " BTC <span class=\"hint\">(full transaction fee)</span></div>" +
        '<div><span class="ec">EXIT TXID</span> ' + Fmt.esc(Fmt.shortTxid(x.txid, 12)) + "</div>" +
        (x.broker ? '<div><span class="ec">BROKER</span> ' + Fmt.esc(x.broker) + "</div>" : "") +
        '<div class="exit-detail-actions">' +
        '<button class="btn small edit-exit-btn" data-xid="' + x.exit_id + '">Edit</button>' +
        '<button class="btn small danger delete-exit-btn" data-xid="' + x.exit_id + '">Delete Exchange Exit</button>' +
        "</div></div>";
    });
    html += '</div>' +
      (closedViaUnattributedExit(acq) ?
        '<div class="exit-detail-remaining hint">Closed via a non-attributed exit input</div>' :
        '<div class="exit-detail-remaining"><span class="ec">REMAINING</span> ' + remainingBtc(acq) + " BTC</div>") +
      '<div class="exit-actions"><button class="btn" id="exit-detail-close">Close</button></div></div>';
    return html;
  }

  // "Delete Exchange Exit" and "Delete Acquisition" are two DIFFERENT destructive
  // concepts and must never share a label or a confirmation. Removing an Exit is
  // a classification revert (DELETE /api/exchange-exits/{id}); removing an
  // Acquisition destroys the record and its Acquisition-owned trace
  // (DELETE /api/acquisitions/{id}).
  //
  // The Exit is resolved server-side first so the confirmation can be honest
  // about multi-Acquisition Exits: one Exit may classify several Acquisition
  // sources, and deleting it releases ALL of them from that Exit. No TXID, vout,
  // address or amount is put in the confirmation text - it must not leak
  // watch-only material into a native dialog.
  function exchangeExitDeleteConfirmText(exit) {
    var ids = {};
    ((exit && exit.sources) || []).forEach(function (s) {
      if (s && s.acquisition_id != null) ids[s.acquisition_id] = true;
    });
    var n = Object.keys(ids).length;
    var scope = n > 1
      ? "This Exchange Exit classifies " + n + " Acquisition sources. Deleting it releases ALL " +
        n + " of those Acquisitions from this Exit."
      : "This Exchange Exit classifies 1 Acquisition source. Deleting it releases that " +
        "Acquisition from this Exit.";
    return "Delete this Exchange Exit?\n\n" + scope + "\n\n" +
      "This removes the Exchange Exit classification.\n" +
      "The Acquisition(s) and their provenance traces are preserved.\n" +
      "Their position state will be re-derived.\n\n" +
      "To remove the Acquisition record itself, use \"Delete Acquisition\" instead.";
  }

  function openExitDetail(containerEl, acq) {
    closeExitEditor(containerEl);
    var row = actionRow(containerEl, acq.id);
    if (!row) return;
    var wrapper = document.createElement("div");
    wrapper.innerHTML = exitDetailHtml(acq);
    var detail = wrapper.firstElementChild;
    row.parentNode.insertBefore(detail, row.nextSibling);

    detail.querySelectorAll(".edit-exit-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        var xid = Number(b.getAttribute("data-xid"));
        API.getExchangeExit(xid).then(function (exit) {
          openExitEditorForEdit(containerEl, acq, exit);
        }).catch(function (err) {
          setFeedback(containerEl, "error", Fmt.esc(err.message));
        });
      });
    });
    detail.querySelectorAll(".delete-exit-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        var xid = Number(b.getAttribute("data-xid"));
        // Resolve the Exit (never the Acquisition) to build an accurate
        // confirmation. If that read fails, fall back to the wording that is
        // correct for both the single- and multi-source case rather than
        // guessing a narrower one.
        API.getExchangeExit(xid).catch(function () { return null; }).then(function (exit) {
          if (!confirm(exchangeExitDeleteConfirmText(exit))) return;
          API.deleteExchangeExit(xid).then(function () {
            closeExitEditor(containerEl);
            refreshAfterExitDelete(containerEl, acq.id);
          }).catch(function (err) {
            setFeedback(containerEl, "error", Fmt.esc(err.message));
          });
        });
      });
    });
    detail.querySelector("#exit-detail-close").addEventListener("click", function () {
      closeExitEditor(containerEl);
    });
  }

  function canRecordExit(a) {
    var state = a.position_state;
    return state === "CURRENT" || state === "NEEDS_REVIEW" || state === "PARTIALLY_DISPOSED";
  }

  // Deleting an Exchange Exit removes only the Exit classification/evidence.
  // The Acquisition and its trace are untouched, so the backend re-derives its
  // position state from the remaining facts (for the usual confirmed-exit case
  // back to NEEDS_REVIEW). Nothing here decides that state - it is read back
  // from the canonical derivation, never hard-coded per deletion.
  //
  // The active position filter is a VIEW choice, so it must be re-pointed at
  // the filter that now contains this Acquisition. Re-fetching the list under
  // the stale filter (e.g. DISPOSED after the state moved to NEEDS_REVIEW)
  // would hide the Acquisition and read as if it had been deleted.
  function refreshAfterExitDelete(containerEl, acqId) {
    function reload() {
      loadPositionCounts(containerEl);
      loadList(containerEl);
    }
    return API.getAcquisition(acqId).then(function (a) {
      // The state reported below is READ BACK from the canonical derivation. It
      // is never computed or hard-coded here.
      if (a && CURRENT_FILTER != null && !isMembershipFilter(CURRENT_FILTER) && a.position_state &&
          a.position_state !== CURRENT_FILTER) {
        CURRENT_FILTER = a.position_state;
      }
      if (WORKSPACE) WORKSPACE.filter(containerEl, CURRENT_FILTER);
      reload();
      if (a) {
        setFeedback(containerEl, "ok",
          "Exchange Exit deleted. Acquisition #" + acqId + " and its provenance trace are preserved." +
          "<br /><span class=\"hint\">Canonical position state re-derived: <strong>" +
          Fmt.esc(positionStateLabel(a.position_state)) +
          "</strong> · now showing <strong>" +
          Fmt.esc(filterLabelFor(CURRENT_FILTER)) + "</strong>" +
          (isMembershipFilter(CURRENT_FILTER) ? "; wallet membership is refreshed separately." : " so the acquisition stays visible.") +
          "</span>");
      }
    }).catch(function () {
      reload();
    });
  }

  // --- v0.11.0 Exchange Exit editor ----------------------------------------

  function exitEditorHtml() {
    return '<div class="panel exit-editor" id="exit-editor">' +
      '<h3 id="exit-editor-title">Record Exchange Exit</h3>' +
      '<p class="hint exit-boundary-hint">An Exchange Exit marks the selected custody boundary. It does not record a sale, disposal or taxable event.</p>' +
      '<div class="exit-tx-info"><span class="ec">spending transaction</span> <span id="exit-txid"></span></div>' +
      '<div id="exit-candidates"></div>' +
      '<div class="exit-grid">' +
      formRow("Exit date", '<input type="date" id="exit-date" required />',
        "Proposed from the confirmed block of the spending transaction. Editable; never taken from the acquisition date.") +
      formRow("Exit time", '<input type="time" id="exit-time" />') +
      formRow("Exchange / Broker", '<input type="text" id="exit-broker" placeholder="optional" />') +
      formRow("Reference", '<input type="text" id="exit-reference" placeholder="optional" />') +
      '<div class="acq-field span-3"><label>Notes</label><input type="text" id="exit-notes" placeholder="optional" /></div>' +
      "</div>" +
      '<div class="exit-section">' +
      '<div class="exit-section-title">Inputs of the exit transaction (sources leaving self-custody)</div>' +
      '<div id="exit-sources"></div>' +
      '<div class="exit-totals" id="exit-source-totals"></div>' +
      "</div>" +
      '<div class="exit-section">' +
      '<div class="exit-section-title">Exchange deposit output(s)</div>' +
      '<div id="exit-outputs"></div>' +
      '<div class="exit-totals" id="exit-output-totals"></div>' +
      "</div>" +
      '<div class="exit-section">' +
      '<div class="exit-section-title">Proposed accounting allocation (confirm or adjust)</div>' +
      '<div id="exit-accounting"></div>' +
      "</div>" +
      '<div class="exit-section">' +
      '<div class="exit-section-title">Related acquisitions</div>' +
      '<div id="exit-acquisitions"></div>' +
      "</div>" +
      '<div class="exit-actions">' +
      '<button class="btn primary" id="exit-save">Save Exit</button>' +
      '<button class="btn" id="exit-cancel">Cancel</button>' +
      "</div>" +
      '<div id="exit-feedback"></div>' +
      "</div>";
  }

  function setExitFeedback(editorEl, kind, html) {
    editorEl.querySelector("#exit-feedback").innerHTML = '<div class="alert ' + kind + '">' + html + "</div>";
  }

  function allocateProportionally(total, weights) {
    total = Number(total || 0);
    var out = weights.map(function () { return 0; });
    if (total <= 0 || !weights.length) return out;
    var sum = weights.reduce(function (a, b) { return a + Number(b); }, 0);
    if (sum <= 0) return out;
    var allocated = 0;
    var fracs = weights.map(function (w, i) {
      var exact = total * Number(w) / sum;
      var share = Math.floor(exact);
      out[i] = share;
      allocated += share;
      return { idx: i, frac: exact - share, used: false };
    });
    var remainder = total - allocated;
    for (var r = 0; r < remainder; r++) {
      var maxIdx = -1;
      for (var k = 0; k < fracs.length; k++) {
        if (!fracs[k].used && (maxIdx === -1 || fracs[k].frac > fracs[maxIdx].frac)) {
          maxIdx = k;
        }
      }
      if (maxIdx === -1) maxIdx = r % fracs.length;
      out[fracs[maxIdx].idx] += 1;
      fracs[maxIdx].used = true;
    }
    return out;
  }

  // FX-1: the candidate lists every input of the exit transaction. Only an
  // input the provenance evaluation attributes to exactly one Acquisition is a
  // source; all others are shown with their class and anchors and are never
  // allocated. FX-2: an unattributed input (OWNED, anchored, but MIXED or from
  // several Acquisitions) is saved by the backend with its anchors and no
  // amount; it is never split.
  var EXIT_INPUT_CLASS_LABELS = { ACQUISITION: "Covered", MIXED: "Mixed", NO_ACQUISITION: "No acquisition (verified)", UNRESOLVED: "Needs Review" };

  function isExitSource(inp) {
    return inp.acquisition_id != null;
  }

  function exitInputAnchors(inp) {
    return (inp.anchor_acquisition_ids || []).map(function (id) { return "#" + id; })
      .concat((inp.no_acquisition_anchor_outpoints || []).map(function (op) {
        var parts = String(op).split(":");
        return Fmt.shortTxid(parts[0], 8) + ":" + parts[1];
      }));
  }

  // FIX-EXIT-5: an anchor list is shown once per input; more than 5 anchors
  // are collapsed behind their count ("60 acquisitions as anchors",
  // expandable). FIX-UI-6: English, correct singular/plural.
  function anchorCountLabel(n, allAcquisitions) {
    if (allAcquisitions) return n + (n === 1 ? " acquisition as anchor" : " acquisitions as anchors");
    return n + (n === 1 ? " anchor" : " anchors");
  }

  function exitAnchorListHtml(anchors, allAcquisitions) {
    if (!anchors.length) return "";
    if (anchors.length <= 5) return Fmt.esc(anchors.join(", "));
    var label = anchorCountLabel(anchors.length, allAcquisitions);
    return '<details class="exit-anchor-list"><summary>' + Fmt.esc(label) + "</summary>" +
      Fmt.esc(anchors.join(", ")) + "</details>";
  }

  function exitInputProvenance(inp) {
    var anchors = exitInputAnchors(inp);
    var allAcq = !(inp.no_acquisition_anchor_outpoints || []).length;
    var label = EXIT_INPUT_CLASS_LABELS[inp.provenance_class] || "—";
    return '<span class="hint exit-input-provenance">' + Fmt.esc(label) + (anchors.length ? " · " + exitAnchorListHtml(anchors, allAcq) : "") + "</span>";
  }

  // FX-2 status of a non-source input, worded neutrally. The anchors are shown
  // once, in the provenance line above (FIX-EXIT-5).
  function exitInputStatus(inp) {
    if (inp.unattributed) {
      var kind = inp.provenance_class === "MIXED" ? "MIXED" :
        (inp.anchor_acquisition_ids || []).length > 1 ? "several Acquisitions" :
        (EXIT_INPUT_CLASS_LABELS[inp.provenance_class] || "—");
      return "Not attributed (" + kind + "); saved without an amount, reduces no remaining amount.";
    }
    if (inp.ownership_status === "OWNED" || inp.ownership_status === "LIKELY_OWNED") return "No Acquisition anchor; not a source.";
    return "No ownership evidence (" + inp.ownership_status + "); not a source.";
  }

  function unattributedInputs(candidate) {
    return (candidate.inputs || []).filter(function (i) { return i.unattributed === true; });
  }

  function computeExitAllocations(candidate, selectedVouts) {
    var inputs = candidate.inputs || [];
    var inputAmounts = inputs.map(function (i) { return isExitSource(i) ? Number(i.amount_sats || 0) : 0; });
    var outputs = candidate.outputs || [];
    var depositTotal = 0;
    outputs.forEach(function (o) {
      if (selectedVouts.indexOf(o.vout) !== -1) depositTotal += Number(o.amount_sats || 0);
    });
    if (depositTotal <= 0) {
      return inputs.map(function () { return { disposed_sats: 0, network_fee_sats: 0 }; });
    }
    var feeTotal = candidate.fee_sats != null ? Number(candidate.fee_sats) : 0;
    if (feeTotal <= 0 && candidate.input_total_sats != null && candidate.output_total_sats != null) {
      feeTotal = Number(candidate.input_total_sats) - Number(candidate.output_total_sats);
    }
    var feeShares = allocateProportionally(feeTotal, inputAmounts);
    var depositShares = allocateProportionally(depositTotal, inputAmounts);
    return inputs.map(function (inp, i) {
      var maxAmount = Number(inp.amount_sats || 0);
      // FX-2: with unattributed inputs the deposit may exceed the sources; a
      // source never allocates more than its own amount, the rest stays
      // unattributed.
      var disposed = Math.min(depositShares[i], maxAmount);
      var fee = feeShares[i];
      if (disposed + fee > maxAmount) {
        fee = Math.max(0, maxAmount - disposed);
      }
      return { disposed_sats: disposed, network_fee_sats: fee };
    });
  }

  function selectedDepositVouts(editorEl) {
    var vouts = [];
    editorEl.querySelectorAll(".exit-deposit-check:checked").forEach(function (chk) {
      vouts.push(Number(chk.getAttribute("data-vout")));
    });
    return vouts;
  }

  function updateExitRemaining(row) {
    var amount = Number(row.getAttribute("data-amount")) || 0;
    var disposed = Number(btcStringToSats(row.querySelector(".exit-disposed-sats").value.trim())) || 0;
    var fee = Number(btcStringToSats(row.querySelector(".exit-network-fee").value.trim())) || 0;
    var remaining = amount - disposed - fee;
    if (remaining < 0) remaining = 0;
    row.querySelector(".exit-remaining").textContent = Fmt.btc(remaining) + " BTC";
  }

  function attachExitInputListeners(editorEl) {
    editorEl.querySelectorAll(".exit-disposed-sats, .exit-network-fee").forEach(function (input) {
      input.addEventListener("input", function () {
        updateExitRemaining(input.closest("tr"));
      });
    });
    editorEl.querySelectorAll(".exit-deposit-check").forEach(function (chk) {
      chk.addEventListener("change", function () {
        renderExitAccounting(editorEl, EXIT_EDITOR.candidate);
      });
    });
  }

  function renderExitSources(editorEl, candidate, allocations, readOnly) {
    var box = editorEl.querySelector("#exit-sources");
    if (!candidate || !candidate.inputs || !candidate.inputs.length) {
      box.innerHTML = '<div class="hint">No owned source inputs found.</div>';
      return;
    }
    var html = '<table class="exit-table"><thead><tr><th>Acquisition · Source</th><th>Original amount</th><th>Transferred to exchange</th><th>On-chain miner fee</th><th>Remaining</th></tr></thead><tbody>';
    candidate.inputs.forEach(function (inp, idx) {
      if (!isExitSource(inp)) {
        html += '<tr class="exit-input-context' + (inp.unattributed ? " exit-input-unattributed" : "") + '">' +
          '<td>— · ' + Fmt.esc(Fmt.shortTxid(inp.txid, 12)) + ":" + inp.vout + "<br/><span class=\"hint\">" + Fmt.esc(inp.address || "") + "</span></td>" +
          '<td>' + Fmt.btc(inp.amount_sats) + " BTC</td>" +
          '<td colspan="3">' + exitInputProvenance(inp) + '<br/><span class="hint exit-input-status">' +
          Fmt.esc(exitInputStatus(inp)) + "</span></td></tr>";
        return;
      }
      var alloc = (allocations && allocations[idx]) ? allocations[idx] : { disposed_sats: 0, network_fee_sats: 0 };
      var remaining = Number(inp.amount_sats || 0) - alloc.disposed_sats - alloc.network_fee_sats;
      var disposedVal = Fmt.btc(alloc.disposed_sats);
      var feeVal = Fmt.btc(alloc.network_fee_sats);
      html += '<tr data-input-idx="' + idx + '" data-amount="' + (inp.amount_sats || 0) + '">' +
        '<td>#' + (inp.acquisition_id || "?") + ' · ' + Fmt.esc(Fmt.shortTxid(inp.txid, 12)) + ":" + inp.vout + "<br/><span class=\"hint\">" + Fmt.esc(inp.address || "") + "</span><br/>" + exitInputProvenance(inp) + '<br/><span class="hint exit-input-status">Attributed to Acquisition #' + Fmt.esc(inp.acquisition_id) + "</span></td>" +
        '<td>' + Fmt.btc(inp.amount_sats) + " BTC</td>" +
        '<td>' + (readOnly ? '<span class="exit-readonly-value">' + disposedVal + " BTC</span>" : '<input type="text" class="exit-disposed-sats" data-idx="' + idx + '" value="' + disposedVal + '" />') + "</td>" +
        '<td>' + (readOnly ? '<span class="exit-readonly-value">' + feeVal + " BTC</span>" : '<input type="text" class="exit-network-fee" data-idx="' + idx + '" value="' + feeVal + '" />') + "</td>" +
        '<td class="exit-remaining" data-idx="' + idx + '">' + Fmt.btc(remaining) + " BTC</td>" +
        "</tr>";
      // v0.11.2: an UNKNOWN acquisition-origin source has ownership that is not
      // independently established (TRACE TARGET != OWNERSHIP EVIDENCE). The
      // user must explicitly confirm self-custody for THIS exit before saving.
      if (inp.ownership_confirmed && !readOnly) {
        html += '<tr class="exit-ownership-confirm-row"><td colspan="5">' +
          '<div class="exit-ownership-confirm-warn">Source ownership not independently established.</div>' +
          '<label class="exit-ownership-confirm-label"><input type="checkbox" class="exit-ownership-confirm" data-input-idx="' + idx + '" /> I confirm that this source outpoint was under my control.</label>' +
          "</td></tr>";
      }
    });
    html += "</tbody></table>";
    box.innerHTML = html;
  }

  function renderExitOutputs(editorEl, candidate, selectedVouts, readOnly) {
    var box = editorEl.querySelector("#exit-outputs");
    if (!candidate || !candidate.outputs || !candidate.outputs.length) {
      box.innerHTML = '<div class="hint">No outputs found.</div>';
      return;
    }
    var html = '<table class="exit-table"><thead><tr><th>Deposit?</th><th>Output</th><th>Address</th><th>Amount</th><th>Status</th></tr></thead><tbody>';
    var possibleDeposits = candidate.outputs.filter(function (o) { return o.possible_deposit; });
    candidate.outputs.forEach(function (o) {
      var isOwned = o.ownership_status === "OWNED" || o.ownership_status === "LIKELY_OWNED";
      var selected = selectedVouts && selectedVouts.indexOf(o.vout) !== -1;
      var checked = selected || (!readOnly && !isOwned && o.possible_deposit && (o.ownership_status !== "UNKNOWN" || possibleDeposits.length === 1));
      var checkAttr = checked ? " checked" : "";
      html += '<tr>' +
        '<td>' + (readOnly ? (checked ? "Yes" : "No") : '<input type="checkbox" class="exit-deposit-check" data-vout="' + o.vout + '"' + checkAttr + ' />') + "</td>" +
        '<td>' + o.vout + "</td>" +
        '<td>' + Fmt.esc(o.address || "—") + "</td>" +
        '<td>' + Fmt.btc(o.amount_sats) + " BTC</td>" +
        '<td>' + Fmt.esc(o.ownership_status) + (isOwned ? ' <span class="hint">(change)</span>' : "") + "</td>" +
        "</tr>";
    });
    html += "</tbody></table>";
    box.innerHTML = html;
  }

  function renderExitAccounting(editorEl, candidate) {
    var box = editorEl.querySelector("#exit-accounting");
    if (!candidate) {
      box.innerHTML = "";
      return;
    }
    var inputs = candidate.inputs || [];
    var outputs = candidate.outputs || [];
    var totalSource = inputs.reduce(function (a, i) { return isExitSource(i) ? a + Number(i.amount_sats || 0) : a; }, 0);
    var selected = selectedDepositVouts(editorEl);
    var depositTotal = outputs.reduce(function (a, o) { return selected.indexOf(o.vout) !== -1 ? a + Number(o.amount_sats || 0) : a; }, 0);
    var feeTotal = candidate.fee_sats != null ? Number(candidate.fee_sats) : 0;
    if (feeTotal <= 0 && candidate.input_total_sats != null && candidate.output_total_sats != null) {
      feeTotal = Number(candidate.input_total_sats) - Number(candidate.output_total_sats);
    }
    var notAttributed = unattributedInputs(candidate).reduce(function (a, i) { return a + Number(i.amount_sats || 0); }, 0);
    var remaining = totalSource - depositTotal - feeTotal;
    if (remaining < 0) remaining = 0;
    var html = '<div class="exit-accounting-line"><span class="ec">total source</span> ' + Fmt.btc(totalSource) + " BTC</div>" +
      (notAttributed ? '<div class="exit-accounting-line"><span class="ec">not attributed inputs</span> ' + Fmt.btc(notAttributed) + ' BTC <span class="hint">(no amount allocated to any Acquisition)</span></div>' : "") +
      '<div class="exit-accounting-line"><span class="ec">transferred to exchange</span> ' + Fmt.btc(depositTotal) + " BTC</div>" +
      '<div class="exit-accounting-line"><span class="ec">on-chain miner fee</span> ' + Fmt.btc(feeTotal) + " BTC</div>" +
      '<div class="exit-accounting-line"><span class="ec">remaining / change</span> ' + Fmt.btc(remaining) + " BTC</div>";
    box.innerHTML = html;
  }

  function renderExitAcquisitions(editorEl, candidate, allocations) {
    var box = editorEl.querySelector("#exit-acquisitions");
    if (!candidate || !candidate.acquisitions || !candidate.acquisitions.length) {
      // FIX-UI-6: a 0/0 involvement (FX-2: anchored but unattributed inputs)
      // is named with its anchors instead of the misleading "No linked
      // acquisitions." The same collapsible list as in the sources table.
      var anchors = [], seen = {}, allAcq = true;
      ((candidate && candidate.inputs) || []).forEach(function (inp) {
        if (!inp.unattributed) return;
        exitInputAnchors(inp).forEach(function (label) {
          if (!seen[label]) { seen[label] = true; anchors.push(label); }
        });
        if ((inp.no_acquisition_anchor_outpoints || []).length) allAcq = false;
      });
      if (anchors.length) {
        box.innerHTML = '<div class="hint">' + anchors.length +
          " involved, not attributed · " + exitAnchorListHtml(anchors, allAcq) + "</div>";
      } else {
        box.innerHTML = '<div class="hint">No linked acquisitions.</div>';
      }
      return;
    }
    var html = '<table class="exit-table"><thead><tr><th>Acquisition</th><th>Original amount</th><th>Transferred</th><th>Miner fee</th><th>Remaining</th></tr></thead><tbody>';
    candidate.acquisitions.forEach(function (acq) {
      var involved = 0, disposed = 0, fee = 0;
      candidate.inputs.forEach(function (inp, idx) {
        if (inp.acquisition_id === acq.acquisition_id) {
          involved += Number(inp.amount_sats || 0);
          var alloc = (allocations && allocations[idx]) ? allocations[idx] : { disposed_sats: 0, network_fee_sats: 0 };
          disposed += alloc.disposed_sats;
          fee += alloc.network_fee_sats;
        }
      });
      var remaining = involved - disposed - fee;
      html += '<tr>' +
         '<td>#' + acq.acquisition_id + " " + Fmt.esc(acq.source || "") + "</td>" +
        '<td>' + Fmt.btc(involved) + " BTC</td>" +
        '<td>' + Fmt.btc(disposed) + " BTC</td>" +
        '<td>' + Fmt.btc(fee) + " BTC</td>" +
        '<td>' + Fmt.btc(remaining) + " BTC</td>" +
        "</tr>";
    });
    html += "</tbody></table>";
    box.innerHTML = html;
  }

  function exitBtcInputToSats(inputEl) {
    if (!inputEl) return 0;
    return Number(btcStringToSats(inputEl.value.trim())) || 0;
  }

  function buildExitPayload(editorEl, candidate) {
    var sources = [];
    var rows = editorEl.querySelectorAll("#exit-sources tbody tr[data-input-idx]");
    rows.forEach(function (row) {
      var idx = Number(row.getAttribute("data-input-idx"));
      var inp = candidate.inputs[idx];
      if (!inp) return;
      var disposed = exitBtcInputToSats(row.querySelector(".exit-disposed-sats"));
      var fee = exitBtcInputToSats(row.querySelector(".exit-network-fee"));
      // v0.11.2: carry the user's per-source self-custody confirmation for the
      // narrow UNKNOWN origin fallback. Established OWNED sources send false.
      var needsConfirm = inp.ownership_confirmed === true;
      var confirmBox = editorEl.querySelector('#exit-sources .exit-ownership-confirm[data-input-idx="' + idx + '"]');
      var confirmed = !needsConfirm || (confirmBox && confirmBox.checked);
      sources.push({
        acquisition_id: inp.acquisition_id,
        source_txid: inp.txid,
        source_vout: inp.vout,
        disposed_sats: disposed,
        network_fee_sats: fee,
        ownership_confirmed: confirmed,
      });
    });

    var outputSelection = [];
    editorEl.querySelectorAll(".exit-deposit-check:checked").forEach(function (chk) {
      outputSelection.push({ vout: Number(chk.getAttribute("data-vout")), is_exchange_deposit: true });
    });

    return {
      txid: candidate.txid,
      exit_date: editorEl.querySelector("#exit-date").value,
      exit_time: editorEl.querySelector("#exit-time").value || null,
      broker: editorEl.querySelector("#exit-broker").value.trim() || null,
      reference: editorEl.querySelector("#exit-reference").value.trim() || null,
      notes: editorEl.querySelector("#exit-notes").value.trim() || null,
      sources: sources,
      output_selection: outputSelection,
    };
  }

  function saveExit(editorEl) {
    if (!EXIT_EDITOR) return;
    if (EXIT_EDITOR.mode === "edit") {
      var updatePayload = {
        exit_date: editorEl.querySelector("#exit-date").value,
        exit_time: editorEl.querySelector("#exit-time").value || null,
        broker: editorEl.querySelector("#exit-broker").value.trim() || null,
        reference: editorEl.querySelector("#exit-reference").value.trim() || null,
        notes: editorEl.querySelector("#exit-notes").value.trim() || null,
      };
      if (!updatePayload.exit_date) {
        setExitFeedback(editorEl, "error", "Please choose an exit date.");
        return;
      }
      API.updateExchangeExit(EXIT_EDITOR.exitId, updatePayload).then(function (r) {
        if (r && r.warning) {
          setExitFeedback(editorEl, "warn", Fmt.esc(r.warning));
          return;
        }
        var containerEl = EXIT_EDITOR.containerEl;
        closeExitEditor(containerEl);
        loadList(containerEl);
        loadPositionCounts(containerEl);
      }).catch(function (err) {
        setExitFeedback(editorEl, err.status === 409 ? "warn" : "error", Fmt.esc(err.message));
      });
      return;
    }

    if (!EXIT_EDITOR.candidate) return;
    var candidate = EXIT_EDITOR.candidate;
    var payload = buildExitPayload(editorEl, candidate);

    if (!payload.exit_date) {
      setExitFeedback(editorEl, "error", "Please choose an exit date.");
      return;
    }
    // FX-2: unattributed inputs are not in the payload; the backend stores
    // them with their anchors from its own provenance evaluation.
    if (!payload.sources.length && !unattributedInputs(candidate).length) {
      setExitFeedback(editorEl, "error", "No source outpoints to save.");
      return;
    }
    if (!payload.output_selection.length) {
      setExitFeedback(editorEl, "error", "Please select at least one exchange deposit output.");
      return;
    }
    var sourceError = false;
    payload.sources.forEach(function (s) {
      if (s.disposed_sats + s.network_fee_sats <= 0) sourceError = true;
    });
    if (sourceError) {
      setExitFeedback(editorEl, "error", "Disposed amount plus network fee share must be greater than zero.");
      return;
    }
    // v0.11.2: the narrow UNKNOWN origin fallback requires explicit per-source
    // confirmation of self-custody; Save is blocked until every such checkbox
    // is checked.
    var unconfirmed = [];
    candidate.inputs.forEach(function (inp, idx) {
      if (inp.ownership_confirmed !== true) return;
      var confirmBox = editorEl.querySelector('#exit-sources .exit-ownership-confirm[data-input-idx="' + idx + '"]');
      if (!confirmBox || !confirmBox.checked) unconfirmed.push("#" + (inp.acquisition_id || "?") + " · " + Fmt.shortTxid(inp.txid, 12) + ":" + inp.vout);
    });
    if (unconfirmed.length) {
      setExitFeedback(editorEl, "error", "Confirm self-custody for the source outpoint(s) " + Fmt.esc(unconfirmed.join(", ")) + " before recording this Exchange Exit.");
      return;
    }
    if (!confirm("Record this Exchange Exit? The selected output marks the end of tracked self-custody; no sale or Disposal is created.")) return;

    API.createExchangeExit(payload).then(function (r) {
      if (r && r.warning) {
        setExitFeedback(editorEl, "warn", Fmt.esc(r.warning));
        return;
      }
      var containerEl = EXIT_EDITOR.containerEl;
      closeExitEditor(containerEl);
      loadList(containerEl);
      loadPositionCounts(containerEl);
    }).catch(function (err) {
      setExitFeedback(editorEl, err.status === 409 ? "warn" : "error", Fmt.esc(err.message));
    });
  }

  function closeExitEditor(el) {
    var editor = el.querySelector("#exit-editor");
    if (editor) editor.parentNode.removeChild(editor);
    var detail = el.querySelector("#exit-detail");
    if (detail) detail.parentNode.removeChild(detail);
    EXIT_EDITOR = null;
  }

  // FIX-EXIT-5: one selectable line per candidate. Date (UTC block date),
  // shortened txid, sum of the possible deposit outputs, input count, number
  // of involved Acquisitions (or anchors, when no input is attributed), class.
  function exitCandidateLabel(c) {
    var deposit = (c.outputs || []).reduce(function (a, o) {
      return o.possible_deposit ? a + Number(o.amount_sats || 0) : a;
    }, 0);
    var acqCount = (c.acquisitions || []).length;
    var anchorKeys = {};
    var allAcq = true;
    (c.inputs || []).forEach(function (inp) {
      (inp.anchor_acquisition_ids || []).forEach(function (id) { anchorKeys["#" + id] = true; });
      (inp.no_acquisition_anchor_outpoints || []).forEach(function (op) { anchorKeys[String(op)] = true; allAcq = false; });
    });
    var anchorCount = Object.keys(anchorKeys).length;
    var involved = acqCount ? acqCount + (acqCount === 1 ? " acquisition" : " acquisitions") :
      anchorCountLabel(anchorCount, allAcq);
    return (c.suggested_exit_date || "unconfirmed") + " · " + Fmt.shortTxid(c.txid, 12) +
      " · " + Fmt.btc(deposit) + " BTC deposit · " + (c.inputs || []).length + " input(s) · " +
      involved + " · " + (EXIT_INPUT_CLASS_LABELS[c.provenance_class] || "—");
  }

  function renderExitCandidateChoice(editorEl, candidates, selectedIdx) {
    var box = editorEl.querySelector("#exit-candidates");
    if (!candidates || candidates.length < 2) {
      box.innerHTML = "";
      return;
    }
    var html = '<div class="exit-section-title">Candidate spending transactions (oldest first)</div>';
    candidates.forEach(function (c, idx) {
      html += '<div class="exit-candidate-item">' +
        '<label><input type="radio" name="exit-candidate" value="' + idx + '"' + (idx === selectedIdx ? " checked" : "") + " /> " +
        Fmt.esc(exitCandidateLabel(c)) + "</label></div>";
    });
    box.innerHTML = html;
    box.querySelectorAll('input[name="exit-candidate"]').forEach(function (radio) {
      radio.addEventListener("change", function () {
        selectExitCandidate(editorEl, Number(radio.getAttribute("value")));
      });
    });
  }

  // Render one candidate into the editor. On a switch everything from the
  // previous candidate is discarded (sources, outputs, allocations, feedback)
  // and the proposed exit date is reset from the new candidate (FIX-EXIT-5).
  function showExitCandidate(editorEl, candidate) {
    EXIT_EDITOR.candidate = candidate;
    editorEl.querySelector("#exit-txid").textContent = Fmt.esc(candidate.txid);
    // The Exchange Exit boundary is this spending transaction, so the
    // proposed Exit Date is the calendar date of its confirmed block. This
    // is a proposal only: the field stays user-editable, and when the
    // spending transaction has no confirmed block timestamp the field is
    // deliberately left empty instead of falling back to any other date.
    editorEl.querySelector("#exit-date").value = candidate.suggested_exit_date || "";
    editorEl.querySelector("#exit-feedback").innerHTML = "";
    var possibleDeposits = candidate.outputs.filter(function (o) { return o.possible_deposit; });
    var selectedVouts = possibleDeposits
      .filter(function (o) { return o.ownership_status !== "UNKNOWN" || possibleDeposits.length === 1; })
      .map(function (o) { return o.vout; });
    var allocations = computeExitAllocations(candidate, selectedVouts);
    renderExitSources(editorEl, candidate, allocations, false);
    renderExitOutputs(editorEl, candidate, selectedVouts, false);
    renderExitAccounting(editorEl, candidate);
    renderExitAcquisitions(editorEl, candidate, allocations);
    attachExitInputListeners(editorEl);
  }

  function selectExitCandidate(editorEl, idx) {
    if (!EXIT_EDITOR || EXIT_EDITOR.el !== editorEl) return;
    var candidate = (EXIT_EDITOR.candidates || [])[idx];
    if (!candidate) return;
    showExitCandidate(editorEl, candidate);
  }

  // preselectTxid (FIX-UI-6): when the editor is opened from an "Open: …
  // Record as exit" link, the candidate with exactly this spending txid is
  // preselected instead of the oldest one.
  function openExitEditor(containerEl, acq, preselectTxid) {
    closeExitEditor(containerEl);
    closeDisposalEditor(containerEl);
    var row = actionRow(containerEl, acq.id);
    if (!row) return;
    var wrapper = document.createElement("div");
    wrapper.innerHTML = exitEditorHtml();
    var editor = wrapper.firstElementChild;
    row.parentNode.insertBefore(editor, row.nextSibling);

    var editorEl = containerEl.querySelector("#exit-editor");
    EXIT_EDITOR = {
      mode: "create",
      exitId: null,
      acqId: acq.id,
      acq: acq,
      containerEl: containerEl,
      el: editorEl,
      candidates: [],
      candidate: null,
    };

    // The proposed Exit Date comes from the candidate's spending transaction
    // (see the .then below), never from the user-supplied acquisition date, so the
    // field starts empty and is left to the user if no confirmed block date
    // can be established.
    editorEl.querySelector("#exit-date").value = "";
    editorEl.querySelector("#exit-sources").innerHTML = '<div class="loading"><span class="spinner"></span>Loading candidate…</div>';
    editorEl.querySelector("#exit-outputs").innerHTML = '<div class="loading"><span class="spinner"></span>Loading candidate…</div>';

    editorEl.querySelector("#exit-save").addEventListener("click", function () { saveExit(editorEl); });
    editorEl.querySelector("#exit-cancel").addEventListener("click", function () { closeExitEditor(containerEl); });

    API.exchangeExitCandidates(acq.id).then(function (result) {
      if (!EXIT_EDITOR || EXIT_EDITOR.acqId !== acq.id) return;
      // FIX-UI-6: the API reports candidates plus load errors for candidate
      // transactions the node could not deliver.
      EXIT_EDITOR.candidates = (result && result.candidates) || [];
      var loadErrorHtml = ((result && result.load_errors) || []).map(function (e) {
        return "Transaction " + Fmt.esc(e.txid) + " could not be loaded from the node: " + Fmt.esc(e.reason);
      }).join("<br />");
      if (!EXIT_EDITOR.candidates.length) {
        EXIT_EDITOR.candidate = null;
        if (loadErrorHtml) {
          setExitFeedback(editorEl, "error", loadErrorHtml);
          editorEl.querySelector("#exit-sources").innerHTML = '';
        } else {
          setExitFeedback(editorEl, "warn", "No exchange-exit candidate found for this acquisition.");
          editorEl.querySelector("#exit-sources").innerHTML = '<div class="hint">No candidate found.</div>';
        }
        editorEl.querySelector("#exit-outputs").innerHTML = '';
        editorEl.querySelector("#exit-accounting").innerHTML = '';
        editorEl.querySelector("#exit-acquisitions").innerHTML = '';
        return;
      }
      // FIX-EXIT-5: all candidates are offered (backend order: oldest first);
      // the oldest is preselected. With exactly one candidate the view stays
      // as before, without a selection. FIX-UI-6: a preselectTxid from an
      // open-movement link wins when it matches a candidate.
      var selectedIdx = 0;
      if (preselectTxid) {
        EXIT_EDITOR.candidates.forEach(function (c, idx) {
          if (c.txid === preselectTxid) selectedIdx = idx;
        });
      }
      renderExitCandidateChoice(editorEl, EXIT_EDITOR.candidates, selectedIdx);
      showExitCandidate(editorEl, EXIT_EDITOR.candidates[selectedIdx]);
      // Loaded candidates stay usable; unloadable ones are named as a hint.
      if (loadErrorHtml) setExitFeedback(editorEl, "warn", loadErrorHtml);
    }).catch(function (err) {
      if (!EXIT_EDITOR || EXIT_EDITOR.acqId !== acq.id) return;
      editorEl.querySelector("#exit-sources").innerHTML = '';
      editorEl.querySelector("#exit-outputs").innerHTML = '';
      editorEl.querySelector("#exit-source-totals").innerHTML = '';
      editorEl.querySelector("#exit-output-totals").innerHTML = '';
      editorEl.querySelector("#exit-accounting").innerHTML = '';
      editorEl.querySelector("#exit-acquisitions").innerHTML = '';
      setExitFeedback(editorEl, "error", Fmt.esc(err.message));
    });
  }

  function openExitEditorForEdit(containerEl, acq, exit) {
    closeExitEditor(containerEl);
    closeDisposalEditor(containerEl);
    var row = actionRow(containerEl, acq.id);
    if (!row) return;
    var wrapper = document.createElement("div");
    wrapper.innerHTML = exitEditorHtml();
    var editor = wrapper.firstElementChild;
    row.parentNode.insertBefore(editor, row.nextSibling);

    var editorEl = containerEl.querySelector("#exit-editor");
    EXIT_EDITOR = {
      mode: "edit",
      exitId: exit.id || exit.exit_id,
      acqId: acq.id,
      acq: acq,
      containerEl: containerEl,
      el: editorEl,
      candidates: [],
      candidate: null,
    };

    editorEl.querySelector("#exit-editor-title").textContent = "Confirmed Exchange Exit";
    editorEl.querySelector("#exit-txid").textContent = Fmt.esc(exit.txid);
    editorEl.querySelector("#exit-date").value = exit.exit_date;
    editorEl.querySelector("#exit-time").value = exit.exit_time || "";
    editorEl.querySelector("#exit-broker").value = exit.broker || "";
    editorEl.querySelector("#exit-reference").value = exit.reference || "";
    editorEl.querySelector("#exit-notes").value = exit.notes || "";

    // FX-2: rows without an amount record an input that is not attributed
    // to a single Acquisition; they are grouped per outpoint with all anchors.
    var isUnattributedRow = function (s) { return !Number(s.disposed_sats || 0) && !Number(s.network_fee_sats || 0); };
    var sources = (exit.sources || []).filter(function (s) { return !isUnattributedRow(s); });
    var unattributed = [];
    (exit.sources || []).filter(isUnattributedRow).forEach(function (s) {
      var key = s.source_txid + ":" + s.source_vout;
      var group = unattributed.find(function (g) { return g.key === key; });
      if (!group) unattributed.push(group = { key: key, source: s, anchors: [] });
      group.anchors.push("#" + s.acquisition_id);
    });
    var outputs = exit.outputs || [];

    // FX-2, read-only: the stored attribution is compared with the current
    // on-chain evaluation; nothing is rewritten automatically.
    var checkHtml = "";
    if (exit.allocation_check === "DIFFERS") {
      checkHtml = '<div class="exit-allocation-differs exit-ownership-confirm-warn">The stored attribution differs from the current on-chain evaluation. ' +
        'Nothing has been changed automatically.<ul>' +
        (exit.allocation_differences || []).map(function (n) { return "<li>" + Fmt.esc(n) + "</li>"; }).join("") + "</ul></div>";
    } else if (exit.allocation_check === "UNAVAILABLE") {
      checkHtml = '<div class="hint">The stored attribution could not be compared with the on-chain evaluation right now.</div>';
    }

    var sourcesBox = editorEl.querySelector("#exit-sources");
    if (!sources.length && !unattributed.length) {
      sourcesBox.innerHTML = checkHtml + '<div class="hint">No source outpoints recorded.</div>';
    } else {
      var html = checkHtml + '<table class="exit-table"><thead><tr><th>Acquisition · Source</th><th>Original amount</th><th>Transferred to exchange</th><th>On-chain miner fee</th><th>Remaining</th></tr></thead><tbody>';
      sources.forEach(function (s) {
        var amount = Number(s.source_amount_sats || 0);
        var disposed = Number(s.disposed_sats || 0);
        var fee = Number(s.network_fee_sats || 0);
        var remaining = amount - disposed - fee;
        html += '<tr>' +
          '<td>#' + (s.acquisition_id || "?") + ' · ' + Fmt.esc(Fmt.shortTxid(s.source_txid, 12)) + ":" + s.source_vout +
          '<br/><span class="hint exit-input-status">Attributed to Acquisition #' + Fmt.esc(s.acquisition_id) + "</span></td>" +
          '<td>' + Fmt.btc(amount) + " BTC</td>" +
          '<td><span class="exit-readonly-value">' + Fmt.btc(disposed) + " BTC</span></td>" +
          '<td><span class="exit-readonly-value">' + Fmt.btc(fee) + " BTC</span></td>" +
          '<td>' + Fmt.btc(remaining) + " BTC</td>" +
          "</tr>";
        // v0.11.2: show that this source's ownership was NOT independently
        // established and only trusted because the user confirmed self-custody
        // for this Exchange Exit.
        if (s.ownership_confirmed === true) {
          html += '<tr class="exit-ownership-confirm-row"><td colspan="5">' +
            '<span class="exit-ownership-confirm-warn">Source ownership not independently established.</span> ' +
            '<span class="hint">self-custody confirmed for this Exchange Exit</span></td></tr>';
        }
      });
      unattributed.forEach(function (g) {
        html += '<tr class="exit-input-context exit-input-unattributed">' +
          '<td>— · ' + Fmt.esc(Fmt.shortTxid(g.source.source_txid, 12)) + ":" + g.source.source_vout + "</td>" +
          '<td>' + Fmt.btc(g.source.source_amount_sats) + " BTC</td>" +
          '<td colspan="3"><span class="hint exit-input-status">' +
          "Not attributed (anchors " + exitAnchorListHtml(g.anchors, true) + "); no amount, reduces no remaining amount." +
          "</span></td></tr>";
      });
      html += "</tbody></table>";
      sourcesBox.innerHTML = html;
    }

    var outputsBox = editorEl.querySelector("#exit-outputs");
    if (!outputs.length) {
      outputsBox.innerHTML = '<div class="hint">No outputs recorded.</div>';
    } else {
      var html2 = '<table class="exit-table"><thead><tr><th>Deposit?</th><th>Output</th><th>Address</th><th>Amount</th><th>Status</th></tr></thead><tbody>';
      outputs.forEach(function (o) {
        html2 += '<tr>' +
          '<td>' + (o.is_exchange_deposit ? "Yes" : "No") + "</td>" +
          '<td>' + o.vout + "</td>" +
          '<td>' + Fmt.esc(o.address || "—") + "</td>" +
          '<td>' + Fmt.btc(o.amount_sats) + " BTC</td>" +
          '<td>' + Fmt.esc(o.ownership_status) +
          (o.ownership_conflict ? '<br/><span class="exit-ownership-confirm-warn">Exchange Exit conflicts with newly discovered ownership evidence.</span>' : "") +
          "</td>" +
          "</tr>";
      });
      html2 += "</tbody></table>";
      outputsBox.innerHTML = html2;
    }

    var accountingBox = editorEl.querySelector("#exit-accounting");
    var totalSource = sources.reduce(function (a, s) { return a + Number(s.source_amount_sats || 0); }, 0);
    var disposedTotal = sources.reduce(function (a, s) { return a + Number(s.disposed_sats || 0); }, 0);
    var feeTotal = sources.reduce(function (a, s) { return a + Number(s.network_fee_sats || 0); }, 0);
    var remaining = totalSource - disposedTotal - feeTotal;
    var notAttributed = unattributed.reduce(function (a, g) { return a + Number(g.source.source_amount_sats || 0); }, 0);
    accountingBox.innerHTML = '<div class="exit-accounting-line"><span class="ec">total source</span> ' + Fmt.btc(totalSource) + " BTC</div>" +
      (notAttributed ? '<div class="exit-accounting-line"><span class="ec">not attributed inputs</span> ' + Fmt.btc(notAttributed) + ' BTC <span class="hint">(no amount allocated to any Acquisition)</span></div>' : "") +
      '<div class="exit-accounting-line"><span class="ec">transferred to exchange</span> ' + Fmt.btc(disposedTotal) + " BTC</div>" +
      '<div class="exit-accounting-line"><span class="ec">on-chain miner fee</span> ' + Fmt.btc(feeTotal) + " BTC</div>" +
      '<div class="exit-accounting-line"><span class="ec">remaining / change</span> ' + Fmt.btc(remaining) + " BTC</div>";

    editorEl.querySelector("#exit-acquisitions").innerHTML = '<div class="hint">Blockchain evidence is read-only. Edit only the metadata above.</div>';

    editorEl.querySelector("#exit-save").addEventListener("click", function () { saveExit(editorEl); });
    editorEl.querySelector("#exit-cancel").addEventListener("click", function () { closeExitEditor(containerEl); });
  }


  function originCell(a) {
    return '<div class="acq-origin">' +
      '<div><span class="hint">Received by</span> ' + Fmt.esc(Fmt.shortAddress(a.origin_address)) + "</div>" +
      '<div><span class="hint">Original outpoint</span> ' + Fmt.esc(Fmt.shortTxid(a.origin_txid, 12)) + ":" + a.origin_vout + "</div>" +
      '<div><span class="hint">On-chain amount</span> ' + Fmt.btc(a.origin_amount_sats) + " BTC" +
      (a.origin_block_height != null ? " · block " + a.origin_block_height : "") + "</div>" +
      '<div><span class="hint">Block time</span> ' +
      (a.origin_block_time == null ? "Unconfirmed / unavailable" : Fmt.esc(new Date(a.origin_block_time * 1000).toISOString())) + "</div>" +
      "</div>";
  }

  function currentCell(a) {
    var d = a.descendant || {};
    var bits = [];
    if (d.mixed) {
      // FIX-UI-7 point 3: MIXED is reserved for the report class (Acquisition
      // and No-Acquisition ancestry). This summary only knows the merged
      // topology and makes no origin-class claim.
      bits.push("<strong>Merged</strong> transaction involved — exact sat-level attribution is not claimed.");
    }
    if (d.unspent_terminal_count > 1) {
      bits.push(d.unspent_terminal_count + " current descendant UTXOs");
      if (d.primary_terminal_txid) {
        bits.push("first: " + Fmt.esc(Fmt.shortTxid(d.primary_terminal_txid, 10)) + ":" + d.primary_terminal_vout +
          " · " + (d.primary_terminal_address ? Fmt.esc(Fmt.shortAddress(d.primary_terminal_address)) + " · " : "") +
          (d.primary_terminal_amount_sats != null ? Fmt.btc(d.primary_terminal_amount_sats) + " BTC" : "") + " <span class=\"hint\">UNSPENT</span>");
      }
    } else if (d.unspent_terminal_count === 1) {
      bits.push("Current descendant: " + Fmt.esc(Fmt.shortTxid(d.primary_terminal_txid, 10)) + ":" + d.primary_terminal_vout +
        " · " + (d.primary_terminal_address ? Fmt.esc(Fmt.shortAddress(d.primary_terminal_address)) + " · " : "") +
        (d.primary_terminal_amount_sats != null ? Fmt.btc(d.primary_terminal_amount_sats) + " BTC" : "") + " <span class=\"hint\">UNSPENT</span>");
    } else if ((a.exchange_exits || []).length && (a.exchange_exits || []).every(function (x) {
      return !Number(x.allocated_sats || 0) && Number(x.unattributed_inputs || 0) > 0;
    })) {
      bits.push("Involved in an Exchange Exit (not attributed).");
    } else if (a.exchange_exit_count || (a.exchange_exits || []).length) {
      bits.push("<strong>EXCHANGE EXIT</strong> · Custody ended here.");
    } else if (d.spend_unknown) {
      bits.push("No confirmed unspent descendant could be established (spend state unknown).");
    } else {
      bits.push("No current unspent descendant");
    }
    // FIX-UI-6/FIX-UI-7: name each open movement instead of only the generic
    // sentence. Unresolved spends and partial spends (own change plus a
    // non-own value output, the FX-3b open side) are listed together.
    // "Record as exit" appears only when the backend hints that the spending
    // transaction is an Exit candidate; otherwise the existing review path for
    // the receiving outputs (transaction modal) is linked.
    var open = openMovements(d);
    if (open.length) {
      bits.push("Unexplained movement from own custody:");
      open.slice(0, 3).forEach(function (s) {
        var line = Fmt.esc(Fmt.openMovementText(s));
        if (s.exit_candidate && canRecordExit(a)) {
          line += ' <a href="#" class="open-spend-exit" data-aid="' + a.id +
            '" data-txid="' + Fmt.esc(s.spending_txid) + '">Record as exit</a>';
        } else {
          line += ' <a href="#" class="open-spend-review" data-txid="' + Fmt.esc(s.spending_txid) +
            '">Review ownership of the receiving outputs</a>';
        }
        bits.push(line);
      });
      if (open.length > 3) bits.push("+ " + (open.length - 3) + " more");
    } else {
      if (d.note) bits.push(d.note);
      // FIX-UI-7 point 2: NEEDS_REVIEW never appears without guidance.
      if (a.position_state === "NEEDS_REVIEW") {
        bits.push("No open movement could be identified; check Record Exit candidates and ownership.");
      }
    }
    return '<div class="hint" style="line-height:1.6">' + bits.join("<br />") + "</div>";
  }

  // Combined open-movement list, chronological by spending block time
  // (undated/unconfirmed last, then by outpoint). Display order only.
  function openMovements(d) {
    return (d.unresolved_spends || []).concat(d.partial_unexplained_spends || [])
      .slice().sort(function (x, y) {
        var xt = x.spending_block_time, yt = y.spending_block_time;
        if ((xt == null) !== (yt == null)) return xt == null ? 1 : -1;
        if (xt != null && xt !== yt) return xt - yt;
        var xk = x.txid + ":" + x.vout, yk = y.txid + ":" + y.vout;
        return xk < yk ? -1 : xk > yk ? 1 : 0;
      });
  }

  function statusCell(a) {
    // POSITION column (v0.10.1): only the derived Acquisition position state is
    // shown here. Internal trace/classification states such as
    // NEEDS_CLASSIFICATION are NOT rendered in this compact column - the badge
    // set is CURRENT/NEEDS REVIEW/PARTIAL/DISPOSED/TRACING.
    return positionBadgeHtml(a);
  }

  function exitAllocatedSats(a) {
    var total = 0;
    (a.exchange_exits || []).forEach(function (x) {
      total += Number(x.allocated_sats || 0);
    });
    return String(total);
  }

  function totalAllocatedBtc(a) {
    return Fmt.btc(sumInts(disposedSats(a), exitAllocatedSats(a)));
  }

  // FIX-EXIT-3: DISPOSED purely through exits whose rows are all 0/0 (inputs not
  // attributed to a single Acquisition). allocated_sats stays the basis; no
  // remaining amount is shown for such a position.
  function closedViaUnattributedExit(a) {
    var exits = a.exchange_exits || [];
    return a.position_state === "DISPOSED" && !(a.disposals && a.disposals.length) &&
      exits.length > 0 && exits.every(function (x) {
        return Number(x.allocated_sats || 0) === 0 && Number(x.unattributed_inputs || 0) > 0;
      });
  }

  function disposalSummaryCell(a) {
    if (a.position_state === "PARTIALLY_DISPOSED") {
      var dCount = a.disposal_count || (a.disposals || []).length;
      var xCount = a.exchange_exit_count || (a.exchange_exits || []).length;
      var parts = [];
      if (dCount) parts.push(dCount + " sale" + (dCount > 1 ? "s" : ""));
      if (xCount) parts.push(xCount + " exit" + (xCount > 1 ? "s" : ""));
      var suffix = parts.length ? ' <span class="hint">(' + parts.join(", ") + ")</span>" : "";
      return '<div class="acq-disposal-summary">' +
        '<div><span class="ec">allocated</span> ' + totalAllocatedBtc(a) + " BTC</div>" +
        '<div><span class="ec">remaining</span> ' + remainingBtc(a) + " BTC</div>" +
        suffix + "</div>";
    }
    if (a.position_state === "DISPOSED") {
      var d = a.disposals && a.disposals[0];
      var x = a.exchange_exits && a.exchange_exits[0];
      if (d) {
        return '<div class="acq-disposal-summary">' +
          '<div><span class="ec">sale date</span> ' + Fmt.esc(d.disposal_date) + "</div>" +
          '<div><span class="ec">BTC</span> ' + Fmt.btc(d.allocated_sats) + "</div>" +
          '<div><span class="ec">net</span> €' + Fmt.deMoney(d.net_proceeds_eur) + "</div>" +
          (d.broker ? '<div><span class="ec">broker</span> ' + Fmt.esc(d.broker) + "</div>" : "") +
          "</div>";
      }
      if (x && closedViaUnattributedExit(a)) {
        return '<div class="acq-disposal-summary">' +
          '<div class="hint">Closed via a non-attributed exit input</div>' +
          '<div><span class="ec">exit date</span> ' + Fmt.esc(x.exit_date) + "</div>" +
          '<div><span class="ec">txid</span> ' + Fmt.esc(Fmt.shortTxid(x.txid, 12)) + "</div>" +
          "</div>";
      }
      if (x) {
        return '<div class="acq-disposal-summary">' +
          '<div><span class="ec">exit date</span> ' + Fmt.esc(x.exit_date) + "</div>" +
          '<div><span class="ec">source</span> ' + Fmt.btc(x.allocated_sats) + " BTC</div>" +
          '<div><span class="ec">transferred</span> ' + Fmt.btc(x.disposed_sats) + " BTC</div>" +
          '<div><span class="ec">txid</span> ' + Fmt.esc(Fmt.shortTxid(x.txid, 12)) + "</div>" +
          (x.broker ? '<div><span class="ec">broker</span> ' + Fmt.esc(x.broker) + "</div>" : "") +
          "</div>";
      }
      return "";
    }
    return "";
  }

  function tableHtml(content, provenance) {
    var header =
      '<div class="acq-table-head">' +
      '<span>ID · Date</span><span>Source</span><span>Amount</span><span>Metadata</span>' +
      '<span>Origin</span><span>Current location</span><span>Position</span>' +
      (provenance ? '<span>Provenance</span>' : '') + '<span>Actions</span>' +
      "</div>";
    return '<div class="acq-list-wrap' + (provenance ? ' provenance-table' : '') + '"><div class="acq-grid-table">' +
      header + content + '</div></div>';
  }

  function actionsHtml(a) {
      var actions = '<div class="acq-cell-actions">';
        if (WORKSPACE) actions += '<button class="btn small" data-review-output="' + Fmt.esc(a.origin_txid + ':' + a.origin_vout) + '">Review ownership</button>';
        var needsReview = a.position_state === "NEEDS_REVIEW";
        if (a.trace_id) {
          actions += '<button class="btn small primary view-trace-btn" data-tid="' + a.trace_id + '">View Trace</button>';
          actions += '<button class="btn small retrace-btn">Re-run Trace</button>';
        } else {
          // No trace (never traced, or the trace was deleted and trace_id
          // cleared): no View Trace target exists, only Run Trace.
          actions += '<button class="btn small primary retrace-btn">Run Trace</button>';
        }
        if (a.disposal_count || (a.disposals || []).length) {
          var oCount = a.disposal_count || (a.disposals || []).length;
          actions += '<button class="btn small view-sale-btn" data-aid="' + a.id + '">' + (oCount === 1 ? "View Sale" : oCount + " Disposals") + "</button>";
        }
        if (a.exchange_exit_count || (a.exchange_exits || []).length) {
          var xCount = a.exchange_exit_count || (a.exchange_exits || []).length;
          // "Manage Exit", not a bare "View Exit": this row action is the
          // explicit, discoverable way to reach "Delete Exchange Exit" WITHOUT
          // touching the Acquisition. It is rendered for every Acquisition that
          // participates in an Exit regardless of position state, so a
          // DISPOSED / PARTIALLY_DISPOSED row can always be reverted here.
          actions += '<button class="btn small view-exit-btn" data-aid="' + a.id + '">' + (xCount === 1 ? "Manage Exit" : "Manage Exits (" + xCount + ")") + "</button>";
        }
        if (canRecordExit(a)) {
          actions += '<button class="btn small primary record-exit-btn" data-aid="' + a.id + '">Record Exit</button>';
        }
        actions += '<button class="btn small edit-btn">Edit metadata</button>' +
          '<button class="btn small danger delete-btn">Delete Acquisition</button>';
      actions += "</div>";
      return actions;
  }

  function recordRowHtml(a, provenanceCell) {
      var disposalSummary = disposalSummaryCell(a);
      var actions = actionsHtml(a);
      return '<div class="acq-row" data-id="' + a.id + '">' +
        // FIX-UI-4: the real database ID, the same #n other views reference.
        '<div><div class="acq-id mono">#' + Fmt.esc(a.id) + "</div>" + Fmt.esc(a.acquisition_date || "—") + "</div>" +
        '<div>' + Fmt.esc(a.source || "—") + "</div>" +
        '<div class="mono">' + Fmt.btc(a.origin_amount_sats) + " BTC" +
        (provenanceCell !== undefined ? '<div class="hint">Original amount</div>' : '') + '</div>' +
        "<div>" + Fmt.esc(a.reference || "") + '<br>' + Fmt.esc(a.notes || "") + "</div>" +
        '<div>' + originCell(a) + "</div>" +
        '<div><div data-acq-membership="' + a.id + '">' + membershipBadgeHtml(a) + '</div>' + currentCell(a) + (disposalSummary ? '<div style="margin-top:6px">' + disposalSummary + "</div>" : "") + "</div>" +
        "<div>" + statusCell(a) + "</div>" +
        (provenanceCell !== undefined ? '<div>' + provenanceCell + '</div>' : '') +
        "<div>" + actions + "</div>" +
        "</div>";
  }

  function renderList(el, acqs) {
    if (!acqs.length) {
      var emptyMsg = CURRENT_FILTER == null
        ? "No Acquisition anchors yet. Use + Add Acquisition or mark an output in a trace."
        : "No acquisitions in this filter.";
      el.querySelector("#acq-list").innerHTML =
        '<div class="panel hint">' + emptyMsg + "</div>";
      return;
    }

    el.querySelector("#acq-list").innerHTML =
      tableHtml(acqs.map(function (a) { return recordRowHtml(a); }).join(''), false);
    wireRows(el, acqs);
  }

  function wireRows(el, acqs, scope) {
    (scope || el).querySelectorAll(".acq-row[data-id], .acq-actions-scope[data-id]").forEach(function (rowEl) {
      var id = Number.isSafeInteger(Number(rowEl.dataset.id)) ? Number(rowEl.dataset.id) : rowEl.dataset.id;
      rowEl.addEventListener('click', function () { activeActionRow = rowEl; }, true);
      var viewBtn = rowEl.querySelector(".view-trace-btn");
      if (viewBtn) {
        viewBtn.addEventListener("click", function () {
          window.App.openTrace(Number(viewBtn.dataset.tid));
        });
      }
      rowEl.querySelectorAll(".retrace-btn").forEach(function (b) {
        b.addEventListener("click", function () {
          b.disabled = true;
          API.retraceAcquisition(id).then(function () { loadList(el); loadPositionCounts(el); }).catch(function (err) {
            b.disabled = false;
            setFeedback(el, "error", Fmt.esc(err.message));
          });
        });
      });
      rowEl.querySelector(".edit-btn").addEventListener("click", function () { startEdit(el, id); });
      rowEl.querySelector(".delete-btn").addEventListener("click", function () {
        // This is Acquisition deletion, NOT Exit deletion. The wording must make
        // the destruction irreversible and must explicitly redirect anyone who
        // came here to undo an Exchange Exit.
        if (!confirm("Permanently delete this Acquisition?\n\n" +
            "The Acquisition record itself AND its Acquisition-owned provenance trace " +
            "will be permanently deleted. This cannot be undone.\n\n" +
            "This is NOT the way to undo an Exchange Exit. To remove the Exit " +
            "classification and keep the Acquisition, use \"Manage Exit\" and then " +
            "\"Delete Exchange Exit\".")) return;
        API.deleteAcquisition(id).then(function () { loadList(el); loadPositionCounts(el); }).catch(function (err) {
          setFeedback(el, "error", Fmt.esc(err.message));
        });
      });
      var viewSaleBtn = rowEl.querySelector(".view-sale-btn");
      if (viewSaleBtn) {
        viewSaleBtn.addEventListener("click", function () {
          var acq = acqs.find(function (x) { return String(x.id) === String(id); });
          if (acq) openDisposalDetail(el, acq);
        });
      }
      var recordExitBtn = rowEl.querySelector(".record-exit-btn");
      if (recordExitBtn) {
        recordExitBtn.addEventListener("click", function () {
          var acq = acqs.find(function (x) { return String(x.id) === String(id); });
          if (acq) openExitEditor(el, acq);
        });
      }
      // FIX-UI-6: open-movement links of the NEEDS_REVIEW row.
      rowEl.querySelectorAll(".open-spend-exit").forEach(function (link) {
        link.addEventListener("click", function (ev) {
          ev.preventDefault();
          var acq = acqs.find(function (x) { return String(x.id) === String(id); });
          if (acq) openExitEditor(el, acq, link.getAttribute("data-txid"));
        });
      });
      rowEl.querySelectorAll(".open-spend-review").forEach(function (link) {
        link.addEventListener("click", function (ev) {
          ev.preventDefault();
          window.App.openTransaction(link.getAttribute("data-txid"));
        });
      });
      var viewExitBtn = rowEl.querySelector(".view-exit-btn");
      if (viewExitBtn) {
        viewExitBtn.addEventListener("click", function () {
          var acq = acqs.find(function (x) { return String(x.id) === String(id); });
          if (acq) openExitDetail(el, acq);
        });
      }
    });
  }

  function actionRow(containerEl, id) {
    var row = activeActionRow;
    if (!row || String(row.dataset.id) !== String(id) || !containerEl.contains(row)) {
      row = containerEl.querySelector('.acq-row[data-id="' + id + '"], .acq-actions-scope[data-id="' + id + '"]');
    }
    // Merged/split positions may reference the same record. Put the existing
    // inline editor after the clicked POSITION, never an arbitrary sibling.
    return row && (row.closest('[data-prov-position]') || row);
  }

  function startEdit(el, id) {
    API.getAcquisition(id).then(function (a) {
      EDITING_ID = id;
      el.querySelector("#acq-add-panel").open = true;
      el.querySelector("#acq-date").value = a.acquisition_date || "";
      el.querySelector("#acq-source").value = a.source || "";
      el.querySelector("#acq-reference").value = a.reference || "";
      el.querySelector("#acq-notes").value = a.notes || "";
      el.querySelector("#acq-txid").value = a.origin_txid;
      el.querySelector("#acq-txid").disabled = true;
      el.querySelector("#acq-load-outputs").disabled = true;
      hideSelection(el);
      el.querySelector("#acq-output-facts").textContent = a.origin_txid + ":" + a.origin_vout +
        " · " + Fmt.btc(a.origin_amount_sats) + " BTC · " + (a.origin_address || "Non-address output");
      el.querySelector("#acq-cancel").style.display = "";
      el.querySelector("#acq-submit").textContent = "Save changes";
      el.querySelector("#acq-submit").scrollIntoView({ behavior: "smooth", block: "center" });
    }).catch(function (err) {
      setFeedback(el, "error", Fmt.esc(err.message));
    });
  }

  return { render: render, stopCoverage: stopCoverage,
    tableHtml: tableHtml, actionsHtml: actionsHtml, recordRowHtml: recordRowHtml,
    wireRows: wireRows, positionBadgeHtml: positionBadgeHtml,
    membership: updateWalletPositions, downloadExport: downloadExport,
    renderFilters: function (el) { renderFilterTabs(el, positionCounts); },
    closeEditors: function (el) { closeExitEditor(el); closeDisposalEditor(el); },
    canRecordExit: canRecordExit,
    closedViaUnattributedExit: closedViaUnattributedExit,
    refresh: function (el) { loadList(el); loadPositionCounts(el); } };
})();

// Trace list screen (create/resume/delete traces).
window.TracesPage = (function () {

  function render(el) {
    el.innerHTML = '<div class="loading"><span class="spinner"></span>Loading traces…</div>';
    API.listTraces().then(function (traces) {
      if (!traces.length) {
        el.innerHTML =
          '<h1>Traces</h1>' +
          '<div class="panel hint">No saved traces yet. Go to <strong>Start Trace</strong> and paste a transaction ID to begin.</div>';
        return;
      }
      let html =
        '<div class="traces-header">' +
        '<h1>Traces <span class="hint">(' + traces.length + ')</span></h1>' +
        '<button class="btn small danger" id="btn-delete-all">Delete all traces</button>' +
        '</div>' +
        '<label>Filter traces <select id="trace-prov-filter" disabled><option value="">All</option>' +
        '<option value="UNANCHORED">Contains UNANCHORED managed outputs</option></select></label>' +
        '<p class="hint" id="trace-prov-feedback">Checking Acquisition provenance of managed outputs in saved traces…</p>';
      traces.forEach(function (t) {
        html +=
          '<div class="panel trace-list-item" data-id="' + t.id + '">' +
          '  <div class="meta">' +
          '    <div class="name">' + Fmt.esc(t.name || ("Trace #" + t.id)) + '</div>' +
          '    <div class="sub">' + Fmt.esc(Fmt.shortTxid(t.start_txid, 12)) + (t.start_address ? " · " + Fmt.esc(Fmt.shortAddress(t.start_address)) : "") + "</div>" +
          '    <div class="sub">created ' + Fmt.dateOnly(Date.parse(t.created_at) / 1000) + " · updated " + Fmt.dateOnly(Date.parse(t.updated_at) / 1000) + "</div>" +
          "  </div>" +
          '  <span class="status-tag ' + t.status + '">' + t.status + "</span>" +
          '<span class="trace-acquisition-provenance"></span>' +
          '  <div class="spacer"></div>' +
          '  <div style="display:flex;gap:6px;flex-wrap:wrap">' +
          '    <button class="btn small primary open-btn">Open</button>' +
          '    <button class="btn small export-json-btn">Export JSON</button>' +
          '    <button class="btn small export-csv-btn">Export CSV</button>' +
          '    <button class="btn small danger delete-btn">Delete</button>' +
          "  </div>" +
          "</div>";
      });
      el.innerHTML = html;
      const provenanceFilter = el.querySelector('#trace-prov-filter');
      API.traceProvenance().then(function (outputs) {
        if (el.querySelector('#trace-prov-filter') !== provenanceFilter) return;
        var counts = {};
        outputs.forEach(function (output) {
          output.trace_ids.forEach(function (id) {
            if (!counts[id]) counts[id] = { UNANCHORED: 0, PARTIAL: 0 };
            if (output.acquisition_provenance in counts[id]) counts[id][output.acquisition_provenance]++;
          });
        });
        el.querySelectorAll('.trace-list-item').forEach(function (item) {
          var count = counts[item.dataset.id] || { UNANCHORED: 0, PARTIAL: 0 };
          item.dataset.unanchored = count.UNANCHORED;
          item.querySelector('.trace-acquisition-provenance').innerHTML =
            (count.UNANCHORED ? Fmt.acquisitionBadge('UNANCHORED') + ' (' + count.UNANCHORED + ' outputs)' : '') +
            (count.PARTIAL ? Fmt.acquisitionBadge('PARTIAL') + ' (' + count.PARTIAL + ' outputs)' : '');
        });
        el.querySelector('#trace-prov-feedback').textContent = 'Badges describe contained managed outputs, including historical outputs, not the whole trace or a current balance.';
        var filter = el.querySelector('#trace-prov-filter');
        filter.disabled = false;
        filter.addEventListener('change', function () {
          el.querySelectorAll('.trace-list-item').forEach(function (item) {
            item.hidden = filter.value === 'UNANCHORED' && Number(item.dataset.unanchored) === 0;
          });
        });
      }).catch(function () {
        if (el.querySelector('#trace-prov-filter') !== provenanceFilter) return;
        var target = el.querySelector('#trace-prov-feedback');
        if (target) target.textContent = 'Acquisition provenance unavailable. Reload to retry.';
      });

      el.querySelectorAll(".trace-list-item").forEach(function (item) {
        const id = item.dataset.id;
        item.querySelector(".open-btn").addEventListener("click", function () { window.App.openTrace(id); });
        item.querySelector(".delete-btn").addEventListener("click", function () {
          if (!confirm("Delete trace #" + id + " and its trace graph? Blockchain cache remains.")) return;
          API.deleteTrace(id).then(function () { render(el); }).catch(function (err) { showError(el, err); });
        });
        item.querySelector(".export-json-btn").addEventListener("click", function () {
          API.exportJson(id).then(function (data) {
            download("trace-" + id + ".json", JSON.stringify(data, null, 2), "application/json");
          }).catch(function (err) { showError(el, err); });
        });
        item.querySelector(".export-csv-btn").addEventListener("click", function () {
          downloadURL("/api/traces/" + id + "/export.csv");
        });
      });

      // Delete-all traces: explicit destructive double-confirmation.
      el.querySelector("#btn-delete-all").addEventListener("click", function () {
        var n = traces.length;
        if (!confirm("Delete ALL " + n + " traces? Known Address Registry, wallet discovery and cached blockchain data will be preserved. This cannot be undone.")) return;
        var typed = prompt("Type DELETE to confirm removing all " + n + " traces.");
        if (typed !== "DELETE") {
          el.querySelector("#btn-delete-all").insertAdjacentHTML("afterend",
            '<span class="hint" style="margin-left:8px;color:var(--accent)">Cancelled – nothing was deleted.</span>');
          setTimeout(function () { var m = el.querySelector("#btn-delete-all + .hint"); if (m) m.remove(); }, 3000);
          return;
        }
        API.deleteAllTraces().then(function () { render(el); }).catch(function (err) { showError(el, err); });
      });
    }).catch(function (err) {
      el.innerHTML = '<div class="alert error">' + Fmt.esc(err.message) + "</div>";
    });
  }

  function showError(el, err) {
    const old = el.querySelector("#trace-list-error");
    if (old) old.remove();
    const header = el.querySelector(".traces-header");
    if (header) header.insertAdjacentHTML("afterend", '<div class="alert error" id="trace-list-error">' + Fmt.esc(err.message) + "</div>");
  }

  function download(name, content, type) {
    const blob = new Blob([content], { type: type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 100);
  }
  function downloadURL(url) {
    const a = document.createElement("a");
    a.href = url; a.download = "";
    document.body.appendChild(a); a.click(); a.remove();
  }

  return { render: render };
})();

// RAM-only handle; the server owns all evidence. No browser storage or key reuse.
window.WalletDiscoveryPage = (function () {
  let root = null, handle = null, revision = null, timer = null, epoch = 0, busy = false;
  let refresh = function () {};
  function esc(value) { return Fmt.esc(String(value == null ? "" : value)); }
  function template() {
    return '<div class="panel" id="reg-discovery"><h2>Wallet discovery (one-time)</h2>' +
      '<div class="hint">Your public extended key stays in memory and is discarded after scanning. ' +
      'Nothing is imported until you review and confirm. Pending results expire after 15 minutes and are lost on restart.</div>' +
      '<div class="discovery-form" id="d-inputs">' +
      '<div class="discovery-row">' +
      '<div class="discovery-field"><label for="d-wallet">Wallet name</label><input id="d-wallet" type="text" maxlength="200" placeholder="discovery"></div>' +
      '<div class="discovery-field"><label for="d-label">Label</label><input id="d-label" type="text" maxlength="200"></div>' +
      '</div>' +
      '<div class="discovery-field"><label for="d-key">Extended public key</label>' +
      '<textarea id="d-key" autocomplete="off" spellcheck="false" placeholder="XPUB / YPUB / ZPUB — never a seed or private key"></textarea></div>' +
      '<div class="discovery-field discovery-field-narrow"><label for="d-script">Wallet script type</label>' +
      '<select id="d-script"><option value="auto">From key prefix (select explicitly for XPUB)</option>' +
      '<option value="p2wpkh">Native SegWit (P2WPKH)</option><option value="p2sh-p2wpkh">Nested SegWit</option>' +
      '<option value="p2pkh">Legacy (P2PKH)</option></select></div>' +
      '<details class="discovery-advanced"><summary>Scan settings</summary><div class="discovery-row">' +
      '<div class="discovery-field"><label for="d-start">Start height</label><input id="d-start" type="number" min="0" placeholder="0"></div>' +
      '<div class="discovery-field"><label for="d-stop">Stop height</label><input id="d-stop" type="number" min="0" placeholder="Current tip"></div>' +
      '<div class="discovery-field"><label for="d-gap">Gap limit</label><input id="d-gap" type="number" min="1" max="2000" value="20"></div>' +
      '</div></details>' +
      '<p class="hint">Confirm that this is your wallet’s public key and the selected script type/receive and change derivation match your wallet.</p>' +
      '</div>' +
      '<button class="btn" id="btn-d-scan">Scan wallet</button> ' +
      '<button class="btn" id="btn-d-import" hidden>Import</button> ' +
      '<button class="btn" id="btn-d-discard" hidden>Discard pending discovery</button>' +
      '<div id="d-status" class="hint" aria-live="polite" style="white-space:pre-wrap;margin-top:10px"></div>' +
      '<div id="d-review"></div><h3>Previous discoveries</h3><div id="reg-discoveries" class="hint">Loading…</div></div>';
  }
  function controls(state) {
    if (!root) return;
    root.querySelectorAll("#d-inputs input, #d-inputs textarea, #d-inputs select").forEach(function (el) {
      el.disabled = busy || !!handle;
    });
    root.querySelector("#btn-d-scan").disabled = busy || !!handle;
    const imp = root.querySelector("#btn-d-import");
    imp.hidden = state !== "READY";
    imp.disabled = busy;
    const discard = root.querySelector("#btn-d-discard");
    discard.hidden = !handle;
    discard.disabled = busy;
  }
  function message(text) { if (root) root.querySelector("#d-status").textContent = text; }
  function reviewHtml(r) {
    return '<h3>Review discovered results</h3><p>' + esc(r.wallet_name) + ' · ' + esc(r.network) +
      ' · ' + esc(r.script_type) + ' · ' + esc(r.scan_mode) + '</p>' +
      '<p>Discovery snapshot: blocks ' + esc(r.start_height) + '–' + esc(r.stop_height) +
      '. This does not prove that all wallet addresses were found.</p>' +
      '<p>Receive: ' + r.branch0_found + ' (last used index ' + r.branch0_last_index +
      ', scanned through ' + r.branch0_scanned_through + '); change: ' + r.branch1_found +
      ' (last used index ' + r.branch1_last_index + ', scanned through ' + r.branch1_scanned_through + ').</p>' +
      '<p>' + r.found_vout_count + ' matching outputs in ' + r.trace_count + ' transactions.</p>' +
      (r.warnings || []).map(function (w) { return '<div class="alert warn">' + esc(w) + '</div>'; }).join("") +
      '<table style="width:100%"><thead><tr><th>Address</th><th>Derivation</th><th>First seen</th><th>Registry wallet</th></tr></thead><tbody>' +
      (r.matched || []).map(function (m) {
        return '<tr><td class="mono">' + esc(m.address) + '</td><td>' + esc(m.derivation_path) +
          '</td><td>' + esc(m.first_seen_at || "Unknown") + '</td><td>' + esc(m.existing_wallet || "New") + '</td></tr>';
      }).join("") + '</tbody></table><p>Import records these addresses as XPUB_DISCOVERY ownership evidence. ' +
      'Existing wallet conflicts are surfaced rather than silently reassigned. Import does not scan again.</p>';
  }
  function show(s) {
    if (!root) return;
    revision = s.revision;
    const p = s.progress || {};
    const labels = { PREPARING: "Preparing scan...", SCANNING: "Scanning wallet...", PROCESSING: "Processing matching blocks...", READY: "Ready for review" };
    let text = labels[s.state] || s.message || s.state;
    if (s.state === "SCANNING") {
      text += "\nDerivation pass " + (p.scan_pass || 1) + " — further passes may be needed.";
      if (typeof p.progress === "number") text += "\n" + Math.round(p.progress) + "%";
      if (p.current_height != null) text += "\nBlock " + p.current_height + " / " + p.stop_height;
      if (p.scan_mode === "scantxoutset") text += "\nUTXO-only fallback (no block-filter percentage available).";
    }
    if (s.state === "READY") text += "\nNothing imported. Result expires in approximately " + Math.ceil(s.expires_in / 60) + " minutes.";
    if (s.state === "COMMITTED") {
      const o = s.outcome;
      text = "Imported " + o.imported + ", duplicates " + o.duplicates + ", conflicts " + o.conflicts + ". No second scan was started.";
      handle = null;
    }
    message(text);
    root.querySelector("#d-review").innerHTML = s.review ? reviewHtml(s.review) : "";
    controls(s.state);
    if (["PREPARING", "SCANNING", "PROCESSING", "READY"].indexOf(s.state) !== -1) schedule();
  }
  function schedule() {
    clearTimeout(timer);
    const generation = epoch;
    timer = setTimeout(function () { if (epoch === generation) poll(); }, 1500);
  }
  function poll() {
    if (!root || !handle || busy) return;
    const generation = epoch, current = handle;
    API.walletScanStatus(current).then(function (s) {
      if (generation === epoch && current === handle) show(s);
    }).catch(function (err) {
      if (generation !== epoch || current !== handle) return;
      message(err.message);
      if (err.status === 410) {
        handle = null;
        root.querySelector("#d-review").innerHTML = "";
        controls("");
      } else schedule();
    });
  }
  function scan() {
    if (busy || handle) return;
    const key = root.querySelector("#d-key");
    if (!key.value.trim()) { message("Enter a public extended key."); return; }
    const start = root.querySelector("#d-start").value, stop = root.querySelector("#d-stop").value;
    const payload = { key: key.value.trim(), wallet_name: root.querySelector("#d-wallet").value.trim() || "discovery",
      label: root.querySelector("#d-label").value.trim(), script_type: root.querySelector("#d-script").value,
      gap_limit: Number(root.querySelector("#d-gap").value), start_height: start === "" ? null : Number(start),
      end_height: stop === "" ? null : Number(stop) };
    key.value = "";
    busy = true;
    const generation = ++epoch;
    controls("PREPARING"); message("Preparing scan...");
    const promise = API.walletScanStart(payload);
    payload.key = ""; // request() serializes synchronously before its first await
    promise.then(function (s) {
      if (generation !== epoch) { API.walletScanDiscard(s.handle).catch(function () {}); return; }
      busy = false; handle = s.handle; show(s);
    }).catch(function (err) {
      if (generation !== epoch) return;
      busy = false; message(err.message + " A lost request does not mean Core stopped scanning."); controls("");
    });
  }
  function commit() {
    if (busy || !handle || revision == null) return;
    if (!window.confirm("Import the reviewed discovered addresses as ownership evidence?")) return;
    busy = true; clearTimeout(timer);
    const generation = ++epoch, current = handle;
    controls("READY"); message("Importing reviewed results...");
    API.walletScanCommit(current, revision).then(function (s) {
      if (generation !== epoch) return;
      busy = false; show(s); refresh();
    }).catch(function (err) {
      if (generation !== epoch) return;
      busy = false; message(err.message); poll();
    });
  }
  function discard() {
    if (busy || !handle) return;
    const current = handle, generation = ++epoch;
    busy = true; clearTimeout(timer); controls("");
    API.walletScanDiscard(current).then(function (s) {
      if (generation !== epoch) return;
      busy = false; handle = null; revision = null;
      root.querySelector("#d-review").innerHTML = "";
      message(s.message); controls("");
    }).catch(function (err) {
      if (generation !== epoch) return;
      busy = false; message(err.message); poll();
    });
  }
  function stop() {
    ++epoch; clearTimeout(timer); busy = false;
    if (root) root.querySelector("#d-key").value = "";
    root = null;
  }
  function bind(el, afterImport) {
    stop(); root = el.querySelector("#reg-discovery"); refresh = afterImport;
    root.querySelector("#btn-d-scan").addEventListener("click", scan);
    root.querySelector("#btn-d-import").addEventListener("click", commit);
    root.querySelector("#btn-d-discard").addEventListener("click", discard);
    controls(""); if (handle) poll();
  }
  function invalidate() {
    ++epoch; clearTimeout(timer); handle = null; revision = null; busy = false;
    if (root) {
      root.querySelector("#d-key").value = "";
      root.querySelector("#d-review").innerHTML = "";
      message("Pending discovery invalidated by registry reset."); controls("");
    }
  }
  return { template: template, bind: bind, stop: stop, invalidate: invalidate };
})();

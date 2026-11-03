// Known Addresses registry screen.
// Spec: KNOWN_ADDRESS_REGISTRY_REQUIREMENTS.md
//   - Import by paste (CSV / any-format lines) or a .csv file
//   - Automatically recognises header columns address/label/wallet/type
//   - Lists wallet groups with counts, per-address search, batch history
//   - Delete individual addresses or whole batches (with per-address inform
//     that registry classification is what makes outputs auto-OWNED)
window.RegistryPage = (function () {

  function esc(s) { return Fmt.esc(String(s == null ? "" : s)); }

  function importBlock() {
    return '<div class="panel">' +
      '<h2>Import known addresses</h2>' +
      '<div class="hint" style="margin-bottom:8px">' +
      "Paste CSV or plain line-separated addresses below, or pick a .csv file. " +
      'Optional columns: <code>label</code>, <code>wallet</code>, <code>type</code>. ' +
      "Header rows are detected automatically. Only mainnet addresses are accepted. " +
      "</div>" +
      '<textarea id="reg-paste" placeholder="bc1q…\n1BvBMSEY…\n3P…\nlabel,address,wallet\n…" ' +
      'style="min-height:120px"></textarea>' +
      '<div style="margin-top:8px">' +
      '  <label style="margin-right:14px;font-size:12.5px">Default wallet <input type="text" id="reg-wallet" placeholder="default" style="width:130px"></label>' +
      '  <button class="btn" id="btn-reg-import">Import pasted text</button>' +
      '  <input type="file" id="reg-file" accept=".csv,text/csv" style="display:none">' +
      '  <button class="btn" id="btn-reg-file">Choose .csv file</button>' +
      "</div>" +
      '<div id="reg-msg" class="hint" style="margin-top:8px"></div>' +
      "</div>";
  }

  function walletGroupsBlock() {
    return '<div class="panel"><h2>Wallet groups</h2><div id="reg-groups" class="hint">Loading…</div></div>';
  }

  function addressesBlock() {
    return '<div class="panel"><h2>Registry addresses</h2>' +
      '<div class="hint" style="margin-bottom:8px" data-key-access-help>' +
      "<b>Key access</b>: <i>Key available</i> addresses are part of the wallet scan and count in holdings. " +
      "<i>No key access</i> addresses stay ownership evidence (their outputs remain OWNED and provenance through them stays valid), " +
      "but they are not scanned and never count in holdings; documented outputs on them appear under " +
      "<i>Not in current wallets</i>. Existing addresses keep key access until you change it here.</div>" +
      '  <div style="margin-bottom:8px">' +
      '    <input type="text" id="reg-search" placeholder="Search address / label / wallet…" style="width:320px">' +
      '    <select id="reg-wallet-filter" style="margin-left:8px"></select>' +
      '  </div>' +
      '  <div id="reg-addresses" class="hint">Loading…</div>' +
      "</div>";
  }

  function batchesBlock() {
    return '<div class="panel"><h2>Import batches</h2><div id="reg-batches" class="hint">Loading…</div></div>';
  }

  function discoveryBlock() {
    return WalletDiscoveryPage.template();
  }

  function evidenceBlock() {
    return '<div class="panel"><h2>Address evidence</h2>' +
      '<div class="hint" style="margin-bottom:8px">Why an address is treated as owned: ' +
      "<code>USER_CONFIRMED</code> (manual confirmation), <code>XPUB_DISCOVERY</code> (wallet discovery), " +
      "<code>MANUAL_IMPORT</code> (registry import). Evidence-free addresses keep the legacy " +
      "<code>ADDRESS_REGISTRY</code> source.</div>" +
      '<div id="reg-evidence" class="hint">Loading…</div></div>';
  }

  function conflictsBlock() {
    return '<div class="panel"><h2>Address conflicts</h2>' +
      "<div class='hint' style='margin-bottom:8px'>The same address claimed by two wallets after discovery. " +
      "Resolve with KEEP (registry stays), SHARED (both valid) or MOVED (address moved to the discovery wallet).</div>" +
      '<div id="reg-conflicts" class="hint">Loading…</div></div>';
  }

  // FIX-OWN-1: the output dialog opens this page filtered to one address.
  let pendingSearch = "";
  function focusAddress(address) {
    pendingSearch = address || "";
  }

  function render(el) {
    el.innerHTML =
      '<h1>Known Addresses Registry</h1>' +
      '<div class="hint">Addresses imported here are automatically classified as <span class="status-tag COMPLETE">OWNED</span> ' +
      "(source ADDRESS_REGISTRY) wherever they appear in traces. You can still override any output manually." +
      "</div>" +
      '<div id="reg-stats" class="hint" style="margin:8px 0"></div>' +
      '<div style="margin:8px 0">' +
      '  <button class="btn danger" id="btn-delete-all-addresses">Delete all Bitcoin addresses</button>' +
      "</div>" +
      importBlock() +
      discoveryBlock() +
      walletGroupsBlock() +
      addressesBlock() +
      batchesBlock() +
      evidenceBlock() +
      conflictsBlock();
    if (pendingSearch) {
      el.querySelector("#reg-search").value = pendingSearch;
      pendingSearch = "";
    }

    loadStats();
    loadGroups();
    loadAddresses();
    loadBatches();
    loadDiscoveries();
    loadEvidence();
    loadConflicts();

    el.querySelector("#btn-reg-import").addEventListener("click", function () {
      doImport();
    });
    el.querySelector("#btn-delete-all-addresses").addEventListener("click", function () {
      deleteAllAddresses();
    });
    WalletDiscoveryPage.bind(el, function () {
      loadStats(); loadGroups(); loadAddresses(); loadBatches();
      loadDiscoveries(); loadEvidence(); loadConflicts();
    });
    el.querySelector("#btn-reg-file").addEventListener("click", function () {
      el.querySelector("#reg-file").click();
    });
    el.querySelector("#reg-file").addEventListener("change", function (e) {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      const r = new FileReader();
      r.onload = function () {
        document.getElementById("reg-paste").value = r.result;
        doImport();
      };
      r.readAsText(f);
    });
    el.querySelector("#reg-search").addEventListener("input", function () {
      loadAddresses();
    });
    el.querySelector("#reg-wallet-filter").addEventListener("change", function () {
      loadAddresses();
    });
  }

  function msg(html, ok) {
    const box = document.getElementById("reg-msg");
    if (!box) return;
    box.innerHTML = ok ? '<span style="color:#3fb950">' + html + "</span>"
      : '<span style="color:#d29922">' + html + "</span>";
  }

  function walletFilterOptions() {
    let sel = document.getElementById("reg-wallet-filter");
    if (!sel || sel.options.length > 1) return;
    API.registryWalletGroups().then(function (groups) {
      if (!sel || sel.options.length > 1) return;
      let opts = '<option value="">All wallets</option>';
      groups.forEach(function (g) {
        opts += '<option value="' + esc(g.wallet_name) + '">' + esc(g.wallet_name) + " (" + g.address_count + ")</option>";
      });
      sel.innerHTML = opts;
    }).catch(function () {});
  }

  function loadStats() {
    API.registryStats().then(function (s) {
      const el2 = document.getElementById("reg-stats");
      if (el2) el2.textContent = "Known addresses: " + s.known_addresses;
    }).catch(function () {});
  }

  function loadGroups() {
    const box = document.getElementById("reg-groups");
    API.registryWalletGroups().then(function (groups) {
      if (!groups.length) {
        box.innerHTML = "No addresses imported yet.";
        return;
      }
      box.innerHTML = '<div class="io-list">' + groups.map(function (g) {
        return '<div class="io-row">' +
          '<div class="idx">' + esc(g.wallet_name) + "</div>" +
          '<div class="hint">' + (g.batch_count + " batch(es)") + "</div>" +
          '<div class="amt">' + g.address_count + " addresses</div></div>";
      }).join("") + "</div>";
      walletFilterOptions();
    }).catch(function (err) {
      box.innerHTML = '<span style="color:#d29922">' + esc(err.message) + "</span>";
    });
  }

  function loadAddresses() {
    const box = document.getElementById("reg-addresses");
    const q = document.getElementById("reg-search");
    const f = document.getElementById("reg-wallet-filter");
    const query = q ? q.value.trim() : "";
    const wallet = f ? f.value : "";
    API.registryAddresses(query, wallet).then(function (addrs) {
      if (!addrs.length) {
        box.innerHTML = "No matching addresses.";
        return;
      }
      box.innerHTML = '<table class="mono" style="width:100%;font-size:12.5px;border-collapse:collapse">' +
        "<thead><tr style='text-align:left;color:var(--text-dim)'>" +
        "<th style='padding:4px 8px'>Address</th><th style='padding:4px 8px'>Wallet</th>" +
        "<th style='padding:4px 8px'>Label</th><th style='padding:4px 8px'>Type</th>" +
        "<th style='padding:4px 8px'>Key access</th>" +
        "<th></th></tr></thead><tbody>" +
        addrs.map(function (a) {
          const hasKey = a.key_access !== false;
          return '<tr style="border-top:1px solid var(--border, #222)">' +
            "<td style='padding:4px 8px'>" + esc(a.address) + "</td>" +
            "<td style='padding:4px 8px'>" + esc(a.wallet_name) + "</td>" +
            "<td style='padding:4px 8px'>" + esc(a.label) + "</td>" +
            "<td style='padding:4px 8px'>" + esc(a.address_type) + "</td>" +
            '<td style="padding:4px 8px" data-key-access-state>' +
              (hasKey ? "Key available (counts in holdings)" : "No key access (documented only)") +
              ' <button class="btn small" data-key-access-addr="' + esc(a.address) + '" data-key-access-next="' + (hasKey ? "false" : "true") + '">' +
              (hasKey ? "Set: no key access" : "Set: key available") + "</button></td>" +
            "<td style='padding:4px 8px'><button class='btn small danger' data-del-addr='" + esc(a.address) + "'>Remove</button></td>" +
            "</tr>";
        }).join("") +
        "</tbody></table>";
      box.querySelectorAll("[data-del-addr]").forEach(function (b) {
        b.addEventListener("click", function () {
          deleteAddress(b.dataset.delAddr);
        });
      });
      box.querySelectorAll("[data-key-access-addr]").forEach(function (b) {
        b.addEventListener("click", function () {
          b.disabled = true;
          API.registryUpdateAddress(b.dataset.keyAccessAddr, { key_access: b.dataset.keyAccessNext === "true" })
            .then(function () { loadAddresses(); })
            .catch(function (err) {
              b.disabled = false;
              b.insertAdjacentHTML("afterend", ' <span class="hint" role="status">' + esc(err.message) + "</span>");
            });
        });
      });
    }).catch(function (err) {
      box.innerHTML = '<span style="color:#d29922">' + esc(err.message) + "</span>";
    });
  }

  function loadBatches() {
    const box = document.getElementById("reg-batches");
    API.registryBatches().then(function (batches) {
      if (!batches.length) {
        box.innerHTML = "No import batches recorded.";
        return;
      }
      box.innerHTML = '<div class="io-list">' + batches.map(function (b) {
        return '<div class="io-row">' +
          '<div class="idx">#' + b.id + "</div>" +
          '<div class="hint">' + esc(b.name) + " · " + esc(b.wallet_name) + " · " + esc(b.source) + "</div>" +
          '<div class="amt">' + b.address_count + " addr · " + b.duplicate_count + " dup · " + b.invalid_count + " invalid</div>" +
          '<div><button class="btn small danger" data-del-batch="' + b.id + '">Delete</button></div>' +
          "</div>";
      }).join("") + "</div>";
      box.querySelectorAll("[data-del-batch]").forEach(function (b) {
        b.addEventListener("click", function () {
          deleteBatch(Number(b.dataset.delBatch));
        });
      });
    }).catch(function (err) {
      box.innerHTML = '<span style="color:#d29922">' + esc(err.message) + "</span>";
    });
  }

  function doImport() {
    const content = document.getElementById("reg-paste").value;
    const wallet = document.getElementById("reg-wallet").value.trim() || "default";
    if (!content.trim()) {
      msg("Nothing to import.", false);
      return;
    }
    msg("Importing…");
    API.importRegistry({ content: content, wallet_name: wallet, source: "PASTE" }).then(function (r) {
      msg("Imported <b>" + r.imported + "</b>, duplicates <b>" + r.duplicates +
        "</b>, invalid <b>" + (r.total - r.imported) + "</b> of " + r.total + ".");
      loadStats(); loadGroups(); loadAddresses(); loadBatches();
    }).catch(function (err) {
      msg(esc(err.message), false);
    });
  }

  function deleteAddress(addr) {
    if (!window.confirm("Remove this address from the registry?\n\n" + addr)) return;
    API.registryDeleteAddress(addr).then(function () {
      loadStats(); loadGroups(); loadAddresses();
    }).catch(function (err) {
      window.alert(err.message);
    });
  }

  function deleteBatch(id) {
    if (!window.confirm("Delete import batch #" + id + " and ALL its addresses?")) return;
    API.registryDeleteBatch(id).then(function () {
      loadStats(); loadGroups(); loadAddresses(); loadBatches();
    }).catch(function (err) {
      window.alert(err.message);
    });
  }

  function deleteAllAddresses() {
    if (!window.confirm(
      "Delete all Bitcoin addresses?\n\n" +
      "This removes all registered addresses, import batches, wallet-discovery metadata, " +
      "and MANUAL_IMPORT / XPUB_DISCOVERY evidence. USER_CONFIRMED evidence is preserved.\n\n" +
      "It does not delete Acquisitions, Exchange Exits, Disposals, Node Settings, or traces.\n\n" +
      "Ownership, position, and documentation coverage will be recalculated from remaining facts."
    )) return;
    API.deleteAllBitcoinAddresses().then(function (r) {
      WalletDiscoveryPage.invalidate();
      msg(
        "Deleted " + r.deleted_addresses + " address(es), " +
        r.deleted_batches + " batch(es), " + r.deleted_evidence + " evidence row(s).",
        true
      );
      loadStats(); loadGroups(); loadAddresses(); loadBatches();
      loadDiscoveries(); loadEvidence(); loadConflicts();
    }).catch(function (err) {
      window.alert(err.message);
    });
  }

  function loadDiscoveries() {
    const box = document.getElementById("reg-discoveries");
    API.listDiscoveries().then(function (rows) {
      if (!rows || !rows.length) {
        box.innerHTML = "No discoveries yet.";
        return;
      }
      box.innerHTML = '<div class="io-list">' + rows.map(function (d) {
        return '<div class="io-row">' +
          '<div class="idx">#' + d.id + "</div>" +
          '<div class="hint">' + esc(d.wallet_name) + " · " + esc(d.key_type) + " · " + esc(d.account_path) + "</div>" +
          '<div class="amt">' + esc(d.status) + " · r" + d.branch0_found + " c" + d.branch1_found +
          '</div><div><button class="btn small danger" data-del-discovery="' + d.id + '">Delete</button></div>' +
          "</div>";
      }).join("") + "</div>";
      box.querySelectorAll("[data-del-discovery]").forEach(function (b) {
        b.addEventListener("click", function () {
          deleteDiscovery(Number(b.dataset.delDiscovery));
        });
      });
    }).catch(function (err) {
      box.innerHTML = '<span style="color:#d29922">' + esc(err.message) + "</span>";
    });
  }

  function deleteDiscovery(id) {
    if (!window.confirm("Delete discovery #" + id + " including addresses/evidence it alone introduced?")) return;
    API.deleteDiscovery(id).then(function () {
      loadStats(); loadGroups(); loadAddresses(); loadDiscoveries(); loadEvidence();
    }).catch(function (err) {
      window.alert(err.message);
    });
  }

  function loadEvidence() {
    const box = document.getElementById("reg-evidence");
    API.registryEvidence(null).then(function (rows) {
      if (!rows || !rows.length) {
        box.innerHTML = "No evidence rows yet — every address the wallet treats as owned has one.";
        return;
      }
      box.innerHTML = '<div class="io-list">' + rows.slice(0, 200).map(function (e) {
        return '<div class="io-row">' +
          '<div class="idx">' + esc(e.wallet_name) + "</div>" +
          '<div class="hint">' + esc(e.address) + (e.derivation_path ? " · " + esc(e.derivation_path) : "") + "</div>" +
          '<div class="amt">' + esc(e.source) +
          ' <button class="btn small danger" data-del-ev="' + encodeURIComponent(e.address) + ":" + esc(e.source) + ":" + encodeURIComponent(e.wallet_name) + '">Remove</button></div>' +
          "</div>";
      }).join("") + "</div>";
      box.querySelectorAll("[data-del-ev]").forEach(function (b) {
        b.addEventListener("click", function () {
          const parts = b.dataset.delEv.split(":");
          API.registryDeleteEvidence(decodeURIComponent(parts[0]), decodeURIComponent(parts[1]), decodeURIComponent(parts[2]))
            .then(function () { loadEvidence(); })
            .catch(function (err) { window.alert(err.message); });
        });
      });
    }).catch(function (err) {
      box.innerHTML = '<span style="color:#d29922">' + esc(err.message) + "</span>";
    });
  }

  function loadConflicts() {
    const box = document.getElementById("reg-conflicts");
    API.listConflicts().then(function (rows) {
      if (!rows || !rows.length) {
        box.innerHTML = "No open ownership conflicts.";
        return;
      }
      box.innerHTML = '<div class="io-list">' + rows.map(function (c) {
        return '<div class="io-row">' +
          '<div class="idx">#' + c.id + "</div>" +
          '<div class="hint">' + esc(c.address) + "<br>" + esc(c.wallet_name) + " vs " + esc(c.evidence_wallet_name) +
          " · " + esc(c.reason || "") + "</div>" +
          '<div class="amt">' + esc(c.status || "PENDING") +
          " · <button class='btn small' data-resolve-conflict='" + c.id + ":KEEP'>KEEP</button> " +
          "<button class='btn small' data-resolve-conflict='" + c.id + ":SHARED'>SHARED</button> " +
          "<button class='btn small' data-resolve-conflict='" + c.id + ":MOVED'>MOVED</button></div>" +
          "</div>";
      }).join("") + "</div>";
      box.querySelectorAll("[data-resolve-conflict]").forEach(function (b) {
        b.addEventListener("click", function () {
          const parts = b.dataset.resolveConflict.split(":");
          API.resolveConflict(Number(parts[0]), parts[1]).then(function () {
            loadConflicts(); loadAddresses(); loadEvidence();
          }).catch(function (err) {
            window.alert(err.message);
          });
        });
      });
    }).catch(function (err) {
      box.innerHTML = '<span style="color:#d29922">' + esc(err.message) + "</span>";
    });
  }

  return { render: render, focusAddress: focusAddress };
})();

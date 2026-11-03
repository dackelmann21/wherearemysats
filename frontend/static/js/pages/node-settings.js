// Secrets are submitted only to WAMS. No local/session storage, URL credentials,
// console logging or dynamic RPC URLs. Passwords never come back from the API.
window.NodeSettingsPage = (function () {
  function clearPassword() {
    const field = document.getElementById("node-password");
    if (field) field.value = "";
  }

  async function request(method, suffix, payload) {
    const options = { method: method, cache: "no-store", headers: { "X-WAMS-Settings": "1" } };
    if (payload) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    let response;
    try {
      response = await fetch("/api/node-settings" + suffix, options);
    } catch (_) {
      throw new Error("Cannot reach WAMS. Check the application connection.");
    }
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Node Settings request failed.");
    return result;
  }

  function field(id, label, control, hint) {
    return '<div class="ns-field"><label for="' + id + '">' + label + '</label>' + control +
      (hint ? '<p class="hint">' + hint + '</p>' : '') + '</div>';
  }

  function render(el) {
    el.innerHTML = '<div class="ns-page"><div class="ns-head"><div>' +
      '<h1>Bitcoin Node Settings</h1><p class="ns-sub">Connection from WAMS to your Bitcoin Core RPC.</p></div>' +
      '<div class="ns-badges"><span id="node-config-source" class="badge"></span>' +
      '<span id="node-restart-badge" class="badge ns-badge-warn hidden">Restart pending</span></div></div>' +
      '<div class="panel"><form id="node-settings-form" autocomplete="off">' +
      '<h3>Connection</h3><div class="ns-grid ns-grid-host">' +
      field("node-host", "RPC Host (DNS, IPv4 or IPv6)",
        '<input id="node-host" type="text" required maxlength="253" placeholder="bitcoin-node.example" />',
        'Inside Docker, localhost means the WAMS container. See the Linux installation guide for host connections.') +
      field("node-port", "RPC Port", '<input id="node-port" type="number" min="1" max="65535" value="8332" required />') +
      '</div><div class="ns-grid">' +
      field("node-username", "RPC Username",
        '<input id="node-username" type="text" required maxlength="256" autocomplete="off" />') +
      field("node-password", "RPC Password",
        '<input id="node-password" type="password" maxlength="4096" autocomplete="new-password" />',
        'Leave blank to keep the saved or environment password. Changing host, port, username or TLS requires re-entry. ' +
        'Test Connection keeps the entered password for Save; it is cleared after a successful Save or when leaving this page.') +
      '</div><h3>Network and transport</h3><div class="ns-grid">' +
      field("node-network", "Bitcoin Network",
        '<select id="node-network"><option value="mainnet">Mainnet</option><option value="testnet">Testnet (testnet3)</option>' +
        '<option value="signet">Signet</option><option value="regtest">Regtest</option></select>',
        'Use the same Bitcoin network as the existing database.') +
      '<div class="ns-field ns-check"><label><input id="node-tls" type="checkbox" /> HTTPS to RPC (verified TLS proxy)</label></div>' +
      '</div><div class="alert info ns-info">' +
      // The snapshot belongs to the previous node/network (STALE_NODE).
      '<p id="node-inventory-hint">Saving marks the Current Wallet inventory snapshot as stale. ' +
      'It stays stored but is not shown until WAMS is restarted and the inventory is refreshed on the saved node.</p>' +
      '<p>Save persists settings. The operator must restart WAMS to activate changes for all workers.</p>' +
      '<p>Acquisitions, traces and notes are not affected.</p></div>' +
      '<div class="ns-actions"><button type="button" id="node-test" class="btn">Test Connection</button>' +
      '<button type="submit" id="node-save" class="btn primary">Save</button></div>' +
      '<div id="node-settings-result" role="status" aria-live="polite"></div></form></div></div>';

    const form = el.querySelector("form");
    const resultEl = el.querySelector("#node-settings-result");
    const test = el.querySelector("#node-test");
    const save = el.querySelector("#node-save");
    function busy(value) { test.disabled = value; save.disabled = value; }
    function result(message, ok, details) {
      resultEl.className = "alert " + (ok ? "ok" : "warn");
      resultEl.textContent = message;
      if (!details) return;
      // API values only as text nodes, never as markup.
      const list = document.createElement("dl");
      list.className = "ns-kv";
      details.forEach(function (pair) {
        const key = document.createElement("dt");
        key.textContent = pair[0];
        const value = document.createElement("dd");
        value.textContent = pair[1];
        list.appendChild(key);
        list.appendChild(value);
      });
      resultEl.appendChild(list);
    }
    function source(data) {
      if (data.source !== undefined) el.querySelector("#node-config-source").textContent = "Source: " + data.source;
      el.querySelector("#node-restart-badge").classList.toggle("hidden", !data.restart_required);
    }
    busy(true);
    request("GET", "").then(function (data) {
      ["host", "port", "username", "network"].forEach(function (key) {
        el.querySelector("#node-" + key).value = data[key];
      });
      el.querySelector("#node-tls").checked = data.tls;
      el.querySelector("#node-password").placeholder = data.password_configured ? "Unchanged (write-only)" : "Enter RPC password";
      source(data);
      if (data.configuration_error) result(data.configuration_error, false);
      if (data.restart_required) result("Saved changes are pending. Restart WAMS to activate them.", false);
    }).catch(function (err) { result(err.message, false); }).finally(function () { busy(false); });

    async function submit(isTest) {
      if (!form.reportValidity()) return;
      busy(true);
      result(isTest ? "Testing through WAMS backend…" : "Saving…", true);
      const passwordField = el.querySelector("#node-password");
      const payload = {
        host: el.querySelector("#node-host").value,
        port: Number(el.querySelector("#node-port").value),
        username: el.querySelector("#node-username").value,
        // Read only the value, never the write-only placeholder; do not trim.
        password: passwordField.value,
        network: el.querySelector("#node-network").value,
        tls: el.querySelector("#node-tls").checked,
      };
      try {
        const data = await request(isTest ? "POST" : "PUT", isTest ? "/test" : "", payload);
        if (!isTest) {
          // Clear only the saved value, not a newer edit made during the request.
          if (passwordField.value === payload.password) passwordField.value = "";
          passwordField.placeholder = "Unchanged (write-only)";
          source(data);
        }
        let details = null;
        if (isTest && data.network) {
          const ibd = data.initial_block_download;
          details = [["Network", data.network], ["Block height", data.block_height],
            ["Initial block download", ibd === true ? "Yes" : ibd === false ? "No" : ibd]];
        }
        result(data.message, isTest ? data.state === "connected" : true, details);
      } catch (err) {
        result(err.message, false);
      } finally {
        payload.password = "";
        busy(false);
      }
    }
    test.addEventListener("click", function () { submit(true); });
    form.addEventListener("submit", function (event) { event.preventDefault(); submit(false); });
  }
  return { render: render, clearPassword: clearPassword };
})();

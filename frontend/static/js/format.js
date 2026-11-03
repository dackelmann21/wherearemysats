// Formatting helpers.
window.Fmt = (function () {
  function sats(num) {
    if (num === null || num === undefined || isNaN(num)) return "—";
    return Number(num).toLocaleString("en-US");
  }
  function btcFromSats(num) {
    if (num === null || num === undefined || isNaN(num)) return "—";
    return (num / 100000000).toFixed(8);
  }
  function shortTxid(txid, len) {
    if (!txid) return "";
    const n = len || 8;
    if (txid.length <= n * 2 + 1) return txid;
    return txid.slice(0, n) + "…" + txid.slice(-4);
  }
  function shortAddress(addr) {
    if (!addr) return "—";
    if (addr.length <= 18) return addr;
    return addr.slice(0, 12) + "…" + addr.slice(-6);
  }
  // --- "Spent in merge transaction" presentation helpers (FIX-UI-4) ---------
  // Pure, observable-only presentation logic; no sat attribution between inputs.
  function plural(n, one, many) {
    return n + " " + (n === 1 ? one : (many || one + "s"));
  }
  // Lineage input first (the viewed UTXO), then the rest by amount descending.
  // The original vin index is kept as _vin.
  function mergeInputRows(inputs, currentKey) {
    const rows = (inputs || []).map(function (i, idx) {
      const key = (i.txid || "") + ":" + (i.vout !== undefined ? i.vout : "");
      return { input: i, _vin: idx, key: key,
               isThis: i.trace_role === "LINEAGE_INPUT" || key === currentKey };
    });
    rows.sort(function (a, b) {
      if (a.isThis !== b.isThis) return a.isThis ? -1 : 1;
      const av = a.input.value_sats == null ? -1 : a.input.value_sats;
      const bv = b.input.value_sats == null ? -1 : b.input.value_sats;
      if (av !== bv) return bv - av;
      return a._vin - b._vin;
    });
    return rows;
  }
  // Visible role label; the internal enum is only exposed as a tooltip.
  function mergeRoleLabel(role, isThis) {
    if (role === "LINEAGE_INPUT" || (isThis && !role)) return "This UTXO (lineage)";
    if (role === "KNOWN_TRACE_INPUT") return "Co-input (known trace)";
    return "Co-input";
  }
  // At most max chips visible; the rest is behind "+N".
  function splitTraceChips(ids, max) {
    const all = ids || [];
    const m = max === undefined ? 3 : max;
    return { visible: all.slice(0, m), hidden: all.slice(m) };
  }
  // Observable balance only: sums of inputs/outputs and the fee. A sum with a
  // missing amount is null (never guessed).
  function mergeBalance(inputs, outputs) {
    function total(list) {
      let t = 0;
      for (let k = 0; k < list.length; k++) {
        if (list[k].value_sats == null) return null;
        t += list[k].value_sats;
      }
      return t;
    }
    const inSum = total(inputs || []), outSum = total(outputs || []);
    return { inputs: inSum, outputs: outSum,
             fee: inSum !== null && outSum !== null ? inSum - outSum : null };
  }
  // Ownership text naming what the data actually carries. The wallet name is
  // the label stored with the ownership/known-address evidence; "default" is the
  // fallback label when an address was registered without a wallet name. The
  // evidence source is not part of the input payload, so it is not claimed.
  function mergeOwnershipText(status, walletName) {
    const st = status || "UNKNOWN";
    const label = st.charAt(0) + st.slice(1).toLowerCase();
    if (!walletName) return label;
    if (walletName === "default") return label + " · wallet label \"default\" (no name given)";
    return label + " · wallet \"" + walletName + "\"";
  }

  function date(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.toISOString().slice(0, 10) + " " + d.toUTCString().slice(17, 22);
  }
  function dateOnly(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toISOString().slice(0, 10);
  }
  // One open movement (FIX-UI-6/FIX-UI-7): unresolved spends read "spent in",
  // partial spends (foreign_amount_sats present) read "partly sent to a
  // non-own output". Plain text; callers escape and append their own links.
  function openMovementText(s) {
    var text = "Open: " + shortTxid(s.txid, 8) + ":" + s.vout +
      (s.amount_sats != null ? " (" + btcFromSats(s.amount_sats) + " BTC)" : "");
    if (s.foreign_amount_sats !== undefined) {
      text += " partly sent to a non-own output" +
        (s.foreign_amount_sats != null ? " (" + btcFromSats(s.foreign_amount_sats) + " BTC)" : "") +
        " in " + shortTxid(s.spending_txid, 8);
    } else {
      text += " spent in " + shortTxid(s.spending_txid, 8);
    }
    if (s.spending_block_time) text += " on " + dateOnly(s.spending_block_time);
    return text;
  }
  function statusLabel(s) {
    const map = {
      UNSPENT: "UNSPENT", SPENT: "SPENT", UNKNOWN: "UNKNOWN",
      OWNED: "Owned", LIKELY_OWNED: "Likely owned", EXTERNAL: "External", UNKNOWN_O: "Unknown",
    };
    return map[s] || s;
  }
  function esc(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  // SEC-19: a trace ID from the URL hash (or anywhere else) is accepted only
  // as a positive decimal safe integer; everything else is null.
  function traceId(value) {
    if (typeof value === "number") return Number.isSafeInteger(value) && value > 0 ? value : null;
    if (typeof value !== "string" || !/^[1-9][0-9]*$/.test(value)) return null;
    const n = Number(value);
    return Number.isSafeInteger(n) ? n : null;
  }
  function relClass(rel) {
    const map = { DIRECT: "ok", SPLIT: "info", MERGED: "warn", MIXED: "warn", CUSTODY_BRIDGE: "warn", UNKNOWN: "unknown" };
    return map[rel] || "unknown";
  }
  function sourceLabel(src) {
    const map = {
      TRACE_START: "trace start assumption",
      USER_CONFIRMED: "user confirmed",
      XPUB_DISCOVERY: "wallet discovery",
      MANUAL_IMPORT: "manual import",
      ADDRESS_REGISTRY: "registry",
      PROPAGATED: "propagated from owned",
      USER: "manual",
      KNOWN_ADDRESS: "known address",
      DESCRIPTOR: "descriptor",
      XPUB: "xpub",
      HEURISTIC: "heuristic",
    };
    return map[src] || src;
  }
  // EUR money input boundary (v0.9.1). Accepts the simple German decimal comma
  // ("1000,00"), the dot ("1000.00") and confident thousands grouping when BOTH
  // separators appear and the full pattern is unambiguous (Germany "2.000,00",
  // US "2,000.00" -> "2000.00"). A single separator is ALWAYS a decimal
  // separator, so a plain "2.000" is never reinterpreted as 2000. Returns the
  // canonical dot-decimal string or "" when the input is not a valid amount.
  // The backend is authoritative and re-validates; no binary float is involved.
  function normalizeEconomicDecimal(v) {
    if (v === null || v === undefined) return "";
    var s = String(v).trim();
    if (s === "") return "";
    var neg = s.charAt(0) === "-";
    var body = neg ? s.slice(1) : s;
    if (body === "") return "";
    var sign = neg ? "-" : "";
    if (body.indexOf(",") !== -1 && body.indexOf(".") !== -1) {
      var norm;
      if (body.lastIndexOf(",") > body.lastIndexOf(".")) {
        norm = expandGroups(body, ",", ".");
      } else {
        norm = expandGroups(body, ".", ",");
      }
      if (norm === null) return "";
      return sign + norm;
    }
    if (body.indexOf(",") !== -1) {
      if (body.split(",").length !== 2) return "";
      var parts = body.split(",");
      if (parts[0] === "" || parts[1] === "") return "";
      if (!/^\d+$/.test(parts[0]) || !/^\d+$/.test(parts[1])) return "";
      return sign + parts[0] + "." + parts[1];
    }
    return /^-?\d+(\.\d+)?$/.test(s) ? s : "";
  }
  // Shared grouped-decimal expander (mirror of the backend _expand_currency_groups).
  // decimal_sep is the LAST separator (the fraction part follows it); group_sep
  // is the thousands separator inside the integer part. Returns the canonical
  // "digits.frac" string, or null when the grouping pattern is malformed.
  function expandGroups(body, decimalSep, groupSep) {
    var idx = body.lastIndexOf(decimalSep);
    var intPart = body.slice(0, idx);
    var frac = body.slice(idx + 1);
    if (!/^\d+$/.test(frac)) return null;
    var groups = intPart.split(groupSep);
    if (groups.length < 2) return null;
    for (var i = 0; i < groups.length; i++) {
      if (!/^\d+$/.test(groups[i])) return null;
    }
    if (groups[0].length > 3) return null;
    for (var j = 1; j < groups.length; j++) {
      if (groups[j].length !== 3) return null;
    }
    return groups.join("") + "." + frac;
  }
  // Fee-percent boundary: simple comma/dot decimal ONLY. A monetary value like
  // "1.000,50" must NOT gain thousands-grouping semantics here (separated from
  // the EUR money normalizer, mirroring the backend).
  function normalizeFeePercent(v) {
    if (v === null || v === undefined) return "";
    var s = String(v).trim();
    if (s === "") return "";
    if (s.indexOf(",") === -1) {
      return /^-?\d+(\.\d+)?$/.test(s) ? s : "";
    }
    if (s.indexOf(".") !== -1 || s.split(",").length !== 2) return "";
    var parts = s.split(",");
    if (parts[0] === "" || parts[1] === "") return "";
    if (!/^-?\d+$/.test(parts[0]) || !/^\d+$/.test(parts[1])) return "";
    return parts[0] + "." + parts[1];
  }
  function _decParts(s) {
    var neg = s.charAt(0) === "-";
    var t = neg ? s.slice(1) : s;
    var dot = t.indexOf(".");
    return {
      neg: neg,
      whole: dot === -1 ? t : t.slice(0, dot),
      frac: dot === -1 ? "" : t.slice(dot + 1),
    };
  }
  function deMoney(v) {
    var s = v === null || v === undefined ? "" : String(v);
    var norm = normalizeEconomicDecimal(s);
    if (norm === "") return "";
    var p = _decParts(norm);
    var frac = p.frac;
    if (frac.length > 2) {
      frac = frac.slice(0, 2);
    } else if (frac.length < 2) {
      while (frac.length < 2) frac += "0";
    }
    return (p.neg ? "-" : "") + p.whole + "," + frac;
  }
  function de(v) {
    var s = v === null || v === undefined ? "" : String(v);
    var norm = normalizeEconomicDecimal(s);
    if (norm === "") return "";
    return norm.replace(".", ",");
  }
  function acquisitionBadge(state) {
    var descriptions = {
      ANCHORED: "All known relevant ancestry reaches documented Acquisition anchors; user assertions are not blockchain proof of acquisition or ownership.",
      PARTIAL: "Documented and undocumented ancestry contribute. No sat-level fraction is assigned.",
      UNANCHORED: "No documented on-chain Acquisition anchor is established for this managed lineage. This is a neutral evidence state.",
      NEEDS_REVIEW: "Required ancestry evidence is unresolved; absence of an anchor cannot be concluded."
    };
    if (!descriptions[state]) return "";
    return '<span class="badge provenance-badge" title="' + esc(descriptions[state]) + '">' + esc(state) + '</span>';
  }
  return {
    sats: sats, btc: btcFromSats, shortTxid: shortTxid,
    shortAddress: shortAddress, date: date, dateOnly: dateOnly,
    openMovementText: openMovementText,
    statusLabel: statusLabel, esc: esc, traceId: traceId, relClass: relClass,
    plural: plural, mergeInputRows: mergeInputRows, mergeRoleLabel: mergeRoleLabel,
    splitTraceChips: splitTraceChips, mergeBalance: mergeBalance, mergeOwnershipText: mergeOwnershipText,
    sourceLabel: sourceLabel, normalizeEconomicDecimal: normalizeEconomicDecimal,
    normalizeFeePercent: normalizeFeePercent,
    deMoney: deMoney, de: de,
    acquisitionBadge: acquisitionBadge,
  };
})();

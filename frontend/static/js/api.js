// API client - all requests go to the local backend (no external APIs, REQ 34).
window.API = (function () {
  async function request(path, method, body, signal) {
    const opts = { method: method || "GET", headers: {} };
    if (signal) opts.signal = signal;
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      if (e.name === "AbortError") throw e;
      throw { message: "Network error: cannot reach the application server.", status: 0 };
    }
    let data = null;
    const text = await res.text();
    if (text) {
      try { data = JSON.parse(text); } catch (e) { data = text; }
    }
    if (!res.ok) {
      let message = "Request failed (" + res.status + ")";
      if (data && typeof data === "object" && data.detail) {
        message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      } else if (typeof data === "string") {
        message = data;
      }
      throw { message: message, status: res.status, data: data };
    }
    return data;
  }

  async function csvDownload(path) {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) {
      const error = await response.json();
      throw new Error(typeof error.detail === 'string' ? error.detail : 'CSV export failed.');
    }
    return response.blob();
  }

  return {
    reportRequest: request,
    health: () => request("/api/health"),
    gettingStarted: () => request("/api/getting-started"),
    documentationCoverage: (signal, saved, wallet) => request(saved ? '/api/reports/workspace?part=coverage' + (wallet ? '&wallet=' + encodeURIComponent(wallet) : '') : "/api/reports/documentation-coverage", "GET", undefined, signal),
    // All known No-Acquisition anchors in scope (Dashboard Origins card, D-06).
    noAcquisitionAnchors: (offset, limit, signal, wallet) => request('/api/reports/view?dataset=origins&offset=' + offset + '&limit=' + limit + (wallet ? '&wallet=' + encodeURIComponent(wallet) : ''), 'GET', undefined, signal),
    walletReceipts: (offset, limit, signal, wallet) => request('/api/reports/workspace?part=no-acquisition&offset=' + offset + '&limit=' + limit + (wallet ? '&wallet=' + encodeURIComponent(wallet) : ''), 'GET', undefined, signal),
    inventory: (signal) => request("/api/inventory", "GET", undefined, signal),
    inventoryRefresh: () => request("/api/inventory/refresh", "POST", {}),
    inventoryRefreshStatus: () => request("/api/inventory/refresh"),
    inventoryCsv: (query) => csvDownload("/api/inventory/export.csv" + query),
    provenance: (query, signal, saved, wallet) => request(saved ? '/api/reports/workspace?part=provenance&' + query.replace(/^\?/, '') + (wallet ? '&wallet=' + encodeURIComponent(wallet) : '') : '/api/provenance' + query, 'GET', undefined, signal),
    provenanceCsv: (query) => csvDownload('/api/provenance/export.csv' + query),
    provenanceAnchor: (txid, vout) => request('/api/provenance/anchors/' + encodeURIComponent(txid) + '/' + encodeURIComponent(vout)),
    provenanceAnchorEdit: (txid, vout, payload) => request('/api/provenance/anchors/' + encodeURIComponent(txid) + '/' + encodeURIComponent(vout), 'PATCH', payload),
    provenanceAnchorHistory: () => request('/api/provenance/anchors'),
    analyze: (txid, address, vout) =>
      request("/api/analyze", "POST", {
        start_txid: txid,
        start_address: address || null,
        start_vout: vout === null || vout === "" ? null : Number(vout),
      }),
    createTrace: (payload) => request("/api/traces", "POST", payload),
    listTraces: () => request("/api/traces"),
    getTrace: (id) => request("/api/traces/" + encodeURIComponent(id)),
    deleteTrace: (id) => request("/api/traces/" + encodeURIComponent(id), "DELETE"),
    deleteAllTraces: () => request("/api/traces", "DELETE"),
    selectOutputs: (id, txid, vouts) =>
      request("/api/traces/" + encodeURIComponent(id) + "/select", "POST", { txid: txid, vouts: vouts }),
    classify: (id, txid, vout, payload) =>
      request("/api/traces/" + encodeURIComponent(id) + "/outpoints/" + encodeURIComponent(txid) + "/" + encodeURIComponent(vout) + "/classify", "POST", payload),
    continueTrace: (id) => request("/api/traces/" + encodeURIComponent(id) + "/continue", "POST", {}),
    follow: (id, txid, vout) =>
      request("/api/traces/" + encodeURIComponent(id) + "/follow", "POST", { txid: txid, vout: vout }),
    // Background Blockchain Analysis job state (v0.5.6): QUEUED / RUNNING /
    // COMPLETE / LIMIT_REACHED / FAILED plus live progress counters.
    analysisJob: (id) => request("/api/traces/" + encodeURIComponent(id) + "/analysis-job"),
    summary: (id) => request("/api/traces/" + encodeURIComponent(id) + "/summary"),
    exportJson: (id) => request("/api/traces/" + encodeURIComponent(id) + "/export"),
    // opts.provenance "neutral": ownership-neutral provenance (analysis dialog).
    transaction: (txid, opts) => request("/api/transactions/" + encodeURIComponent(txid) +
      (opts && opts.provenance ? "?provenance=" + encodeURIComponent(opts.provenance) : "")),
    addAnnotation: (payload) => request("/api/annotations", "POST", payload),
    settings: () => request("/api/settings"),
    importRegistry: (payload) => request("/api/registry/import", "POST", payload),
    registryAddresses: (query, walletName) =>
      request("/api/registry/addresses?query=" + encodeURIComponent(query || "") +
        (walletName ? "&wallet_name=" + encodeURIComponent(walletName) : "")),
    registryGetAddress: (address) => request("/api/registry/addresses/" + encodeURIComponent(address)),
    registryDeleteAddress: (address) => request("/api/registry/addresses/" + encodeURIComponent(address), "DELETE"),
    registryUpdateAddress: (address, payload) =>
      request("/api/registry/addresses/" + encodeURIComponent(address), "PATCH", payload),
    registryBatches: () => request("/api/registry/batches"),
    registryDeleteBatch: (id) => request("/api/registry/batches/" + encodeURIComponent(id), "DELETE"),
    registryWalletGroups: () => request("/api/registry/wallet-groups"),
    registryStats: () => request("/api/registry/stats"),
    deleteAllBitcoinAddresses: () => request("/api/registry/addresses", "DELETE"),
    custodialBoundaries: (id) => request("/api/traces/" + encodeURIComponent(id) + "/custodial-boundaries"),
    markCustodialBoundary: (id, txid, vout, serviceName, note) =>
      request("/api/traces/" + encodeURIComponent(id) + "/custodial-boundaries", "POST", {
        txid: txid, vout: vout,
        service_name: serviceName || "", note: note || null,
      }),
    deleteCustodialBoundary: (id, boundaryId) =>
      request("/api/traces/" + encodeURIComponent(id) + "/custodial-boundaries/" + encodeURIComponent(boundaryId), "DELETE"),
    deleteOutpointCustody: (id, txid, vout) =>
      request("/api/traces/" + encodeURIComponent(id) + "/outpoints/" + encodeURIComponent(txid) + "/" + encodeURIComponent(vout) + "/custody", "DELETE"),
    custodyCandidates: (id, boundaryId) =>
      request("/api/traces/" + encodeURIComponent(id) + "/custodial-boundaries/" + encodeURIComponent(boundaryId) + "/candidates"),
    custodyBridges: (id) => request("/api/traces/" + encodeURIComponent(id) + "/custody-bridges"),
    confirmCustodyBridge: (id, boundaryId, destTxid, destVout, confidence) =>
      request("/api/traces/" + encodeURIComponent(id) + "/custody-bridges", "POST", {
        boundary_id: boundaryId,
        destination_txid: destTxid,
        destination_vout: destVout,
        confidence: confidence,
      }),
    deleteCustodyBridge: (id, bridgeId) =>
      request("/api/traces/" + encodeURIComponent(id) + "/custody-bridges/" + encodeURIComponent(bridgeId), "DELETE"),
    declineCustodialSuggestion: (id, txid, vout, opts) =>
      request("/api/traces/" + encodeURIComponent(id) + "/custodial-suggestions/" + encodeURIComponent(txid) + "/" + encodeURIComponent(vout) + "/decline", "POST", {
        reasons: (opts && opts.reasons) || [],
        confidence: (opts && opts.confidence) || "likely",
        service_name: (opts && opts.service_name) || "",
        note: (opts && opts.note) || null,
      }),
    resetCustodialSuggestion: (id, txid, vout) =>
      request("/api/traces/" + encodeURIComponent(id) + "/custodial-suggestions/" + encodeURIComponent(txid) + "/" + encodeURIComponent(vout) + "/decline", "DELETE"),
    // Wallet discovery.
    discoveryPlan: (payload) => request("/api/discovery/plan", "POST", payload),
    discoveryPreview: (payload) => request("/api/discovery/preview", "POST", payload),
    discoveryRun: (payload) => request("/api/discovery/run", "POST", payload),
    walletScanStart: (payload) => request("/api/discovery/sessions", "POST", payload),
    walletScanStatus: (handle) => request("/api/discovery/sessions/" + encodeURIComponent(handle)),
    walletScanCommit: (handle, revision) => request("/api/discovery/sessions/" + encodeURIComponent(handle) + "/commit", "POST", { revision: revision }),
    walletScanDiscard: (handle) => request("/api/discovery/sessions/" + encodeURIComponent(handle), "DELETE"),
    listDiscoveries: () => request("/api/discovery"),
    getDiscovery: (id) => request("/api/discovery/" + encodeURIComponent(id)),
    deleteDiscovery: (id) => request("/api/discovery/" + encodeURIComponent(id), "DELETE"),
    // Evidence / conflict resolution.
    registryEvidence: (address) =>
      request("/api/registry/evidence" + (address ? "/" + encodeURIComponent(address) : "")),
    registryDeleteEvidence: (address, source, walletName) =>
      request("/api/registry/evidence/" + encodeURIComponent(address) + "/" + encodeURIComponent(source) +
        "?wallet_name=" + encodeURIComponent(walletName || "default"), "DELETE"),
    registryConfirmAddress: (address, walletName) =>
      request("/api/registry/addresses/" + encodeURIComponent(address) + "/confirm", "POST",
        { wallet_name: walletName || null }),
    listConflicts: () => request("/api/registry/conflicts"),
    resolveConflict: (id, resolution) =>
      request("/api/registry/conflicts/" + encodeURIComponent(id) + "/resolve", "POST", { resolution: resolution }),
    ownershipCustodyConflicts: (id) =>
      request("/api/traces/" + encodeURIComponent(id) + "/ownership-custody-conflicts"),
    resolveOwnershipCustodyConflict: (id, conflictId, resolution) =>
      request("/api/traces/" + encodeURIComponent(id) + "/ownership-custody-conflicts/" + encodeURIComponent(conflictId) + "/resolve",
        "POST", { resolution: resolution }),
    // Reverse-mode address discovery (address -> candidate outpoints).
    // Local only, no ownership evidence, no trace creation.
    reverseAddressOutputs: (address) =>
      request("/api/reverse/address/" + encodeURIComponent(address) + "/outputs"),
    // Persistent address-discovery scan lifecycle: scanblocks start returns
    // promptly and the frontend polls the job (QUEUED/RUNNING/COMPLETE/
    // CANCELLED/FAILED) with real Core progress. Duplicate starts return the
    // active job; a non-app Core scan is reported as foreign_scan.
    addressScanStart: (address) =>
      request("/api/reverse/address/scan", "POST", { address: address }),
    addressScanActive: () => request("/api/reverse/address/scan"),
    addressScanJob: (id) => request("/api/reverse/address/scan/" + encodeURIComponent(id)),
    addressScanAbort: (id) =>
      request("/api/reverse/address/scan/" + encodeURIComponent(id) + "/abort", "POST", {}),
    // Acquisitions: user-supplied provenance metadata
    // anchored to an exact on-chain outpoint; saving creates a forward trace.
    listAcquisitions: (positionState, signal, saved, wallet, review) => saved ?
      request('/api/reports/workspace?part=acquisitions' + (wallet ? '&wallet=' + encodeURIComponent(wallet) : '') + (positionState ? '&position=' + encodeURIComponent(positionState) : '') + (review ? '&review=' + encodeURIComponent(review) : ''), 'GET', undefined, signal).then(function (r) { r.records.snapshot_id = r.snapshot_id; return r.records; }) :
      request("/api/acquisitions" + (positionState ? "?position_state=" + encodeURIComponent(positionState) : "")),
    createAcquisition: (payload) => request("/api/acquisitions", "POST", payload),
    traceProvenance: (traceId) => request('/api/reports/trace-provenance' + (traceId ? '?trace_id=' + traceId : '')),
    outpointProvenance: (txid, vout) => request('/api/outpoints/' + encodeURIComponent(txid) + '/' + encodeURIComponent(vout) + '/acquisition-provenance'),
    originDetail: (txid, vout) => request('/api/provenance/origins/' + encodeURIComponent(txid) + '/' + encodeURIComponent(vout)),
    resolveOrigin: (txid, vout, comment) => request('/api/provenance/origins/' + encodeURIComponent(txid) + '/' + encodeURIComponent(vout) + '/resolution', 'PUT', { type: 'NO_ACQUISITION', comment: comment }),
    removeOriginResolution: (txid, vout) => request('/api/provenance/origins/' + encodeURIComponent(txid) + '/' + encodeURIComponent(vout) + '/resolution', 'DELETE'),
    exportAcquisitions: () => csvDownload('/api/acquisitions/export.csv'),
    exportRecordedExits: (from, to) => csvDownload('/api/exchange-exits/export.csv?' +
      (from ? 'from=' + encodeURIComponent(from) + '&' : '') + (to ? 'to=' + encodeURIComponent(to) : '')),
    exportExitProvenance: (from, to) => csvDownload('/api/exchange-exits/provenance-v1.csv?' +
      (from ? 'from=' + encodeURIComponent(from) + '&' : '') + (to ? 'to=' + encodeURIComponent(to) : '')),
    acquisitionOutputs: (txid) => request("/api/acquisitions/outputs/" + encodeURIComponent(txid)),
    getAcquisition: (id) => request("/api/acquisitions/" + encodeURIComponent(id)),
    updateAcquisition: (id, payload) => request("/api/acquisitions/" + encodeURIComponent(id), "PATCH", payload),
    deleteAcquisition: (id) => request("/api/acquisitions/" + encodeURIComponent(id), "DELETE"),
    // Bulk dataset reset. One canonical backend operation (NOT N single
    // deletes): removes every Acquisition anchor, its Disposal
    // and Exchange Exit allocation links and the Acquisition-owned forward
    // traces, so the same Acquisition CSV can be imported again from a clean
    // state. Independent Exchange Exits, Disposals, ownership-neutral
    // Blockchain Analysis, the blockchain cache and the Known Address
    // Registry are preserved. Returns counts only.
    deleteAllAcquisitions: () => request("/api/acquisitions", "DELETE"),
    retraceAcquisition: (id) => request("/api/acquisitions/" + encodeURIComponent(id) + "/retrace", "POST", {}),
    positionCounts: (saved, wallet) => request(saved ? '/api/reports/workspace?part=counts' + (wallet ? '&wallet=' + encodeURIComponent(wallet) : '') : "/api/acquisitions/position-counts"),
    classifyOutpoint: (txid, vout, payload) => request('/api/outpoints/' + encodeURIComponent(txid) + '/' + encodeURIComponent(vout) + '/classify', 'POST', payload),
    disposalCandidates: (id) => request("/api/acquisitions/" + encodeURIComponent(id) + "/disposal-candidates"),
    listDisposals: () => request("/api/disposals"),
    createDisposal: (payload) => request("/api/disposals", "POST", payload),
    getDisposal: (id) => request("/api/disposals/" + encodeURIComponent(id)),
    updateDisposal: (id, payload) => request("/api/disposals/" + encodeURIComponent(id), "PATCH", payload),
    deleteDisposal: (id) => request("/api/disposals/" + encodeURIComponent(id), "DELETE"),
    // Exchange Exit (v0.11.0): ownership boundary when BTC leaves self-custody.
    exchangeExitCandidates: (id) => request("/api/acquisitions/" + encodeURIComponent(id) + "/exchange-exit-candidates", "POST"),
    listExchangeExits: () => request("/api/exchange-exits"),
    createExchangeExit: (payload) => request("/api/exchange-exits", "POST", payload),
    getExchangeExit: (id) => request("/api/exchange-exits/" + encodeURIComponent(id)),
    updateExchangeExit: (id, payload) => request("/api/exchange-exits/" + encodeURIComponent(id), "PATCH", payload),
    deleteExchangeExit: (id) => request("/api/exchange-exits/" + encodeURIComponent(id), "DELETE"),
    // Two-phase CSV import (v0.8.0): preview = non-destructive classification
    // (READY/DUPLICATE/INVALID/REVIEW_REQUIRED, never writes); import = create
    // exactly the READY lines the user selected.
    importAcquisitionsPreview: (payload) => request("/api/acquisitions/import/preview", "POST", payload),
    importAcquisitions: (payload) => request("/api/acquisitions/import", "POST", payload),
  };
})();

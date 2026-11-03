# How WAMS works

This page explains, for interested users, how WhereAreMySats (WAMS) is built and what
its terms mean. For installation and operation see [INSTALL.md](INSTALL.md); for an
introduction see [README.md](README.md); for the security model see
[SECURITY.md](SECURITY.md).

## Architecture

WAMS runs as **one Docker container** on a Linux host. Inside it, a Python backend
(FastAPI) serves the REST API and the static web interface. There is no separate
database server and no external service.

- **Bitcoin Core node.** WAMS reads blockchain data only from your own node over JSON-RPC
  (`txindex` and `txospenderindex` are required, `blockfilterindex=basic` is recommended
  for wallet discovery). It never writes to the node, never signs and never broadcasts.
  Optionally, your own Electrs server can serve as a fallback for address lookups.
  See [NODE_SETUP.md](NODE_SETUP.md).
- **SQLite database.** Everything WAMS knows, from traces and classifications to
  acquisitions, exits and wallet observations, lives in one SQLite file in the
  `lineage-data` volume together with the saved node settings. The schema is upgraded
  by forward-only migrations when a new version starts; the database is never
  downgraded, so back it up before every update.
- **Browser UI.** A plain JavaScript single-page application without build step and
  without third-party scripts, served from the container. It talks only to the WAMS
  API on the same origin. Interactive API documentation is available at `/docs`.
- **No login.** Access is controlled by where the port is published and by the host
  firewall or a reverse proxy. By default the port is reachable only on the host itself.

RPC credentials are stored server-side (environment or a settings file with mode 0600)
and are never sent to the browser. Extended public keys for wallet discovery are parsed
in memory only and never stored or logged; the node is only asked about derived
addresses.

## Basic terms

- **Acquisition.** The exact on-chain output (`TXID:VOUT`) with which coins entered your
  self-custody, for example the withdrawal from a purchase, with optional date, source,
  reference and notes. Acquisitions are your records; WAMS does not reconstruct
  exchange-internal purchases or balances.
- **Exit (Exchange Exit).** A transaction in which coins left your self-custody towards an
  exchange. An exit is a custody boundary, not a sale: WAMS records no prices, gains or
  holding periods.
- **Provenance.** The on-chain path between acquisitions and current wallet outputs or
  exits. Every current output is shown as `ACQUISITION` (descends from documented
  acquisitions), `NO_ACQUISITION` (entered self-custody without an acquisition record),
  `MIXED` (both kinds of origin merged in one transaction) or `UNRESOLVED` (the required
  lineage is incomplete). `MIXED` is a structural fact, not a quality judgement: when
  several inputs meet several outputs, the blockchain does not say which input ended up
  where, and WAMS does not invent such a share.
- **No acquisition (verified).** A `NO_ACQUISITION` origin that you reviewed and marked as
  verified, optionally with a source and a note (for example a payout or a gift). It stays
  a neutral statement about your records; it does not claim or deny ownership.
- **Needs Review.** Open questions that only you can answer: a current output whose
  required lineage is incomplete (`UNRESOLVED`), an acquisition with a spend from your
  own custody that is neither explained as a transfer nor recorded as an exit, or a
  conflict between ownership evidence and a custody boundary. Open items are not errors;
  they mark where the picture is incomplete. "Needs Review" is distinct from "No
  acquisition (verified)".
- **Owned, external, custodial.** For every output on a path WAMS keeps the blockchain
  fact (the output exists and is spent or unspent) separate from the ownership evidence
  (imported address, wallet discovery, your explicit confirmation) and from the custody
  interpretation (for example a deposit to an exchange). Direct evidence always wins over
  heuristics; unknown is neither owned nor external.

## Data flow

1. **Wallet setup.** You import known addresses or run a one-time wallet discovery from an
   extended public key. The result is a registry of addresses with the evidence for each
   one. A manual refresh asks the node for the current unspent outputs of these addresses
   and stores them as the wallet inventory.
2. **Records.** You record acquisitions (single or CSV import) and, when coins went to an
   exchange, exits. Both refer to exact outputs and transactions.
3. **Tracing.** From an acquisition or any start output, WAMS asks the node for each
   transaction and its spending transaction and walks forward through splits, merges and
   transfers. Outputs that are yours are followed; external outputs end a branch. The
   same engine walks backwards from current outputs to find their origins.
4. **Classification.** The stored traces, inventory and records are combined into the
   provenance classes, the coverage figures on the dashboard and the review queues.
   Nothing here is computed from prices or external data.
5. **Reports and exports.** The Blockchain History Report shows recorded exits of selected
   years with their path back to the documented origins; CSV exports deliver the same
   facts as tables. Sensitive free text is excluded from exports by default.

Long operations (node scans, large imports, reports over many records) run in the
background inside the container; the UI polls for progress. WAMS is built for one
operator at a time.

## Limits

- **Only what the node and your records contain.** WAMS uses no block explorer and no
  other external data. A missing acquisition shows up as an open review item.
- **No sat identity through merges.** Paths through transactions with several inputs and
  outputs are shown as mixed; WAMS assigns no shares and proves no individual sat.
- **Not a proof of ownership.** Which addresses are yours is your statement. WAMS
  documents on-chain connections and what you recorded; it cannot verify ownership.
- **On-chain self-custody only.** What happens inside an exchange, a custodial service or
  a Lightning wallet is outside its view. Do not mark such outputs as owned; record them
  where they enter your wallet as an acquisition or a no-acquisition origin.
- **Mainnet wallet features.** Address import, wallet discovery and inventory work with
  mainnet keys and addresses; signet is useful for trying the flow
  ([SIGNET.md](SIGNET.md)).
- **Not a tax report.** No prices, no gains, no holding periods; an exit is not a sale.

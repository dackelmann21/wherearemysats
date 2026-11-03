# WhereAreMySats

WhereAreMySats (WAMS) documents where your own bitcoin came from. It follows your
coins on-chain from the point where they entered your self-custody (an
**acquisition**) through every transaction to the point where they left it towards
an exchange (an **exchange exit**), and turns that path into a printable **Blockchain
History Report**.

WAMS is self-hosted and watch-only. It reads blockchain data only from your own
Bitcoin Core node, keeps everything in a local SQLite database, and never holds
private keys.

**Status: beta.** The main workflow works, but expect rough edges and changes between
releases. Read the release notes before every update and keep backups.

## What WAMS does

- **Provenance from acquisition to exit.** Record the exact output (`TXID:VOUT`) you
  received from a purchase or another source, with date, source, reference and notes.
  WAMS traces the coins through splits, merges and transfers and shows which current
  wallet outputs and which exits descend from which acquisitions.
- **Blockchain History Report.** A printable page for selected years that shows recorded exits
  with their on-chain path back to the documented origins. It is meant as supporting
  material, for example when a bank or an exchange asks where coins came from.
- **Wallet inventory and coverage.** Import your addresses or scan an extended public
  key (xpub/ypub/zpub) once, see your current UTXOs and how many of them are covered
  by documented acquisitions, and which still need review.
- **Origins without a purchase.** Coins that entered self-custody without an
  acquisition record (for example a payout or a gift) can be documented as
  "No acquisition (verified)" with a note.
- **Analysis tools.** Forward and reverse tracing from any TXID, ownership and custody
  markings, review queues, CSV exports.

## What WAMS is not

- **Not a tax report and not tax or legal advice.** WAMS records no prices, no gains
  and no holding periods. An exit means "left self-custody to an exchange", not
  "sold". Ask a qualified advisor how to use its output.
- **Not a proof of ownership.** WAMS shows on-chain connections and what you recorded.
  Whether an address is yours is your own statement; WAMS cannot verify it.
- **No invented sat identity.** When a transaction has several inputs and several
  outputs, the blockchain does not say which input ended up in which output. WAMS
  shows such paths as mixed and does not assign shares.
- **Not a wallet.** No seeds, no private keys, no signing, no broadcasting.
- **Not for custodial or off-chain history.** What happens inside an exchange or a
  Lightning wallet is outside WAMS; its view starts and ends at on-chain self-custody.

## Limits to know

- You need **your own Bitcoin Core node** (31.1 or newer) with `txindex` and
  `txospenderindex`. WAMS uses no public block explorer.
- **No login.** Everyone who can reach the web interface can read and change all data.
  By default it is reachable only on the host itself. See [SECURITY.md](SECURITY.md).
- Wallet features (address import, wallet discovery, inventory) work with **mainnet**
  only. Signet is useful for trying the acquisition-to-report flow
  ([SIGNET.md](SIGNET.md)).
- The result is only as complete as your records. Missing acquisitions show up as
  open review items, not as errors.
- One operator at a time; long node scans and large imports take time.

## Requirements

- Your own Bitcoin Core node, fully synced, see [NODE_SETUP.md](NODE_SETUP.md).
- A Linux host (e.g. an Ubuntu Server VM) with Docker Engine 20.10+ and the Docker
  Compose plugin 2.24+, see [INSTALL.md](INSTALL.md).

## Quick start

On a Linux host with Docker, after preparing the node, with the archive and its
checksum file from the [Releases page](https://github.com/dackelmann21/wherearemysats/releases):

```bash
sha256sum -c wams-public-<version>.tar.gz.sha256
mkdir ~/wherearemysats && tar -xzf wams-public-<version>.tar.gz -C ~/wherearemysats --strip-components=1
cd ~/wherearemysats && sudo docker compose -p wherearemysats up -d --build
ssh -N -L 8080:127.0.0.1:8080 <user>@<host>    # on your computer; then open http://127.0.0.1:8080
sudo docker compose -p wherearemysats restart bitcoin-lineage    # after Node Settings → Test Connection → Save
```

Then follow the **Getting started** checklist on the Dashboard.

## Documentation

| Document | Content |
|---|---|
| [INSTALL.md](INSTALL.md) | Installation, first steps, updates, backup and restore, troubleshooting |
| [NODE_SETUP.md](NODE_SETUP.md) | Bitcoin Core requirements and RPC access, Umbrel, Start9 |
| [SECURITY.md](SECURITY.md) | Security model, access variants, reporting a vulnerability |
| [SIGNET.md](SIGNET.md) | Trying WAMS on signet |
| [TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md) | How WAMS works: architecture, terms, data flow, limits |
| [docs/releases/](docs/releases/) | Release notes (German) |

## Feedback and security reports

Bugs and suggestions: [GitHub Issues](https://github.com/dackelmann21/wherearemysats/issues). Security problems: privately through GitHub's
private vulnerability reporting, see [SECURITY.md](SECURITY.md#reporting-a-vulnerability).
Never post real addresses, TXIDs, extended keys or database files.

Development happens in a private repository; issues welcome.

## License

WAMS is licensed under the GNU Affero General Public License v3.0 only
(SPDX `AGPL-3.0-only`).

Copyright (C) 2026 Dackelmann

In plain words: you may run WAMS yourself and change it freely; if you offer a
modified version to others over a network, you must make its source code available
to them. The full terms are in [LICENSE](LICENSE); this summary is not legal advice.

## Disclaimer

WAMS is provided "as is", without warranty of any kind. It is a documentation and
analysis tool, not financial, tax or legal advice. Its results depend on the data of
your node and on what you record; check them before you rely on them or pass them on.
You are responsible for the security of your installation, your node and your backups.
The authors are not liable for losses or damages arising from its use.

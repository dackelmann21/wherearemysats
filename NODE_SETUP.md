# Bitcoin node setup

WAMS reads all blockchain data from **your own Bitcoin Core node** over JSON-RPC. It
uses no external blockchain service. This page describes what the node must provide
and how to allow the connection from the WAMS host. The node operator (you) makes
these changes; WAMS never changes the node configuration.

Placeholders: `<node-ip>` (node's LAN address), `<vm-ip>` (WAMS host's LAN address),
`<rpc-user>` (RPC username for WAMS).

## 1. Requirements

| Requirement | Why | Checked by WAMS |
|---|---|---|
| Bitcoin Core **31.1 or newer** | Version the app is developed and tested against; `txospenderindex` is a recent Core feature | shown in Node Status, not enforced |
| `server=1` (RPC enabled) | WAMS talks JSON-RPC | Test Connection |
| `txindex=1`, **synced** | look up any transaction by TXID | Getting started checklist and Node Status |
| `txospenderindex=1`, **synced** | find the transaction that spent an output (`gettxspendingprevout`); without it traces cannot follow spent outputs | Getting started checklist and Node Status |
| initial block download finished | WAMS refuses data operations while Core is in IBD | Test Connection, checklist ("Blockchain synced") |
| `blockfilterindex=basic` (optional) | historical wallet discovery and address history via `scanblocks`; without it wallet discovery finds only current UTXOs (`scantxoutset`) | shown in Node Status |

`txindex` needs a node that is **not pruned**. Enabling an index on an existing node
starts a background build that takes hours to days; the checklist and Node Status show
the progress. Work with WAMS once both required indexes are synced.

Use the same Bitcoin network for the node and for the WAMS database. For another
network (e.g. signet), run a separate WAMS installation with its own data volume.

## 2. RPC methods WAMS uses

All calls are read-only. WAMS calls **no wallet RPC** and never signs or broadcasts
transactions:

`getnetworkinfo`, `getblockchaininfo`, `getindexinfo`, `getrawtransaction`,
`gettxout`, `gettxspendingprevout`, `getblockcount`, `getblockhash`,
`getblockheader`, `getblock`, `scantxoutset`, `scanblocks`
(`start`/`status`/`abort`; WAMS aborts only its own scans).

`scantxoutset` and `scanblocks` are CPU- and disk-intensive on the node during wallet
discovery and inventory refreshes.

During wallet discovery and inventory refresh, WAMS sends `addr(<address>)`
descriptors of your addresses to the node. This is one reason to connect WAMS only to
a node you control.

## 3. Configure Bitcoin Core

Example `bitcoin.conf` for a node on another machine in the LAN (mainnet):

```ini
server=1
txindex=1
txospenderindex=1
blockfilterindex=basic

# Listen for RPC on loopback and on the LAN interface.
rpcbind=127.0.0.1
rpcbind=<node-ip>
# Allow RPC only from loopback and from the WAMS host.
rpcallowip=127.0.0.1
rpcallowip=<vm-ip>/32

# Dedicated RPC user for WAMS (generated line, see below).
rpcauth=<rpc-user>:<salt>$<hash>
```

- **Never** allow `0.0.0.0/0` or whole networks, and never expose RPC to the internet.
- Keep existing `rpcallowip` lines for other clients. When WAMS moves to a new host,
  or you run a second WAMS instance, **add** its address as another
  `rpcallowip=<new-ip>/32` line.
- Without `rpcbind=<node-ip>`, Core listens on loopback only and the WAMS host cannot
  connect.
- For an IPv6 connection use a `/128` entry for the WAMS address.

Create a dedicated RPC user with the `rpcauth.py` script that ships with Bitcoin Core
(`share/rpcauth/rpcauth.py` in the source and release archives):

```bash
python3 share/rpcauth/rpcauth.py <rpc-user>
```

It prints an `rpcauth=` line for `bitcoin.conf` and a password. WAMS needs the
username and that **plain password**, not the `rpcauth` line. Do not reuse the
password elsewhere. An existing RPC user also works, but a dedicated user can be
removed without affecting other clients.

**Restart Bitcoin Core** after changing `bitcoin.conf`, for example:

```bash
sudo systemctl restart bitcoind     # or: bitcoin-cli stop, then start bitcoind as usual
```

**Firewall on the node** (example with ufw, mainnet port 8332):

```bash
sudo ufw allow from <vm-ip> to any port 8332 proto tcp
sudo ufw status numbered
```

Default RPC ports: mainnet 8332, testnet3 18332, signet 38332, regtest 18443. In WAMS
the port is a separate field; choosing a network does not change it. Testnet4 is not
offered.

WAMS runs in Docker; a node on another machine sees the WAMS **host** address
(`<vm-ip>`) as the source, not a container address.

### Optional: restrict the WAMS user to these methods

Core can limit an RPC user to a list of methods. This is optional hardening:

```ini
rpcwhitelist=<rpc-user>:getnetworkinfo,getblockchaininfo,getindexinfo,getrawtransaction,gettxout,gettxspendingprevout,getblockcount,getblockhash,getblockheader,getblock,scantxoutset,scanblocks
rpcwhitelistdefault=0
```

Without `rpcwhitelistdefault=0`, setting any `rpcwhitelist` line locks every **other**
RPC user (including `bitcoin-cli` and other apps) out of all methods. A future WAMS
version may need further methods; the release notes say so.

## 4. Test from the WAMS host

Run these on the WAMS host **before** you enter the connection in WAMS. Port reachable?

```bash
timeout 5 bash -c '</dev/tcp/<node-ip>/8332' && echo "port open" || echo "port NOT reachable"
```

RPC call (curl asks for the password, so it does not end up in the shell history):

```bash
curl -sS --user <rpc-user> -H 'content-type: text/plain;' \
  --data-binary '{"jsonrpc":"1.0","id":"t","method":"getblockchaininfo","params":[]}' \
  http://<node-ip>:8332/ | python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; print("chain", r["chain"], "blocks", r["blocks"], "IBD", r["initialblockdownload"])'
```

Indexes:

```bash
curl -sS --user <rpc-user> -H 'content-type: text/plain;' \
  --data-binary '{"jsonrpc":"1.0","id":"t","method":"getindexinfo","params":[]}' \
  http://<node-ip>:8332/ | python3 -c 'import json,sys; [print(k, "synced" if v["synced"] else "NOT synced", v["best_block_height"]) for k,v in json.load(sys.stdin)["result"].items()]'
```

Expected: `chain main`, `IBD False`; `txindex` and `txospenderindex` (and `basic block
filter index`, if enabled) `synced`.

| Result | Meaning |
|---|---|
| no connection / timeout | `rpcbind` missing, Core not running, or a firewall blocks the port |
| HTTP 403 | the WAMS host's address is not in `rpcallowip`, or Core was not restarted |
| HTTP 401 | wrong username or password |
| an index is missing | add the line to `bitcoin.conf` and restart Core |

Test Connection in **Node Settings** reports the same cases. Continue with
[INSTALL.md, section 7](INSTALL.md#7-connect-the-node).

## 5. Core on the same host

Inside the WAMS container, `localhost` and `127.0.0.1` mean **the container itself**,
not the host. For a Core process running directly on the WAMS host, use
**`host.docker.internal`** as RPC host. Compose maps this name to the host gateway
(Docker Engine 20.10 or newer).

Find the gateway address and the container network's subnet:

```bash
cd ~/wherearemysats
sudo docker compose -p wherearemysats exec -T bitcoin-lineage python -c 'import socket; print(socket.gethostbyname("host.docker.internal"))'
CID=$(sudo docker compose -p wherearemysats ps -q bitcoin-lineage)
sudo docker inspect --format '{{range .NetworkSettings.Networks}}{{.NetworkID}}{{println}}{{end}}' "$CID"
sudo docker network inspect <network-id> --format '{{range .IPAM.Config}}{{.Subnet}}{{println}}{{end}}'
```

Core must also listen on that gateway address and allow the subnet:

```ini
rpcbind=127.0.0.1
rpcbind=<host-gateway-ip>
rpcallowip=127.0.0.1
rpcallowip=<wams-compose-subnet>
```

Allow the RPC port from that subnet in the host firewall. Do not run untrusted
containers in the same Compose network. The subnet stays the same across normal
restarts and reboots; if the Compose network is deleted and recreated it can change,
then update `rpcallowip`.

If Core runs in **another container**, publish its RPC port on a host address and use
that address (or `host.docker.internal`) as RPC host.

## 6. Umbrel and Start9

The WAMS project has not tested these platforms in the current release. What WAMS
needs is the same as above: an RPC host and port reachable from the WAMS host, a
username and password, the right network, and `txindex` plus `txospenderindex`.

- **Umbrel:** the Bitcoin Node app shows its RPC connection details (host, port,
  username, password) in its connection dialog, and offers index options in its
  settings. Check whether your version offers `txospenderindex`; WAMS cannot work
  without it.
- **Start9 (StartOS):** the Bitcoin Core service shows its RPC credentials and
  interface addresses, and has index options in its configuration. Check the same
  points as above.

General points for node platforms:

- Enter the node's **IP address** as RPC host. Names like `<name>.local` (mDNS) are
  often not resolvable from inside a Docker container.
- Run the test from section 4 on the WAMS host. A 403 or no connection means the
  platform does not allow RPC from your WAMS host; how to allow it depends on the
  platform and its version; see its documentation. Do not open RPC to the whole
  network.
- **HTTPS to RPC** in Node Settings requires a certificate that the WAMS container
  trusts by default; WAMS has no option to add your own certificate authority, and
  certificate verification cannot be switched off. If a platform offers RPC only over
  HTTPS with its own certificate authority, WAMS cannot connect to that endpoint.
- WAMS does not need the platform's internal Docker network or container names.

## 7. Electrs (optional)

Address-history lookups prefer Core's `scanblocks` with `blockfilterindex=basic`. If
your node has no block filter index and you run **your own** Electrs server, WAMS can
use it as a fallback for reverse address discovery. It is configured only through the
environment, not in the web interface:

```ini
# .env on the WAMS host; tcp:// is plain text, ssl:// is TLS
ELECTRS_URL=tcp://<electrs-ip>:50001
```

Run `sudo docker compose -p wherearemysats up -d` afterwards. Use only an Electrs
server you control in your own network; never a public server, because WAMS sends
your addresses to it.

## 8. Where the connection settings come from

WAMS decides in this order:

1. Saved Node Settings (`/app/data/node-settings.json` in the data volume, written by
   **Save**). They replace all environment values together.
2. Environment from `.env`: `BTC_RPC_HOST`, `BTC_RPC_PORT`, `BTC_RPC_USER`,
   `BTC_RPC_PASSWORD`, `BTC_NETWORK`, `BTC_RPC_TLS`. The legacy `BTC_RPC_URL` overrides
   the separate fields if set.
3. Safe defaults without credentials. Without username and password WAMS makes no RPC
   call; Node Settings stays available.

**Save** takes effect after a restart of WAMS
(`sudo docker compose -p wherearemysats restart bitcoin-lineage`). A damaged or
too-permissive settings file is not silently replaced by the environment: WAMS stays
unconfigured and shows the error, and a complete new entry in Node Settings repairs
it.

To go back from saved settings to `.env` only, stop WAMS, rename the file inside the
volume and start again (the renamed file still contains the password; protect it like
a backup):

```bash
cd ~/wherearemysats
sudo docker compose -p wherearemysats stop -t 660 bitcoin-lineage
sudo docker compose -p wherearemysats run --rm --no-deps bitcoin-lineage python -c 'from pathlib import Path; p=Path("/app/data/node-settings.json"); q=Path("/app/data/node-settings.recovery.json"); assert p.is_file() and not q.exists(); p.rename(q)'
sudo docker compose -p wherearemysats up -d
```

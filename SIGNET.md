# Try WAMS on signet

Signet is a Bitcoin test network whose coins have no value. This walk-through lets you
try the core workflow of WAMS without touching your real data: record an acquisition,
follow the coins to a second wallet, record that transfer as an exchange exit, and
create the Blockchain History Report.

Run it as a **separate WAMS installation** next to (or instead of) your mainnet one.
Never point a WAMS database that holds mainnet records at a signet node.

## What works on signet

| Works | Does not work on signet |
|---|---|
| Node connection, Node Status, Getting started checklist | **Known Addresses import** and **wallet discovery** (they accept mainnet addresses and keys only) |
| Start Trace, trace view, marking outputs | **Wallet Inventory refresh** (refuses non-mainnet chains) |
| Acquisitions (by TXID and vout) | Current Wallet and Dashboard holdings (they depend on the inventory) |
| Record Exit, Blockchain History Report, CSV exports | |

So signet is good for learning the acquisition → trace → exit → report flow, not for
the wallet views. The "Wallet added" step of the checklist can still be completed on
signet by marking an output as Owned in a trace (step 6).

## 1. Bitcoin Core on signet

Run a separate Core instance with its own data directory, here `~/.bitcoin-signet`,
on the WAMS host or another machine. Create `~/.bitcoin-signet/bitcoin.conf`:

```ini
signet=1

[signet]
server=1
txindex=1
txospenderindex=1
blockfilterindex=basic
# Only for this test node: lets the test wallet send without fee estimates.
fallbackfee=0.0001

rpcbind=127.0.0.1
rpcallowip=127.0.0.1
# Node on another machine: also listen on its LAN address and allow the WAMS host.
# rpcbind=<node-ip>
# rpcallowip=<vm-ip>/32
rpcauth=<rpc-user>:<salt>$<hash>
```

Create the `rpcauth` line as described in [NODE_SETUP.md](NODE_SETUP.md#3-configure-bitcoin-core).
Network options such as `rpcbind` must be inside the `[signet]` section. If the node
runs on the WAMS host itself, follow
[NODE_SETUP.md, Core on the same host](NODE_SETUP.md#5-core-on-the-same-host) instead
of `rpcbind=<node-ip>`.

Start it and wait until the sync and the indexes are complete:

```bash
bitcoind -datadir=$HOME/.bitcoin-signet -daemon
scli() { bitcoin-cli -datadir="$HOME/.bitcoin-signet" "$@"; }
scli getblockchaininfo | grep -E '"chain"|"blocks"|"initialblockdownload"'
scli getindexinfo
```

Signet is much smaller than mainnet, but the first sync and the index build still
take a while. Continue when `initialblockdownload` is `false` and all indexes are
`synced`. The signet RPC port is **38332**.

## 2. A second WAMS installation for signet

Use its own directory, project name and port, so it never shares data with a mainnet
installation:

```bash
install -d -m 755 ~/wams-signet
tar -xzf wams-public-<version>.tar.gz -C ~/wams-signet --strip-components=1
cd ~/wams-signet
( umask 077 && printf 'WAMS_HTTP_PORT=8081\n' > .env )
chmod 600 .env
sudo docker compose -p wams-signet up -d --build
curl -fsS --retry 12 --retry-delay 5 --retry-connrefused http://127.0.0.1:8081/health; echo
```

Use the **same WAMS version** for both installations: both use the image name
`wherearemysats:local`, so building one replaces the image of the other.

Open it through an SSH tunnel (`ssh -N -L 8081:127.0.0.1:8081 <user>@<vm-ip>`, then
<http://127.0.0.1:8081>). In **Node Settings** enter the signet node: RPC host, port
**38332**, username, password, network **Signet**. Test Connection, Save, then
restart:

```bash
sudo docker compose -p wams-signet restart bitcoin-lineage
```

The Getting started checklist on the Dashboard should now show the node, blockchain
and both indexes as done.

## 3. A test wallet with faucet coins

Create two wallets in the signet node: one stands for your own wallet, the other for
an exchange.

```bash
scli createwallet wams-try
scli createwallet wams-try-exchange
scli -rpcwallet=wams-try getnewaddress
```

Request coins for that address from a **signet faucet**. Faucets are run by
volunteers and change over time; search for "bitcoin signet faucet". Wait until the
payment has one confirmation (signet blocks come about every 10 minutes), then find
its TXID and output index:

```bash
scli -rpcwallet=wams-try listtransactions
```

Note `txid` and `vout` of the `receive` entry.

## 4. Record the acquisition

In WAMS open **Records & Origins → + Add Acquisition**. Enter the TXID and the vout,
and as Source for example `Signet faucet`. Save. The acquisition appears with its ID
(`#1`) and WAMS starts a trace from this output.

## 5. Send to the "exchange"

Send part of the coins to an address of the second wallet:

```bash
EXCHANGE=$(scli -rpcwallet=wams-try-exchange getnewaddress)
scli -rpcwallet=wams-try sendtoaddress "$EXCHANGE" 0.0001
```

The command prints the TXID of the transfer. Wait for one confirmation. The
transaction has two outputs: the deposit to the "exchange" and the change back to
your test wallet.

## 6. Follow the coins

Open **Traces** and the trace of the acquisition (or use **Start Trace** with the
acquisition's TXID). The transfer now appears as the spend of your acquisition output.
Click the **change** output (the one that is not the exchange address) and mark it
**Owned**. This also adds the change address to Known Addresses, which completes the
"Wallet added" step of the checklist. Leave the exchange deposit unmarked or mark it
External.

Only mark outputs Owned that belong to your own on-chain wallet. On mainnet,
Lightning wallets, exchanges and other services are not your wallet.

## 7. Record the exit

In **Records & Origins**, click **Record Exit** at the acquisition. WAMS offers the
transfer transaction as a candidate. Mark the output to the exchange address as the
**exchange deposit**, leave the change output unmarked, confirm self-custody of the
source output if asked, choose the exit date and save. The acquisition now shows the
recorded exit; the checklist step "Exchange exit recorded" is done.

An exit means "left self-custody to an exchange", not "sold". WAMS does not record
prices or gains.

## 8. Create the report

On the **Dashboard**, click **Export CSV**. In the dialog, select the year of the exit
and click **Blockchain History Report**. A printable page opens with the path from the
acquisition to the exit; save it as PDF with Ctrl+P. You can also prepare and download
the Exit Provenance CSV in the same dialog.

## Clean up

```bash
cd ~/wams-signet
sudo docker compose -p wams-signet down          # keeps the data volume
scli stop
```

`down` keeps the volume `wams-signet_lineage-data`. To remove the signet test data
completely, delete that volume **by its exact name** after checking it:

```bash
sudo docker volume ls
sudo docker volume rm wams-signet_lineage-data
```

Never remove the mainnet volume (`wherearemysats_lineage-data`).

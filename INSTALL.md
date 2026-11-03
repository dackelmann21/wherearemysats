# Installation and operation

This guide installs WhereAreMySats (WAMS) with Docker on a Linux host, connects it to
your own Bitcoin Core node, and covers updates, backups and troubleshooting. It was
written for **Ubuntu Server LTS** (tested on 24.04) in a virtual machine; other Linux
distributions work if you install Docker with their own instructions.

Related guides:

- [NODE_SETUP.md](NODE_SETUP.md): what WAMS needs from Bitcoin Core and how to allow
  the RPC connection.
- [SECURITY.md](SECURITY.md): who can reach WAMS, access variants (SSH tunnel, LAN,
  reverse proxy), secrets and backups. **Read it before you open WAMS to your LAN.**
- [SIGNET.md](SIGNET.md): try WAMS on signet before using it with mainnet data.

## Placeholders

Every value in angle brackets is a placeholder. Replace it with your own value and do
not paste real values into public issues.

| Placeholder | Meaning |
|---|---|
| `<user>` | Linux user on the WAMS host (with `sudo`) |
| `<vm-ip>` | LAN address of the WAMS host |
| `<node-ip>` | LAN address of the Bitcoin Core host |
| `<rpc-user>` | RPC username of your node |
| `<lan-cidr>` | your home network in CIDR form, e.g. `192.0.2.0/24` |
| `<version>` | WAMS release version, e.g. `0.19.7` |

All commands run on the WAMS host unless a step says otherwise. Docker commands use
`sudo`; this guide does not add your user to the `docker` group, because membership
in that group is equivalent to root access.

## 1. Requirements

**WAMS host** (recommendation, not a measured minimum):

| Resource | Recommendation |
|---|---|
| OS | Ubuntu Server LTS (24.04 tested), 64-bit |
| CPU | 2 vCPU |
| RAM | 4 GB (2 GB works for small datasets; large report/export runs peaked near 1 GB in synthetic benchmarks) |
| Disk | 20 GB, local and persistent (do not put the SQLite database on a network file system) |
| Network | a fixed LAN address (static or DHCP reservation); **internet access while building** the image (Docker packages, base image `python:3.11-slim`, Python packages) |

**Software:** Docker Engine 20.10 or newer and the Docker Compose **plugin 2.24 or
newer** (`docker compose`, not the old `docker-compose`).

**Bitcoin node:** Bitcoin Core 31.1 or newer with `txindex=1` and
`txospenderindex=1`, fully synced, reachable over RPC from the WAMS host. See
[NODE_SETUP.md](NODE_SETUP.md). WAMS does not run a node itself.

WAMS runs on the host itself; the node can run on the same host or on another machine
in your LAN.

## 2. Prepare the host

### 2.1 User and SSH

If the installer did not create one, add a user with `sudo` (at the console):

```bash
sudo adduser <user>
sudo usermod -aG sudo <user>
```

Install the SSH server (often already present on Ubuntu Server) and note the address:

```bash
sudo apt-get update
sudo apt-get install -y openssh-server
sudo systemctl enable --now ssh
ip -4 addr show | grep inet        # note <vm-ip>
```

From **your own computer**, set up key login and test it:

```bash
ls ~/.ssh/id_ed25519.pub 2>/dev/null || ssh-keygen -t ed25519
ssh-copy-id <user>@<vm-ip>
ssh <user>@<vm-ip> 'whoami; sudo -v && echo "sudo ok"'
```

Optional, after key login works: disable password login on the host. Keep the current
session open and test a second login before you close it.

```bash
echo 'PasswordAuthentication no' | sudo tee /etc/ssh/sshd_config.d/10-nopassword.conf
sudo systemctl reload ssh
```

### 2.2 Host firewall

Allow SSH only from your LAN:

```bash
sudo apt-get install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from <lan-cidr> to any port 22 proto tcp
sudo ufw enable
sudo ufw status verbose
```

**ufw does not protect ports published by Docker.** Docker inserts its own packet
filter rules that are evaluated before ufw. By default WAMS is published on
`127.0.0.1` only, so this does not matter yet. If you open WAMS to the LAN, use the
`DOCKER-USER` rule in [SECURITY.md](SECURITY.md#variant-b-lan-access-opt-in).
Never forward the WAMS port on your router.

## 3. Install Docker

These commands use Docker's official Ubuntu repository. For other distributions see
<https://docs.docker.com/engine/install/>.

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Check the result:

```bash
systemctl is-enabled docker        # enabled
systemctl is-active docker         # active
v=$(sudo docker compose version --short); echo "Compose $v"
printf '%s\n2.24.0\n' "${v#v}" | sort -V -C -r && echo "OK (>= 2.24)" || echo "TOO OLD"
```

Docker must start at boot (`enabled`); WAMS needs no systemd unit of its own.

## 4. Get WAMS

This guide uses the directory `~/wherearemysats` and the Compose project name
**`wherearemysats`**. Always pass `-p wherearemysats` to `docker compose`: the data
volume is named after the project (`wherearemysats_lineage-data`), so the same name
finds the same data even if you later move or rename the directory.

### Option A: release archive (recommended)

Download the archive `wams-public-<version>.tar.gz` and its checksum file
`wams-public-<version>.tar.gz.sha256` from the [Releases page](https://github.com/dackelmann21/wherearemysats/releases), on the
host itself (`curl -LO <url>`) or on your computer and then copied over:

```bash
# on your computer, only if you downloaded there
scp wams-public-<version>.tar.gz wams-public-<version>.tar.gz.sha256 <user>@<vm-ip>:~
```

On the host, check and unpack:

```bash
cd ~
sha256sum -c wams-public-<version>.tar.gz.sha256      # must print: OK
test ! -e ~/wherearemysats || echo "STOP: ~/wherearemysats already exists (see section 9 for updates)"
install -d -m 755 ~/wherearemysats
tar -xzf wams-public-<version>.tar.gz -C ~/wherearemysats --strip-components=1
cd ~/wherearemysats
grep APP_VERSION backend/version.py                    # shows <version>
```

If `sha256sum -c` does not print `OK`, do not use the archive.

### Option B: Git

```bash
sudo apt-get install -y git
git clone https://github.com/dackelmann21/wherearemysats.git ~/wherearemysats
cd ~/wherearemysats
git tag --list                    # release tags
git checkout v<version>           # use a release tag, not an arbitrary branch state
```

## 5. Configure `.env` (optional)

WAMS starts without a `.env` file. You need one only to change the published address
or port, to allow a host name, or to configure the node through environment
variables instead of the Node Settings page.

```bash
cd ~/wherearemysats
( umask 077 && cp -n .env.example .env )
chmod 600 .env
nano .env        # enter real values only in the editor, never on the command line
```

> **LAN access needs `.env`.** Without a `.env` containing `WAMS_BIND_ADDRESS=0.0.0.0`
> (or the host's LAN address), WAMS listens only on `127.0.0.1` of the host (default
> since 0.19.3). A browser on another computer in the LAN then reports "connection
> refused"; only the SSH tunnel (section 6) works. If you want to open WAMS directly
> as `http://<vm-ip>:8080`, set `WAMS_BIND_ADDRESS` here and read
> [SECURITY.md](SECURITY.md#variant-b-lan-access-opt-in) first.

| Variable | Default | Purpose |
|---|---|---|
| `WAMS_BIND_ADDRESS` | `127.0.0.1` | Host address the web port is published on. `127.0.0.1` = only this host (SSH tunnel or local reverse proxy). `0.0.0.0` = all interfaces (LAN access). **WAMS has no login: everyone who reaches the port can read and change all data, including the node connection.** See [SECURITY.md](SECURITY.md). |
| `WAMS_HTTP_PORT` | `8080` | Host port. Change it if 8080 is already in use. |
| `WAMS_ALLOWED_HOSTS` | empty | Host names (besides IP addresses and `localhost`) under which you open WAMS, comma-separated, e.g. a reverse-proxy name. Other names get HTTP 400. |
| `BTC_RPC_HOST`, `BTC_RPC_PORT`, `BTC_RPC_USER`, `BTC_RPC_PASSWORD`, `BTC_NETWORK`, `BTC_RPC_TLS` | empty, 8332, empty, empty, `mainnet`, `0` | Node connection from the environment. Leave them empty and use **Node Settings** in the app (section 7) unless you run headless. |

Recommendation: keep the RPC fields empty and enter the connection in Node Settings.
Values in `.env` are visible to everyone who can run `docker inspect` on the host; the
Node Settings file is stored with mode 0600 inside the data volume.

Notes:

- Saved Node Settings (`/app/data/node-settings.json`) override **all** `BTC_RPC_*`
  values together. Environment values are used only when nothing is saved.
- For a password containing `$`, put the value in single quotes in `.env`.
- After changing `.env`, run `up -d` again (section 6). A plain `restart` does not
  reload the environment.
- If you set `WAMS_BIND_ADDRESS` to **one specific address** (e.g. `<vm-ip>`), WAMS no
  longer answers on `127.0.0.1`. Replace `http://127.0.0.1:8080` with
  `http://<vm-ip>:8080` in every check command of this guide. With `0.0.0.0` the
  `127.0.0.1` commands keep working.

## 6. Build and start

```bash
cd ~/wherearemysats
sudo docker compose -p wherearemysats config --quiet
sudo docker compose -p wherearemysats up -d --build
sudo docker compose -p wherearemysats ps
curl -fsS --retry 12 --retry-delay 5 --retry-connrefused http://127.0.0.1:8080/health; echo
```

Expected health answer (the version is the one you installed):

```json
{"status":"ok","app":"wherearemysats","version":"<version>","db_connected":true}
```

The first build takes several minutes. `/health` does not depend on the node, so it is
`ok` before a node is configured. Check the database line in the log:

```bash
sudo docker compose -p wherearemysats logs bitcoin-lineage | grep -E 'database ready|schema version'
```

Expected: `database ready at /app/data/lineage.db (schema version <n>)`, where `<n>` is
the schema of your release (25 for 0.19.x), and no `schema version mismatch`.

Optional permission and integrity check (prints metadata only, never secrets):

```bash
sudo docker compose -p wherearemysats exec -T bitcoin-lineage python - <<'PY'
import sqlite3, stat
from pathlib import Path
for p in (Path('/app/data'), Path('/app/data/lineage.db'), Path('/app/data/node-settings.json')):
    if p.exists():
        s = p.stat()
        print(p.name, 'uid/gid', s.st_uid, s.st_gid, 'mode', oct(stat.S_IMODE(s.st_mode)))
c = sqlite3.connect('file:/app/data/lineage.db?mode=ro', uri=True)
print('schema', c.execute('PRAGMA user_version').fetchone()[0])
print('integrity', c.execute('PRAGMA integrity_check').fetchone()[0])
c.close()
PY
```

Expected: `data` mode `0o700`, files `0o600`, uid/gid `10001 10001`, `integrity ok`.

The container uses the restart policy `unless-stopped`: it comes back after a reboot
of the host **unless you stopped it by hand** (`docker compose stop`). After a manual
stop, start it again with `up -d`.

### Open the web interface

With the default `WAMS_BIND_ADDRESS=127.0.0.1`, WAMS is reachable only on the host.
From your computer, open an SSH tunnel and keep it running:

```bash
ssh -N -L 8080:127.0.0.1:8080 <user>@<vm-ip>
```

Then open <http://127.0.0.1:8080> in your browser. With LAN access enabled (see
[SECURITY.md](SECURITY.md)), open `http://<vm-ip>:8080` instead.

## 7. Connect the node

Prepare the node first ([NODE_SETUP.md](NODE_SETUP.md)) and run the RPC test from the
WAMS host described there. Then, in WAMS:

1. Open **Node Settings**.
2. Enter RPC Host (address or DNS name only, no `http://`, no path), RPC Port (mainnet
   8332), RPC Username, RPC Password and the Bitcoin Network. Leave **HTTPS to RPC**
   off unless you use a TLS proxy in front of Core.
3. Click **Test Connection**. Expected: network, block height, "Initial block
   download: No".
4. Click **Save**.
5. Restart WAMS so that all workers use the saved connection:

   ```bash
   cd ~/wherearemysats
   sudo docker compose -p wherearemysats restart bitcoin-lineage
   curl -fsS --retry 12 --retry-delay 5 --retry-connrefused http://127.0.0.1:8080/health; echo
   ```

6. Open **Node Status**: connected, expected network, blockchain synced, `txindex` and
   `txospenderindex` synced.

A node running on the **same host** is not `localhost` from inside the container; use
`host.docker.internal` as RPC host (see [NODE_SETUP.md](NODE_SETUP.md#5-core-on-the-same-host)).

Test Connection never changes the active connection. A blank password field keeps the
saved password only while host, port, username and TLS stay the same.

## 8. First steps

The **Dashboard** shows a **Getting started** checklist. It is complete when these
steps are done:

| Step | Where |
|---|---|
| Node connected | Node Settings (section 7) |
| Blockchain synced | Bitcoin Core; WAMS waits for the end of the initial block download |
| txindex enabled and synced | `bitcoin.conf`, see [NODE_SETUP.md](NODE_SETUP.md) |
| txospenderindex enabled and synced | `bitcoin.conf`, see [NODE_SETUP.md](NODE_SETUP.md) |
| Wallet added | **Wallet Setup → Known Addresses** |
| Acquisition recorded | **Records & Origins → + Add Acquisition** |
| Exchange exit recorded (optional) | **Records & Origins → Record Exit** at the acquisition |

**Add your wallet.** Under **Wallet Setup → Known Addresses**:

- **Wallet discovery (one-time):** wallet name, an extended **public** key (xpub,
  ypub or zpub; there is no descriptor field), script type, start height and gap
  limit → **Scan wallet** → review the result → import. The key is used in memory for
  this scan only and is not stored. Full history needs `blockfilterindex=basic` on the
  node; without it only current UTXOs are found.
- **Import known addresses:** paste addresses or load a `.csv` file.

Only mark addresses of your own on-chain wallets. Lightning wallets, exchanges and
other services are not your on-chain wallet; record their payouts as acquisitions.
Known Addresses and wallet discovery accept **mainnet** addresses and keys only.

Then open **Wallet Setup → Wallet Inventory** and click **Update known wallet
scripts**. The current holdings then appear in Wallet Inventory, Current Wallet and on
the Dashboard.

**Record acquisitions.** In **Records & Origins**, use **+ Add Acquisition** with the
TXID and output index (vout) of the output you received, or **Import Acquisitions from
CSV** (semicolon-separated columns `acquisition_date;txid;vout;source;reference;notes`,
header row first; the import dialog shows the format). The field **Source for all
rows** fills empty `source` cells; a non-empty cell wins. Each acquisition shows its
ID as `#n`.

**Record exits and create the report.** When coins went to an exchange, use **Record
Exit** at the acquisition. On the Dashboard, **Export CSV** opens the export dialog;
choose the years and click **Blockchain History Report** for a printable page (save it
as PDF with Ctrl+P). It documents on-chain history; it is not a tax report.

## 9. Update

1. Read the release notes of every version between yours and the new one. Releases
   that change the database schema say so.
2. Make a backup (section 10). Let running scans and imports finish.

   > **Updating from a version before 0.19.3 with LAN access?** Since 0.19.3 the web
   > port is published only on `127.0.0.1` unless `.env` sets `WAMS_BIND_ADDRESS`.
   > Before, no `.env` was needed if you configured the node in the app. Create `.env`
   > now if necessary (section 5) and set `WAMS_BIND_ADDRESS=0.0.0.0`; otherwise the
   > browser in the LAN reports "connection refused" after the update.

3. Get the new version:

   - **Archive:** download and check the new archive as in section 4, then unpack it
     over the existing directory. `.env` and the data volume are not part of the
     archive and stay as they are.

     ```bash
     cd ~
     sha256sum -c wams-public-<version>.tar.gz.sha256
     tar -xzf wams-public-<version>.tar.gz -C ~/wherearemysats --strip-components=1
     ```

     Unpack with your normal umask (usually `022`). Under a restrictive umask such as
     `077` the extracted files are readable only by you, and the container user cannot
     read the sources copied into the image.

     Unpacking over the directory does not delete files that a new version removed.
     For a clean tree, unpack into a new empty directory instead, copy `.env` over
     (`cp -p ~/wherearemysats/.env <new-directory>/`), and keep using
     `-p wherearemysats`; the data belongs to the project name, not to the directory.

   - **Git:** `git fetch --tags && git checkout v<version>`.

4. Rebuild and start:

   ```bash
   cd ~/wherearemysats
   sudo docker compose -p wherearemysats up -d --build
   curl -fsS --retry 12 --retry-delay 5 --retry-connrefused http://127.0.0.1:8080/health; echo
   ```

5. Check that `/health` shows the new version, reload the browser page with Ctrl+F5 and
   check the version next to the WhereAreMySats logo in the top bar. Open Node Status.

**Database migrations run automatically at start.** They are forward-only and keep
existing data. WAMS starts only if the database ends at exactly the schema version the
program expects; otherwise it stops with `schema version mismatch` and does not change
the database. This also happens when you start an **older** WAMS on a database that a
newer version already migrated. A downgrade after a schema change is therefore only
possible by restoring the backup made before the update.

## 10. Backup and restore

The database contains your complete history: addresses, wallet names, labels, notes,
amounts and derivation data. The data volume also holds `node-settings.json` with the
RPC password in plain text. **Protect every backup like wallet data:** store it
encrypted, never in Git, cloud chat or issue trackers.

Use both:

1. **VM snapshot** of the whole host (your hypervisor's snapshot or backup function),
   e.g. before every update. It contains the database and the RPC password; keep
   snapshots on encrypted storage.
2. **Database copy while WAMS keeps running**, made with SQLite's backup API from a
   helper container that mounts the data volume read-only.

### Database copy

```bash
cd ~/wherearemysats
VOL=wherearemysats_lineage-data
sudo docker volume inspect "$VOL" > /dev/null && echo "volume found"
install -d -m 700 ~/wams-backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sudo docker run --name wams-backup --user 10001:10001 -v "$VOL":/data:ro wherearemysats:local \
  python -c "import sqlite3; s=sqlite3.connect('file:/data/lineage.db?mode=ro', uri=True); d=sqlite3.connect('/tmp/lineage-backup.db'); s.backup(d); d.execute('PRAGMA journal_mode=DELETE'); print('integrity', d.execute('PRAGMA integrity_check').fetchone()[0], 'schema', d.execute('PRAGMA user_version').fetchone()[0]); d.close(); s.close()"
sudo docker cp wams-backup:/tmp/lineage-backup.db ~/wams-backups/lineage-$STAMP.db
sudo docker rm wams-backup
sudo chown "$(id -u):$(id -g)" ~/wams-backups/lineage-$STAMP.db
chmod 600 ~/wams-backups/lineage-$STAMP.db
```

Expected output: `integrity ok schema <n>`. If a previous run failed, remove the
leftover helper first with `sudo docker rm wams-backup`. The copy contains the
database only, not the Node Settings; after a restore on a new host, enter the node
connection again. Move the copy to encrypted storage.

### Restore

Restore replaces the current database. The current files are kept next to it with the
suffix `.pre-restore-<time>`. A backup from an **older** schema is migrated forward at
start; a backup from a **newer** schema than the installed WAMS is refused (section 9).

```bash
cd ~/wherearemysats
sudo docker compose -p wherearemysats stop -t 660 bitcoin-lineage
STAGE=$(sudo mktemp -d)
sudo install -o 10001 -g 10001 -m 600 ~/wams-backups/lineage-<stamp>.db "$STAGE/lineage.db"
sudo chown 10001:10001 "$STAGE"
sudo docker compose -p wherearemysats run --rm --no-deps -T -v "$STAGE:/restore:ro" bitcoin-lineage python - <<'PY'
import os, shutil, sqlite3, time
from pathlib import Path
os.umask(0o077)
source = Path('/restore/lineage.db')
data = Path('/app/data')
target = data / 'lineage.db'
assert source.is_file(), 'Backup file missing'
con = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
try:
    assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Backup failed the integrity check'
    print('backup schema', con.execute('PRAGMA user_version').fetchone()[0])
finally:
    con.close()
stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
for suffix in ('', '-wal', '-shm'):
    current = data / f'lineage.db{suffix}'
    if current.exists():
        current.rename(data / f'lineage.db{suffix}.pre-restore-{stamp}')
with source.open('rb') as src, target.open('xb') as dst:
    shutil.copyfileobj(src, dst)
    dst.flush()
    os.fsync(dst.fileno())
con = sqlite3.connect(target)
try:
    assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Restored copy failed the integrity check'
    print('restored, schema', con.execute('PRAGMA user_version').fetchone()[0])
finally:
    con.close()
PY
sudo rm -r "$STAGE"
sudo docker compose -p wherearemysats up -d
curl -fsS --retry 12 --retry-delay 5 --retry-connrefused http://127.0.0.1:8080/health; echo
```

Always finish with `up -d`: after the manual `stop`, the container would otherwise not
start at the next reboot.

**Moving to another host:** install WAMS on the new host (sections 2 to 6), copy the
backup over a secure channel, restore it there, and enter the node connection in Node
Settings. Allow the new host's address on the node ([NODE_SETUP.md](NODE_SETUP.md)).

## 11. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Test Connection: "Cannot reach Bitcoin Core" | Wrong host/port, Core listens only on loopback (`rpcbind`), or a firewall blocks the port. For a node on the same host use `host.docker.internal`. Run the RPC test in [NODE_SETUP.md](NODE_SETUP.md#4-test-from-the-wams-host). |
| "RPC access forbidden" (HTTP 403 from Core) | The WAMS host's address is missing in `rpcallowip`, or Core was not restarted after the change. |
| "RPC authentication failed" (HTTP 401) | Username or password wrong. WAMS needs the plain password, not the `rpcauth` hash line. |
| "Bitcoin Core did not respond within 10 seconds" | Node overloaded or network path broken; try again, check the node. |
| "Connected, but Bitcoin network does not match" | The network in Node Settings differs from the node's chain (e.g. signet node, mainnet selected). Use one WAMS database per network. |
| "Initial block download is still running" | Core is still syncing. Status pages work; WAMS refuses data operations until the sync is complete. |
| Index "In progress" in the checklist or Node Status | Core is building `txindex`/`txospenderindex`. This can take hours to days after enabling an index. Tracing with historical spends is complete only afterwards. |
| Browser shows `Host not allowed` (HTTP 400) | You opened WAMS under a host name. Use the IP address or `localhost`, or add the name to `WAMS_ALLOWED_HOSTS` in `.env` and run `up -d`. |
| Changes fail with "Use same-origin requests." (HTTP 403) behind a proxy | The reverse proxy does not pass the original `Host` header. See [SECURITY.md](SECURITY.md#variant-c-reverse-proxy-with-login). |
| `up` fails with "port is already allocated" / "address already in use" | Another service uses 8080 (`sudo ss -ltnp` lists listening ports). Set `WAMS_HTTP_PORT` in `.env` and run `up -d`; use the new port in all URLs. |
| Not reachable from another computer ("connection refused"), e.g. after an update from a version before 0.19.3 | Expected with the default `WAMS_BIND_ADDRESS=127.0.0.1`. Check with `sudo docker ps`: the column PORTS shows `127.0.0.1:8080->8080/tcp` (only this host) or `0.0.0.0:8080->8080/tcp` (all interfaces). Use the SSH tunnel, or set `WAMS_BIND_ADDRESS=0.0.0.0` in `.env` (section 5; create `.env` if it does not exist) and run `up -d` ([SECURITY.md](SECURITY.md#variant-b-lan-access-opt-in)). |
| Container restarts repeatedly; log shows `schema version mismatch` | The database is from a newer WAMS version. Install that version again or restore a matching backup. |
| WAMS shows an empty installation after an update | Compose was started with another project name, so a new empty volume was created. Stop it and start again with `-p wherearemysats`. Do not delete any volume. `sudo docker volume ls` lists them. |
| WAMS did not come back after a reboot | It was stopped by hand before. Run `sudo docker compose -p wherearemysats up -d`. Check `systemctl is-enabled docker`. |

Logs: `sudo docker compose -p wherearemysats logs --tail 200 bitcoin-lineage`. The log
contains no addresses or passwords, but it can contain TXIDs; review it before you
share it.

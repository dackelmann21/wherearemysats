# Installation

This guide installs WhereAreMySats (WAMS) with Docker on a fresh **Ubuntu Server LTS**
(tested on 22.04; 24.04 should work the same), for example a virtual machine in your
home network.

**Before you start:**

- The server has 2 CPU cores, 4 GB RAM, 20 GB disk and internet access.
- You run your own **Bitcoin Core node** (31.1 or newer, fully synced, with `txindex=1`
  and `txospenderindex=1`) that the server can reach over RPC. See
  [NODE_SETUP.md](NODE_SETUP.md). WAMS does not run a node itself.
- Want to try first without mainnet data? See [SIGNET.md](SIGNET.md).

## Quick install

Log in to the server and paste this block:

```bash
# 1. Install Docker
curl -fsSL https://get.docker.com | sudo sh

# 2. Download WAMS, check the checksum and unpack it into ~/wherearemysats
VERSION=0.19.9
BASE=https://github.com/dackelmann21/wherearemysats/releases/download/v$VERSION
mkdir -p ~/wherearemysats && cd ~/wherearemysats
curl -fsSLO $BASE/wams-public-$VERSION.tar.gz
curl -fsSLO $BASE/wams-public-$VERSION.tar.gz.sha256
sha256sum -c wams-public-$VERSION.tar.gz.sha256 && tar -xzf wams-public-$VERSION.tar.gz --strip-components=1

# 3. Allow access from your home network
#    0.0.0.0 = WAMS listens on all network addresses of this server, not only locally.
#    WAMS has no login: every device in your home network can open it.
#    More protection (one PC only, SSH tunnel, reverse proxy): see SECURITY.md.
echo 'WAMS_BIND_ADDRESS=0.0.0.0' > .env && chmod 600 .env

# 4. Build and start (the first build takes a few minutes)
sudo docker compose -p wherearemysats up -d --build

# 5. Check that WAMS is running
curl -fsS --retry 12 --retry-delay 5 --retry-all-errors http://127.0.0.1:8080/health; echo
```

`sha256sum -c` must print `OK`; otherwise nothing is unpacked and you should not use
the download.

**Expected result of step 5:** `{"status":"ok","app":"wherearemysats","version":"0.19.9","db_connected":true}`.
The first start takes a few seconds (the container shows `health: starting` in
`sudo docker ps`); the check retries for about a minute. Lines like
`curl: (7) Failed to connect`, "Connection reset by peer" or "connection refused"
before the result are normal. If no `"status":"ok"` follows, wait a moment and run the
`curl` line again.

**6. Open WAMS in your browser** at `http://<vm-ip>:8080`, where `<vm-ip>` is the
server's address in your home network (`hostname -I` prints it; use the first
address). Then:

1. Open **Node Settings**, enter the RPC data of your node (host, port, username,
   password, network), click **Test Connection**, then **Save**. Where to find these
   values and how to allow the connection on the node: [NODE_SETUP.md](NODE_SETUP.md).
2. Restart WAMS once so that the saved connection is used everywhere:

   ```bash
   cd ~/wherearemysats && sudo docker compose -p wherearemysats restart bitcoin-lineage
   ```

3. Follow the **Getting started** checklist on the Dashboard.

## Manual download (alternative to step 2)

If you prefer to download the archive in your browser and copy it to the server
yourself, use this instead of step 2 of the Quick install. Step 1 (Docker) stays the
same; installing Docker and building the image still need internet access on the server.

1. On the [Releases page](https://github.com/dackelmann21/wherearemysats/releases),
   download both files of the version: `wams-public-0.19.9.tar.gz` and
   `wams-public-0.19.9.tar.gz.sha256`.
2. Optional, before copying: check the checksum on your computer. On Windows
   (PowerShell, in the download folder):

   ```powershell
   Get-FileHash .\wams-public-0.19.9.tar.gz -Algorithm SHA256
   ```

   The printed hash must match the first word in the `.sha256` file (the case of the
   letters does not matter). On Linux run `sha256sum -c wams-public-0.19.9.tar.gz.sha256`,
   on macOS `shasum -a 256 -c wams-public-0.19.9.tar.gz.sha256`; both must print `OK`.
3. Copy both files to your home directory on the server. On Windows (PowerShell; `scp`
   is included since Windows 10), Linux or macOS, in the download folder:

   ```bash
   scp wams-public-0.19.9.tar.gz wams-public-0.19.9.tar.gz.sha256 <user>@<vm-ip>:~
   ```

   Replace `<user>` (your Linux user on the server) and `<vm-ip>` (the server's address
   in your home network) **including the angle brackets**. A graphical tool such as
   WinSCP works as well.
4. On the server, move the files into the installation directory, check the checksum
   and unpack:

   ```bash
   VERSION=0.19.9
   mkdir -p ~/wherearemysats && cd ~/wherearemysats
   mv ~/wams-public-$VERSION.tar.gz* .
   sha256sum -c wams-public-$VERSION.tar.gz.sha256 && tar -xzf wams-public-$VERSION.tar.gz --strip-components=1
   ```

   `sha256sum -c` must print `OK`; otherwise nothing is unpacked.

Then continue with step 3 of the Quick install (`.env`, start, health check, browser),
from inside `~/wherearemysats`.

## Update

Read the [release notes](docs/releases/) of the new version first and make a backup
([Backup and restore](#backup-and-restore)). Then repeat step 2 of the Quick install
with the new version number, rebuild and check:

```bash
VERSION=0.19.9        # set the new version here
BASE=https://github.com/dackelmann21/wherearemysats/releases/download/v$VERSION
cd ~/wherearemysats
curl -fsSLO $BASE/wams-public-$VERSION.tar.gz
curl -fsSLO $BASE/wams-public-$VERSION.tar.gz.sha256
sha256sum -c wams-public-$VERSION.tar.gz.sha256 && tar -xzf wams-public-$VERSION.tar.gz --strip-components=1
sudo docker compose -p wherearemysats up -d --build
curl -fsS --retry 12 --retry-delay 5 --retry-all-errors http://127.0.0.1:8080/health; echo
```

If you download manually, replace the two `curl -fsSLO` lines with the
[Manual download](#manual-download-alternative-to-step-2) steps 1 to 3 and the `mv` line
of its step 4.

Your `.env` and your data stay as they are; they are not part of the archive.
`/health` shows the new version. Reload the browser page with Ctrl+F5. More details:
[Update details](#update-details).

## Advanced

Everything below is optional or for special cases. Commands in this part use
placeholders in angle brackets, e.g. `<vm-ip>`: replace them **including the brackets**
with your own value before you run the command, and do not paste real values into
public issues.

| Placeholder | Meaning |
|---|---|
| `<user>` | Linux user on the WAMS server (with `sudo`) |
| `<vm-ip>` | address of the WAMS server in your home network |
| `<pc-ip>` | address of the computer you open WAMS from |
| `<lan-cidr>` | your home network in CIDR form, e.g. `192.0.2.0/24` |

All commands run on the WAMS server unless a step says otherwise. Docker commands use
`sudo`; this guide does not add your user to the `docker` group, because membership
in that group is equivalent to root access.

### Requirements in detail

| Resource | Recommendation (not a measured minimum) |
|---|---|
| OS | Ubuntu Server LTS (22.04 tested, 24.04 should work the same), 64-bit; other Linux distributions work with Docker installed their own way |
| CPU | 2 vCPU |
| RAM | 4 GB (2 GB works for small datasets; large report/export runs peaked near 1 GB in synthetic benchmarks) |
| Disk | 20 GB, local and persistent (do not put the SQLite database on a network file system) |
| Network | a fixed address in your home network (static or DHCP reservation); **internet access while building** the image (Docker packages, base image `python:3.11-slim`, Python packages) |
| Docker | Docker Engine 20.10 or newer and the Docker Compose **plugin 2.24 or newer** (`docker compose`, not the old `docker-compose`) |

The node can run on the same server or on another machine in your home network.

### Install Docker with apt

Instead of the script in step 1 you can install Docker from Docker's official Ubuntu
repository. For other distributions see <https://docs.docker.com/engine/install/>.

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

Check the result (either installation method):

```bash
systemctl is-enabled docker        # enabled
systemctl is-active docker         # active
v=$(sudo docker compose version --short); echo "Compose $v"
printf '%s\n2.24.0\n' "${v#v}" | sort -V -C -r && echo "OK (>= 2.24)" || echo "TOO OLD"
```

Docker must start at boot (`enabled`); WAMS needs no systemd unit of its own.

### Server user, SSH and firewall (optional)

Hardening for the server. WAMS works without it.

If the Ubuntu installer did not create one, add a user with `sudo` (at the console):

```bash
sudo adduser <user>
sudo usermod -aG sudo <user>
```

Install the SSH server (often already present on Ubuntu Server):

```bash
sudo apt-get update
sudo apt-get install -y openssh-server
sudo systemctl enable --now ssh
```

**Key login, from your own computer** (Linux or macOS):

```bash
ls ~/.ssh/id_ed25519.pub 2>/dev/null || ssh-keygen -t ed25519
ssh-copy-id <user>@<vm-ip>
ssh <user>@<vm-ip> 'whoami; sudo -v && echo "sudo ok"'
```

On **Windows** (PowerShell; `ssh` and `ssh-keygen` are built in, `ssh-copy-id` is not):

```powershell
ssh-keygen -t ed25519
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh <user>@<vm-ip> "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
ssh <user>@<vm-ip> "whoami; sudo -v && echo sudo-ok"
```

After key login works you can disable password login on the server. Keep the current
session open and test a second login before you close it:

```bash
echo 'PasswordAuthentication no' | sudo tee /etc/ssh/sshd_config.d/10-nopassword.conf
sudo systemctl reload ssh
```

**Host firewall:** allow SSH only from your home network:

```bash
sudo apt-get install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from <lan-cidr> to any port 22 proto tcp
sudo ufw enable
sudo ufw status verbose
```

**ufw does not protect ports published by Docker.** Docker inserts its own packet
filter rules that are evaluated before ufw, so ufw neither blocks nor allows the WAMS
port. To limit who reaches WAMS, use the `DOCKER-USER` rule in
[SECURITY.md](SECURITY.md#variant-b-lan-access-opt-in). Never forward the WAMS port on
your router.

### Alternative to the release archive: Git

Use this **instead of step 2** of the Quick install, not in addition. Git needs an
empty or new directory; if you already unpacked the archive into `~/wherearemysats`,
stay with the archive.

```bash
sudo apt-get install -y git
git clone https://github.com/dackelmann21/wherearemysats.git ~/wherearemysats
cd ~/wherearemysats
git checkout v0.19.9              # a release tag, not an arbitrary branch state
```

Then continue with step 3. To update: `git fetch --tags && git checkout v<new-version>`,
then `up -d --build` as in [Update](#update).

Which version is unpacked or checked out:

```bash
grep '^APP_VERSION' ~/wherearemysats/backend/version.py
```

### Configuration file `.env`

`.env` in `~/wherearemysats` is needed for access from another computer
(`WAMS_BIND_ADDRESS`, step 3 of the Quick install). It is **not** needed for the node
connection: enter that in **Node Settings**. Edit it with an editor, never with real
secrets on the command line, and keep it at `chmod 600`:

```bash
cd ~/wherearemysats
nano .env
chmod 600 .env
sudo docker compose -p wherearemysats up -d       # apply changes; a plain restart does not reload .env
```

`.env.example` lists all variables with comments.

| Variable | Default | Purpose |
|---|---|---|
| `WAMS_BIND_ADDRESS` | `127.0.0.1` | Address **of this server** on which the web port is published. `127.0.0.1` = only this server (SSH tunnel or local reverse proxy). `0.0.0.0` = all addresses of this server (home network). See below. |
| `WAMS_HTTP_PORT` | `8080` | Port on the server. Change it if 8080 is already in use; then use the new port in all URLs. |
| `WAMS_ALLOWED_HOSTS` | empty | Host names (besides IP addresses and `localhost`) under which you open WAMS, comma-separated, e.g. a reverse-proxy name. Other names get HTTP 400. |
| `BTC_RPC_HOST`, `BTC_RPC_PORT`, `BTC_RPC_USER`, `BTC_RPC_PASSWORD`, `BTC_NETWORK`, `BTC_RPC_TLS` | empty, 8332, empty, empty, `mainnet`, `0` | Node connection from the environment. Leave them empty and use **Node Settings** unless you run headless. |

**About `WAMS_BIND_ADDRESS`:**

- It is an address of **this server** (`127.0.0.1`, the server's own address
  `<vm-ip>`, or `0.0.0.0`), **not** the address of the computer you browse from.
  Entering your PC's address makes Docker fail to start the container.
- WAMS has **no login**: everyone who reaches the port can read and change all data,
  including the node connection. To allow only one computer, keep `0.0.0.0` (or
  `<vm-ip>`) and add a firewall rule in Docker's `DOCKER-USER` chain
  ([SECURITY.md](SECURITY.md#variant-b-lan-access-opt-in)).
- Without `.env`, WAMS listens only on `127.0.0.1` of the server; open it then through
  an SSH tunnel from your computer, `ssh -N -L 8080:127.0.0.1:8080 <user>@<vm-ip>`,
  and browse to <http://127.0.0.1:8080>
  ([SECURITY.md](SECURITY.md#variant-a-localhost-only-with-ssh-tunnel-recommended)).
- If you set **one specific address** (e.g. `<vm-ip>`), WAMS no longer answers on
  `127.0.0.1`; use `http://<vm-ip>:8080` in the check commands of this guide. With
  `0.0.0.0` the `127.0.0.1` commands keep working.

**Node connection in `.env`:** keep the RPC fields empty and use Node Settings.
Values in `.env` are visible to everyone who can run `docker inspect` on the server;
the Node Settings file is stored with mode 0600 inside the data volume. Saved Node
Settings (`/app/data/node-settings.json`) override **all** `BTC_RPC_*` values
together; environment values are used only when nothing is saved. For a password
containing `$`, put the value in single quotes.

**Host names and reverse proxy:** to open WAMS under a name instead of an IP address,
add the name to `WAMS_ALLOWED_HOSTS`. For a reverse proxy with TLS and a password,
see [SECURITY.md](SECURITY.md#variant-c-reverse-proxy-with-login).

### Connect the node in detail

Prepare the node first ([NODE_SETUP.md](NODE_SETUP.md)) and run the RPC test from the
WAMS server described there. In **Node Settings**:

- **RPC Host** is an address or DNS name only (no `http://`, no path); **RPC Port** is
  8332 for mainnet. Leave **HTTPS to RPC** off unless you use a TLS proxy in front of
  Core.
- **Test Connection** shows network, block height and "Initial block download: No".
  It never changes the active connection.
- **Save** takes effect after `sudo docker compose -p wherearemysats restart
  bitcoin-lineage` (step 6 of the Quick install). Then **Node Status** shows:
  connected, expected network, blockchain synced, `txindex` and `txospenderindex`
  synced.
- A blank password field keeps the saved password only while host, port, username and
  TLS stay the same.
- A node running on the **same server** is not `localhost` from inside the container;
  use `host.docker.internal` as RPC host
  ([NODE_SETUP.md](NODE_SETUP.md#5-core-on-the-same-host)).

### First steps in the app

The **Dashboard** shows a **Getting started** checklist. It is complete when these
steps are done:

| Step | Where |
|---|---|
| Node connected | Node Settings |
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

### Checks after the start

`/health` does not depend on the node, so it is `ok` before a node is configured.
Container state and database line in the log:

```bash
cd ~/wherearemysats
sudo docker compose -p wherearemysats ps
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

Always pass `-p wherearemysats` to `docker compose`: the data volume is named after
the project (`wherearemysats_lineage-data`), so the same name finds the same data even
if you later move or rename the directory. The container uses the restart policy
`unless-stopped`: it comes back after a reboot of the server **unless you stopped it
by hand** (`docker compose stop`). After a manual stop, start it again with `up -d`.

### Update details

- Releases that change the database schema say so in their release notes. Let running
  scans and imports finish before you update.
- **Database migrations run automatically at start.** They are forward-only and keep
  existing data. WAMS starts only if the database ends at exactly the schema version
  the program expects; otherwise it stops with `schema version mismatch` and does not
  change the database. This also happens when you start an **older** WAMS on a
  database that a newer version already migrated. A downgrade after a schema change is
  therefore only possible by restoring the backup made before the update.
- **Updating from a version before 0.19.3 with access from your home network?** Since
  0.19.3 the web port is published only on `127.0.0.1` unless `.env` sets
  `WAMS_BIND_ADDRESS`. Create `.env` as in step 3 of the Quick install; otherwise the
  browser on another computer reports "connection refused" after the update.
- Unpack with your normal umask (usually `022`). Under a restrictive umask such as
  `077` the extracted files are readable only by you, and the container user cannot
  read the sources copied into the image.
- Unpacking over the directory does not delete files that a new version removed. For a
  clean tree, unpack into a new empty directory instead, copy `.env` over
  (`cp -p ~/wherearemysats/.env <new-directory>/`), and keep using
  `-p wherearemysats`; the data belongs to the project name, not to the directory.
- You can delete old `wams-public-*.tar.gz` files in `~/wherearemysats` after a
  successful update.

### Backup and restore

The database contains your complete history: addresses, wallet names, labels, notes,
amounts and derivation data. The data volume also holds `node-settings.json` with the
RPC password in plain text. **Protect every backup like wallet data:** store it
encrypted, never in Git, cloud chat or issue trackers.

Use both:

1. **VM snapshot** of the whole server (your hypervisor's snapshot or backup
   function), e.g. before every update. It contains the database and the RPC
   password; keep snapshots on encrypted storage.
2. **Database copy while WAMS keeps running**, made with SQLite's backup API from a
   helper container that mounts the data volume read-only.

#### Database copy

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
database only, not the Node Settings; after a restore on a new server, enter the node
connection again. Move the copy to encrypted storage.

#### Restore

Restore replaces the current database. The current files are kept next to it with the
suffix `.pre-restore-<time>`. A backup from an **older** schema is migrated forward at
start; a backup from a **newer** schema than the installed WAMS is refused
([Update details](#update-details)).

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
curl -fsS --retry 12 --retry-delay 5 --retry-all-errors http://127.0.0.1:8080/health; echo
```

Always finish with `up -d`: after the manual `stop`, the container would otherwise not
start at the next reboot.

**Moving to another server:** install WAMS on the new server (Quick install), copy the
backup over a secure channel, restore it there, and enter the node connection in Node
Settings. Allow the new server's address on the node ([NODE_SETUP.md](NODE_SETUP.md)).

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Health check right after the start: "Connection reset by peer" or "connection refused" | WAMS is still starting (`health: starting` in `sudo docker ps`). Wait a few seconds and run the check again. |
| Test Connection: "Cannot reach Bitcoin Core" | Wrong host/port, Core listens only on loopback (`rpcbind`), or a firewall blocks the port. For a node on the same server use `host.docker.internal`. Run the RPC test in [NODE_SETUP.md](NODE_SETUP.md#4-test-from-the-wams-host). |
| "RPC access forbidden" (HTTP 403 from Core) | The WAMS server's address is missing in `rpcallowip`, or Core was not restarted after the change. |
| "RPC authentication failed" (HTTP 401) | Username or password wrong. WAMS needs the plain password, not the `rpcauth` hash line. |
| "Bitcoin Core did not respond within 10 seconds" | Node overloaded or network path broken; try again, check the node. |
| "Connected, but Bitcoin network does not match" | The network in Node Settings differs from the node's chain (e.g. signet node, mainnet selected). Use one WAMS database per network. |
| "Initial block download is still running" | Core is still syncing. Status pages work; WAMS refuses data operations until the sync is complete. |
| Index "In progress" in the checklist or Node Status | Core is building `txindex`/`txospenderindex`. This can take hours to days after enabling an index. Tracing with historical spends is complete only afterwards. |
| Browser shows `Host not allowed` (HTTP 400) | You opened WAMS under a host name. Use the IP address or `localhost`, or add the name to `WAMS_ALLOWED_HOSTS` in `.env` and run `up -d`. |
| Changes fail with "Use same-origin requests." (HTTP 403) behind a proxy | The reverse proxy does not pass the original `Host` header. See [SECURITY.md](SECURITY.md#variant-c-reverse-proxy-with-login). |
| `up` fails with "port is already allocated" / "address already in use" | Another service uses 8080 (`sudo ss -ltnp` lists listening ports). Set `WAMS_HTTP_PORT` in `.env` and run `up -d`; use the new port in all URLs. |
| `up` fails with "cannot assign requested address" | `WAMS_BIND_ADDRESS` is not an address of this server (e.g. the address of your PC). Use `0.0.0.0` or the server's own address ([Configuration file `.env`](#configuration-file-env)). |
| Not reachable from another computer ("connection refused"), e.g. after an update from a version before 0.19.3 | `.env` with `WAMS_BIND_ADDRESS` is missing. Check with `sudo docker ps`: the column PORTS shows `127.0.0.1:8080->8080/tcp` (only this server) or `0.0.0.0:8080->8080/tcp` (all addresses). Create `.env` as in step 3 of the Quick install and run `up -d`, or use the SSH tunnel. |
| Container restarts repeatedly; log shows `schema version mismatch` | The database is from a newer WAMS version. Install that version again or restore a matching backup. |
| WAMS shows an empty installation after an update | Compose was started with another project name, so a new empty volume was created. Stop it and start again with `-p wherearemysats`. Do not delete any volume. `sudo docker volume ls` lists them. |
| WAMS did not come back after a reboot | It was stopped by hand before. Run `sudo docker compose -p wherearemysats up -d`. Check `systemctl is-enabled docker`. |

Logs: `sudo docker compose -p wherearemysats logs --tail 200 bitcoin-lineage`. The log
contains no addresses or passwords, but it can contain TXIDs; review it before you
share it.

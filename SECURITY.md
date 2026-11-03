# Security

WAMS keeps a detailed record of your Bitcoin history. This page explains its security
model, how to make it reachable without exposing that record, and how to report a
vulnerability.

## Operating model

- **Single user, self-hosted.** WAMS is meant to run on your own machine or VM in your
  home network, with one operator at a time.
- **No login.** WAMS has no user accounts and no password prompt. **Everyone who can
  reach the web port is an operator:** they can read everything (addresses, wallet
  names, labels, notes, amounts, the node's host, port and RPC username), change or
  delete all records, start expensive node scans, and change the node connection.
- **Analysis only.** WAMS never asks for or stores seeds or private keys, never signs
  or broadcasts transactions, and calls only read-only RPC methods
  ([NODE_SETUP.md](NODE_SETUP.md#2-rpc-methods-wams-uses)). Input that looks like a
  seed phrase, private key or `xprv` is rejected.

Protection therefore comes from **who can reach the port**. Choose one of the variants
below.

## Access variants

### Variant A: localhost only with SSH tunnel (recommended)

This is the default without `.env`. `compose.yaml` publishes WAMS on `127.0.0.1` of the
host only (`WAMS_BIND_ADDRESS=127.0.0.1`). Nothing in your LAN can reach it. To use it
after the Quick install, delete the line `WAMS_BIND_ADDRESS=0.0.0.0` from `.env` and run
`sudo docker compose -p wherearemysats up -d`. From your computer:

```bash
ssh -N -L 8080:127.0.0.1:8080 <user>@<vm-ip>
```

Then open <http://127.0.0.1:8080>. Access is limited to people with an SSH login on
the host. A VPN to the host works the same way.

### Variant B: LAN access (opt-in)

This is what the Quick install in [INSTALL.md](INSTALL.md) sets up. Set in `.env` on the
WAMS host and apply with `sudo docker compose -p wherearemysats up -d`:

```ini
WAMS_BIND_ADDRESS=0.0.0.0      # all addresses of the WAMS host; or <vm-ip> for one interface
```

`WAMS_BIND_ADDRESS` is an address **of the WAMS host** on which it listens, **not** the
address of the computer you browse from. It cannot limit access to one computer; use the
`DOCKER-USER` rule below for that.

Then every device in your LAN that reaches `http://<vm-ip>:8080` is an operator,
including guest devices, smart-home devices and any compromised computer. Use this
only in a network where you trust every device, never with port forwarding on the
router.

**Docker bypasses ufw.** Docker inserts its own packet filter rules for published
ports, and they are evaluated before ufw's rules. `ufw deny 8080` or a missing
`ufw allow` rule has **no effect** on the WAMS port. To limit access to your own
subnet, add a rule to Docker's `DOCKER-USER` chain (example, adjust the port if you
changed `WAMS_HTTP_PORT`):

```bash
sudo iptables -I DOCKER-USER -p tcp -m conntrack --ctdir ORIGINAL --ctorigdstport 8080 ! --ctorigsrc <lan-cidr> -j DROP
sudo iptables -L DOCKER-USER -n -v
```

This drops connections to host port 8080 from outside `<lan-cidr>`. To allow **only one
computer**, use its address as `<pc-ip>/32` instead of `<lan-cidr>`. The rule is not
persistent: re-apply it after each reboot (e.g. with a small systemd unit that runs
after `docker.service`) or use your firewall tool's `DOCKER-USER` support. Verify the
result from an allowed device and from a device outside the subnet (e.g. guest Wi-Fi):
the first must reach WAMS, the second must not.

### Variant C: reverse proxy with login

Keep WAMS on `127.0.0.1` and put a reverse proxy with TLS and a password on the same
host. Example with [Caddy](https://caddyserver.com/) (`/etc/caddy/Caddyfile`):

```caddyfile
wams.example {
    tls internal
    basic_auth {
        <admin-user> <hash from: caddy hash-password>
    }
    reverse_proxy 127.0.0.1:8080
}
```

- `wams.example` stands for the name under which you open WAMS; it must resolve to the
  WAMS host in your network. `tls internal` uses Caddy's own certificate authority for
  a name that is not public; your browser warns until you trust Caddy's root
  certificate. With a public domain you can drop `tls internal`.
- Allow the name in WAMS: `WAMS_ALLOWED_HOSTS=wams.example` in `.env`, then `up -d`.
  Without it, WAMS answers with HTTP 400 "Host not allowed".
- The proxy must pass the original `Host` header. Caddy does this by default; for nginx
  use `proxy_set_header Host $host;`. Otherwise every change fails with HTTP 403
  "Use same-origin requests."
- Do not log request bodies in the proxy (they contain the RPC password when you save
  Node Settings, and your records).
- Open the proxy port (443) in ufw; this port belongs to the proxy, not to Docker, so
  ufw applies.

## Browser-side protection

- **DNS rebinding:** WAMS accepts only requests whose `Host` is an IP address,
  `localhost` or a name listed in `WAMS_ALLOWED_HOSTS`; anything else gets HTTP 400.
  This stops a malicious web page from talking to WAMS through a name it controls.
- **Cross-site requests:** changes are accepted only from the WAMS page itself
  (same-origin check on `Origin` and `Sec-Fetch-Site`). WAMS sends no CORS headers.
- **No external resources:** the web interface loads no external scripts, fonts or
  images, and the Blockchain History Report contains no scripts.
- The donation button in the footer opens a `lightning:` link in your local wallet
  app; the page itself makes no network request.

## Node Settings

- The RPC password is **write-only**: the API never returns it, also not masked, and
  it is never logged. The page does not use browser storage.
- A blank password keeps the saved password only while host, port, username and TLS
  stay the same. Changing any of them requires entering the password again, so a
  client cannot send the saved password to a different server.
- **Save** takes effect after a restart of WAMS. **Test Connection** never changes the
  active connection.
- **Limitation:** because there is no login, anyone who reaches WAMS can replace the
  node connection with a node of their choice (WAMS would then read chain data from
  that node and send your addresses to it), and can use Test Connection to probe
  whether other hosts and ports are reachable from the WAMS host. The only protection
  is restricting access (variants A to C). A read-only switch for Node Settings is
  planned, not available.

## Secrets and data at rest

- `node-settings.json` in the data volume holds the RPC password **in plain text**
  (mode 0600, owner UID 10001). This protects against other users on the host, not
  against root, members of the `docker` group, or anyone with a backup.
- `.env` also holds plain text. Use it only when you need it, keep it at `chmod 600`,
  and prefer Node Settings for the RPC password: environment values are visible to
  everyone who can run `docker inspect`.
- The database `lineage.db` contains your complete history (addresses, wallet names,
  labels, notes, amounts, derivation indexes; extended public keys are not stored).
  It is not encrypted.
- **Backups and VM snapshots contain all of this, including the RPC password.** Store
  them encrypted and never in Git, chat tools or issue trackers. See
  [INSTALL.md, Backup and restore](INSTALL.md#backup-and-restore).
- Use a dedicated RPC user for WAMS with narrow `rpcallowip` entries
  ([NODE_SETUP.md](NODE_SETUP.md#3-configure-bitcoin-core)).

## What WAMS does not do

- No network connections except to your Bitcoin Core node and, only if you configure
  `ELECTRS_URL`, your own Electrs server. No telemetry, no update check, no external
  blockchain or price API.
- No wallet RPC, no signing, no broadcasting.
- No storage of seeds, private keys or extended public keys.

## Container

The container runs as non-root user UID 10001 with a read-only root file system, all
Linux capabilities dropped, `no-new-privileges` and a small `/tmp` in memory. Only
`/app/data` (the named volume) is writable. If you replace the named volume with a
bind mount, the directory must belong to UID 10001 with mode 700.

Optional: limit memory and process count with a `compose.override.yaml` next to
`compose.yaml` (Compose reads it automatically):

```yaml
services:
  bitcoin-lineage:
    mem_limit: 2g
    pids_limit: 256
```

Large reports and exports can use around 1 GB; choose the limit with headroom.

## Known limitations

These are known and accepted for the current release; some are planned for later
versions.

- No login (see above); Node Settings can be changed by anyone with access.
- Request bodies and free-text fields have no size limit; a client with access can fill
  the database or keep the process busy with very large imports.
- WAMS trusts the configured node: a malfunctioning or hostile node can make WAMS use
  a lot of memory or wait a long time for an answer.
- The web interface does not yet send a Content Security Policy or anti-framing
  headers (the report does send a restrictive policy).
- Python dependencies are pinned for direct packages only; the image build pulls the
  newest compatible versions of indirect dependencies.

## Reporting a vulnerability

Please report security problems **privately** through GitHub's **private
vulnerability reporting** for [this repository](https://github.com/dackelmann21/wherearemysats): open the repository's [**Security**](https://github.com/dackelmann21/wherearemysats/security)
tab and choose **Report a vulnerability**. Do not open a public issue for a security
problem. There is no e-mail address for reports.

Describe the problem, the affected version and steps to reproduce. Use synthetic data
only: never include real addresses, TXIDs, extended keys, RPC credentials, database
files or backups.

For other feedback and bugs, use [GitHub Issues](https://github.com/dackelmann21/wherearemysats/issues).

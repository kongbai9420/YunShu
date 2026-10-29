# Native Linux install

[中文](README.md) · [Docker deployment](../README.en.md)

If you would rather not run Docker, install the Linux tarball directly on the server. It **bundles its own Python runtime** (no system Python needed), runs under systemd, and is configured in `/etc/yunshu/yunshu.env`.

## Requirements

| Item | Requirement |
| --- | --- |
| OS | Debian 12 / 13, Ubuntu 22.04 / 24.04, Rocky Linux 8 / 9, AlmaLinux 8 / 9, openEuler 20.03 / 22.03 / 24.03 LTS (each tested in CI on its official container image); Kylin V10, see below |
| Architecture | amd64 (x86_64), arm64 (aarch64) |
| glibc | At least the version in the package's `GLIBC_REQUIRED` file (build baseline 2.28: EL8 / openEuler 20.03 / Kylin V10). Check with `getconf GNU_LIBC_VERSION`. musl systems such as Alpine are not supported; use Docker |
| Service manager | systemd (without systemd, install with `--no-systemd` and run it yourself) |
| Resources | 1 CPU / 1 GB RAM for the lite tier; disk depends on asset count and retention, backups live in the data dir by default |
| Other | ipmitool is not needed; PostgreSQL mode needs `pg_dump` / `pg_restore` on the host for backups |

### Kylin

The tarball is built against a glibc 2.28 baseline, so Kylin V10 (SP1–SP3, glibc 2.28) is within range. **Kylin itself is not tested**, though: there is no trustworthy public Kylin container image. CI covers it through glibc-2.28-compatible bases (openEuler 20.03 LTS SP4, AlmaLinux / Rocky Linux 8) and also tests openEuler 22.03 / 24.03. That shows the same baseline works; it is not a test on Kylin.

- Check `getconf GNU_LIBC_VERSION` first: `install.sh` refuses to install when glibc is older than `GLIBC_REQUIRED`.
- Follow the RHEL-family / openEuler notes below, including SELinux.
- The package brings its own SQLite (the system's 3.26 is too old), Python runtime and OpenSSL; the system copies are not used.

## Install

Download `yunshu-<version>-linux-<arch>.tar.gz` and its `.sha256` from <https://github.com/kongbai9420/YunShu/releases>:

```bash
sha256sum -c yunshu-4.4.0-beta.1-linux-amd64.tar.gz.sha256
tar xzf yunshu-4.4.0-beta.1-linux-amd64.tar.gz
cd yunshu-4.4.0-beta.1
sudo ./install.sh --start
```

Open `http://<server>:8080`, sign in with `admin` / `admin123` and **change the password when prompted**.

What `install.sh` does (safe to re-run):

| What | Where | Owner / mode |
| --- | --- | --- |
| System user and group `yunshu` (no login shell) | — | — |
| Program | `/opt/yunshu/<version>/`, `/opt/yunshu/current` points at the active version | root, read-only |
| Command | `/usr/local/bin/yunshu` (switches to the `yunshu` user when run as root) | root |
| Config | `/etc/yunshu/yunshu.env` (written only if missing) | `root:yunshu` 0640, dir 0750 |
| Master key | `/etc/yunshu/keys/master.key` (created on first start) | `yunshu` 0600, dir 0700 |
| Data and platform backups | `/var/lib/yunshu` (`yunshu.db`, `backups/`) | `yunshu` 0750 |
| Logs | `/var/log/yunshu` (plus `journalctl -u yunshu-api`) | `yunshu` 0750 |
| systemd units | `/etc/systemd/system/yunshu-{api,worker,migrate}.service` | — |

- Without `--start` it only installs and prints the next steps; start with `sudo systemctl enable --now yunshu-api`.
- Without systemd (containers, chroots) use `sudo ./install.sh --no-systemd`, then run `sudo yunshu api` in the foreground.
- On the RHEL family (Rocky, Alma, openEuler) `sudo` does not search `/usr/local/bin`; use the full path `sudo /usr/local/bin/yunshu ...`.

Everyday commands:

```bash
sudo systemctl status yunshu-api
sudo journalctl -u yunshu-api -f
sudo yunshu worker --check     # worker heartbeat (the embedded worker in the lite tier)
sudo yunshu backup create      # back up now
sudo yunshu version
```

## Configuration (yunshu.env)

One `KEY=VALUE` per line, `#` starts a comment; both the systemd units and the `yunshu` command read it. Run `sudo systemctl restart yunshu-api` after editing. Defaults (the full example ships as `yunshu.env.example`; upgrades save the new one as `/etc/yunshu/yunshu.env.example` for comparison):

| Variable | Default | Meaning |
| --- | --- | --- |
| `YUNSHU_DATA_DIR` | `/var/lib/yunshu` | SQLite database and platform backups |
| `YUNSHU_MASTER_KEY_FILE` | `/etc/yunshu/keys/master.key` | Master key; must be outside the data dir |
| `YUNSHU_LOG_DIR` | `/var/log/yunshu` | Log files (10 MB × 5 rotation) |
| `YUNSHU_BIND` / `YUNSHU_PORT` | `0.0.0.0` / `8080` | Listen address and port |
| `YUNSHU_EMBEDDED_WORKER` | `1` | Lite tier: collection and alerting run inside the API process |
| `YUNSHU_SETUP_WIZARD` | `0` | No first-run wizard (the database is chosen here) |
| `YUNSHU_SECURE_COOKIES` | `0` | Set to `1` behind an HTTPS reverse proxy |
| `FORWARDED_ALLOW_IPS` | (commented) | Proxy address when the reverse proxy runs on another host |
| `TZ` | (commented) | Follows the host time zone by default |
| `YUNSHU_SNMP_TRAP` etc. | (commented) | SNMP trap receiver, see below |

If you move the data or log directory, add the new path to the units' `ReadWritePaths=` with `sudo systemctl edit yunshu-api`:

```ini
[Service]
ReadWritePaths=/data/yunshu
```

Drop-ins made with `systemctl edit` survive upgrades; the unit files in `/etc/systemd/system/` are replaced on every install.

## Standard tier: external PostgreSQL

1. Create a PostgreSQL 16 / 17 database and user.
2. Install client tools of the server's major version or newer for backups (e.g. `postgresql-client-17` from the PostgreSQL apt repo, or `postgresql17` from its yum repo). Set `YUNSHU_PG_BIN_DIR` if they are not on `PATH`.
3. Edit `/etc/yunshu/yunshu.env`:

   ```ini
   YUNSHU_DATABASE_URL=postgresql+psycopg://yunshu:fake-change-me@db.example.invalid:5432/yunshu
   YUNSHU_EMBEDDED_WORKER=0
   ```

4. Start (once `yunshu-migrate` is enabled it runs before every start or restart of the API / worker):

   ```bash
   sudo systemctl enable --now yunshu-migrate yunshu-api yunshu-worker
   ```

   On a fresh install `sudo ./install.sh --start` does the same when it finds a PostgreSQL URL.
5. Worker health: `sudo yunshu worker --check`.

To move existing lite (SQLite) data to PostgreSQL use `sudo yunshu upgrade_to_pg ...` (same arguments as in the Docker guide).

## Upgrade

```bash
tar xzf yunshu-<new>-linux-amd64.tar.gz
cd yunshu-<new>
sudo ./install.sh
```

- The new version goes to `/opt/yunshu/<new>/`, `current` switches to it, and the old folder is kept.
- Services that were running are restarted; stopped services stay stopped.
- **SQLite**: when the schema is behind, the API writes a `pre-migrate` backup to `/var/lib/yunshu/backups/` and then upgrades itself.
- **PostgreSQL**: the script stops the services, runs `yunshu-migrate` (which also takes a `pre-migrate` `pg_dump` first) and starts them again. If the migration fails, `current` is switched back and the old version is started.
- Take a manual backup first (`sudo yunshu backup create`) and make sure the master key is backed up.

Once the new version is confirmed, delete old folders under `/opt/yunshu/` you no longer need.

## Rollback

Never point the old version at a database the new version has upgraded. Restore the pre-upgrade backup **with the old version**:

```bash
sudo systemctl stop yunshu-api yunshu-worker
sudo /opt/yunshu/<old>/install.sh                # points current back (services stay stopped)
sudo yunshu backup list                          # find the pre-migrate backup
sudo yunshu backup restore /var/lib/yunshu/backups/<file> --yes
sudo systemctl start yunshu-api                  # and yunshu-worker in the standard tier
```

PostgreSQL `.dump` backups are restored the same way.

## Backup and restore

- The platform backs up every 24 hours to `/var/lib/yunshu/backups/` (`YUNSHU_BACKUP_INTERVAL_HOURS`, `0` disables); `sudo yunshu backup create` backs up now.
- Stop the services before restoring: `sudo systemctl stop yunshu-api yunshu-worker`, then `sudo yunshu backup restore <file> --yes`, then start them. The current database is backed up again before it is replaced.
- The data dir sits on the same disk as the server and is **not an off-site backup**: copy `backups/` to another machine regularly.

## Master key backup

`/etc/yunshu/keys/master.key` encrypts saved device passwords. **Platform backups do not contain it**; without it a restore brings back assets but device passwords must be re-entered.

```bash
sudo cp -a /etc/yunshu/keys /safe/place/yunshu-keys-$(date +%F)
```

To restore on another machine: run `install.sh`, put the key file back into `/etc/yunshu/keys/` (running `install.sh` again fixes owner and mode), then restore the database backup. `sudo yunshu keytool ...` can export / import keys as well (same arguments as in the Docker guide).

## Uninstall

```bash
sudo /opt/yunshu/current/uninstall.sh            # stops and disables the services, removes program, command and units; keeps data, config, master key, logs and the yunshu user
sudo /opt/yunshu/current/uninstall.sh --purge    # removes everything; you must type PURGE
```

`--purge` deletes the master key, after which device passwords in any backup can no longer be decrypted. Copy backups and `/etc/yunshu/keys` elsewhere first. After a normal uninstall, reinstalling picks up the existing data.

## Firewall

`install.sh` **does not touch the firewall**; it only prints the command:

```bash
# firewalld (RHEL family, openEuler, Kylin)
sudo firewall-cmd --permanent --add-port=8080/tcp && sudo firewall-cmd --reload
# ufw (Ubuntu)
sudo ufw allow 8080/tcp
```

Behind a reverse proxy, open only the proxy's port and set `YUNSHU_BIND=127.0.0.1`.

## SELinux (RHEL family / openEuler / Kylin)

- With SELinux enforcing, `install.sh` labels the program `/opt/yunshu/<version>/yunshu/yunshu` as `bin_t` (a permanent rule via `semanage` when available, otherwise `chcon`) so systemd starts it as a regular service; everything else keeps the default labels.
- If the service fails with `avc: denied` in the logs, run `sudo ausearch -m avc -ts recent`. The usual cause is a data dir in an unusual place: `semanage fcontext -a -t var_lib_t '/data/yunshu(/.*)?' && restorecon -R /data/yunshu`.
- Ports 8080 / 1162 need no extra port labels: the service runs as `unconfined_service_t`.
- Do not disable SELinux for this.

## SNMP trap forwarding (162 → 1162)

The trap receiver is off by default. Enable it with `YUNSHU_SNMP_TRAP=1` and `YUNSHU_SNMP_TRAP_TARGET=<host IP reachable from the BMCs>`. The platform listens on **UDP 1162** (no privileged port) while BMCs only send to 162, so forward on the host:

```bash
# firewalld
sudo firewall-cmd --permanent --add-forward-port=port=162:proto=udp:toport=1162
sudo firewall-cmd --reload

# nftables (without firewalld)
sudo nft add table ip yunshu
sudo nft add chain ip yunshu prerouting '{ type nat hook prerouting priority dstnat; }'
sudo nft add rule ip yunshu prerouting udp dport 162 redirect to :1162
# make it permanent in /etc/nftables.conf (or your distribution's equivalent)
```

- Traps sent from the host itself to 162 bypass `prerouting`; test from another machine or send to 1162 directly.
- Only SNMP v1 / v2c is accepted, and the source IP and community must match an asset.
- In the standard tier the separate worker listens; in the lite tier the embedded worker inside the API does.

## systemd units

| Unit | Purpose | Default |
| --- | --- | --- |
| `yunshu-api.service` | Web UI and API; the lite tier also runs the embedded worker | enabled by `--start` |
| `yunshu-worker.service` | Separate worker (standard tier) | disabled |
| `yunshu-migrate.service` | One-shot schema migration (`Type=oneshot`); when enabled it runs before the API / worker start | disabled (enabled for PostgreSQL) |

All three run as `yunshu` with `EnvironmentFile=/etc/yunshu/yunshu.env`, `NoNewPrivileges=yes`, `ProtectSystem=strict` (writable: `/var/lib/yunshu /etc/yunshu /var/log/yunshu`), `PrivateTmp=yes` and no capabilities. The API and worker restart on failure; a failed migration is not retried automatically.

If "started" should mean "ready" (for example when other units depend on it), add this with `sudo systemctl edit yunshu-api` (needs curl; write `$` as `$$` in unit files):

```ini
[Service]
ExecStartPost=/bin/sh -c 'i=0; while [ $$i -lt 90 ]; do curl -fsS -o /dev/null http://127.0.0.1:8080/api/ready && exit 0; i=$$((i+1)); sleep 2; done; exit 1'
```

External monitoring can poll `http://<server>:8080/api/ready` (200 = database reachable, schema current, embedded worker running); for the worker use `sudo yunshu worker --check`.

## Building the tarball from source

Needs Docker (builds in a digest-pinned AlmaLinux 8 image with Python 3.11 from AppStream, dependencies installed by hash; the PyPA manylinux_2_28 image's Python is a static build that PyInstaller cannot use):

```bash
cd frontend && npm ci && npm run build && cd ..
scripts/build-linux.sh --docker      # writes artifacts/linux/yunshu-<version>-linux-<host arch>.tar.gz and .sha256
```

The build compiles SQLite from the sqlite.org source (version and SHA-256 pinned) into the package, checks the glibc / libstdc++ symbol versions every bundled program and library needs (fails above 2.28 / GLIBCXX 3.4.25), and smoke-tests the result in a temp dir (`yunshu version`, API start, `/api/ready`, first login requires a password change, the process loads only the bundled SQLite and libpython, worker heartbeat, backup, migrate). CI: `.github/workflows/linux-package.yml`.

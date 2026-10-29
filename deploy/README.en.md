# Docker deployment guide

[中文](README.md)

The image is built for **amd64 and arm64**, so it runs on Synology, fnOS, QNAP, Unraid and any Linux host. Pick one of the four options below.

| Option | Best for | Database |
| --- | --- | --- |
| ① A single `docker run` command | Trying it out | SQLite |
| ② Compose, lightweight (recommended) | Individuals, home labs, NAS | SQLite |
| ③ Compose, standard | Many assets, several users | PostgreSQL 17 |
| ④ NAS web UI | Synology / fnOS users who prefer clicking | same as ② or ③ |
| Native Linux install (no Docker) | Debian / Ubuntu / Rocky / Alma / openEuler / Kylin V10 servers without Docker, managed by systemd | SQLite or external PostgreSQL |
| Windows service (MSI) | Always-on Windows Server / Windows 10, 11 hosts reached over the LAN | SQLite or external PostgreSQL |

> The native Linux install is a tarball with a bundled Python runtime plus systemd services; see [linux/README.en.md](linux/README.en.md) ([中文](linux/README.md)).
>
> The Windows service edition does not use Docker; see [windows/README.en.md](windows/README.en.md).

> [!TIP]
> To try unreleased code, build the image yourself from the repository root: `docker build -f docker/Dockerfile.ops -t yunshu:local .`, then change `image:` in the examples to `yunshu:local`.

Once it is running, open `http://<host or NAS IP>:8080`, sign in with `admin` / `admin123` and **change the password when prompted**.

---

## ① A single docker run command

```bash
docker run -d --name yunshu --restart unless-stopped \
  -p 8080:8080 \
  -v yunshu-data:/data \
  -v yunshu-keys:/etc/yunshu \
  -e YUNSHU_EMBEDDED_WORKER=1 \
  -e YUNSHU_SETUP_WIZARD=0 \
  ghcr.io/kongbai9420/yunshu:4.4.0-beta.1
```

- `yunshu-data` holds the database and platform backups. `yunshu-keys` holds the **master key** that encrypts device passwords; it must live apart from the data.
- Drop `-e YUNSHU_SETUP_WIZARD=0` to get the first-run wizard, where you can choose PostgreSQL instead.

## ② Compose, lightweight (recommended)

Create a folder, save this as `docker-compose.yml`, then run `docker compose up -d`:

```yaml
# YunShu lightweight: single container with SQLite, for individuals / home labs / NAS
services:
  yunshu:
    image: ghcr.io/kongbai9420/yunshu:4.4.0-beta.1
    container_name: yunshu
    command: ["api"]
    ports:
      - "8080:8080"            # change the left side if the port is taken, e.g. "8090:8080"
    volumes:
      - yunshu-data:/data      # database and platform backups
      - yunshu-keys:/etc/yunshu  # master key, back it up separately
    environment:
      YUNSHU_EMBEDDED_WORKER: "1"   # collection and alerting run in the same container
      YUNSHU_SETUP_WIZARD: "0"      # tier already chosen, skip the first-run wizard
      YUNSHU_SECURE_COOKIES: "0"    # set to "1" behind an HTTPS reverse proxy
      TZ: Asia/Shanghai
    restart: unless-stopped

volumes:              # fixed names: same data after moving folders or switching templates
  yunshu-data:
    name: yunshu-data
  yunshu-keys:
    name: yunshu-keys
```

[`deploy/lite/compose.yml`](lite/compose.yml) is essentially the same (no `container_name`; `TZ` is a comment, the image already defaults to Asia/Shanghai), plus log rotation.

## ③ Compose, standard (PostgreSQL)

PostgreSQL 17, a schema-migration job, the web/API service and one background worker. **Replace both `change-me-to-a-long-random-password` with the same long password:**

```yaml
# YunShu standard: PostgreSQL 17 + migrate + API + worker
x-yunshu: &yunshu
  image: ghcr.io/kongbai9420/yunshu:4.4.0-beta.1
  volumes:
    - yunshu-data:/data
    - yunshu-keys:/etc/yunshu
  environment:
    # password, place 1
    YUNSHU_DATABASE_URL: postgresql+psycopg://yunshu:change-me-to-a-long-random-password@db:5432/yunshu
    YUNSHU_SECURE_COOKIES: "0"
    TZ: Asia/Shanghai

services:
  db:
    image: postgres:17@sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3   # pinned by digest
    environment:
      POSTGRES_DB: yunshu
      POSTGRES_USER: yunshu
      POSTGRES_PASSWORD: change-me-to-a-long-random-password   # password, place 2
    volumes:
      - yunshu-pg:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U yunshu -d yunshu"]
      interval: 10s
      timeout: 5s
      retries: 10
    restart: unless-stopped
  migrate:          # upgrades the schema on every start, then exits
    <<: *yunshu
    command: ["migrate"]
    depends_on:
      db:
        condition: service_healthy
    restart: "no"
  api:              # web UI and API
    <<: *yunshu
    command: ["api"]
    ports:
      - "8080:8080"
    depends_on:
      migrate:
        condition: service_completed_successfully
    restart: unless-stopped
  worker:           # collection, alerting, scheduled jobs
    <<: *yunshu
    command: ["worker"]
    depends_on:
      api:
        condition: service_healthy
    restart: unless-stopped

volumes:              # fixed names; yunshu-data / yunshu-keys are shared with the lightweight tier
  yunshu-pg:
    name: yunshu-pg
  yunshu-data:
    name: yunshu-data
  yunshu-keys:
    name: yunshu-keys
```

If the password contains characters such as `@ : / ? #`, URL-encode them in place 1 — or simply use letters and digits only. From the command line you can also use [`deploy/standard/`](standard/): copy `.env.example` to `.env` and put the password there.

For large installations with an external database and several executor workers, see [`deploy/enterprise/`](enterprise/).

## ④ Deploying from a NAS web UI

### Synology DSM 7.2+ (Container Manager)

1. Install **Container Manager** from Package Center.
2. In File Station, create a folder such as `docker/yunshu`.
3. Container Manager → **Project** → **Create**:
   - Project name: `yunshu`
   - Path: the folder from step 2
   - Source: **Create docker-compose.yml**, paste the YAML from ② or ③
4. Click through, enable "start after creation", finish. Pulling the image takes a few minutes.
5. Open `http://<NAS IP>:8080`.

DSM itself uses 5000 / 5001. If another package already uses 8080, change `"8080:8080"` to e.g. `"8090:8080"`.

### fnOS

1. Open the **Docker** app → **Compose** → **Add project**.
2. Name it `yunshu` and pick a folder (e.g. `docker/yunshu`).
3. Choose to create a `docker-compose` file, paste the YAML from ② or ③, enable "start after creation".
4. Open `http://<NAS IP>:8080`.

### Others

- **QNAP Container Station**: Applications → Create → paste the YAML.
- **Portainer**: Stacks → Add stack → Web editor, paste the YAML.
- **Unraid**: install the Docker Compose Manager plugin, add a stack, paste the YAML.

## Keeping data in a NAS shared folder

The examples use Docker named volumes, which need no permission setup. To use shared folders instead (handy for the NAS's own backup tools), replace `volumes:` with bind mounts and delete the top-level `volumes:` block:

```yaml
    volumes:
      - ./data:/data          # relative to the project folder
      - ./keys:/etc/yunshu
```

The container runs as UID **10001**, so make both folders writable first (once, over SSH):

```bash
cd /volume1/docker/yunshu        # Synology; on fnOS usually /vol1/1000/docker/yunshu
mkdir -p data keys
sudo chown -R 10001:10001 data keys
```

If you'd rather not use SSH, stay with named volumes.

## After deployment

**Receiving server alarm pushes (optional, off by default)**: the platform reads BMC event logs on a schedule. To have BMCs push hardware alarms immediately,
enable a receiver below, then run "configure Trap target" or "subscribe to Redfish events" from Operations → Enable SNMP (also needs `YUNSHU_BMC_SNMP=1`; admins only).

| Method | Settings | Notes |
| --- | --- | --- |
| SNMP Trap | `YUNSHU_SNMP_TRAP=1`, `YUNSHU_SNMP_TRAP_TARGET=<host IP reachable from BMCs>`, port mapping `162:1162/udp` | The worker listens on UDP 1162 (`YUNSHU_SNMP_TRAP_PORT`), no privileged port needed; v1 / v2c only, source IP and community must match an asset |
| Redfish events | `YUNSHU_BMC_EVENTS_URL=https://<platform address reachable from BMCs>` | BMCs post to `/api/bmc-events/<random token>`; must go through an HTTPS reverse proxy, and only that path needs to be open to the BMC network |

**Back up the master key (important).** Device passwords are encrypted with it; without it, saved passwords cannot be decrypted.

```bash
docker exec yunshu yunshu-entrypoint keytool export /tmp/master.key
docker cp yunshu:/tmp/master.key ./yunshu-master.key
docker exec yunshu rm /tmp/master.key
```

For the standard tier use the api container's name (see `docker ps`). Store the exported file away from your data backups.

**Upgrading.** Change the version in `image:`, then `docker compose pull && docker compose up -d` (in a NAS UI: stop → rebuild / recreate → start). The lightweight tier backs up and migrates the database on start; in the standard tier `migrate` does it first.

**Upgrading from 4.0.0-beta.1 (if you used `docker/docker-compose.ops.yml`, now removed from the repository).** That example did not pin volume names, so Docker named them "folder_volume", e.g. `docker_yunshu-data` and `docker_yunshu-keys` when started from the `docker` folder. The current templates pin the names to `yunshu-data` and `yunshu-keys`, so switching templates directly mounts **two empty volumes** and the data looks gone (it is still in the old volumes). Do this instead:

1. In the old folder, back up first: `docker compose exec api yunshu-entrypoint backup create`, and export the master key as described above (use the api container's name).
2. Find the old volume names: `docker volume ls --filter name=yunshu`.
3. Stop the old services in the old folder: `docker compose down` (**without** `-v`, which would delete the volumes). The old example ran a separate worker container; the lightweight template runs the worker inside the same container, so the old one must be stopped.
4. Pick one:
   - **Keep using the old volumes** (no copy, fastest): at the end of the new template, set the volume names to the ones from step 2:
     ```yaml
     volumes:
       yunshu-data:
         name: docker_yunshu-data
       yunshu-keys:
         name: docker_yunshu-keys
     ```
   - **Copy to the new names** (the old volumes stay, so you can roll back to the old version):
     ```bash
     docker run --rm --user 0 -v docker_yunshu-data:/from -v yunshu-data:/to ghcr.io/kongbai9420/yunshu:<new version> sh -c "cp -a /from/. /to/"
     docker run --rm --user 0 -v docker_yunshu-keys:/from -v yunshu-keys:/to ghcr.io/kongbai9420/yunshu:<new version> sh -c "cp -a /from/. /to/"
     ```
5. Start with the new template: `docker compose up -d`. The lightweight tier backs up and then migrates the database on start.
6. Log in, check the asset count and open one device's credential to confirm it decrypts. Only then decide whether to `docker volume rm` the old volumes.

If you deployed with the ① `docker run` example, the volumes are already named `yunshu-data` / `yunshu-keys`; moving to ② or ③ needs none of this.

**Remote access.** Put the platform behind an HTTPS reverse proxy (Synology Login Portal → Reverse Proxy, fnOS, Nginx, Caddy …) and set `YUNSHU_SECURE_COOKIES` to `"1"`. Do not expose port 8080 directly to the internet.

  When the proxy is not on the same host or in the same container, also set `FORWARDED_ALLOW_IPS: "<proxy address>"` (comma-separated for several) so the platform trusts the client address the proxy forwards. Without it, every user appears to come from the proxy: audit records show one source address, and login lockout counts per source and account, with 30 failures in total locking the whole source for 5 minutes. Do not set it to `*`, which lets anyone forge their source address.

**Health checks.** `/api/health` only says the process is alive; `/api/ready` says it can serve (database reachable, schema matches the image version, embedded worker running) and returns 503 with the reason otherwise. The image's own health check uses `/api/ready` for the api container and, for the worker container, whether that container's worker wrote a heartbeat in the last 90 seconds (the Health status in `docker inspect`). Point external monitoring or load balancers at `/api/ready`.

**Troubleshooting**

| Symptom | Fix |
| --- | --- |
| Image pull fails or is slow | Check network / registry mirror; offline networks can use the offline image archive from Releases (`docker load`), or build the image yourself (see top) |
| Port already in use | Change the left side of `ports` |
| Permission errors with bind mounts | Change folder ownership to 10001 as shown above |
| "master key must not be inside the data directory" | `/data` and `/etc/yunshu` must be two different volumes or folders |
| Forgot the admin password | Reset it from another administrator account; with a single admin, restore from backup |

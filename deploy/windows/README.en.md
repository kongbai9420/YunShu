# Windows service edition (MSI)

[中文](README.md) · [Back to the deployment guide](../README.en.md)

Runs Yunshu as a **Windows service**: it starts with the machine, keeps running without anyone signed in, and other computers on the network open `http://<this computer's IP>:8080` in a browser. SQLite is the default; an external PostgreSQL is also supported.

If you only use it yourself on one PC and do not want a service, use the [portable ZIP (desktop edition)](../../scripts/desktop-guide.md).

## Requirements

- 64-bit (amd64) Windows Server 2019 / 2022 / 2025, or Windows 10 / 11. 32-bit and ARM Windows are not supported.
- Installing and the maintenance commands need administrator rights.
- No Python, Node or .NET needed; the UI is used from a browser, so the server does not need WebView2.
- ipmitool is not bundled; collection and control use the built-in Redfish / IPMI implementations.

## Install

Download `Yunshu-<version>-windows-amd64.msi` and the matching `.sha256` from the release page and compare the checksum:

```powershell
(Get-FileHash -Algorithm SHA256 .\Yunshu-4.4.0-beta.1-windows-amd64.msi).Hash.ToLower()
Get-Content .\Yunshu-4.4.0-beta.1-windows-amd64.msi.sha256
```

> [!NOTE]
> The installer is **not code-signed** yet. Windows SmartScreen may say "Windows protected your PC". After checking that the checksum matches the release page, click "More info → Run anyway". The release page lists SHA-256 checksums; when a GitHub build provenance attestation is attached, `gh attestation verify <file> --repo kongbai9420/YunShu` confirms the file was built by this repository's workflow.

**With the UI**: double-click the MSI and follow the wizard. The custom setup page lets you change the program folder and select "Windows firewall rule (TCP 8080 inbound)" (not selected by default). The installer UI is in Simplified Chinese.

**Silent install** (elevated PowerShell or command prompt):

```powershell
msiexec /i Yunshu-4.4.0-beta.1-windows-amd64.msi /qn /l*v install.log
# also add the firewall rule:
msiexec /i Yunshu-4.4.0-beta.1-windows-amd64.msi /qn ADDLOCAL=ALL /l*v install.log
```

What the installer sets up:

| Item | Location / setting |
| --- | --- |
| Program | `C:\Program Files\Yunshu\` (`yunshu.exe` is added to the system PATH) |
| Configuration | `C:\ProgramData\Yunshu\yunshu.env` (default content written only if the file is missing; upgrades never overwrite it) |
| Database and automatic backups | `C:\ProgramData\Yunshu\data\` |
| Master key | `C:\ProgramData\Yunshu\keys\master.key` (created by the service on first start) |
| Logs | `C:\ProgramData\Yunshu\logs\` (10 MB per file, 5 files kept) |
| Service | name `Yunshu`, display name "云枢运维平台", automatic (delayed) start, restarted 10 s / 30 s / 60 s after a failure |
| Start menu | "打开云枢运维平台" (opens `http://localhost:8080/`), "云枢命令行" (command prompt) |

The service starts right after installation (the first start initialises the database and may take a few seconds). Open `http://localhost:8080`, sign in with `admin` / `admin123` and **change the password when prompted**.

### Service account and permissions

The service runs as the virtual account **`NT SERVICE\Yunshu`**: a dedicated low-privilege account that Windows provides for each service, with no password and no interactive logon, and with access only to the folders above. Unlike LocalSystem it does not own the whole machine, and unlike LocalService it is not shared with other services (which could then read the master key).

`C:\ProgramData\Yunshu` does not inherit the write access that ProgramData gives ordinary users by default:

| Folder | SYSTEM, Administrators | `NT SERVICE\Yunshu` | Ordinary users |
| --- | --- | --- | --- |
| `Yunshu\` (incl. `yunshu.env`) | Full control | Read | None |
| `data\`, `logs\` | Full control | Modify | None |
| `keys\`, `master.key` | Full control | Full control | None |

`yunshu.env` may contain a PostgreSQL password, so ordinary users cannot read it. Check with `icacls C:\ProgramData\Yunshu\keys\master.key`.

## Firewall

Windows Firewall blocks other computers from reaching port 8080 by default. Selecting the firewall feature during setup (or `ADDLOCAL=ALL` for a silent install) adds the inbound rule "云枢运维平台 (TCP 8080)", which is removed again on uninstall. It is off by default so that a port is never opened without you knowing, and so that local-only or reverse-proxy setups need no change.

To manage the rule yourself instead (elevated PowerShell):

```powershell
New-NetFirewallRule -DisplayName "Yunshu (TCP 8080)" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
```

If you change `YUNSHU_PORT`, open that port instead (the installer's rule is fixed to 8080).

## Configuration

Edit `C:\ProgramData\Yunshu\yunshu.env` as an administrator (for example Notepad started with "Run as administrator"), then restart the service:

```powershell
Restart-Service Yunshu
```

Default content:

```ini
YUNSHU_DATA_DIR=C:\ProgramData\Yunshu\data
YUNSHU_MASTER_KEY_FILE=C:\ProgramData\Yunshu\keys\master.key
YUNSHU_LOG_DIR=C:\ProgramData\Yunshu\logs
YUNSHU_EMBEDDED_WORKER=1
YUNSHU_SETUP_WIZARD=0
YUNSHU_PORT=8080
```

The file also contains commented examples for PostgreSQL (`YUNSHU_DATABASE_URL`, `YUNSHU_PG_BIN_DIR`), the time zone `TZ` (follows the host by default), a reverse proxy (`FORWARDED_ALLOW_IPS`) and `YUNSHU_SECURE_COOKIES=1` behind HTTPS. Add `YUNSHU_BIND=127.0.0.1` to allow local access only. All other variables are described in [backend/README.md](../../backend/README.md).

## Maintenance commands

Right-click the "云枢命令行" Start-menu entry → **Run as administrator**, or use `yunshu` in an elevated PowerShell (it is on the PATH; windows opened before the install need to be reopened):

```powershell
yunshu version
yunshu backup create            # back up now (fine while the service runs)
yunshu backup list
yunshu keytool fingerprint      # current master key id
yunshu worker --check           # is the background worker running
```

The commands read the same `yunshu.env`, so no extra arguments are needed. The master key and configuration are restricted to administrators; a non-elevated window gets "access denied".

> [!IMPORTANT]
> After the first install, wait until the service has started once (the web page opens) before running maintenance commands, so that the service account creates the master key. If an administrator runs a key-creating command first, `master.key` is restricted to that administrator and the service cannot read it; in that case delete `keys\master.key` (only while no device credentials have been saved) and restart the service.

## Backups and the master key

- Automatic backups run every 24 hours into `C:\ProgramData\Yunshu\data\backups\`; a backup is also taken automatically before every database schema upgrade.
- **Backups do not contain the master key.** Device passwords and other credentials are encrypted with it; without the key they cannot be decrypted. Export a copy right after installing and keep it off this computer:

  ```powershell
  yunshu keytool export D:\safe-place\yunshu-master.key
  ```

- Store `data\backups\` and the exported key separately (different drives or a password vault).
- Restore:

  ```powershell
  Stop-Service Yunshu
  yunshu backup restore C:\ProgramData\Yunshu\data\backups\yunshu-<time>-manual.db --yes
  Start-Service Yunshu
  ```

  On a new computer: install, stop the service, delete the newly created `C:\ProgramData\Yunshu\keys\master.key`, copy the exported key to that path (the new file inherits the keys folder permissions: SYSTEM, Administrators and the service account only), then restore the database.

## External PostgreSQL (standard tier)

For many assets and several simultaneous users. You need a PostgreSQL server (16 or 17) and, for backups, the [official PostgreSQL Windows client](https://www.postgresql.org/download/windows/) (`pg_dump` / `pg_restore`, same or newer major version than the server; selecting only "Command Line Tools" in its installer is enough).

New installation without data:

```powershell
# 1. Uncomment and fill in yunshu.env:
#    YUNSHU_DATABASE_URL=postgresql+psycopg://yunshu:<password>@<server>:5432/yunshu
#    YUNSHU_PG_BIN_DIR=C:\Program Files\PostgreSQL\17\bin
# 2. Create the schema, then restart the service
yunshu migrate
Restart-Service Yunshu
```

Moving existing SQLite data to PostgreSQL (the target must be an empty database; SQLite is opened read-only and left untouched):

```powershell
Stop-Service Yunshu
yunshu upgrade_to_pg precheck --target "postgresql+psycopg://yunshu:<password>@<server>:5432/yunshu"
yunshu upgrade_to_pg run --target "postgresql+psycopg://yunshu:<password>@<server>:5432/yunshu" --yes
# then put YUNSHU_DATABASE_URL and YUNSHU_PG_BIN_DIR into yunshu.env
Start-Service Yunshu
```

With PostgreSQL the service only checks the schema version at start and does not upgrade it: **after upgrading the program, run `yunshu migrate` before starting the service** (for a single instance you can set `YUNSHU_AUTO_MIGRATE=1` in yunshu.env instead). `yunshu backup create` uses `pg_dump` in this mode.

## Upgrade

Run the new MSI (UI or `msiexec /i new.msi /qn`). The installer stops the service, replaces the program and starts the service again:

- Configuration, database, master key and logs under `C:\ProgramData\Yunshu` are kept; `yunshu.env` is never overwritten.
- SQLite: on start the service backs up first (`data\backups\yunshu-<time>-pre-migrate.db`) and then upgrades the schema.
- PostgreSQL: run `yunshu migrate` first, see above.
- A previously selected firewall rule is kept.

Take a manual `yunshu backup create` beforehand and make sure the master key has been exported.

## Roll back

The installer does not install an older version over a newer one.

1. Uninstall the new version (data is kept): `msiexec /x new.msi /qn` or "Apps & features".
2. Install the older MSI. If the database schema is newer than that version understands, the service fails to start and keeps retrying; this is expected.
3. Restore the backup taken before the upgrade:

   ```powershell
   Stop-Service Yunshu
   yunshu backup list      # look for the pre-migrate entry
   yunshu backup restore C:\ProgramData\Yunshu\data\backups\yunshu-<time>-pre-migrate.db --yes
   Start-Service Yunshu
   ```

With PostgreSQL restore the `pg_dump` backup taken before the upgrade (`yunshu backup restore <file>.dump --yes`).

## Logs and troubleshooting

- Service log: `C:\ProgramData\Yunshu\logs\yunshu-service.log`; maintenance commands log to the same folder (e.g. `yunshu-backup.log`).
- Service state: `Get-Service Yunshu`, `sc.exe qc Yunshu`; start failures are also recorded by the Service Control Manager under Event Viewer → Windows Logs → System.
- Readiness: `Invoke-WebRequest http://127.0.0.1:8080/api/ready` (200 means database reachable, schema current and worker running).
- If port 8080 is taken the service fails and keeps retrying: set `YUNSHU_PORT` in `yunshu.env` and `Restart-Service Yunshu`.
- Run exactly as the service does, in a console (stop the service first, use an elevated window, Ctrl+C to stop):

  ```powershell
  Stop-Service Yunshu
  yunshu service --foreground
  ```

## Moving from the portable ZIP (desktop edition) to the service

The desktop edition keeps its database in `data\yunshu.db` inside the extracted folder and its master key in `%APPDATA%\yunshu\master.key`. The new portable ZIP ships `cli\yunshu.exe`, which works on the desktop folders (it never reads the service configuration).

1. Close the desktop window. Open a command prompt in the `cli` folder of the extracted ZIP:

   ```powershell
   .\yunshu.exe backup create
   .\yunshu.exe keytool export D:\yunshu-desktop.key
   ```

   Note the backup file path that `backup create` prints.
2. Install the service MSI, wait until the web page opens, then in an elevated PowerShell:

   ```powershell
   Stop-Service Yunshu
   Remove-Item C:\ProgramData\Yunshu\keys\master.key   # created by the fresh install, nothing encrypted with it yet
   Copy-Item D:\yunshu-desktop.key C:\ProgramData\Yunshu\keys\master.key
   yunshu backup restore <backup file from step 1> --yes
   Start-Service Yunshu
   ```

3. Sign in with your desktop account and check that assets and credentials are there. Only then delete the desktop folder and `D:\yunshu-desktop.key` (keep a separate safe copy of the exported key).

Do not run the desktop edition and the service against the same devices at the same time.

## Uninstall

"Settings → Apps → 云枢运维平台 → Uninstall", or:

```powershell
msiexec /x Yunshu-4.4.0-beta.1-windows-amd64.msi /qn
```

Uninstalling stops and deletes the service and removes the program files, the PATH entry, the Start-menu entries and the firewall rule, but **keeps `C:\ProgramData\Yunshu`** (configuration, database, backups, master key, logs) so that a later reinstall continues where you left off. To remove everything, after backing up the data and the master key:

```powershell
Remove-Item -Recurse -Force C:\ProgramData\Yunshu
```

## Building from source

On Windows amd64 with Python 3.11, Node 20 and a .NET 6 or newer runtime, from the repository root:

```powershell
.\scripts\build-windows.ps1            # artifacts\windows\Yunshu-<version>-windows-amd64.msi and .sha256
.\scripts\build-windows.ps1 -Desktop   # also the portable ZIP including cli\yunshu.exe
```

The script only builds and runs isolated self-tests (temporary folders, 127.0.0.1); it never installs the MSI or registers a service. Installing, upgrading and uninstalling are tested in GitHub Actions (`.github/workflows/windows-package.yml`, windows-2022 / windows-2025). The installer is built with [WiX Toolset](https://wixtoolset.org/) 5.0.2 (Microsoft Reciprocal License, MS-RL; used only as a build tool and not shipped in the installer). The script downloads that pinned version from nuget.org, checks its SHA-256 and keeps it under `artifacts\windows\cache` without any global installation.

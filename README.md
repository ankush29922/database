# CompactDB private deployment

This repository contains private production credentials. Keep the GitHub
repository private and restrict collaborator access.

On a fresh Ubuntu amd64 VPS, run exactly:

```bash
sudo bash -c 'command -v curl >/dev/null || { apt-get update && apt-get install -y curl; }; curl -fsSL https://raw.githubusercontent.com/ankush29922/database/main/deploy/install.sh | bash'
```

The raw script installs the minimal bootstrap prerequisites, clones this
repository, and re-enters the cloned installer in repository mode. The
repository installer creates the service account, low-memory runtime, 2 GiB
swap target, isolated Python environments, immutable application release,
private configuration, durable Google Drive download, systemd services,
watchdog, bounded automatic restart/backoff, notification retry, and optional
authenticated private-repository update timer. It then starts the durable
deployment unit and displays `compactdb progress`. Ctrl+C only detaches the
display; systemd continues the deployment.
The database package is downloaded directly from the configured private Google
Drive remote and is never stored in this repository.

Operational commands:

```bash
sudo compactdb status
sudo compactdb-observer
sudo compactdb logs
sudo compactdb restart
sudo compactdb stop
sudo compactdb repair
```

`compactdb-observer` reads `ActiveState`, `SubState`, and `MainPID` from systemd.
Healthy means active, running, and a nonzero MainPID. It also checks for the
privacy-safe Telegram `Application started` journal marker without printing
journal contents. It does not use process-name matching or process counts.

The application opens both DuckDB files read-only, uses the verified lookup
sidecar plus the direct locator, and has no full-scan fallback. Runtime writes
are confined to `/var/lib/compactdb` and `/var/log/compactdb`; the portable
database tree is mounted read-only to the bot service.

The update timer stays disabled unless `GITHUB_TOKEN` is explicitly added to
`private/update.env`. Initial deployment from a checked-out private repository
does not require that token.

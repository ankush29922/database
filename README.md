# CompactDB private deployment

This repository contains private production credentials. Keep the GitHub
repository private and restrict collaborator access.

On a fresh Ubuntu VPS, check out this private repository and run the installer
from the repository root:

```bash
sudo bash install.sh
```

The installer creates the service account, exact low-memory runtime, 2 GiB swap
target, immutable application release, private configuration, rclone download,
systemd services, watchdog, automatic restart/backoff, notification retry, and
optional authenticated private-repository update timer. It then starts the durable deployment unit.
The database package is downloaded directly from the configured private Google
Drive remote and is never stored in this repository.

Operational commands:

```bash
sudo compactdb status
sudo compactdb-observer
sudo compactdb logs
```

`compactdb-observer` reads `ActiveState`, `SubState`, and `MainPID` from systemd.
It does not use process-name matching or process counts.

The application opens both DuckDB files read-only, uses the verified lookup
sidecar plus the direct locator, and has no full-scan fallback. Runtime writes
are confined to `/var/lib/compactdb` and `/var/log/compactdb`; the portable
database tree is mounted read-only to the bot service.

The update timer stays disabled unless `GITHUB_TOKEN` is explicitly added to
`private/update.env`. Initial deployment from a checked-out private repository
does not require that token.

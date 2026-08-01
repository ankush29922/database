# CompactDB private deployment

This repository contains private production credentials. Keep the GitHub
repository private and restrict collaborator access.

## Fresh VPS requirements

- Ubuntu 24.04 LTS or newer, amd64/x86-64, with at least 1 GiB RAM.
- At least a 250 GB disk and 228 GB free on the filesystem containing `/srv`.
- Outbound HTTPS access to GitHub, Ubuntu mirrors, PyPI, Google Drive, and the
  Telegram Bot API, plus inbound SSH access for administration.

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
The standard-library Telegram notifier starts before the database transfer,
edits one durable progress message, and retries from systemd after terminal
closure, reboot, network loss, or Telegram API timeout. Notifier state is shown
by both `compactdb progress` and `compactdb status`.
The database package is downloaded directly from the configured private Google
Drive remote and is never stored in this repository.

Operational commands:

```bash
sudo compactdb status
sudo compactdb notify-status
sudo compactdb notify-test
sudo compactdb-observer
sudo compactdb logs
sudo compactdb restart
sudo compactdb stop
sudo compactdb repair
```

`compactdb notify-test` is the only command that sends a standalone test
message. The installer never invokes it.

`compactdb-observer` reads `ActiveState`, `SubState`, and `MainPID` from systemd.
Healthy means active, running, and a nonzero MainPID. It also checks for the
privacy-safe Telegram `Application started` journal marker without printing
journal contents. It does not use process-name matching or process counts.

Completion is shown by `Phase: COMPLETE`, `Download:
COMPLETE_BY_EXACT_METADATA`, `Progress: 100%`, `ACTIVE_STATE=active`,
`SUB_STATE=running`, a nonzero `MAIN_PID`, and
`TELEGRAM_APPLICATION_STARTED=yes`.

The application opens both DuckDB files read-only, uses the verified lookup
sidecar plus the direct locator, and has no full-scan fallback. Runtime writes
are confined to `/var/lib/compactdb` and `/var/log/compactdb`; the portable
database tree is mounted read-only to the bot service.

The update timer stays disabled unless `GITHUB_TOKEN` is explicitly added to
`private/update.env`. Initial deployment from a checked-out private repository
does not require that token.

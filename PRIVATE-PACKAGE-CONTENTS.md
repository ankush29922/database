# Private package contents

The `private/` directory intentionally contains the complete live deployment
configuration: Telegram bot credentials, owner and allowed-user access-control
configuration, database/runtime paths, Google Drive remote selection, full
rclone OAuth configuration, and repository update configuration.

The database, lookup index, direct-locator data, virtual environments, logs,
caches, backups, downloaded data, and runtime state are intentionally absent.

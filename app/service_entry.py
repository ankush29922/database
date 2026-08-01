from __future__ import annotations

import os
import socket
import asyncio

from main import build_application


def _notify(payload: str) -> None:
    address = os.getenv("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.connect(address)
        channel.sendall(payload.encode("utf-8"))


async def _watchdog() -> None:
    interval = max(5.0, int(os.getenv("WATCHDOG_USEC", "60000000")) / 2_000_000)
    while True:
        await asyncio.sleep(interval)
        _notify("WATCHDOG=1")


if __name__ == "__main__":
    application = build_application()

    async def ready(_application: object) -> None:
        _notify("READY=1\nSTATUS=Telegram polling is online")
        asyncio.create_task(_watchdog(), name="systemd-watchdog")

    application.post_init = ready
    application.run_polling(close_loop=False, bootstrap_retries=-1)

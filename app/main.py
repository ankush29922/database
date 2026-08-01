from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from bot_handlers import (
    cmd_help,
    cmd_id,
    cmd_on,
    cmd_start,
    cmd_status,
    cmd_stop,
    errors,
    handle_csv_document,
    handle_text,
)
from config import BOT_TOKEN, validate_config
from sidecar_queries import initialize_direct_locator


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)


class TelegramTokenRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if BOT_TOKEN:
            message = message.replace(BOT_TOKEN, "[REDACTED_TOKEN]")
        message = re.sub(
            r"/bot\d+:[A-Za-z0-9_-]+/",
            "/bot[REDACTED_TOKEN]/",
            message,
        )
        record.msg = message
        record.args = ()
        return True


for root_handler in logging.getLogger().handlers:
    root_handler.addFilter(TelegramTokenRedactionFilter())

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.request").setLevel(logging.WARNING)

log = logging.getLogger("telebot.main")


async def log_update_receipt(
    _update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Record receipt without storing user IDs, queries, or message contents."""
    log.info("Telegram update received")


def build_application() -> Application:
    validate_config(require_bot_token=True)
    initialize_direct_locator()
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(20)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(10)
        .get_updates_connect_timeout(20)
        .get_updates_read_timeout(40)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(10)
        .build()
    )

    private = filters.ChatType.PRIVATE
    application.add_handler(TypeHandler(Update, log_update_receipt), group=-1)
    application.add_handler(CommandHandler("start", cmd_start, filters=private))
    application.add_handler(CommandHandler("help", cmd_help, filters=private))
    application.add_handler(CommandHandler("status", cmd_status, filters=private))
    application.add_handler(CommandHandler("on", cmd_on, filters=private))
    application.add_handler(CommandHandler("stop", cmd_stop, filters=private))
    application.add_handler(CommandHandler("id", cmd_id, filters=private))
    application.add_handler(
        MessageHandler(filters.Document.ALL & private, handle_csv_document)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & private, handle_text)
    )
    application.add_error_handler(errors)
    return application


def main() -> None:
    application = build_application()
    log.info("Bot running with read-only CompactDB. Press Ctrl+C to stop.")
    application.run_polling(close_loop=False, bootstrap_retries=-1)


if __name__ == "__main__":
    main()

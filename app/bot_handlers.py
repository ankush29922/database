from __future__ import annotations

import asyncio
import csv
import html
import logging
import re
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

import phonenumbers
from telegram import Update, constants
from telegram.ext import ContextTypes

from config import (
    CSV_HAS_HEADER,
    CSV_MAX_ROWS,
    DEFAULT_REGION,
    DELETE_AFTER_SECONDS,
    RESULT_FILE_FALLBACK,
    RESULT_MAX_MESSAGES,
    RESULT_MESSAGE_MAX_CHARS,
    QUERY_TIMEOUT_SECONDS,
    is_allowed,
    is_owner,
)
from db import (
    JOINABLE_ID_PATTERN,
    export_id_results,
    export_phone_batch,
    export_phone_results,
    search_id,
    search_phone,
)
from formatting import (
    build_result_pages,
    format_not_found,
    phone_consolidation_stats,
)
from local_settings import is_enabled, set_enabled
from lookup_index_common import SidecarUnavailable
from sidecar_queries import (
    close_persistent_connections,
    interrupt_persistent_connections,
)


log = logging.getLogger(__name__)

ACCESS_DENIED = "⛔ Access denied."
PAUSED = "⏸️ Bot is temporarily paused."


class QueryWatchdogTimeout(RuntimeError):
    pass


@lru_cache(maxsize=1000)
def normalize_candidates(text: str) -> Optional[Tuple[str, str, str]]:
    """Return national number, country-code-plus-number, and E.164 display."""
    raw = re.sub(r"\s+", " ", text or "").strip()
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, DEFAULT_REGION)
        if not phonenumbers.is_valid_number(parsed):
            return None
        nsn = str(parsed.national_number)
        ccnsn = f"{parsed.country_code}{nsn}"
        e164 = phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.E164
        )
        return nsn, ccnsn, e164
    except Exception:
        digits = re.sub(r"\D", "", raw)
        if 7 <= len(digits) <= 15:
            return digits, digits, f"+{digits}"
        return None


async def _delete_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    try:
        await context.bot.delete_message(
            chat_id=data["chat_id"], message_id=data["message_id"]
        )
    except Exception:
        pass


def _schedule_delete(
    context: ContextTypes.DEFAULT_TYPE,
    message: Any,
    *,
    owner: bool,
) -> None:
    if owner:
        return
    job_queue = getattr(context, "job_queue", None)
    if job_queue is not None:
        job_queue.run_once(
            _delete_message,
            DELETE_AFTER_SECONDS,
            data={"chat_id": message.chat_id, "message_id": message.message_id},
            name=f"delete-{message.chat_id}-{message.message_id}",
        )
        return

    async def delete_later() -> None:
        await asyncio.sleep(DELETE_AFTER_SECONDS)
        try:
            await context.bot.delete_message(
                chat_id=message.chat_id, message_id=message.message_id
            )
        except Exception:
            pass

    asyncio.create_task(delete_later())


async def _reply_html(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> Any:
    started = time.monotonic()
    message = await update.effective_message.reply_text(
        text,
        parse_mode=constants.ParseMode.HTML,
        protect_content=False,
        disable_web_page_preview=True,
    )
    log.info(
        "Telegram API send phase=html seconds=%.3f",
        time.monotonic() - started,
    )
    _schedule_delete(context, message, owner=is_owner(update.effective_user.id))
    return message


async def _query_only(
    operation: Any,
    *args: Any,
) -> tuple[Any | None, dict[str, float], Exception | None]:
    """Run immediately off-loop and return without intermediate messages."""
    started = time.monotonic()
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        result = await asyncio.wait_for(
            asyncio.shield(task), timeout=QUERY_TIMEOUT_SECONDS
        )
        return (
            result,
            {"query": time.monotonic() - started},
            None,
        )
    except asyncio.TimeoutError:
        interrupt_persistent_connections()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
        except Exception:
            pass
        try:
            await asyncio.wait_for(
                asyncio.to_thread(close_persistent_connections), timeout=3.0
            )
        except asyncio.TimeoutError:
            pass
        duration = time.monotonic() - started
        log.error(
            "CompactDB phase=query duration=%.3f error_class=QueryWatchdogTimeout",
            duration,
        )
        return None, {"query": duration}, QueryWatchdogTimeout(
            "database operation exceeded its bounded deadline"
        )
    except Exception as exc:
        duration = time.monotonic() - started
        log.error(
            "CompactDB phase=query duration=%.3f error_class=%s",
            duration,
            type(exc).__name__,
        )
        return (
            None,
            {"query": duration},
            exc,
        )


async def _reply_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    path: Path,
    *,
    filename: str,
    caption: str,
) -> Any:
    started = time.monotonic()
    with path.open("rb") as stream:
        message = await update.effective_message.reply_document(
            document=stream,
            filename=filename,
            caption=caption,
            protect_content=True,
        )
    log.info(
        "Telegram API send phase=document seconds=%.3f",
        time.monotonic() - started,
    )
    _schedule_delete(context, message, owner=is_owner(update.effective_user.id))
    return message


async def _require_access(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    honor_pause: bool = True,
) -> bool:
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await _reply_html(update, context, ACCESS_DENIED)
        return False
    if honor_pause and not is_owner(user_id):
        try:
            enabled = await asyncio.to_thread(is_enabled)
        except Exception:
            log.exception("Local settings read failed")
            await _reply_html(update, context, "⚠️ Bot settings are unavailable.")
            return False
        if not enabled:
            await _reply_html(update, context, PAUSED)
            return False
    return True


async def _send_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    result: dict[str, Any],
    *,
    original_lookup: str,
    normalized_candidates: Sequence[str] | None = None,
    id_value: str | None = None,
) -> dict[str, float]:
    formatting_started = time.monotonic()
    main_phone = (
        None
        if id_value is not None
        else next(iter(normalized_candidates or ()), None)
    )
    presentation_counts = (
        {}
        if id_value is not None
        else phone_consolidation_stats(
            result["direct"],
            result["related"],
            main_phone=main_phone,
        )
    )
    if not result["direct"] and not result["related"]:
        text = format_not_found(
            original_lookup,
            is_admin=is_owner(update.effective_user.id),
        )
        formatting_seconds = time.monotonic() - formatting_started
        send_started = time.monotonic()
        await _reply_html(update, context, text)
        delivery_seconds = time.monotonic() - send_started
        return {
            "formatting": formatting_seconds,
            "telegram_send": delivery_seconds,
            "result_delivery": delivery_seconds,
            **presentation_counts,
        }

    pages, page_overflow = build_result_pages(
        result["direct"],
        result["related"],
        is_admin=is_owner(update.effective_user.id),
        max_chars=RESULT_MESSAGE_MAX_CHARS,
        max_messages=RESULT_MAX_MESSAGES,
        id_search=id_value is not None,
        main_phone=main_phone,
    )
    formatting_seconds = time.monotonic() - formatting_started
    send_started = time.monotonic()
    requires_file = bool(result.get("truncated") or page_overflow)
    if requires_file and RESULT_FILE_FALLBACK:
        await _reply_html(
            update,
            context,
            "📎 Complete results are attached as a temporary CSV file.",
        )
        with tempfile.TemporaryDirectory(prefix="telebot_result_") as directory:
            output = Path(directory) / "complete_results.csv"
            if id_value is not None:
                await asyncio.to_thread(export_id_results, id_value, output)
            else:
                await asyncio.to_thread(
                    export_phone_results,
                    original_lookup,
                    tuple(normalized_candidates or ()),
                    output,
                )
            await _reply_document(
                update,
                context,
                output,
                filename="complete_results.csv",
                caption="Complete CompactDB results",
            )
        delivery_seconds = time.monotonic() - send_started
        return {
            "formatting": formatting_seconds,
            "telegram_send": delivery_seconds,
            "result_delivery": delivery_seconds,
            **presentation_counts,
        }
    if requires_file:
        await _reply_html(
            update,
            context,
            "⚠️ Result exceeds the configured message limit.",
        )
        delivery_seconds = time.monotonic() - send_started
        return {
            "formatting": formatting_seconds,
            "telegram_send": delivery_seconds,
            "result_delivery": delivery_seconds,
            **presentation_counts,
        }
    for page in pages:
        await _reply_html(update, context, page)
    delivery_seconds = time.monotonic() - send_started
    return {
        "formatting": formatting_seconds,
        "telegram_send": delivery_seconds,
        "result_delivery": delivery_seconds,
        **presentation_counts,
    }


def _log_phone_phase_timings(
    timings: dict[str, float],
    *,
    direct_count: int,
    related_count: int,
) -> None:
    log.info(
        (
            "CompactDB phone result direct_records=%d related_records=%d "
            "consolidated_cards=%d distinct_ids=%d emails=%d "
            "alternate_phones=%d addresses=%d database_seconds=%.3f "
            "telegram_seconds=%.3f total_seconds=%.3f"
        ),
        direct_count,
        related_count,
        int(timings.get("consolidated_cards", 0)),
        int(timings.get("distinct_ids", 0)),
        int(timings.get("emails", 0)),
        int(timings.get("alternate_phones", 0)),
        int(timings.get("addresses", 0)),
        float(timings.get("database_total", timings.get("query", 0.0))),
        float(timings.get("result_delivery", 0.0)),
        float(timings.get("total", 0.0)),
    )


def _log_id_phase_timings(
    timings: dict[str, float],
    *,
    result_count: int,
) -> None:
    ordered = (
        "normalization",
        "query",
        "id_sidecar_lookup",
        "id_locator_lookup",
        "id_payload_fetch",
        "physical_rowid_payload_fetch",
        "formatting",
        "result_delivery",
        "telegram_send",
        "database_total",
        "unaccounted_overhead",
        "total",
    )
    values = " ".join(
        f"{name}={float(timings.get(name, 0.0)):.3f}s"
        for name in ordered
    )
    log.info(
        "CompactDB privacy-safe ID phase timings %s result_count=%d",
        values,
        result_count,
    )


def _complete_request_timings(
    *,
    request_started: float,
    normalization: float,
    query_timings: dict[str, float],
    database_timings: dict[str, float],
    presentation_timings: dict[str, float],
) -> dict[str, float]:
    total = time.monotonic() - request_started
    accounted = (
        normalization
        + float(query_timings.get("query", 0.0))
        + float(presentation_timings.get("formatting", 0.0))
        + float(presentation_timings.get("result_delivery", 0.0))
    )
    return {
        "normalization": normalization,
        **database_timings,
        **query_timings,
        **presentation_timings,
        "unaccounted_overhead": max(0.0, total - accounted),
        "total": total,
    }


async def _present_query_failure(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    error: Exception,
    operation_name: str,
) -> None:
    if isinstance(error, SidecarUnavailable):
        text = "🛠️ Search index is under maintenance. Please try again later."
        log.warning("CompactDB %s blocked by unavailable search index", operation_name)
    elif isinstance(error, QueryWatchdogTimeout):
        text = "⚠️ Database temporarily unavailable. Please try again later."
    else:
        text = "⚠️ Search failed. Please try again."
        log.error("CompactDB %s failed error_class=%s", operation_name, type(error).__name__)
    await _reply_html(update, context, text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("Start command handler invoked")
    if not await _require_access(update, context):
        return
    await cmd_help(update, context, access_checked=True)


async def cmd_help(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    access_checked: bool = False,
) -> None:
    if not access_checked and not await _require_access(update, context):
        return
    if is_owner(update.effective_user.id):
        text = (
            "<b>Owner Help</b>\n\n"
            "<b>Search</b>\n"
            "<code>phone number</code> — direct records plus one-step "
            "records sharing their exact 12-digit ID\n"
            "<code>12_digit_ID</code> — exact direct ID lookup\n"
            "<code>/id 12_digit_ID</code> — owner exact ID lookup\n"
            "<code>CSV file</code> — bounded batch phone search\n\n"
            "<b>Control</b>\n"
            "<code>/on</code> — enable approved users\n"
            "<code>/stop</code> — pause approved non-owner users\n"
            "<code>/status</code> — show local bot state"
        )
    else:
        text = (
            "<b>Help</b>\n\n"
            "Send a phone number for direct records plus one-step records "
            "sharing the direct record’s exact 12-digit ID.\n"
            "Send exactly 12 ASCII digits for ID-linked records.\n"
            "<code>/start</code> — show this help\n"
            "<code>/status</code> — show access and bot state"
        )
    await _reply_html(update, context, text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_access(update, context, honor_pause=False):
        return
    try:
        enabled = await asyncio.to_thread(is_enabled)
    except Exception:
        log.exception("Local settings read failed")
        await _reply_html(update, context, "⚠️ Bot settings are unavailable.")
        return
    role = "OWNER" if is_owner(update.effective_user.id) else "APPROVED USER"
    state = "ENABLED" if enabled else "PAUSED FOR NON-OWNERS"
    await _reply_html(
        update,
        context,
        f"<b>Status</b>\n<blockquote><b>Access:</b> {role}</blockquote>"
        f"\n<blockquote><b>Bot:</b> {state}</blockquote>",
    )


async def cmd_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await _reply_html(update, context, "⛔ Owner only.")
        return
    try:
        await asyncio.to_thread(set_enabled, True)
    except Exception:
        log.exception("Local settings write failed")
        await _reply_html(update, context, "⚠️ Bot state was not changed.")
        return
    await _reply_html(update, context, "✅ Bot is <b>ON</b>.")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await _reply_html(update, context, "⛔ Owner only.")
        return
    try:
        await asyncio.to_thread(set_enabled, False)
    except Exception:
        log.exception("Local settings write failed")
        await _reply_html(update, context, "⚠️ Bot state was not changed.")
        return
    await _reply_html(
        update,
        context,
        "⏸️ Bot is <b>PAUSED</b> for approved non-owner users.",
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await _reply_html(update, context, "⛔ Owner only.")
        return
    value = "".join(context.args or ())
    if not JOINABLE_ID_PATTERN.fullmatch(value):
        await _reply_html(
            update,
            context,
            "Usage: <code>/id 123456789012</code> (exactly 12 ASCII digits)",
        )
        return
    request_started = time.monotonic()
    result, query_timings, error = await _query_only(
        search_id,
        value,
    )
    if error is not None:
        await _present_query_failure(
            update,
            context,
            error=error,
            operation_name="ID query",
        )
        return
    presentation_timings = await _send_result(
        update,
        context,
        result,
        original_lookup=value,
        id_value=value,
    )
    _log_id_phase_timings(
        _complete_request_timings(
            request_started=request_started,
            normalization=0.0,
            query_timings=query_timings,
            database_timings=result.get("timings", {}),
            presentation_timings=presentation_timings,
        ),
        result_count=len(result.get("direct", ())),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("Phone text handler invoked")
    if not await _require_access(update, context):
        return
    request_started = time.monotonic()
    original = (update.effective_message.text or "").strip()
    normalization_started = time.monotonic()
    if JOINABLE_ID_PATTERN.fullmatch(original):
        normalization_seconds = time.monotonic() - normalization_started
        result, query_timings, error = await _query_only(
            search_id,
            original,
        )
        if error is not None:
            await _present_query_failure(
                update,
                context,
                error=error,
                operation_name="raw ID query",
            )
            return
        presentation_timings = await _send_result(
            update,
            context,
            result,
            original_lookup=original,
            id_value=original,
        )
        _log_id_phase_timings(
            _complete_request_timings(
                request_started=request_started,
                normalization=normalization_seconds,
                query_timings=query_timings,
                database_timings=result.get("timings", {}),
                presentation_timings=presentation_timings,
            ),
            result_count=len(result.get("direct", ())),
        )
        return
    normalized = normalize_candidates(original)
    normalization_seconds = time.monotonic() - normalization_started
    if normalized is None:
        await _reply_html(
            update,
            context,
            "❓ Send a valid mobile number, e.g. <code>+91 xxxxx xxxxx</code>.",
        )
        return
    nsn, ccnsn, _display = normalized
    candidates = tuple(dict.fromkeys((nsn, ccnsn)))
    result, query_timings, error = await _query_only(
        search_phone,
        candidates,
    )
    if error is not None:
        await _present_query_failure(
            update,
            context,
            error=error,
            operation_name="phone query",
        )
        return
    presentation_timings = await _send_result(
        update,
        context,
        result,
        original_lookup=original,
        normalized_candidates=candidates,
    )
    timings = _complete_request_timings(
        request_started=request_started,
        normalization=normalization_seconds,
        query_timings=query_timings,
        database_timings=result.get("timings", {}),
        presentation_timings=presentation_timings,
    )
    _log_phone_phase_timings(
        timings,
        direct_count=len(result.get("direct", ())),
        related_count=len(result.get("related", ())),
    )


async def handle_csv_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update.effective_user.id):
        await _reply_html(update, context, "⛔ Owner only (CSV).")
        return
    document = update.effective_message.document
    if document is None or not (document.file_name or "").lower().endswith(".csv"):
        await _reply_html(update, context, "📄 Please send a .csv file.")
        return

    with tempfile.TemporaryDirectory(prefix="telebot_csv_") as directory:
        input_path = Path(directory) / "input.csv"
        output_path = Path(directory) / "lookup_results.csv"
        try:
            telegram_file = await document.get_file()
            await telegram_file.download_to_drive(custom_path=input_path)
            lookups: list[tuple[str, Sequence[str] | None]] = []
            with input_path.open("r", encoding="utf-8", errors="ignore") as stream:
                reader = csv.reader(stream)
                if CSV_HAS_HEADER:
                    next(reader, None)
                for row_number, row in enumerate(reader):
                    if row_number >= CSV_MAX_ROWS:
                        break
                    original = " ".join(row).strip()
                    if not original:
                        continue
                    normalized = normalize_candidates(original)
                    candidates = (
                        tuple(dict.fromkeys(normalized[:2]))
                        if normalized is not None
                        else None
                    )
                    lookups.append((original, candidates))
            await asyncio.to_thread(export_phone_batch, lookups, output_path)
            await _reply_document(
                update,
                context,
                output_path,
                filename="lookup_results.csv",
                caption="CompactDB batch results",
            )
        except Exception:
            log.exception("CSV batch processing failed")
            await _reply_html(update, context, "⚠️ CSV processing failed.")


async def errors(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled bot error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await _reply_html(update, context, "⚠️ Oops, something went wrong.")
    except Exception:
        pass

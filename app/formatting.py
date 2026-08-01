from __future__ import annotations

import html
import re
from typing import Any, Iterable, Sequence


_SEP_RE = re.compile(r"[!|]+")
_PHONE_SPLIT_RE = re.compile(r"[!|,;/]+")
_PHONE_DISPLAY_RE = re.compile(r"[+0-9().\s-]+", re.ASCII)
_SAFE_ID_RE = re.compile(r"[0-9]{12}", re.ASCII)
_ABSENT = {
    "",
    "-",
    "—",
    "n/a",
    "na",
    "nil",
    "none",
    "null",
    "undefined",
}


def _clean(value: Any) -> str:
    """Clean display whitespace while preserving capitalization and text."""
    if value is None:
        return ""
    text = str(value)
    text = _SEP_RE.sub(", ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,")
    return "" if text.casefold() in _ABSENT else text


def _value_key(value: Any) -> str:
    return _clean(value).casefold()


def _ordered_values(
    records: Sequence[dict[str, Any]],
    field: str,
) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for record in records:
        display = _clean(record.get(field))
        key = display.casefold()
        if not display or key in seen:
            continue
        seen.add(key)
        values.append(display)
    return values


def _phone_digits(value: Any) -> str | None:
    display = _clean(value)
    if not display or not _PHONE_DISPLAY_RE.fullmatch(display):
        return None
    digits = "".join(character for character in display if character.isdigit())
    if not digits.isascii() or not 7 <= len(digits) <= 15:
        return None
    return digits


def _phone_key(value: Any) -> str:
    digits = _phone_digits(value)
    if digits is None:
        return f"raw:{_value_key(value)}"
    if digits.startswith("91") and len(digits) == 12:
        return digits[2:]
    return digits


def _phone_values(value: Any) -> Iterable[str]:
    display = _clean(value)
    if not display:
        return ()
    pieces = [_clean(piece) for piece in _PHONE_SPLIT_RE.split(display)]
    return tuple(piece for piece in pieces if piece)


def _contact_links(value: Any) -> str:
    """Port of the original contact links with strict numeric validation."""
    digits = _phone_digits(value)
    if digits is None:
        return ""
    if len(digits) == 10:
        digits = "91" + digits
    elif not digits.startswith("91"):
        digits = "91" + digits
    if not 9 <= len(digits) <= 15:
        return ""
    return (
        f'<a href="https://wa.me/+{digits}">WHATSAPP</a>'
        "   |   "
        f'<a href="https://t.me/+{digits}">TELEGRAM</a>'
    )


def _ordered_addresses(records: Sequence[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for record in records:
        display = _clean(record.get("address"))
        if not display:
            continue
        key = re.sub(r"\s+", " ", display).casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(display)
    return values


def _ordered_record_values(
    records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda record: int(record.get("record_id", 0)),
    )


def _ordered_unique_records(
    records: Sequence[dict[str, Any]],
    *,
    seen: set[int] | None = None,
) -> list[dict[str, Any]]:
    observed = seen if seen is not None else set()
    ordered: list[dict[str, Any]] = []
    for record in _ordered_record_values(records):
        record_id = int(record["record_id"])
        if record_id in observed:
            continue
        observed.add(record_id)
        ordered.append(record)
    return ordered


def _first_value(
    direct: Sequence[dict[str, Any]],
    related: Sequence[dict[str, Any]],
    field: str,
) -> str:
    values = _ordered_values([*direct, *related], field)
    return values[0] if values else ""


def _ordered_safe_ids(
    records: Sequence[dict[str, Any]],
) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for record in records:
        display = _clean(record.get("id"))
        if not _SAFE_ID_RE.fullmatch(display) or display in seen:
            continue
        seen.add(display)
        values.append(display)
    return values


def _sorted_alternate_phones(
    direct: Sequence[dict[str, Any]],
    related: Sequence[dict[str, Any]],
    *,
    main_phone: str,
) -> list[str]:
    candidates: list[str] = []
    # Keep the first stored display for a duplicate: all alt fields first,
    # followed by mobile values from related records.
    for record in [*direct, *related]:
        candidates.extend(_phone_values(record.get("alt")))
    for record in related:
        candidates.extend(_phone_values(record.get("mobile")))

    seen = {_phone_key(main_phone)}
    valid: list[tuple[int, int, str]] = []
    invalid: list[tuple[int, str]] = []
    for position, display in enumerate(candidates):
        key = _phone_key(display)
        if not key or key in seen:
            continue
        seen.add(key)
        if key.isascii() and key.isdecimal():
            valid.append((int(key), position, display))
        else:
            invalid.append((position, display))
    valid.sort(key=lambda item: (item[0], item[1]))
    return [
        *(display for _number, _position, display in valid),
        *(display for _position, display in invalid),
    ]


def _consolidated_card(
    direct: Sequence[dict[str, Any]],
    related: Sequence[dict[str, Any]],
    *,
    main_phone: str | None,
) -> dict[str, Any]:
    seen: set[int] = set()
    ordered_direct = _ordered_unique_records(direct, seen=seen)
    ordered_related = _ordered_unique_records(related, seen=seen)
    ordered = [*ordered_direct, *ordered_related]
    stored_mobiles = _ordered_values(ordered, "mobile")
    displayed_main = _clean(main_phone) or (
        stored_mobiles[0] if stored_mobiles else ""
    )
    name = _first_value(ordered_direct, ordered_related, "name")
    father = _first_value(ordered_direct, ordered_related, "fname")
    circle = _first_value(ordered_direct, ordered_related, "circle")
    return {
        "record_ids": [int(item["record_id"]) for item in ordered],
        "mobile": displayed_main,
        "names": [name] if name else [],
        "fathers": [father] if father else [],
        "ids": _ordered_safe_ids(ordered),
        "emails": _ordered_values(ordered, "email"),
        "alternate_phones": _sorted_alternate_phones(
            ordered_direct,
            ordered_related,
            main_phone=displayed_main,
        ),
        "circles": [circle] if circle else [],
        "addresses": _ordered_addresses(ordered),
        "notes": _ordered_values(ordered, "exception_reason"),
    }


def consolidate_phone_records(
    records: Sequence[dict[str, Any]],
    *,
    main_phone: str | None,
) -> list[dict[str, Any]]:
    if not records:
        return []
    return [_consolidated_card(records, (), main_phone=main_phone)]


def consolidate_phone_results(
    direct: Sequence[dict[str, Any]],
    related: Sequence[dict[str, Any]],
    *,
    main_phone: str | None,
) -> list[dict[str, Any]]:
    """Group strictly by searched phone and return one enriched card."""
    if not direct and not related:
        return []
    return [_consolidated_card(direct, related, main_phone=main_phone)]


def _single_record_card(record: dict[str, Any]) -> dict[str, Any]:
    return _consolidated_card(
        [record],
        (),
        main_phone=_clean(record.get("mobile")),
    )


def phone_consolidation_stats(
    direct: Sequence[dict[str, Any]],
    related: Sequence[dict[str, Any]],
    *,
    main_phone: str | None,
) -> dict[str, int]:
    cards = consolidate_phone_results(
        direct,
        related,
        main_phone=main_phone,
    )
    if not cards:
        return {
            "consolidated_cards": 0,
            "distinct_ids": 0,
            "emails": 0,
            "alternate_phones": 0,
            "addresses": 0,
        }
    card = cards[0]
    return {
        "consolidated_cards": 1,
        "distinct_ids": len(card["ids"]),
        "emails": len(card["emails"]),
        "alternate_phones": len(card["alternate_phones"]),
        "addresses": len(card["addresses"]),
    }


def _row(label: str, value: Any, *, copyable: bool) -> str:
    display = _clean(value)
    if not display:
        return ""
    escaped_label = html.escape(label)
    escaped_value = html.escape(display)
    if copyable:
        escaped_value = f"<code>{escaped_value}</code>"
    return (
        f"<blockquote><b>{escaped_label} :</b> "
        f"{escaped_value}</blockquote>"
    )


def _numbered_rows(
    first_label: str,
    values: Sequence[str],
    *,
    copyable: bool,
) -> list[str]:
    rows: list[str] = []
    for index, value in enumerate(values, 1):
        label = first_label if index == 1 else f"{first_label} {index}"
        row = _row(label, value, copyable=copyable)
        if row:
            rows.append(row)
    return rows


def _card_lines(card: dict[str, Any], *, is_admin: bool) -> list[str]:
    lines = ["<b>🗂️ Result</b>"]
    mobile = _row("Mobile", card.get("mobile"), copyable=is_admin)
    if mobile:
        lines.append(mobile)
    lines.extend(
        _numbered_rows("Name", card.get("names", ()), copyable=is_admin)
    )
    lines.extend(
        _numbered_rows("Father", card.get("fathers", ()), copyable=is_admin)
    )
    lines.extend(
        _numbered_rows("ID", card.get("ids", ()), copyable=is_admin)
    )
    lines.extend(
        _numbered_rows("Email", card.get("emails", ()), copyable=is_admin)
    )
    alternate = list(card.get("alternate_phones", ()))
    if alternate:
        lines.append("")
        lines.append("<b>ALT PHONE NO:</b>")
        for index, value in enumerate(alternate, 1):
            lines.append(_row(f"Alt-{index}", value, copyable=is_admin))
    circles = list(card.get("circles", ()))
    if circles:
        lines.append(_row("Circle", circles[0], copyable=is_admin))
    for index, value in enumerate(card.get("addresses", ()), 1):
        lines.append(_row(f"Address {index}", value, copyable=is_admin))
    lines.extend(
        _numbered_rows(
            "Record note", card.get("notes", ()), copyable=False
        )
    )
    main_links = _contact_links(card.get("mobile"))
    link_lines: list[str] = []
    if main_links:
        link_lines.append(
            f"<blockquote><b>MAIN   :</b> {main_links}</blockquote>"
        )
    for index, value in enumerate(alternate, 1):
        links = _contact_links(value)
        if links:
            link_lines.append(
                f"<blockquote><b>ALT {index}    :</b> {links}</blockquote>"
            )
    if link_lines:
        lines.append("")
        lines.extend(link_lines)
    return [line for line in lines if line is not None]


def format_result(
    record: dict[str, Any],
    is_admin: bool = False,
) -> str:
    """Format one record using the restored person-card presentation."""
    return "\n".join(_card_lines(_single_record_card(record), is_admin=is_admin))


def format_not_found(value: str, is_admin: bool = False) -> str:
    display = _clean(value)
    escaped = html.escape(display)
    if is_admin and display:
        escaped = f"<code>{escaped}</code>"
    return (
        "❌ No record for "
        f"<blockquote><b>Mobile :</b> {escaped}</blockquote>."
    )


def _split_oversized_row(line: str, max_chars: int) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    match = re.fullmatch(
        r"<blockquote><b>(.*?) :</b> (?:<code>)?(.*?)(?:</code>)?</blockquote>",
        line,
    )
    if match is None:
        raise ValueError("an indivisible presentation line exceeds the limit")
    label = html.unescape(match.group(1))
    value = html.unescape(re.sub(r"</?code>", "", match.group(2)))
    copyable = "<code>" in line
    pieces: list[str] = []
    remaining = value
    continuation = 0
    while remaining:
        current_label = label if continuation == 0 else f"{label} (cont.)"
        low, high = 1, len(remaining)
        best = 0
        while low <= high:
            midpoint = (low + high) // 2
            candidate = _row(
                current_label,
                remaining[:midpoint],
                copyable=copyable,
            )
            if len(candidate) <= max_chars:
                best = midpoint
                low = midpoint + 1
            else:
                high = midpoint - 1
        if best == 0:
            raise ValueError("message limit is too small for presentation markup")
        pieces.append(
            _row(
                current_label,
                remaining[:best],
                copyable=copyable,
            )
        )
        remaining = remaining[best:]
        continuation += 1
    return pieces


def _minimum_pages(lines: Sequence[str], max_chars: int) -> list[str]:
    atomic: list[str] = []
    for line in lines:
        atomic.extend(_split_oversized_row(line, max_chars))
    pages: list[str] = []
    current = ""
    for line in atomic:
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            pages.append(current)
            current = line
    if current:
        pages.append(current)
    return pages


def build_result_pages(
    direct: list[dict[str, Any]],
    related: list[dict[str, Any]],
    *,
    is_admin: bool,
    max_chars: int,
    max_messages: int,
    id_search: bool = False,
    main_phone: str | None = None,
) -> tuple[list[str], bool]:
    """Consolidate phone records and create the minimum safe HTML pages."""
    if id_search:
        cards = [_single_record_card(record) for record in direct]
    else:
        cards = consolidate_phone_results(
            direct,
            related,
            main_phone=main_phone,
        )
    lines: list[str] = []
    for index, card in enumerate(cards):
        if index:
            lines.extend(("", "━━━━━━━━━━━━━━━━━━━━━━━━━━", ""))
        lines.extend(_card_lines(card, is_admin=is_admin))
    pages = _minimum_pages(lines, max_chars) if lines else []
    return pages[:max_messages], len(pages) > max_messages

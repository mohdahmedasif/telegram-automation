"""
Shared Google Sheets schema + helpers for the unified household inventory.

One Telegram bot reads/writes a single spreadsheet tab using this row schema.
This module owns everything that schema touches so bot handlers stay thin.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

import gspread
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger("automations.inventory")

# A=item_id … M=last_updated
SHEET_HEADERS = [
    "item_id",
    "name",
    "brand",
    "category",
    "location",
    "package_type",
    "package_count",
    "units_per_package",
    "size_value",
    "size_unit",
    "expiry_date",
    "notes",
    "last_updated",
]

COL_ITEM_ID = 1
COL_NAME = 2
COL_BRAND = 3
COL_CATEGORY = 4
COL_LOCATION = 5
COL_PACKAGE_TYPE = 6
COL_PACKAGE_COUNT = 7
COL_UNITS_PER_PACKAGE = 8
COL_SIZE_VALUE = 9
COL_SIZE_UNIT = 10
COL_EXPIRY_DATE = 11
COL_NOTES = 12
COL_LAST_UPDATED = 13

# Sheet-level dropdown lists (data validation). Gemini's enums use the same sets.
CATEGORIES = [
    "Grains & Rice",
    "Canned Goods",
    "Pasta & Noodles",
    "Seasonings & Spices",
    "Beverages",
    "Medicine",
    "Supplement",
]

STORAGE_LOCATIONS = [
    "Sofa Storage",
    "Kitchen Cabinet",
    "Basement",
    "Washroom Cabinet",
]

PACKAGE_TYPES = [
    "Tablet Strip",
    "Bottle",
    "Flask",
    "Box",
    "Sachet",
    "Jar",
    "Can",
    "Pack",
    "Loose",
]

# Optional fields use blank cells — never write N/A placeholders.
BLANK_PLACEHOLDERS = {
    "",
    "n/a",
    "na",
    "n.a.",
    "none",
    "null",
    "unknown",
    "skip",
    "-",
    "no",
}

BASE_BAD_NAMES = {
    "",
    "unknown",
    "unknown item",
    "n/a",
    "na",
    "none",
    "null",
    "item",
    "product",
}

_MONTH_NAMES = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def blank_if_placeholder(value: Any) -> str:
    """Turn N/A-style placeholders into an empty string for the sheet."""
    text = str(value or "").strip()
    if text.casefold() in BLANK_PLACEHOLDERS:
        return ""
    return text


def is_bad_name(value: Any, bad_names: set[str] = BASE_BAD_NAMES) -> bool:
    return str(value or "").strip().casefold() in bad_names


def coerce_choice(value: Any, allowed: list[str], default: str) -> str:
    """Map free text onto an allowed dropdown value (case-insensitive)."""
    text = str(value or "").strip()
    if not text:
        return default
    lowered = {opt.casefold(): opt for opt in allowed}
    if text.casefold() in lowered:
        return lowered[text.casefold()]
    for opt in allowed:
        if opt.casefold() in text.casefold() or text.casefold() in opt.casefold():
            return opt
    return default


def escape_md(text: Any) -> str:
    value = str(text) if text is not None else ""
    return re.sub(r"([_*`\[\]])", r"\\\1", value)


def split_size(text: Any) -> tuple[str, str]:
    """Parse free text like '500g' / '20 mg' / '1.5 L' into (value, unit)."""
    cleaned = blank_if_placeholder(text)
    if not cleaned:
        return "", ""
    match = re.match(r"^\s*([\d.,]+)\s*([A-Za-zµ%]+)?\s*$", cleaned)
    if match:
        value = match.group(1).replace(",", ".")
        unit = (match.group(2) or "").strip()
        return value, unit
    return cleaned, ""


def normalize_expiration(value: Any) -> str | None:
    """
    Normalize an expiration value to YYYY-MM-DD, or blank if skipped.

    Returns None when the input cannot be parsed (caller should re-prompt).
    """
    from datetime import datetime

    text = str(value or "").strip()
    if not text:
        return None
    if text.casefold() in BLANK_PLACEHOLDERS:
        return ""

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[./]", "-", text)

    for fmt in ("%Y-%m-%d", "%Y-%m", "%d-%m-%Y", "%m-%d-%Y", "%d-%m-%y", "%m-%y"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt in {"%Y-%m", "%m-%y"}:
                return parsed.strftime("%Y-%m-01")
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue

    month_day_year = re.fullmatch(
        r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if month_day_year:
        month = _MONTH_NAMES.get(month_day_year.group(1).casefold())
        if month:
            day = int(month_day_year.group(2))
            year = int(month_day_year.group(3))
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                return None

    day_month_year = re.fullmatch(
        r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if day_month_year:
        month = _MONTH_NAMES.get(day_month_year.group(2).casefold())
        if month:
            day = int(day_month_year.group(1))
            year = int(day_month_year.group(3))
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                return None

    month_year = re.fullmatch(
        r"([A-Za-z]+)\s+(\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if month_year:
        month = _MONTH_NAMES.get(month_year.group(1).casefold())
        if month:
            year = int(month_year.group(2))
            try:
                return date(year, month, 1).isoformat()
            except ValueError:
                return None

    return None


def extract_expiration_from_text(text: str) -> str | None:
    """Pull a plausible expiration from free text, if present."""
    raw = text or ""
    patterns = [
        r"\bexp(?:iry|ires|iration)?\.?\s*[:=]?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        r"\bexp(?:iry|ires|iration)?\.?\s*[:=]?\s*(\d{4}-\d{2}-\d{2})",
        r"\bexp(?:iry|ires|iration)?\.?\s*[:=]?\s*(\d{4}-\d{2})\b",
        r"\bexp(?:iry|ires|iration)?\.?\s*[:=]?\s*([A-Za-z]+\s+\d{4})",
        r"\b(\d{4}-\d{2}-\d{2})\b",
        r"\b(\d{4}-\d{2})\b",
        r"\b([A-Za-z]+\s+\d{1,2},?\s+\d{4})\b",
        r"\b([A-Za-z]+\s+\d{4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        normalized = normalize_expiration(match.group(1))
        if normalized:
            return normalized
    return None


def is_real_expiration(value: Any) -> bool:
    text = blank_if_placeholder(value)
    if not text:
        return False
    return bool(normalize_expiration(text))


def parse_json_response(raw_text: str) -> Any:
    """Shared Gemini JSON-response parsing with consistent error messages."""
    raw_text = (raw_text or "").strip()
    if not raw_text:
        raise RuntimeError("Gemini returned an empty response.")
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned invalid JSON: {raw_text[:300]}") from exc
    return parsed


def format_item_markdown(item: dict[str, Any], *, row_index: int | None = None) -> str:
    name = escape_md(item.get("name") or "Item")
    brand = blank_if_placeholder(item.get("brand"))
    title = f"*{name}*" + (f" _{escape_md(brand)}_" if brand else "")
    lines = [
        title,
        f"• Category: `{escape_md(item.get('category') or '')}`",
        f"• Location: `{escape_md(item.get('location') or '')}`",
        f"• Package: `{escape_md(item.get('package_type') or '')}` "
        f"× `{escape_md(item.get('package_count', 0))}`",
    ]
    units_per_package = blank_if_placeholder(item.get("units_per_package"))
    if units_per_package:
        lines.append(f"• Units per package: `{escape_md(units_per_package)}`")
    size_value = blank_if_placeholder(item.get("size_value"))
    size_unit = blank_if_placeholder(item.get("size_unit"))
    if size_value or size_unit:
        lines.append(f"• Size: `{escape_md(size_value)} {escape_md(size_unit)}`".rstrip())
    expiry = blank_if_placeholder(item.get("expiry_date"))
    if expiry:
        lines.append(f"• Expires: `{escape_md(expiry)}`")
    notes = blank_if_placeholder(item.get("notes"))
    if notes:
        lines.append(f"• Notes: _{escape_md(notes)}_")
    reason = item.get("match_reason")
    if reason:
        lines.append(f"• Why matched: _{escape_md(reason)}_")
    if row_index is not None:
        lines.append(f"• Sheet row: `{row_index}`")
    return "\n".join(lines)


def confirm_keyboard(save_label: str = "✅ Save") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(save_label, callback_data="cf:save"),
                InlineKeyboardButton("❌ Cancel", callback_data="cf:cancel"),
            ]
        ]
    )


def action_keyboard(row_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ +1", callback_data=f"inc_{row_index}"),
                InlineKeyboardButton("➖ -1", callback_data=f"dec_{row_index}"),
            ],
            [InlineKeyboardButton("❌ Delete", callback_data=f"del_{row_index}")],
        ]
    )


class InventorySheet:
    """Owns the gspread worksheet + all row-shape-aware I/O for the unified sheet."""

    def __init__(self, worksheet: gspread.Worksheet) -> None:
        self.worksheet = worksheet

    @staticmethod
    def connect(
        credentials_path: str,
        spreadsheet_id: str,
        worksheet_name: str = "",
        worksheet_gid: str = "",
    ) -> gspread.Worksheet:
        client = gspread.service_account(filename=credentials_path)
        spreadsheet = client.open_by_key(spreadsheet_id)
        if worksheet_gid:
            sheet = spreadsheet.get_worksheet_by_id(int(worksheet_gid))
        elif worksheet_name:
            sheet = spreadsheet.worksheet(worksheet_name)
        else:
            sheet = spreadsheet.sheet1
        logger.info(
            "Connected to spreadsheet %s (worksheet: %s)", spreadsheet_id, sheet.title
        )
        return sheet

    def get_all_records(self) -> list[dict[str, Any]]:
        records = self.worksheet.get_all_records()
        inventory: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            entry = dict(record)
            entry["row_index"] = index + 2
            inventory.append(entry)
        return inventory

    def next_item_id(self) -> int:
        values = self.worksheet.col_values(COL_ITEM_ID)[1:]
        max_id = 0
        for raw in values:
            try:
                max_id = max(max_id, int(str(raw).strip()))
            except (TypeError, ValueError):
                continue
        return max_id + 1

    def append_item(self, item: dict[str, Any]) -> list[Any]:
        """`item` holds every header except `item_id`/`last_updated`, which are stamped here."""
        item_id = self.next_item_id()
        last_updated = date.today().isoformat()
        row: list[Any] = [item_id]
        for header in SHEET_HEADERS[1:-1]:
            row.append(item.get(header, ""))
        row.append(last_updated)
        self.worksheet.append_row(row, value_input_option="USER_ENTERED")
        return row

    def get_package_count(self, row_index: int) -> int:
        raw = self.worksheet.cell(row_index, COL_PACKAGE_COUNT).value
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    def set_package_count(self, row_index: int, value: int) -> int:
        new_val = max(0, int(value))
        self.worksheet.update_cell(row_index, COL_PACKAGE_COUNT, new_val)
        self.worksheet.update_cell(
            row_index, COL_LAST_UPDATED, date.today().isoformat()
        )
        return new_val

    def delete_row(self, row_index: int) -> None:
        self.worksheet.delete_rows(row_index)

    def row_summary(self, row_index: int) -> dict[str, Any]:
        values = self.worksheet.row_values(row_index)
        padded = values + [""] * (len(SHEET_HEADERS) - len(values))
        return {header: padded[i] for i, header in enumerate(SHEET_HEADERS)}

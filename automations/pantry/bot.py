"""
Pantry inventory Telegram bot — Google Sheets + Gemini extraction.

Managed by `PantryAutomation`; do not run this module as the app entrypoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from difflib import SequenceMatcher
from typing import Any

import gspread
from google import genai
from google.genai import types
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from automations.gemini_util import (
    generate_content_with_fallback,
    resolve_gemini_model,
)

logger = logging.getLogger("automations.pantry")

GEMINI_MODEL = resolve_gemini_model()

COL_COUNT = 4

SHEET_HEADERS = [
    "Item Name",
    "Category",
    "Storage Location",
    "Count",
    "Container Type",
    "Unit Size",
    "Reorder Status",
    "Expiration Date",
    "Notes",
]

# Allowed values from Google Sheets data-validation dropdowns.
CATEGORIES = [
    "Grains & Rice",
    "Oils & Condiments",
    "Canned Goods",
    "Pasta & Noodles",
    "Baking",
    "Spreads & Jams",
    "Seasonings & Spices",
    "Beverages",
]

STORAGE_LOCATIONS = [
    "Kitchen Cabinet",
    "Sofa Storage",
    "Basement",
    "Washroom Cabinet",
]

CONTAINER_TYPES = [
    "Bottles",
    "Tins",
    "Boxes",
    "Packages",
    "Jars",
]

DEFAULTS = {
    "Item Name": "",
    "Category": "Canned Goods",
    "Storage Location": "Kitchen Cabinet",
    "Count": 1,
    "Container Type": "Packages",
    "Unit Size": "N/A",
    "Reorder Status": "OK",
    "Expiration Date": "N/A",
    "Notes": "",
}

BAD_ITEM_NAMES = {
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

# Casual chat must never become pantry rows.
CHAT_MESSAGES = {
    "hi",
    "hii",
    "hiii",
    "hello",
    "hey",
    "heya",
    "hiya",
    "yo",
    "sup",
    "hola",
    "hallo",
    "thanks",
    "thank you",
    "thx",
    "ty",
    "ok",
    "okay",
    "k",
    "kk",
    "bye",
    "goodbye",
    "good morning",
    "good night",
    "good evening",
    "gm",
    "gn",
    "test",
    "testing",
    "help",
    "start",
    "please",
    "yes",
    "no",
    "yep",
    "nope",
    "cool",
    "nice",
    "lol",
    "haha",
}

BOT_COMMANDS = [
    BotCommand("start", "Show help and commands"),
    BotCommand("help", "Show help and commands"),
    BotCommand("add", "Add item — /add pasta"),
    BotCommand("search", "Find items — /search honey"),
    BotCommand("edit", "Edit count/delete — /edit honey"),
    BotCommand("list", "Show recent pantry items"),
    BotCommand("cancel", "Cancel the current add"),
]

# Draft add conversation (stored in context.user_data).
PENDING_ADD_KEY = "pending_add"

# Ask for these when the user/Gemini did not explicitly provide them.
CLARIFY_FIELDS = [
    "Count",
    "Category",
    "Storage Location",
    "Container Type",
    "Unit Size",
    "Expiration Date",
]

FIELD_CHOICES: dict[str, list[str]] = {
    "Category": CATEGORIES,
    "Storage Location": STORAGE_LOCATIONS,
    "Container Type": CONTAINER_TYPES,
    "Count": ["1", "2", "3", "4", "5", "6", "8", "10"],
}

FIELD_PROMPTS = {
    "Count": "How many do you have?",
    "Category": "Which category?",
    "Storage Location": "Where is it stored?",
    "Container Type": "What container type?",
    "Unit Size": "What unit size? (e.g. `500g`, `1L`, or `N/A`)",
    "Expiration Date": (
        "What is the expiration date?\n"
        "Send `YYYY-MM-DD` (e.g. `2028-07-07`), `July 2028`, or tap Skip."
    ),
}

FIELD_CALLBACK_KEYS = {
    "Count": "cnt",
    "Category": "cat",
    "Storage Location": "loc",
    "Container Type": "ctr",
}
CALLBACK_KEY_TO_FIELD = {v: k for k, v in FIELD_CALLBACK_KEYS.items()}

EXTRACTION_SYSTEM_INSTRUCTION = (
    "Extract pantry item details into a JSON object with keys: Item Name, "
    "Category, Storage Location, Count, Container Type, Unit Size, "
    "Reorder Status, Expiration Date, Notes.\n"
    "Item Name MUST be a real product/food name from the user text or image. "
    "Never use Unknown, N/A, or placeholders for Item Name.\n"
    f"Category MUST be exactly one of: {', '.join(CATEGORIES)}.\n"
    f"Storage Location MUST be exactly one of: {', '.join(STORAGE_LOCATIONS)}. "
    "Default to 'Kitchen Cabinet' unless the user/image clearly specifies another.\n"
    f"Container Type MUST be exactly one of: {', '.join(CONTAINER_TYPES)}.\n"
    "Convert dates into YYYY-MM-DD format if provided, otherwise 'N/A'. "
    "Do not invent dropdown values outside these lists."
)

ITEM_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "Item Name": {"type": "string"},
        "Category": {"type": "string", "enum": CATEGORIES},
        "Storage Location": {"type": "string", "enum": STORAGE_LOCATIONS},
        "Count": {"type": "integer"},
        "Container Type": {"type": "string", "enum": CONTAINER_TYPES},
        "Unit Size": {"type": "string"},
        "Reorder Status": {"type": "string"},
        "Expiration Date": {"type": "string"},
        "Notes": {"type": "string"},
    },
    "required": [
        "Item Name",
        "Category",
        "Storage Location",
        "Count",
        "Container Type",
        "Unit Size",
        "Reorder Status",
        "Expiration Date",
        "Notes",
    ],
}


def _coerce_choice(value: Any, allowed: list[str], default: str) -> str:
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


def normalize_expiration(value: Any) -> str | None:
    """
    Normalize an expiration value to YYYY-MM-DD or N/A.

    Returns None when the input cannot be parsed (caller should re-prompt).
    """
    from datetime import date, datetime

    text = str(value or "").strip()
    if not text:
        return None
    if text.casefold() in {
        "n/a",
        "na",
        "none",
        "null",
        "unknown",
        "skip",
        "-",
        "no",
        "n.a.",
    }:
        return "N/A"

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[./]", "-", text)

    for fmt in ("%Y-%m-%d", "%Y-%m", "%d-%m-%Y", "%m-%d-%Y", "%d-%m-%y", "%m-%y"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt in {"%Y-%m", "%m-%y"}:
                # Month-only → last day of month is unknown; use day 01.
                return parsed.strftime("%Y-%m-01")
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # "July 7, 2028" / "7 July 2028" / "July 2028"
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
        r"\bexp(?:iry|ires|iration)?\.?\s*[:=]?\s*([A-Za-z]+\s+\d{4})",
        r"\b(\d{4}-\d{2}-\d{2})\b",
        r"\b([A-Za-z]+\s+\d{1,2},?\s+\d{4})\b",
        r"\b([A-Za-z]+\s+\d{4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        normalized = normalize_expiration(match.group(1))
        if normalized and normalized != "N/A":
            return normalized
    return None


def is_real_expiration(value: Any) -> bool:
    text = str(value or "").strip()
    if not text or text.upper() in {"N/A", "NA", "NONE", "NULL", "UNKNOWN", ""}:
        return False
    return normalize_expiration(text) not in (None, "N/A")


def _is_bad_name(value: Any) -> bool:
    return str(value or "").strip().casefold() in BAD_ITEM_NAMES


def is_non_item_message(text: str) -> bool:
    """True for greetings/chat that should not create inventory rows."""
    cleaned = re.sub(r"[!?.,🙂😀😊👋🙏❤️]+", " ", (text or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip().casefold()
    if not cleaned:
        return True
    if cleaned in CHAT_MESSAGES:
        return True
    if len(cleaned) <= 1:
        return True
    if not re.search(r"[a-z0-9]", cleaned):
        return True
    return False


def _names_related(user_name: str, other_name: str) -> bool:
    """Reject Gemini inventing a different product than what the user typed."""
    user = str(user_name or "").casefold().strip()
    other = str(other_name or "").casefold().strip()
    if not user or not other:
        return False
    if user in other or other in user:
        return True
    user_tokens = {t for t in re.split(r"[^\w]+", user) if len(t) > 2}
    other_tokens = {t for t in re.split(r"[^\w]+", other) if len(t) > 2}
    if user_tokens and user_tokens & other_tokens:
        return True
    return SequenceMatcher(None, user, other).ratio() >= 0.55


def _guess_category(name: str) -> str:
    lowered = name.casefold()
    keywords = [
        ("Pasta & Noodles", ("pasta", "noodle", "spaghetti", "macaroni", "penne")),
        ("Grains & Rice", ("rice", "lentil", "bean", "grain", "quinoa", "flour")),
        ("Oils & Condiments", ("oil", "vinegar", "sauce", "ketchup", "mayo")),
        ("Canned Goods", ("can", "canned", "olive", "jalape")),
        ("Baking", ("sugar", "baking", "yeast", "cocoa")),
        ("Spreads & Jams", ("jam", "honey", "spread", "nutella", "butter")),
        ("Seasonings & Spices", ("spice", "cinnamon", "garlic", "pepper", "salt", "ginger")),
        ("Beverages", ("juice", "tea", "coffee", "water", "soda", "drink")),
    ]
    for category, words in keywords:
        if any(word in lowered for word in words):
            return category
    return DEFAULTS["Category"]


def item_from_text(text: str) -> dict[str, Any]:
    item, _provided = parse_add_text(text)
    return item


def parse_add_text(text: str) -> tuple[dict[str, Any], set[str]]:
    """
    Build a pantry draft from plain user text and note which fields were explicit.

    Supports: "pasta", "pasta 2", "2x pasta", "pasta x2", "add pasta",
    plus optional hints like "basement", "jar", "500g".
    """
    raw = re.sub(r"\s+", " ", (text or "").strip())
    raw = re.sub(r"^(?:add|/add)\s+", "", raw, flags=re.IGNORECASE).strip()
    if not raw:
        raise ValueError("Empty item description.")
    if is_non_item_message(raw):
        raise ValueError("That looks like a chat message, not a pantry item.")

    provided: set[str] = set()
    working = raw

    storage = None
    container = None
    unit_size = None

    for loc in STORAGE_LOCATIONS:
        if re.search(rf"\b{re.escape(loc)}\b", working, flags=re.IGNORECASE):
            storage = loc
            working = re.sub(
                rf"\b{re.escape(loc)}\b", " ", working, flags=re.IGNORECASE
            )
            provided.add("Storage Location")
            break

    for cont in CONTAINER_TYPES:
        singular = cont[:-1] if cont.endswith("s") else cont
        if re.search(rf"\b{re.escape(cont)}\b", working, flags=re.IGNORECASE) or re.search(
            rf"\b{re.escape(singular)}\b", working, flags=re.IGNORECASE
        ):
            container = cont
            working = re.sub(
                rf"\b{re.escape(cont)}\b|\b{re.escape(singular)}\b",
                " ",
                working,
                flags=re.IGNORECASE,
            )
            provided.add("Container Type")
            break

    unit_match = re.search(
        r"(\d+(?:[.,]\d+)?\s*(?:kg|g|l|ml|oz|lb)s?)\b",
        working,
        flags=re.IGNORECASE,
    )
    if unit_match:
        unit_size = unit_match.group(1).replace(" ", "")
        working = working[: unit_match.start()] + " " + working[unit_match.end() :]
        provided.add("Unit Size")

    expiration = extract_expiration_from_text(raw)
    if expiration:
        provided.add("Expiration Date")

    working = re.sub(r"\s+", " ", working).strip(" -,:;")
    count = 1
    name = working
    count_explicit = False

    patterns = [
        r"^(?P<count>\d+)\s*[x×]\s*(?P<name>.+)$",
        r"^(?P<name>.+?)\s*[x×]\s*(?P<count>\d+)$",
        r"^(?P<name>.+?)\s+(?P<count>\d+)$",
        r"^(?P<count>\d+)\s+(?P<name>.+)$",
    ]
    for pattern in patterns:
        match = re.fullmatch(pattern, working, flags=re.IGNORECASE)
        if match:
            name = match.group("name").strip(" -,:;")
            count = max(1, int(match.group("count")))
            count_explicit = True
            break

    name = name.strip(" -,:;")
    if _is_bad_name(name):
        raise ValueError("Could not determine an item name from that text.")

    item = dict(DEFAULTS)
    item["Item Name"] = name.title() if name.islower() else name
    item["Count"] = count
    item["Category"] = _guess_category(name)
    provided.add("Item Name")
    if count_explicit:
        provided.add("Count")
    if storage:
        item["Storage Location"] = storage
    if container:
        item["Container Type"] = container
    if unit_size:
        item["Unit Size"] = unit_size
    if expiration:
        item["Expiration Date"] = expiration

    lowered = raw.casefold()
    for category, words in [
        ("Grains & Rice", ("rice", "lentil", "grain")),
        ("Pasta & Noodles", ("pasta", "noodle", "spaghetti")),
        ("Oils & Condiments", ("oil", "vinegar", "sauce")),
        ("Canned Goods", ("canned",)),
        ("Baking", ("baking", "flour", "sugar")),
        ("Spreads & Jams", ("jam", "honey", "spread")),
        ("Seasonings & Spices", ("spice", "seasoning", "cinnamon", "garlic")),
        ("Beverages", ("juice", "tea", "coffee", "drink")),
    ]:
        if any(word in lowered for word in words):
            item["Category"] = category
            if category.casefold() in lowered:
                provided.add("Category")
            break

    return item, provided


def missing_clarify_fields(provided: set[str]) -> list[str]:
    return [field for field in CLARIFY_FIELDS if field not in provided]


def local_search(
    query: str, inventory: list[dict[str, Any]], *, limit: int = 12
) -> list[dict[str, Any]]:
    """Fuzzy/local inventory search — no Gemini required."""
    q = re.sub(r"\s+", " ", (query or "").strip().casefold())
    if not q or not inventory:
        return []

    tokens = [t for t in re.split(r"[^\w]+", q) if len(t) > 1]
    scored: list[tuple[float, dict[str, Any]]] = []

    for entry in inventory:
        name = str(entry.get("Item Name") or "")
        name_cf = name.casefold()
        category = str(entry.get("Category") or "").casefold()
        notes = str(entry.get("Notes") or "").casefold()

        if q == name_cf:
            score = 300.0
        elif q in name_cf:
            score = 220.0
        else:
            name_token_hits = sum(1 for t in tokens if t in name_cf)
            if name_token_hits:
                score = 150.0 + name_token_hits * 25.0
                score += SequenceMatcher(None, q, name_cf).ratio() * 40.0
            elif q in notes or any(t in notes for t in tokens):
                score = 90.0
            elif q in category or any(t in category for t in tokens):
                # Category-only matches rank lower than name hits.
                score = 70.0
            else:
                ratio = SequenceMatcher(None, q, name_cf).ratio()
                if ratio < 0.58:
                    continue
                score = 40.0 + (ratio * 80.0)

        scored.append((score, entry))

    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("Item Name") or "")))
    # Prefer real name hits; only fall back to category-only when nothing named matches.
    strong = [entry for score, entry in scored if score >= 150]
    if strong:
        return strong[:limit]
    return [entry for _, entry in scored[:limit]]


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_first(*names: str, default: str = "") -> tuple[str, str]:
    """Return (value, resolved_name) for the first non-empty env var."""
    for name in names:
        value = _env(name)
        if value:
            return value, name
    return default, names[0]


def require_config() -> dict[str, str]:
    """
    Load pantry-specific settings.

    Prefer `PANTRY_*` names so each automation can have its own bot/sheet.
    Unprefixed names are accepted as a fallback for local setups.
    """
    token, token_key = _env_first("PANTRY_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
    gemini_key, gemini_key_name = _env_first(
        "PANTRY_GEMINI_API_KEY", "GEMINI_API_KEY"
    )
    spreadsheet_id, sheet_key = _env_first(
        "PANTRY_SPREADSHEET_ID", "SPREADSHEET_ID"
    )
    credentials_path, creds_key = _env_first(
        "PANTRY_CREDENTIALS_PATH",
        "CREDENTIALS_PATH",
        default="credentials.json",
    )
    if not credentials_path:
        credentials_path = "credentials.json"
        creds_key = "PANTRY_CREDENTIALS_PATH"

    worksheet_name, _ = _env_first("PANTRY_WORKSHEET", default="")
    worksheet_gid_raw, _ = _env_first("PANTRY_WORKSHEET_GID", default="")
    worksheet_gid = ""
    if worksheet_gid_raw:
        try:
            worksheet_gid = str(int(worksheet_gid_raw))
        except ValueError as exc:
            raise RuntimeError(
                "PANTRY_WORKSHEET_GID must be an integer "
                f"(got {worksheet_gid_raw!r})"
            ) from exc

    missing = [
        key
        for key, value in (
            (token_key, token),
            (gemini_key_name, gemini_key),
            (sheet_key, spreadsheet_id),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + " (prefer PANTRY_* names per automation)"
        )
    if not os.path.isfile(credentials_path):
        raise RuntimeError(
            f"Service-account credentials file not found: {credentials_path} "
            f"(set {creds_key})"
        )

    return {
        "token": token,
        "gemini_key": gemini_key,
        "spreadsheet_id": spreadsheet_id,
        "credentials_path": credentials_path,
        "worksheet_name": worksheet_name,
        "worksheet_gid": worksheet_gid,
    }


def _escape_md(text: Any) -> str:
    value = str(text) if text is not None else ""
    return re.sub(r"([_*`\[\]])", r"\\\1", value)


class PantryBotRuntime:
    """Owns Telegram Application + Sheets/Gemini clients for one bot instance."""

    def __init__(self) -> None:
        self.genai_client: genai.Client | None = None
        self.worksheet: gspread.Worksheet | None = None
        self.application: Application | None = None

    def init_sheet(
        self,
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
            "Connected to spreadsheet %s (worksheet: %s)",
            spreadsheet_id,
            sheet.title,
        )
        return sheet

    def _normalize_item(
        self, raw: dict[str, Any], *, fallback_name: str | None = None
    ) -> dict[str, Any]:
        item: dict[str, Any] = {}
        for key in SHEET_HEADERS:
            value = raw.get(key, DEFAULTS[key])
            if value is None or (isinstance(value, str) and not value.strip()):
                value = DEFAULTS[key]
            item[key] = value

        if _is_bad_name(item.get("Item Name")) and fallback_name:
            cleaned = fallback_name.strip()
            if not _is_bad_name(cleaned):
                item["Item Name"] = cleaned.title() if cleaned.islower() else cleaned

        if _is_bad_name(item.get("Item Name")):
            raise ValueError("Item Name is missing or unknown.")

        try:
            item["Count"] = max(0, int(item["Count"]))
        except (TypeError, ValueError):
            item["Count"] = DEFAULTS["Count"]

        item["Category"] = _coerce_choice(
            item["Category"], CATEGORIES, _guess_category(str(item["Item Name"]))
        )
        item["Storage Location"] = _coerce_choice(
            item["Storage Location"],
            STORAGE_LOCATIONS,
            DEFAULTS["Storage Location"],
        )
        item["Container Type"] = _coerce_choice(
            item["Container Type"], CONTAINER_TYPES, DEFAULTS["Container Type"]
        )

        expiration = str(item["Expiration Date"]).strip()
        normalized_exp = normalize_expiration(expiration)
        item["Expiration Date"] = (
            normalized_exp if normalized_exp is not None else DEFAULTS["Expiration Date"]
        )

        if not str(item["Reorder Status"]).strip():
            item["Reorder Status"] = DEFAULTS["Reorder Status"]

        return item

    def sheet_append_item(self, item: dict[str, Any]) -> list[Any]:
        assert self.worksheet is not None
        row = [
            item["Item Name"],
            item["Category"],
            item["Storage Location"],
            item["Count"],
            item["Container Type"],
            item["Unit Size"],
            item["Reorder Status"],
            item["Expiration Date"],
            item["Notes"],
        ]
        self.worksheet.append_row(row, value_input_option="USER_ENTERED")
        return row

    def sheet_get_inventory(self) -> list[dict[str, Any]]:
        assert self.worksheet is not None
        records = self.worksheet.get_all_records()
        inventory: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            entry = dict(record)
            entry["row_index"] = index + 2
            inventory.append(entry)
        return inventory

    def sheet_get_count(self, row_index: int) -> int:
        assert self.worksheet is not None
        raw = self.worksheet.cell(row_index, COL_COUNT).value
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    def sheet_set_count(self, row_index: int, value: int) -> int:
        assert self.worksheet is not None
        new_val = max(0, int(value))
        self.worksheet.update_cell(row_index, COL_COUNT, new_val)
        return new_val

    def sheet_delete_row(self, row_index: int) -> None:
        assert self.worksheet is not None
        self.worksheet.delete_rows(row_index)

    def sheet_get_row_summary(self, row_index: int) -> dict[str, Any]:
        assert self.worksheet is not None
        values = self.worksheet.row_values(row_index)
        padded = values + [""] * (len(SHEET_HEADERS) - len(values))
        return {header: padded[i] for i, header in enumerate(SHEET_HEADERS)}

    async def gemini_extract_item(
        self,
        *,
        text: str | None = None,
        image_bytes: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> dict[str, Any]:
        assert self.genai_client is not None

        parts: list[Any] = []
        if image_bytes:
            parts.append(
                types.Part.from_bytes(data=image_bytes, mime_type=image_mime)
            )

        prompt_bits = [
            "Extract the pantry item details from the provided input.",
            "Item Name must be a concrete product/food name — never Unknown.",
        ]
        if text and text.strip():
            prompt_bits.append(f"User text/caption:\n{text.strip()}")
        elif not image_bytes:
            raise ValueError("Either text or image_bytes must be provided.")
        else:
            prompt_bits.append(
                "No caption was provided; infer details from the image."
            )

        parts.append("\n".join(prompt_bits))

        response = await generate_content_with_fallback(
            self.genai_client,
            contents=parts,
            config=types.GenerateContentConfig(
                system_instruction=EXTRACTION_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_json_schema=ITEM_RESPONSE_SCHEMA,
                temperature=0.2,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
            primary_model=GEMINI_MODEL,
        )

        raw_text = (response.text or "").strip()
        if not raw_text:
            raise RuntimeError("Gemini returned an empty extraction response.")

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Gemini returned invalid JSON: {raw_text[:300]}"
            ) from exc

        if isinstance(parsed, list):
            if not parsed:
                raise RuntimeError("Gemini returned an empty item list.")
            parsed = parsed[0]
        if not isinstance(parsed, dict):
            raise RuntimeError("Gemini JSON was not an object.")

        return self._normalize_item(parsed, fallback_name=text)

    async def resolve_item_from_text(
        self, text: str
    ) -> tuple[dict[str, Any], set[str]]:
        """Prefer Gemini enrichment; always fall back to local parsing.

        `provided` only includes fields the user explicitly typed — Gemini may
        suggest values, but missing clarifications are still asked.
        """
        local_item, provided = parse_add_text(text)
        try:
            gemini_item = await self.gemini_extract_item(text=text)
            gemini_name = str(gemini_item.get("Item Name") or "")
            if _is_bad_name(gemini_name) or not _names_related(
                str(local_item["Item Name"]), gemini_name
            ):
                gemini_item["Item Name"] = local_item["Item Name"]
                gemini_item["Count"] = local_item["Count"]
                gemini_item["Category"] = local_item["Category"]

            # Explicit user hints always win.
            for key in (
                "Count",
                "Storage Location",
                "Container Type",
                "Unit Size",
            ):
                if key in provided:
                    gemini_item[key] = local_item[key]
            if "Category" in provided:
                gemini_item["Category"] = local_item["Category"]
            if not gemini_item.get("Count"):
                gemini_item["Count"] = local_item["Count"]

            notes = str(gemini_item.get("Notes") or "")
            if re.search(
                r"no item|not provided|unknown|n/?a", notes, flags=re.IGNORECASE
            ):
                gemini_item["Notes"] = ""
            return gemini_item, provided | {"Item Name"}
        except Exception:
            logger.warning(
                "Gemini extract failed; using local parse for %r",
                text,
                exc_info=True,
            )
            return local_item, provided

    @staticmethod
    def format_item_markdown(
        item: dict[str, Any], *, row_index: int | None = None
    ) -> str:
        lines = [
            f"*📦 {_escape_md(item.get('Item Name', 'Item'))}*",
            f"• Category: `{_escape_md(item.get('Category', 'N/A'))}`",
            f"• Storage: `{_escape_md(item.get('Storage Location', 'N/A'))}`",
            f"• Count: `{_escape_md(item.get('Count', 0))}`",
            f"• Container: `{_escape_md(item.get('Container Type', 'N/A'))}`",
            f"• Unit Size: `{_escape_md(item.get('Unit Size', 'N/A'))}`",
            f"• Reorder: `{_escape_md(item.get('Reorder Status', 'OK'))}`",
            f"• Expires: `{_escape_md(item.get('Expiration Date', 'N/A'))}`",
        ]
        notes = item.get("Notes")
        if notes:
            lines.append(f"• Notes: _{_escape_md(notes)}_")
        if row_index is not None:
            lines.append(f"• Sheet row: `{row_index}`")
        return "\n".join(lines)

    @staticmethod
    def action_keyboard(row_index: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ Count +1", callback_data=f"inc_{row_index}"
                    ),
                    InlineKeyboardButton(
                        "➖ Count -1", callback_data=f"dec_{row_index}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "❌ Delete", callback_data=f"del_{row_index}"
                    ),
                ],
            ]
        )

    def build_application(self, token: str) -> Application:
        request = HTTPXRequest(
            connect_timeout=30.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=30.0,
        )
        application = (
            Application.builder()
            .token(token)
            .request(request)
            .get_updates_request(
                HTTPXRequest(
                    connect_timeout=30.0,
                    read_timeout=30.0,
                    write_timeout=30.0,
                    pool_timeout=30.0,
                )
            )
            .concurrent_updates(True)
            .build()
        )

        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("add", self.add_command))
        application.add_handler(CommandHandler("cancel", self.cancel_command))
        application.add_handler(CommandHandler("search", self.search_command))
        application.add_handler(CommandHandler("edit", self.edit_command))
        application.add_handler(CommandHandler("list", self.list_command))
        application.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text)
        )
        application.add_handler(CallbackQueryHandler(self.callback_handler))
        application.add_error_handler(self.error_handler)
        return application

    async def start_polling(self) -> None:
        config = require_config()
        self.genai_client = genai.Client(api_key=config["gemini_key"])
        self.worksheet = await asyncio.to_thread(
            self.init_sheet,
            config["credentials_path"],
            config["spreadsheet_id"],
            config.get("worksheet_name", ""),
            config.get("worksheet_gid", ""),
        )
        self.application = self.build_application(config["token"])

        await self.application.initialize()
        await self.application.bot.set_my_commands(BOT_COMMANDS)
        await self.application.start()
        assert self.application.updater is not None
        await self.application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES
        )
        logger.info("Pantry bot polling started (model=%s)", GEMINI_MODEL)

    async def stop_polling(self) -> None:
        if self.application is None:
            return

        updater = self.application.updater
        if updater and updater.running:
            await updater.stop()
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()
        self.application = None
        self.worksheet = None
        self.genai_client = None
        logger.info("Pantry bot stopped")

    # --- Telegram handlers -------------------------------------------------

    async def start_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return
        await update.message.reply_text(
            self._options_message(greeting=False),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.start_command(update, context)

    @staticmethod
    def _options_message(*, greeting: bool) -> str:
        header = (
            "👋 Hey! I'm your pantry bot.\n\n"
            if greeting
            else "*Pantry Inventory Bot*\n\n"
        )
        return (
            f"{header}"
            "Here's what I can do — pick one:\n\n"
            "• `/add <item>` — add something (e.g. `/add pasta 2`)\n"
            "• `/search <query>` — find items\n"
            "• `/edit <query>` — change count or delete\n"
            "• `/list` — show recent items\n"
            "• `/cancel` — cancel an add in progress\n"
            "• send a *photo* of a product to add it\n\n"
            "_Tip: I'll ask for missing details before saving._"
        )

    def _clear_pending(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop(PENDING_ADD_KEY, None)

    def _get_pending(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> dict[str, Any] | None:
        pending = context.user_data.get(PENDING_ADD_KEY)
        return pending if isinstance(pending, dict) else None

    def _set_pending(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        item: dict[str, Any],
        provided: set[str],
        awaiting: str | None,
    ) -> None:
        context.user_data[PENDING_ADD_KEY] = {
            "item": item,
            "provided": sorted(provided),
            "awaiting": awaiting,
        }

    def _clarify_keyboard(
        self, field: str, item: dict[str, Any]
    ) -> InlineKeyboardMarkup | None:
        if field == "Unit Size":
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Skip (N/A)", callback_data="cf:u:N/A"
                        )
                    ],
                    [InlineKeyboardButton("Cancel", callback_data="cf:cancel")],
                ]
            )

        if field == "Expiration Date":
            rows: list[list[InlineKeyboardButton]] = []
            suggested = str(item.get("Expiration Date", "")).strip()
            if is_real_expiration(suggested):
                normalized = normalize_expiration(suggested) or suggested
                rows.append(
                    [
                        InlineKeyboardButton(
                            f"✅ Keep suggested: {normalized}",
                            callback_data="cf:exp:keep",
                        )
                    ]
                )
            rows.append(
                [
                    InlineKeyboardButton(
                        "Skip (N/A)", callback_data="cf:exp:na"
                    )
                ]
            )
            rows.append(
                [InlineKeyboardButton("Cancel", callback_data="cf:cancel")]
            )
            return InlineKeyboardMarkup(rows)

        choices = FIELD_CHOICES.get(field) or []
        key = FIELD_CALLBACK_KEYS[field]
        rows = []
        row: list[InlineKeyboardButton] = []
        for idx, choice in enumerate(choices):
            row.append(
                InlineKeyboardButton(
                    choice, callback_data=f"cf:v:{key}:{idx}"
                )
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        suggested = str(item.get(field, "")).strip()
        if suggested in choices:
            idx = choices.index(suggested)
            rows.insert(
                0,
                [
                    InlineKeyboardButton(
                        f"✅ Keep suggested: {suggested}",
                        callback_data=f"cf:v:{key}:{idx}",
                    )
                ],
            )
        rows.append([InlineKeyboardButton("Cancel", callback_data="cf:cancel")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _confirm_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Save to pantry", callback_data="cf:save"
                    ),
                    InlineKeyboardButton("❌ Cancel", callback_data="cf:cancel"),
                ]
            ]
        )

    async def _prompt_next_clarification(
        self,
        message,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        edit: bool = False,
    ) -> None:
        pending = self._get_pending(context)
        if not pending:
            return
        item = pending["item"]
        provided = set(pending.get("provided") or [])
        missing = missing_clarify_fields(provided)

        if not missing:
            pending["awaiting"] = "confirm"
            self._set_pending(
                context, item=item, provided=provided, awaiting="confirm"
            )
            text = (
                "Please confirm this item:\n\n"
                + self.format_item_markdown(item)
                + "\n\nSave it to the pantry?"
            )
            markup = self._confirm_keyboard()
            if edit:
                await message.edit_text(
                    text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
                )
            else:
                await message.reply_text(
                    text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
                )
            return

        field = missing[0]
        pending["awaiting"] = field
        self._set_pending(
            context, item=item, provided=provided, awaiting=field
        )
        prompt = (
            f"Almost there for *{_escape_md(item.get('Item Name', 'item'))}*.\n"
            f"{FIELD_PROMPTS.get(field, field)}\n\n"
            f"_Draft so far:_\n{self.format_item_markdown(item)}"
        )
        markup = self._clarify_keyboard(field, item)
        if edit:
            await message.edit_text(
                prompt, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
            )
        else:
            await message.reply_text(
                prompt, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
            )

    async def _begin_add_flow(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        item: dict[str, Any],
        provided: set[str],
        status_message,
    ) -> None:
        provided = set(provided) | {"Item Name"}
        self._set_pending(
            context, item=item, provided=provided, awaiting=None
        )
        missing = missing_clarify_fields(provided)
        if not missing:
            self._set_pending(
                context, item=item, provided=provided, awaiting="confirm"
            )
            await status_message.edit_text(
                "Please confirm this item:\n\n"
                + self.format_item_markdown(item)
                + "\n\nSave it to the pantry?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._confirm_keyboard(),
            )
            return

        await status_message.edit_text(
            f"Got *{_escape_md(item.get('Item Name'))}*. "
            f"I need {len(missing)} more detail(s) before saving.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await self._prompt_next_clarification(
            update.effective_message, context, edit=False
        )

    async def _apply_field_answer(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        field: str,
        raw_value: str,
    ) -> str | None:
        pending = self._get_pending(context)
        if not pending:
            return "Nothing to update — start with `/add <item>`."
        item = pending["item"]
        provided = set(pending.get("provided") or [])
        value = raw_value.strip()
        if not value:
            return "Please send a value, or tap a button."

        if field == "Count":
            match = re.search(r"\d+", value)
            if not match:
                return "Send a number for count (e.g. `2`)."
            item["Count"] = max(0, int(match.group(0)))
        elif field == "Category":
            item["Category"] = _coerce_choice(
                value, CATEGORIES, item.get("Category") or DEFAULTS["Category"]
            )
        elif field == "Storage Location":
            item["Storage Location"] = _coerce_choice(
                value,
                STORAGE_LOCATIONS,
                item.get("Storage Location") or DEFAULTS["Storage Location"],
            )
        elif field == "Container Type":
            item["Container Type"] = _coerce_choice(
                value,
                CONTAINER_TYPES,
                item.get("Container Type") or DEFAULTS["Container Type"],
            )
        elif field == "Unit Size":
            item["Unit Size"] = value
        elif field == "Expiration Date":
            if value.casefold() in {"keep", "suggested"}:
                normalized = normalize_expiration(item.get("Expiration Date"))
            else:
                normalized = normalize_expiration(value)
            if normalized is None:
                return (
                    "Could not read that date. Try `2028-07-07`, `July 2028`, "
                    "or `N/A`."
                )
            item["Expiration Date"] = normalized
        else:
            return f"Unexpected field: {field}"

        provided.add(field)
        self._set_pending(
            context, item=item, provided=provided, awaiting=None
        )
        return None

    async def _save_pending_item(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> dict[str, Any]:
        pending = self._get_pending(context)
        if not pending:
            raise RuntimeError("No pending item to save.")
        item = pending["item"]
        await asyncio.to_thread(self.sheet_append_item, item)
        self._clear_pending(context)
        return item

    async def cancel_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return
        if self._get_pending(context):
            self._clear_pending(context)
            await update.message.reply_text("Cancelled. Nothing was saved.")
        else:
            await update.message.reply_text("Nothing in progress to cancel.")

    async def add_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return
        text = " ".join(context.args).strip() if context.args else ""
        if not text:
            await update.message.reply_text(
                "Usage: `/add <item>`\nExamples:\n"
                "• `/add pasta`\n"
                "• `/add pasta 2`\n"
                "• `/add 2x olive oil kitchen cabinet`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        status = await update.message.reply_text("➕ Preparing item…")
        try:
            self._clear_pending(context)
            item, provided = await self.resolve_item_from_text(text)
            await self._begin_add_flow(
                update, context, item=item, provided=provided, status_message=status
            )
        except Exception:
            logger.exception("Add command failed for %r", text)
            self._clear_pending(context)
            await status.edit_text(
                "❌ Could not start that add. Try `/add pasta` or "
                "`/add pasta 2`."
            )

    async def _download_best_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bytes:
        assert update.message and update.message.photo
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buffer = await file.download_as_bytearray()
        return bytes(buffer)

    async def handle_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return

        status = await update.message.reply_text(
            "🔍 Analyzing photo with Gemini…"
        )
        caption = update.message.caption or ""

        try:
            image_bytes = await self._download_best_photo(update, context)
            provided: set[str] = set()
            try:
                item = await self.gemini_extract_item(
                    text=caption or None, image_bytes=image_bytes
                )
                # Photo extraction can fill details; still clarify blanks.
                provided.add("Item Name")
                if caption.strip():
                    _cap_item, cap_provided = parse_add_text(caption)
                    provided |= cap_provided
                    for key in cap_provided:
                        if key in _cap_item:
                            item[key] = _cap_item[key]
                unit = str(item.get("Unit Size") or "").strip()
                if unit and unit.upper() not in {"N/A", "NA", "UNKNOWN", ""}:
                    provided.add("Unit Size")
                if is_real_expiration(item.get("Expiration Date")):
                    item["Expiration Date"] = (
                        normalize_expiration(item["Expiration Date"])
                        or item["Expiration Date"]
                    )
                    provided.add("Expiration Date")
                if item.get("Category") in CATEGORIES:
                    provided.add("Category")
                if item.get("Storage Location") in STORAGE_LOCATIONS and (
                    caption
                    and any(
                        loc.casefold() in caption.casefold()
                        for loc in STORAGE_LOCATIONS
                    )
                ):
                    provided.add("Storage Location")
                if item.get("Container Type") in CONTAINER_TYPES:
                    provided.add("Container Type")
                if item.get("Count") not in (None, "", 0):
                    # Count from photo alone is a guess — only trust caption count.
                    if "Count" in provided:
                        pass
                    else:
                        # leave Count for clarification unless caption had it
                        pass
            except Exception:
                if caption.strip():
                    logger.warning(
                        "Gemini photo extract failed; falling back to caption",
                        exc_info=True,
                    )
                    item, provided = parse_add_text(caption)
                else:
                    raise
            await self._begin_add_flow(
                update,
                context,
                item=item,
                provided=provided,
                status_message=status,
            )
        except Exception:
            logger.exception("Photo item creation failed")
            self._clear_pending(context)
            await status.edit_text(
                "❌ Could not extract that photo. "
                "Try a clearer image, a caption, or `/add <item>`."
            )

    async def handle_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Free text is conversation — answers clarifications, else shows options."""
        if not update.message or not update.message.text:
            return

        text = update.message.text.strip()
        pending = self._get_pending(context)
        if pending:
            awaiting = pending.get("awaiting")
            if awaiting == "confirm":
                lowered = text.casefold()
                if lowered in {"yes", "y", "save", "ok", "okay"}:
                    try:
                        item = await self._save_pending_item(context)
                    except Exception:
                        logger.exception("Failed saving confirmed item")
                        await update.message.reply_text(
                            "❌ Save failed. Please try `/add` again."
                        )
                        return
                    await update.message.reply_text(
                        "✅ *Item added to pantry*\n\n"
                        + self.format_item_markdown(item),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                if lowered in {"no", "n", "cancel", "stop"}:
                    self._clear_pending(context)
                    await update.message.reply_text(
                        "Cancelled. Nothing was saved."
                    )
                    return
                await update.message.reply_text(
                    "Please tap *Save* / *Cancel*, or reply `yes` / `no`.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=self._confirm_keyboard(),
                )
                return

            if awaiting in CLARIFY_FIELDS:
                error = await self._apply_field_answer(context, awaiting, text)
                if error:
                    await update.message.reply_text(error)
                    return
                await self._prompt_next_clarification(
                    update.message, context, edit=False
                )
                return

        greeting = is_non_item_message(text) or bool(
            re.match(
                r"^(hi+|hello|hey|howdy|what'?s up|what u do|what do you do|"
                r"who are you|help me)\b",
                text,
                flags=re.IGNORECASE,
            )
        )
        await update.message.reply_text(
            self._options_message(greeting=greeting),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _reply_search_results(
        self,
        update: Update,
        query: str,
        *,
        heading: str,
    ) -> None:
        assert update.message
        status = await update.message.reply_text(
            f"🔎 Searching pantry for *{_escape_md(query)}*…",
            parse_mode=ParseMode.MARKDOWN,
        )

        try:
            inventory = await asyncio.to_thread(self.sheet_get_inventory)
            matches = local_search(query, inventory)
        except Exception:
            logger.exception("Search failed for query=%r", query)
            await status.edit_text("❌ Search failed while reading the sheet.")
            return

        if not matches:
            await status.edit_text(
                f"No matches found for *{_escape_md(query)}*.\n"
                f"Try `/add {_escape_md(query)}` to create it.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await status.edit_text(
            f"{heading}\nFound *{len(matches)}* match(es) for "
            f"*{_escape_md(query)}*:",
            parse_mode=ParseMode.MARKDOWN,
        )

        for entry in matches:
            row_index = int(entry["row_index"])
            await update.message.reply_text(
                self.format_item_markdown(entry, row_index=row_index),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.action_keyboard(row_index),
            )

    async def search_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return

        query = " ".join(context.args).strip() if context.args else ""
        if not query:
            await update.message.reply_text(
                "Usage: `/search <item_name_or_category>`\n"
                "Example: `/search pasta`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await self._reply_search_results(
            update, query, heading="Search results"
        )

    async def edit_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return

        query = " ".join(context.args).strip() if context.args else ""
        if not query:
            await update.message.reply_text(
                "Usage: `/edit <item>`\n"
                "Example: `/edit pasta`\n"
                "Then use ➕ / ➖ / ❌ on the result.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await self._reply_search_results(
            update,
            query,
            heading="Edit mode — use the buttons to change count or delete",
        )

    async def list_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return

        status = await update.message.reply_text("📋 Loading recent items…")
        try:
            inventory = await asyncio.to_thread(self.sheet_get_inventory)
        except Exception:
            logger.exception("List failed")
            await status.edit_text("❌ Could not read the pantry sheet.")
            return

        if not inventory:
            await status.edit_text("Pantry is empty. Try `/add pasta`.")
            return

        recent = inventory[-15:]
        lines = [f"*Recent items* ({len(inventory)} total):\n"]
        for entry in reversed(recent):
            name = _escape_md(entry.get("Item Name", "Item"))
            count = _escape_md(entry.get("Count", "?"))
            category = _escape_md(entry.get("Category", ""))
            lines.append(f"• *{name}* — `{count}` _{category}_")

        await status.edit_text(
            "\n".join(lines)
            + "\n\nUse `/search <name>` or `/edit <name>` to manage one.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def callback_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not query.data:
            return

        await query.answer()
        data = query.data

        if data.startswith("cf:"):
            try:
                if data == "cf:cancel":
                    self._clear_pending(context)
                    await query.edit_message_text(
                        "Cancelled. Nothing was saved."
                    )
                    return

                if data == "cf:save":
                    item = await self._save_pending_item(context)
                    await query.edit_message_text(
                        "✅ *Item added to pantry*\n\n"
                        + self.format_item_markdown(item),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return

                if data.startswith("cf:u:"):
                    value = data[5:] or "N/A"
                    error = await self._apply_field_answer(
                        context, "Unit Size", value
                    )
                    if error:
                        await query.edit_message_text(error)
                        return
                    await self._prompt_next_clarification(
                        query.message, context, edit=True
                    )
                    return

                if data.startswith("cf:exp:"):
                    action = data.split(":", 2)[-1]
                    if action == "keep":
                        value = "keep"
                    else:
                        value = "N/A"
                    error = await self._apply_field_answer(
                        context, "Expiration Date", value
                    )
                    if error:
                        await query.edit_message_text(error)
                        return
                    await self._prompt_next_clarification(
                        query.message, context, edit=True
                    )
                    return

                match = re.fullmatch(r"cf:v:([a-z]+):(\d+)", data)
                if match:
                    key, idx_s = match.groups()
                    field = CALLBACK_KEY_TO_FIELD.get(key)
                    if not field:
                        await query.edit_message_text("❌ Unknown field.")
                        return
                    choices = FIELD_CHOICES.get(field) or []
                    idx = int(idx_s)
                    if idx < 0 or idx >= len(choices):
                        await query.edit_message_text("❌ Invalid choice.")
                        return
                    error = await self._apply_field_answer(
                        context, field, choices[idx]
                    )
                    if error:
                        await query.edit_message_text(error)
                        return
                    await self._prompt_next_clarification(
                        query.message, context, edit=True
                    )
                    return

                await query.edit_message_text("❌ Unknown action.")
            except Exception:
                logger.exception("Clarify callback failed: %s", data)
                self._clear_pending(context)
                try:
                    await query.edit_message_text(
                        "❌ Something went wrong. Please `/add` again."
                    )
                except Exception:
                    logger.debug("Could not edit clarify callback", exc_info=True)
            return

        match = re.fullmatch(r"(inc|dec|del)_(\d+)", data)
        if not match:
            await query.edit_message_text("❌ Unknown action.")
            return

        action, row_str = match.groups()
        row_index = int(row_str)

        try:
            if action in {"inc", "dec"}:
                current = await asyncio.to_thread(self.sheet_get_count, row_index)
                delta = 1 if action == "inc" else -1
                new_val = await asyncio.to_thread(
                    self.sheet_set_count, row_index, current + delta
                )
                item = await asyncio.to_thread(
                    self.sheet_get_row_summary, row_index
                )

                verb = "+1" if action == "inc" else "-1"
                header = f"✅ Count updated to `{new_val}` ({verb})\n\n"
                await query.edit_message_text(
                    header
                    + self.format_item_markdown(item, row_index=row_index),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=self.action_keyboard(row_index),
                )
                return

            item = await asyncio.to_thread(self.sheet_get_row_summary, row_index)
            await asyncio.to_thread(self.sheet_delete_row, row_index)
            await query.edit_message_text(
                (
                    f"🗑 *Deleted* {_escape_md(item.get('Item Name', 'item'))} "
                    f"(sheet row `{row_index}`)."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            logger.exception("Callback action failed: %s", data)
            try:
                await query.edit_message_text(
                    "❌ That action failed. The row may no longer exist — "
                    "try `/search` or `/edit` again."
                )
            except Exception:
                logger.debug(
                    "Could not edit callback message after failure",
                    exc_info=True,
                )

    async def error_handler(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        logger.error(
            "Unhandled exception while processing update",
            exc_info=context.error,
        )
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "⚠️ Something went wrong processing that request. "
                    "Please try again."
                )
            except Exception:
                logger.debug(
                    "Failed to send error notice to user", exc_info=True
                )

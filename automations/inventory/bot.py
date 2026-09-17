"""
Household inventory Telegram bot — Google Sheets + Gemini.

One bot covers pantry groceries and medicine/supplements in a single sheet.
Adding an item is a conversation: describe it (text or photo), confirm a
one-message recap, correct anything by just typing, tap Save. There is no
step-by-step field wizard. Managed by `InventoryAutomation`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from difflib import SequenceMatcher
from typing import Any

from google import genai
from google.genai import types
from telegram import BotCommand, InlineKeyboardMarkup, Update
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

from automations.gemini_util import generate_content_with_fallback, resolve_gemini_model
from automations.inventory_sheet import (
    BASE_BAD_NAMES,
    CATEGORIES,
    PACKAGE_TYPES,
    SHEET_HEADERS,
    STORAGE_LOCATIONS,
    InventorySheet,
    action_keyboard,
    blank_if_placeholder,
    coerce_choice,
    confirm_keyboard,
    escape_md,
    extract_expiration_from_text,
    format_item_markdown,
    is_bad_name,
    normalize_expiration,
    parse_json_response,
    split_size,
)

logger = logging.getLogger("automations.inventory")

GEMINI_MODEL = resolve_gemini_model()

DEFAULTS: dict[str, Any] = {
    "name": "",
    "brand": "",
    "category": "Canned Goods",
    "location": "Kitchen Cabinet",
    "package_type": "Pack",
    "package_count": 1,
    "units_per_package": "",
    "size_value": "",
    "size_unit": "",
    "expiry_date": "",
    "notes": "",
}

DRAFT_FIELDS = [h for h in SHEET_HEADERS if h not in ("item_id", "last_updated")]

BAD_ITEM_NAMES = BASE_BAD_NAMES | {"medicine", "supplement", "food", "grocery"}

# Casual chat must never become inventory rows.
CHAT_MESSAGES = {
    "hi", "hii", "hiii", "hello", "hey", "heya", "hiya", "yo", "sup",
    "hola", "hallo", "thanks", "thank you", "thx", "ty", "ok", "okay",
    "k", "kk", "bye", "goodbye", "good morning", "good night", "good evening",
    "gm", "gn", "test", "testing", "help", "start", "please", "yes", "no",
    "yep", "nope", "cool", "nice", "lol", "haha",
}

BOT_COMMANDS = [
    BotCommand("start", "Show help and commands"),
    BotCommand("help", "Show help and commands"),
    BotCommand("add", "Add an item — /add 2 rolled oats, or /add nexpro 20mg"),
    BotCommand("search", "Find items — /search pasta or /search headache"),
    BotCommand("edit", "Edit count/delete — /edit pasta"),
    BotCommand("list", "Show recent items"),
    BotCommand("cancel", "Cancel the current add"),
]

PENDING_ADD_KEY = "pending_add"

EXTRACTION_SYSTEM_INSTRUCTION = (
    "Extract one household inventory item — a pantry/grocery item OR a "
    "medicine/supplement — into a JSON object with keys: name, brand, "
    "category, location, package_type, package_count, units_per_package, "
    "size_value, size_unit, expiry_date, notes.\n"
    "name MUST be a real, concrete product or medicine name in English "
    "(translate from the user's language or label text if needed). For a "
    "branded medicine use the label's product name (e.g. 'Nexpro-20 "
    "Tablets'); if only the generic/active ingredient is known, use that "
    "(e.g. 'Pantoprazol'). For groceries use the plain English product name "
    "(e.g. 'Chickpeas', 'Sella Basmati Rice'). Never use Unknown, N/A, or a "
    "placeholder.\n"
    "brand = manufacturer/company if identifiable from the text or label "
    "(e.g. 'Freshona', 'Aristo', 'K-Classic'); else leave blank.\n"
    f"category MUST be exactly one of: {', '.join(CATEGORIES)}. Use "
    "'Medicine' for drugs/OTC treatments, 'Supplement' for vitamins/herbal/"
    "wellness products, and the closest grocery category for food/pantry "
    "items — never invent a new category.\n"
    f"location MUST be exactly one of: {', '.join(STORAGE_LOCATIONS)}. "
    "Default to 'Kitchen Cabinet' for groceries and 'Washroom Cabinet' for "
    "medicine/supplements unless the text clearly says otherwise.\n"
    f"package_type MUST be exactly one of: {', '.join(PACKAGE_TYPES)} "
    "(blister packs/strips → 'Tablet Strip'; loose pills in a bottle → "
    "'Bottle'; canned food → 'Can'; jarred food → 'Jar'; boxed → 'Box').\n"
    "package_count = number of packs/strips/cans/bottles on hand (integer, "
    "default 1 if not stated).\n"
    "units_per_package = tablets/capsules/bottles per pack if known "
    "(e.g. tablets per strip); else leave blank (never write N/A).\n"
    "size_value/size_unit = the strength or size split into a plain number "
    "and its unit (e.g. '20 mg' -> size_value 20, size_unit mg; '500g' -> "
    "size_value 500, size_unit g); leave both blank if unknown.\n"
    "notes: for medicine/supplement, briefly say what it treats/is used for "
    "(symptoms/conditions) — infer from the drug/brand if the label doesn't "
    "spell it out; for groceries, leave blank unless there is something "
    "notable to record.\n"
    "expiry_date: convert to YYYY-MM-DD if provided, otherwise leave blank.\n"
    "Never write 'N/A' anywhere — use an empty string for unknown optional "
    "fields."
)

ITEM_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "brand": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
        "location": {"type": "string", "enum": STORAGE_LOCATIONS},
        "package_type": {"type": "string", "enum": PACKAGE_TYPES},
        "package_count": {"type": "integer"},
        "units_per_package": {"type": "string"},
        "size_value": {"type": "string"},
        "size_unit": {"type": "string"},
        "expiry_date": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": DRAFT_FIELDS,
}

REFINE_SYSTEM_INSTRUCTION = (
    EXTRACTION_SYSTEM_INSTRUCTION
    + "\n\nYou will be given the current draft item as JSON, plus a user "
    "correction or addition in plain text. Return the corrected full item "
    "JSON with the same keys, changing only what the correction implies and "
    "leaving every other field exactly as it was in the draft."
)

SEARCH_SYSTEM_INSTRUCTION = (
    "You help match a household inventory (groceries + medicine/supplements) "
    "to a user query.\n"
    "The query may be:\n"
    "1) a product/brand/medicine name (e.g. 'Nexpro', 'Chickpeas'),\n"
    "2) an active ingredient / formula (e.g. 'paracetamol', 'pantoprazole'),\n"
    "3) a grocery category (e.g. 'pasta', 'canned goods'), or\n"
    "4) a symptom or condition (e.g. 'headache', 'acid reflux', 'fever').\n"
    "Use name, brand, category, and notes (what a medicine is used for) to "
    "find the best matches. Only return items that are actually in the "
    "inventory list below — never invent items.\n"
    'Respond with JSON: {"matches": [{"row_index": <int>, "reason": '
    '<short why it matches>}]}. row_index must be the Google Sheets 1-based '
    "row number from the inventory. If nothing matches, return "
    '{"matches": []}.'
)


def _guess_category(name: str) -> str:
    lowered = (name or "").casefold()
    keywords = [
        ("Medicine", (
            "medicine", "tablet", "capsule", "syrup", "paracetamol", "ibuprofen",
            "pantoprazol", "pantoprazole", "nexpro", "aspirin", "antibiotic",
            "ointment", "drops",
        )),
        ("Supplement", (
            "vitamin", "supplement", "omega", "probiotic", "magnesium", "zinc",
            "multivitamin", "collagen",
        )),
        ("Pasta & Noodles", ("pasta", "noodle", "spaghetti", "macaroni", "penne")),
        ("Grains & Rice", ("rice", "lentil", "bean", "grain", "quinoa", "flour", "oat")),
        ("Canned Goods", ("canned", "olive", "jalape", "tuna", "corn")),
        ("Seasonings & Spices", ("spice", "cinnamon", "garlic", "pepper", "salt", "ginger", "honey")),
        ("Beverages", ("juice", "tea", "coffee", "water", "soda", "drink")),
    ]
    for category, words in keywords:
        if any(re.search(rf"\b{re.escape(word)}", lowered) for word in words):
            return category
    if re.search(r"\bcan\b", lowered):
        return "Canned Goods"
    return DEFAULTS["category"]


def _default_location_for_category(category: str) -> str:
    if category in {"Medicine", "Supplement"}:
        return "Washroom Cabinet"
    return str(DEFAULTS["location"])


def _is_bad_name(value: Any) -> bool:
    return is_bad_name(value, BAD_ITEM_NAMES)


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


def parse_add_text(text: str) -> tuple[dict[str, Any], set[str]]:
    """Lightweight offline parse for `/add …` text (used as a Gemini fallback,
    and to detect explicit user hints that should override Gemini's guess)."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    raw = re.sub(r"^(?:add|/add)\s+", "", raw, flags=re.IGNORECASE).strip()
    provided: set[str] = set()
    item = dict(DEFAULTS)
    if not raw:
        return item, provided

    working = raw

    for loc in STORAGE_LOCATIONS:
        if re.search(rf"\b{re.escape(loc)}\b", working, flags=re.IGNORECASE):
            item["location"] = loc
            working = re.sub(rf"\b{re.escape(loc)}\b", " ", working, flags=re.IGNORECASE)
            provided.add("location")
            break

    size_match = re.search(
        r"(\d+(?:[.,]\d+)?\s*(?:kg|g|mg|mcg|l|ml|oz|lb)s?)\b", working, flags=re.IGNORECASE
    )
    if size_match:
        value, unit = split_size(size_match.group(1))
        item["size_value"], item["size_unit"] = value, unit
        working = working[: size_match.start()] + " " + working[size_match.end() :]
        provided.add("size_value")

    expiration = extract_expiration_from_text(raw)
    if expiration:
        item["expiry_date"] = expiration
        provided.add("expiry_date")
        # Strip expiry phrases/dates so they don't pollute the name or count.
        expiry_strip_patterns = [
            r"\bexp(?:iry|ires|iration)?\.?\s*[:=]?\s*[A-Za-z]+\s+\d{1,2},?\s+\d{4}\b",
            r"\bexp(?:iry|ires|iration)?\.?\s*[:=]?\s*\d{4}-\d{2}(?:-\d{2})?\b",
            r"\bexp(?:iry|ires|iration)?\.?\s*[:=]?\s*[A-Za-z]+\s+\d{4}\b",
            r"\b\d{4}-\d{2}(?:-\d{2})?\b",
            r"\b[A-Za-z]+\s+\d{1,2},?\s+\d{4}\b",
            r"\b[A-Za-z]+\s+\d{4}\b",
        ]
        for pattern in expiry_strip_patterns:
            working = re.sub(pattern, " ", working, flags=re.IGNORECASE)

    count_explicit = False
    count = 1
    explicit_count = re.search(
        r"\b(\d+)\s*(?:packs?|strips?|boxes?|cans?|bottles?|jars?)\b",
        working,
        flags=re.IGNORECASE,
    )
    if explicit_count:
        count = max(1, int(explicit_count.group(1)))
        count_explicit = True
        working = working[: explicit_count.start()] + " " + working[explicit_count.end() :]

    working = re.sub(r"\s+", " ", working).strip(" -,:;")
    name = working
    # Only treat numbers as counts when tied to x/× — bare trailing years must
    # not become package_count (e.g. "rice 2025").
    patterns = [
        r"^(?P<count>\d+)\s*[x×]\s*(?P<name>.+)$",
        r"^(?P<name>.+?)\s*[x×]\s*(?P<count>\d+)$",
    ]
    for pattern in patterns:
        match = re.fullmatch(pattern, working, flags=re.IGNORECASE)
        if match:
            name = match.group("name").strip(" -,:;")
            if not count_explicit:
                count = max(1, int(match.group("count")))
                count_explicit = True
            break

    name = name.strip(" -,:;")
    if name and not _is_bad_name(name):
        item["name"] = name.title() if name.islower() else name
        item["category"] = _guess_category(name)
        if "location" not in provided:
            item["location"] = _default_location_for_category(item["category"])
        provided.add("name")
    if count_explicit:
        item["package_count"] = count
        provided.add("package_count")

    return item, provided


def local_search(
    query: str, inventory: list[dict[str, Any]], *, limit: int = 12
) -> list[dict[str, Any]]:
    """Fallback name/brand/category/notes search when Gemini is unavailable."""
    q = re.sub(r"\s+", " ", (query or "").strip().casefold())
    if not q or not inventory:
        return []

    tokens = [t for t in re.split(r"[^a-z0-9]+", q) if t]
    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in inventory:
        name = str(entry.get("name") or "").casefold()
        brand = str(entry.get("brand") or "").casefold()
        category = str(entry.get("category") or "").casefold()
        notes = str(entry.get("notes") or "").casefold()

        score = 0.0
        if q == name:
            score += 300
        elif q in name:
            score += 200
        if q in brand:
            score += 120
        if q in notes:
            score += 120
        if q in category:
            score += 70
        for token in tokens:
            if token in name:
                score += 40
            if token in notes:
                score += 25
            if token in brand:
                score += 20
        score += 40 * SequenceMatcher(None, q, name).ratio()
        if score >= 40:
            scored.append((score, entry))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in scored[:limit]]


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_first(*names: str, default: str = "") -> tuple[str, str]:
    for name in names:
        value = _env(name)
        if value:
            return value, name
    return default, names[0]


def _parse_user_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning(
                "Ignoring invalid Telegram user id in INVENTORY_ALLOWED_USER_IDS: %r", part
            )
    return ids


def require_config() -> dict[str, Any]:
    """Load inventory bot settings (`INVENTORY_*` preferred; legacy pantry as fallback)."""
    token, token_key = _env_first(
        "INVENTORY_TELEGRAM_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "PANTRY_TELEGRAM_BOT_TOKEN",
    )
    gemini_key, gemini_key_name = _env_first("INVENTORY_GEMINI_API_KEY", "GEMINI_API_KEY")
    spreadsheet_id, sheet_key = _env_first(
        "INVENTORY_SPREADSHEET_ID",
        "SPREADSHEET_ID",
        "PANTRY_SPREADSHEET_ID",
    )
    credentials_path, creds_key = _env_first(
        "INVENTORY_CREDENTIALS_PATH", "CREDENTIALS_PATH", default="credentials.json"
    )
    if not credentials_path:
        credentials_path = "credentials.json"
        creds_key = "INVENTORY_CREDENTIALS_PATH"

    # Do not fall back to PANTRY_/MEDICINE_ worksheet gids — those tabs use the
    # pre-merge column schema and would silently corrupt writes.
    worksheet_name, _ = _env_first("INVENTORY_WORKSHEET", default="")
    worksheet_gid_raw, _ = _env_first("INVENTORY_WORKSHEET_GID", default="")
    worksheet_gid = ""
    if worksheet_gid_raw:
        try:
            worksheet_gid = str(int(worksheet_gid_raw))
        except ValueError as exc:
            raise RuntimeError(
                f"INVENTORY_WORKSHEET_GID must be an integer (got {worksheet_gid_raw!r})"
            ) from exc

    allowed_ids_raw, _ = _env_first("INVENTORY_ALLOWED_USER_IDS", default="")
    allowed_user_ids = _parse_user_ids(allowed_ids_raw)
    if not allowed_user_ids:
        logger.warning(
            "INVENTORY_ALLOWED_USER_IDS is not set — anyone who finds this bot "
            "can use it. Set it to your Telegram numeric user id(s), comma-"
            "separated, to restrict access."
        )

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
            + " (prefer INVENTORY_* names)"
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
        "allowed_user_ids": allowed_user_ids,
    }


class InventoryBotRuntime:
    """Owns Telegram Application + Sheets/Gemini clients for the inventory bot."""

    def __init__(self) -> None:
        self.genai_client: genai.Client | None = None
        self.sheet: InventorySheet | None = None
        self.application: Application | None = None
        self.allowed_user_ids: set[int] = set()

    def _normalize_item(
        self, raw: dict[str, Any], *, fallback_name: str | None = None
    ) -> dict[str, Any]:
        item: dict[str, Any] = {}
        for key in DRAFT_FIELDS:
            value = raw.get(key, DEFAULTS[key])
            if value is None or (isinstance(value, str) and not value.strip()):
                value = DEFAULTS[key]
            item[key] = value

        if _is_bad_name(item.get("name")) and fallback_name:
            cleaned = fallback_name.strip()
            if not _is_bad_name(cleaned):
                item["name"] = cleaned.title() if cleaned.islower() else cleaned

        item["brand"] = blank_if_placeholder(item.get("brand"))

        try:
            item["package_count"] = max(0, int(item["package_count"]))
        except (TypeError, ValueError):
            item["package_count"] = DEFAULTS["package_count"]

        item["category"] = coerce_choice(
            item["category"], CATEGORIES, _guess_category(str(item.get("name", "")))
        )
        item["location"] = coerce_choice(
            item["location"], STORAGE_LOCATIONS, DEFAULTS["location"]
        )
        item["package_type"] = coerce_choice(
            item["package_type"], PACKAGE_TYPES, DEFAULTS["package_type"]
        )

        units_per_package = blank_if_placeholder(item.get("units_per_package"))
        if units_per_package:
            digits = re.search(r"\d+", units_per_package)
            item["units_per_package"] = digits.group(0) if digits else units_per_package
        else:
            item["units_per_package"] = ""

        size_value = blank_if_placeholder(item.get("size_value"))
        size_unit = blank_if_placeholder(item.get("size_unit"))
        if not size_value and size_unit and re.search(r"\d", size_unit):
            size_value, size_unit = split_size(size_unit)
        item["size_value"] = size_value
        item["size_unit"] = size_unit

        expiration = blank_if_placeholder(item.get("expiry_date"))
        if not expiration:
            item["expiry_date"] = ""
        else:
            normalized_exp = normalize_expiration(expiration)
            item["expiry_date"] = normalized_exp if normalized_exp is not None else ""

        item["notes"] = blank_if_placeholder(item.get("notes"))

        return item

    def sheet_append_item(self, item: dict[str, Any]) -> list[Any]:
        assert self.sheet is not None
        return self.sheet.append_item(item)

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
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))

        prompt_bits = ["Extract the household inventory item from the provided input."]
        if text and text.strip():
            prompt_bits.append(f"User text/caption:\n{text.strip()}")
        elif not image_bytes:
            raise ValueError("Either text or image_bytes must be provided.")
        else:
            prompt_bits.append("No caption was provided; infer details from the image/label.")
        parts.append("\n".join(prompt_bits))

        response = await generate_content_with_fallback(
            self.genai_client,
            contents=parts,
            config=types.GenerateContentConfig(
                system_instruction=EXTRACTION_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_json_schema=ITEM_RESPONSE_SCHEMA,
                temperature=0.2,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
            primary_model=GEMINI_MODEL,
        )

        parsed = parse_json_response(response.text)
        if isinstance(parsed, list):
            if not parsed:
                raise RuntimeError("Gemini returned an empty item list.")
            parsed = parsed[0]
        if not isinstance(parsed, dict):
            raise RuntimeError("Gemini JSON was not an object.")

        return self._normalize_item(parsed, fallback_name=text)

    async def gemini_refine_item(self, draft: dict[str, Any], correction: str) -> dict[str, Any]:
        """Merge a free-text correction into the current draft via Gemini."""
        assert self.genai_client is not None
        prompt = (
            "Current draft JSON:\n"
            + json.dumps(draft, ensure_ascii=False)
            + "\n\nUser's correction/addition:\n"
            + correction.strip()
        )
        response = await generate_content_with_fallback(
            self.genai_client,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=REFINE_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_json_schema=ITEM_RESPONSE_SCHEMA,
                temperature=0.1,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
            primary_model=GEMINI_MODEL,
        )
        parsed = parse_json_response(response.text)
        if isinstance(parsed, list):
            parsed = parsed[0] if parsed else {}
        return self._normalize_item(parsed, fallback_name=draft.get("name"))

    def apply_local_correction(self, item: dict[str, Any], text: str) -> dict[str, Any]:
        """Best-effort correction without Gemini: count/location/expiry hints only."""
        updated = dict(item)
        # Require an explicit quantity cue — never treat years/strengths as counts.
        count_match = re.search(
            r"(?:\b(?:make\s+it|count|qty|quantity)\s*)?(\d+)\s*(?:packs?|strips?|boxes?|cans?|bottles?|jars?)\b"
            r"|\b(?:make\s+it|count|qty|quantity)\s+(\d+)\b",
            text,
            flags=re.IGNORECASE,
        )
        if count_match:
            raw_count = count_match.group(1) or count_match.group(2)
            updated["package_count"] = max(0, int(raw_count))
        for loc in STORAGE_LOCATIONS:
            if loc.casefold() in text.casefold():
                updated["location"] = loc
                break
        for cat in CATEGORIES:
            if cat.casefold() in text.casefold():
                updated["category"] = cat
                break
        expiration = extract_expiration_from_text(text)
        if expiration:
            updated["expiry_date"] = expiration
        return self._normalize_item(updated)

    async def resolve_item_from_text(self, text: str) -> tuple[dict[str, Any], bool]:
        """Returns (item, has_name)."""
        local_item, provided = parse_add_text(text)
        try:
            gemini_item = await self.gemini_extract_item(text=text)
        except Exception:
            logger.exception("Gemini extract failed; using local parse")
            return self._normalize_item(local_item), "name" in provided

        merged = dict(gemini_item)
        # Prefer local hints for structured fields, but keep Gemini's name unless
        # local is clearly better — local names often retain expiry residue.
        override_keys = provided - {"name"}
        for key in override_keys:
            merged[key] = local_item[key]

        if _is_bad_name(merged.get("name")) and not _is_bad_name(local_item.get("name")):
            merged["name"] = local_item["name"]

        merged = self._normalize_item(merged, fallback_name=text)
        return merged, not _is_bad_name(merged.get("name"))

    async def gemini_search_matches(
        self, query: str, inventory: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        assert self.genai_client is not None
        if not inventory:
            return []

        compact = [
            {
                "row_index": entry["row_index"],
                "name": entry.get("name", ""),
                "brand": entry.get("brand", ""),
                "category": entry.get("category", ""),
                "notes": entry.get("notes", ""),
                "expiry_date": entry.get("expiry_date", ""),
            }
            for entry in inventory
        ]
        prompt = (
            f"User query: {query}\n\nInventory JSON:\n"
            f"{json.dumps(compact, ensure_ascii=False)}"
        )
        response = await generate_content_with_fallback(
            self.genai_client,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SEARCH_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                temperature=0.1,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
            primary_model=GEMINI_MODEL,
        )

        raw_text = (response.text or "").strip()
        if not raw_text:
            return []
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("Inventory search JSON parse failed: %s", raw_text[:300])
            return []

        reasons: dict[int, str] = {}
        for match in parsed.get("matches", []):
            if not isinstance(match, dict) or "row_index" not in match:
                continue
            try:
                row_index = int(match["row_index"])
            except (TypeError, ValueError):
                continue
            reasons[row_index] = str(match.get("reason") or "").strip()

        results: list[dict[str, Any]] = []
        for entry in inventory:
            row_index = int(entry["row_index"])
            if row_index in reasons:
                enriched = dict(entry)
                if reasons[row_index]:
                    enriched["match_reason"] = reasons[row_index]
                results.append(enriched)
        return results

    def build_application(self, token: str, allowed_user_ids: set[int]) -> Application:
        request = HTTPXRequest(
            connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=30.0
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
            .concurrent_updates(False)
            .build()
        )

        auth = filters.User(user_id=allowed_user_ids) if allowed_user_ids else None

        def guarded(base_filter: filters.BaseFilter | None = None) -> filters.BaseFilter | None:
            if auth is None:
                return base_filter
            return auth if base_filter is None else base_filter & auth

        application.add_handler(CommandHandler("start", self.start_command, filters=auth))
        application.add_handler(CommandHandler("help", self.help_command, filters=auth))
        application.add_handler(CommandHandler("add", self.add_command, filters=auth))
        application.add_handler(CommandHandler("cancel", self.cancel_command, filters=auth))
        application.add_handler(CommandHandler("search", self.search_command, filters=auth))
        application.add_handler(CommandHandler("edit", self.edit_command, filters=auth))
        application.add_handler(CommandHandler("list", self.list_command, filters=auth))
        application.add_handler(MessageHandler(guarded(filters.PHOTO), self.handle_photo))
        application.add_handler(
            MessageHandler(guarded(filters.TEXT & ~filters.COMMAND), self.handle_text)
        )
        if auth is not None:
            application.add_handler(MessageHandler(~auth, self._reject_unauthorized))
        application.add_handler(CallbackQueryHandler(self.callback_handler))
        application.add_error_handler(self.error_handler)
        return application

    async def _reject_unauthorized(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if update.effective_message:
            await update.effective_message.reply_text("🔒 This is a private bot.")

    def _is_authorized(self, update: Update) -> bool:
        if not self.allowed_user_ids:
            return True
        return bool(update.effective_user and update.effective_user.id in self.allowed_user_ids)

    async def start_polling(self) -> None:
        config = require_config()
        self.genai_client = genai.Client(api_key=config["gemini_key"])
        worksheet = await asyncio.to_thread(
            InventorySheet.connect,
            config["credentials_path"],
            config["spreadsheet_id"],
            config.get("worksheet_name", ""),
            config.get("worksheet_gid", ""),
        )
        self.sheet = InventorySheet(worksheet)
        self.allowed_user_ids = config["allowed_user_ids"]
        self.application = self.build_application(config["token"], self.allowed_user_ids)

        await self.application.initialize()
        await self.application.bot.set_my_commands(BOT_COMMANDS)
        await self.application.start()
        assert self.application.updater is not None
        await self.application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Inventory bot polling started (model=%s)", GEMINI_MODEL)

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
        self.sheet = None
        self.genai_client = None
        logger.info("Inventory bot stopped")

    # --- Telegram handlers -------------------------------------------------

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        await update.message.reply_text(
            self._options_message(greeting=True), parse_mode=ParseMode.MARKDOWN
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.start_command(update, context)

    @staticmethod
    def _options_message(*, greeting: bool) -> str:
        intro = "👋 Hi — I'm your *Household Inventory* bot.\n\n" if greeting else ""
        return (
            f"{intro}"
            "Just tell me what you got — text or a photo works — and I'll "
            "ask if anything important is missing.\n\n"
            "• `/add <item>` — e.g. `/add 2 chickpeas` or `/add nexpro 20mg`\n"
            "• `/search <name|category|symptom>` — e.g. `/search pasta` or "
            "`/search headache`\n"
            "• `/edit <query>` — adjust count or delete\n"
            "• `/list` — show recent items\n"
            "• `/cancel` — cancel an in-progress add\n"
        )

    def _clear_pending(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop(PENDING_ADD_KEY, None)

    def _get_pending(self, context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
        pending = context.user_data.get(PENDING_ADD_KEY)
        return pending if isinstance(pending, dict) else None

    def _set_pending(
        self, context: ContextTypes.DEFAULT_TYPE, *, item: dict[str, Any], awaiting: str | None
    ) -> None:
        context.user_data[PENDING_ADD_KEY] = {"item": item, "awaiting": awaiting}

    async def _send_prompt(
        self, message, text: str, *, edit: bool, reply_markup: InlineKeyboardMarkup | None
    ) -> None:
        try:
            if edit:
                await message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
            else:
                await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        except Exception:
            logger.warning("Markdown prompt failed; retrying as plain text", exc_info=True)
            plain = text.replace("*", "").replace("`", "").replace("_", "")
            if edit:
                await message.edit_text(plain, reply_markup=reply_markup)
            else:
                await message.reply_text(plain, reply_markup=reply_markup)

    async def _show_recap(
        self, message, context: ContextTypes.DEFAULT_TYPE, *, edit: bool
    ) -> None:
        pending = self._get_pending(context)
        if not pending:
            return
        item = pending["item"]
        self._set_pending(context, item=item, awaiting="confirm")
        text = (
            "Got it — here's what I'll save. Anything to change, just tell "
            "me, or tap Save.\n\n" + format_item_markdown(item)
        )
        await self._send_prompt(message, text, edit=edit, reply_markup=confirm_keyboard())

    async def _begin_add_flow(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, item: dict[str, Any], has_name: bool
    ) -> None:
        if not has_name:
            self._set_pending(context, item=item, awaiting="name")
            await update.effective_message.reply_text(
                "What's this called?", parse_mode=ParseMode.MARKDOWN
            )
            return
        self._set_pending(context, item=item, awaiting=None)
        await self._show_recap(update.effective_message, context, edit=False)

    async def _save_pending_item(self, context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
        pending = self._get_pending(context)
        assert pending is not None
        item = self._normalize_item(pending["item"])
        await asyncio.to_thread(self.sheet_append_item, item)
        self._clear_pending(context)
        return item

    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        if self._get_pending(context):
            self._clear_pending(context)
            await update.message.reply_text("Cancelled. Nothing was saved.")
        else:
            await update.message.reply_text("Nothing in progress to cancel.")

    async def add_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        text = " ".join(context.args).strip() if context.args else ""
        if not text:
            await update.message.reply_text(
                "Usage: `/add <item>`\nExamples:\n"
                "• `/add 2 chickpeas`\n"
                "• `/add nexpro 20mg 2 strips`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        status = await update.message.reply_text("🔍 Preparing details…")
        try:
            self._clear_pending(context)
            item, has_name = await self.resolve_item_from_text(text)
        except Exception:
            logger.exception("Add failed for text=%r", text)
            await status.edit_text(
                "❌ Could not understand that. Try `/add <name>` with more detail."
            )
            return
        await status.delete()
        await self._begin_add_flow(update, context, item=item, has_name=has_name)

    async def _download_best_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bytes:
        assert update.message and update.message.photo
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buffer = await file.download_as_bytearray()
        return bytes(buffer)

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        status = await update.message.reply_text("🔍 Reading the photo with Gemini…")
        caption = update.message.caption or ""
        self._clear_pending(context)
        try:
            image_bytes = await self._download_best_photo(update, context)
            item = await self.gemini_extract_item(text=caption or None, image_bytes=image_bytes)
            if caption.strip():
                _, cap_provided = parse_add_text(caption)
                local_item, _ = parse_add_text(caption)
                for key in cap_provided:
                    if key != "name":
                        item[key] = local_item[key]
            has_name = not _is_bad_name(item.get("name"))
        except Exception:
            logger.exception("Photo extraction failed")
            await status.edit_text(
                "❌ Could not read that photo. Try a clearer image or `/add <name>`."
            )
            return
        await status.delete()
        await self._begin_add_flow(update, context, item=item, has_name=has_name)

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return
        text = update.message.text.strip()
        pending = self._get_pending(context)

        if pending:
            awaiting = pending.get("awaiting")

            if awaiting == "name":
                if is_non_item_message(text) or _is_bad_name(text):
                    await update.message.reply_text(
                        "I need an actual item name to continue, or `/cancel`."
                    )
                    return
                item = dict(pending["item"])
                item["name"] = text.title() if text.islower() else text
                item = self._normalize_item(item, fallback_name=text)
                self._set_pending(context, item=item, awaiting=None)
                await self._show_recap(update.message, context, edit=False)
                return

            if awaiting == "confirm":
                lowered = text.casefold()
                if lowered in {"yes", "y", "save", "ok", "okay"}:
                    try:
                        item = await self._save_pending_item(context)
                    except Exception:
                        logger.exception("Save pending item failed")
                        await update.message.reply_text("❌ Failed to save to Google Sheets.")
                        return
                    await update.message.reply_text(
                        "✅ *Saved*\n\n" + format_item_markdown(item), parse_mode=ParseMode.MARKDOWN
                    )
                    return
                if lowered in {"no", "n", "cancel", "stop"}:
                    self._clear_pending(context)
                    await update.message.reply_text("Cancelled. Nothing was saved.")
                    return

                # Anything else is a free-text correction to the draft.
                item = dict(pending["item"])
                status = await update.message.reply_text("✏️ Updating…")
                try:
                    updated = await self.gemini_refine_item(item, text)
                except Exception:
                    logger.warning("Gemini refine failed; using local correction", exc_info=True)
                    updated = self.apply_local_correction(item, text)
                self._set_pending(context, item=updated, awaiting="confirm")
                try:
                    await status.delete()
                except Exception:
                    logger.debug("Could not clear 'Updating…' status", exc_info=True)
                await self._show_recap(update.message, context, edit=False)
                return

        if is_non_item_message(text):
            await update.message.reply_text(
                self._options_message(greeting=True), parse_mode=ParseMode.MARKDOWN
            )
            return

        # No pending draft and this looks like an item — start an add
        # conversationally, without requiring the /add prefix.
        status = await update.message.reply_text("🔍 Preparing details…")
        try:
            item, has_name = await self.resolve_item_from_text(text)
        except Exception:
            logger.exception("Implicit add failed for text=%r", text)
            await status.edit_text(
                self._options_message(greeting=False), parse_mode=ParseMode.MARKDOWN
            )
            return
        await status.delete()
        await self._begin_add_flow(update, context, item=item, has_name=has_name)

    async def _reply_search_results(
        self, update: Update, *, query: str, matches: list[dict[str, Any]], status_message
    ) -> None:
        if not update.message:
            return
        if not matches:
            await status_message.edit_text(
                f"No matches for *{escape_md(query)}*.\n"
                "Try a name, brand, category, or symptom.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await status_message.edit_text(
            f"Found *{len(matches)}* match(es) for *{escape_md(query)}*:",
            parse_mode=ParseMode.MARKDOWN,
        )
        for entry in matches:
            row_index = int(entry["row_index"])
            await update.message.reply_text(
                format_item_markdown(entry, row_index=row_index),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=action_keyboard(row_index),
            )

    async def search_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        query = " ".join(context.args).strip() if context.args else ""
        if not query:
            await update.message.reply_text(
                "Usage: `/search <name|brand|category|symptom>`\n"
                "Examples: `/search pasta` · `/search headache` · `/search nexpro`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        status = await update.message.reply_text(f"🔎 Matching *{escape_md(query)}*…", parse_mode=ParseMode.MARKDOWN)
        try:
            assert self.sheet is not None
            inventory = await asyncio.to_thread(self.sheet.get_all_records)
            try:
                matches = await self.gemini_search_matches(query, inventory)
            except Exception:
                logger.exception("Gemini search failed; local fallback")
                matches = local_search(query, inventory)
            if not matches:
                matches = local_search(query, inventory)
        except Exception:
            logger.exception("Search failed for query=%r", query)
            await status.edit_text("❌ Search failed while reading the sheet or calling Gemini.")
            return

        await self._reply_search_results(update, query=query, matches=matches, status_message=status)

    async def edit_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        query = " ".join(context.args).strip() if context.args else ""
        if not query:
            await update.message.reply_text("Usage: `/edit <name or symptom>`", parse_mode=ParseMode.MARKDOWN)
            return
        context.args = query.split()
        await self.search_command(update, context)

    async def list_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        status = await update.message.reply_text("📋 Loading items…")
        try:
            assert self.sheet is not None
            inventory = await asyncio.to_thread(self.sheet.get_all_records)
        except Exception:
            logger.exception("List failed")
            await status.edit_text("❌ Could not read the inventory sheet.")
            return

        if not inventory:
            await status.edit_text("Inventory is empty.")
            return

        recent = inventory[-10:]
        await status.edit_text(
            f"Showing last *{len(recent)}* of *{len(inventory)}* items:", parse_mode=ParseMode.MARKDOWN
        )
        for entry in reversed(recent):
            row_index = int(entry["row_index"])
            await update.message.reply_text(
                format_item_markdown(entry, row_index=row_index),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=action_keyboard(row_index),
            )

    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        if not self._is_authorized(update):
            await query.answer("🔒 Not authorized.", show_alert=True)
            return
        data = query.data

        if data == "cf:cancel":
            await query.answer()
            self._clear_pending(context)
            await query.edit_message_text("Cancelled. Nothing was saved.")
            return

        if data == "cf:save":
            if not self._get_pending(context):
                await query.answer("Add session expired — start again.", show_alert=True)
                return
            await query.answer()
            try:
                item = await self._save_pending_item(context)
            except Exception:
                logger.exception("Callback save failed")
                await query.edit_message_text("❌ Failed to save to Google Sheets.")
                return
            await query.edit_message_text(
                "✅ *Saved*\n\n" + format_item_markdown(item), parse_mode=ParseMode.MARKDOWN
            )
            return

        await query.answer()

        match = re.fullmatch(r"(inc|dec|del)_(\d+)", data)
        if not match:
            await query.edit_message_text("❌ Unknown action.")
            return

        action, row_str = match.groups()
        row_index = int(row_str)
        assert self.sheet is not None

        try:
            if action in {"inc", "dec"}:
                current = await asyncio.to_thread(self.sheet.get_package_count, row_index)
                delta = 1 if action == "inc" else -1
                new_val = await asyncio.to_thread(self.sheet.set_package_count, row_index, current + delta)
                item = await asyncio.to_thread(self.sheet.row_summary, row_index)
                verb = "+1" if action == "inc" else "-1"
                header = f"✅ Count updated to `{new_val}` ({verb})\n\n"
                await query.edit_message_text(
                    header + format_item_markdown(item, row_index=row_index),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=action_keyboard(row_index),
                )
                return

            item = await asyncio.to_thread(self.sheet.row_summary, row_index)
            await asyncio.to_thread(self.sheet.delete_row, row_index)
            await query.edit_message_text(
                f"🗑 *Deleted* {escape_md(item.get('name', 'item'))} (sheet row `{row_index}`).",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            logger.exception("Callback action failed: %s", data)
            try:
                await query.edit_message_text(
                    "❌ That action failed. The row may no longer exist — try `/search` again."
                )
            except Exception:
                logger.debug("Could not edit callback message after failure", exc_info=True)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Unhandled exception while processing update", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "⚠️ Something went wrong processing that request. Please try again."
                )
            except Exception:
                logger.debug("Failed to send error notice to user", exc_info=True)

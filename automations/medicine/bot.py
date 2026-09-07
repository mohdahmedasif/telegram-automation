"""
Medicine inventory Telegram bot — Google Sheets + Gemini.

`/search` uses Gemini to match by drug name, formula, or symptoms
(using Notes / Formula / Item Name). Managed by `MedicineAutomation`.
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

logger = logging.getLogger("automations.medicine")

GEMINI_MODEL = "gemini-3.6-flash"

# A=1 Item Name … E=5 Count
COL_COUNT = 5

SHEET_HEADERS = [
    "Item Name",
    "Formula",
    "Storage Location",
    "Type",
    "Count",
    "Container Type",
    "Count per Units",
    "Unit Size",
    "Reorder Status",
    "Expiration Date",
    "Notes",
]

ITEM_TYPES = [
    "Medicine",
    "Supplement",
]

STORAGE_LOCATIONS = [
    "Washroom Cabinet",
    "Kitchen Cabinet",
    "Basement",
    "Sofa Storage",
]

CONTAINER_TYPES = [
    "Strips",
    "Bottles",
    "Boxes",
    "Tubes",
    "Jars",
]

DEFAULTS = {
    "Item Name": "",
    "Formula": "N/A",
    "Storage Location": "Washroom Cabinet",
    "Type": "Medicine",
    "Count": 1,
    "Container Type": "Strips",
    "Count per Units": "N/A",
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
    "medicine",
}

CHAT_MESSAGES = {
    "hi",
    "hii",
    "hello",
    "hey",
    "yo",
    "thanks",
    "thank you",
    "thx",
    "ty",
    "ok",
    "okay",
    "k",
    "bye",
    "goodbye",
    "good morning",
    "good night",
    "test",
    "help",
    "start",
    "yes",
    "no",
}

BOT_COMMANDS = [
    BotCommand("start", "Show help and commands"),
    BotCommand("help", "Show help and commands"),
    BotCommand("add", "Add medicine — /add nexpro"),
    BotCommand(
        "search",
        "Find by name or symptom — /search headache",
    ),
    BotCommand("edit", "Edit count/delete — /edit lorine"),
    BotCommand("list", "Show recent medicines"),
    BotCommand("cancel", "Cancel the current add"),
]

PENDING_ADD_KEY = "pending_add"

CLARIFY_FIELDS = [
    "Count",
    "Type",
    "Storage Location",
    "Container Type",
    "Unit Size",
    "Formula",
]

FIELD_CHOICES: dict[str, list[str]] = {
    "Type": ITEM_TYPES,
    "Storage Location": STORAGE_LOCATIONS,
    "Container Type": CONTAINER_TYPES,
    "Count": ["1", "2", "3", "4", "5", "6", "8", "10"],
}

FIELD_PROMPTS = {
    "Count": "How many packs/strips do you have?",
    "Type": "Medicine or Supplement?",
    "Storage Location": "Where is it stored?",
    "Container Type": "What container type?",
    "Unit Size": "What strength / unit size? (e.g. `20mg`, `500mg`, or `N/A`)",
    "Formula": "What is the active formula/ingredient? (or `N/A`)",
}

FIELD_CALLBACK_KEYS = {
    "Count": "cnt",
    "Type": "typ",
    "Storage Location": "loc",
    "Container Type": "ctr",
}
CALLBACK_KEY_TO_FIELD = {v: k for k, v in FIELD_CALLBACK_KEYS.items()}

EXTRACTION_SYSTEM_INSTRUCTION = (
    "Extract medicine / supplement details into a JSON object with keys: "
    "Item Name, Formula, Storage Location, Type, Count, Container Type, "
    "Count per Units, Unit Size, Reorder Status, Expiration Date, Notes.\n"
    "Item Name MUST be a real medicine or brand name from the user text or image. "
    "Never use Unknown, N/A, or placeholders for Item Name.\n"
    "Formula is the active ingredient(s), e.g. Paracetamol, Pantoprazole.\n"
    f"Type MUST be exactly one of: {', '.join(ITEM_TYPES)}.\n"
    f"Storage Location MUST be exactly one of: {', '.join(STORAGE_LOCATIONS)}. "
    "Default to 'Washroom Cabinet' unless specified.\n"
    f"Container Type MUST be exactly one of: {', '.join(CONTAINER_TYPES)}. "
    "Default to 'Strips' for blister packs.\n"
    "Count per Units = tablets/capsules per strip or pack if known, else 'N/A'.\n"
    "Unit Size = strength (e.g. 20mg, 500mg), else 'N/A'.\n"
    "Notes should briefly say what the medicine is used for (symptoms/conditions).\n"
    "Convert dates into YYYY-MM-DD if provided, otherwise 'N/A'."
)

ITEM_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "Item Name": {"type": "string"},
        "Formula": {"type": "string"},
        "Storage Location": {"type": "string", "enum": STORAGE_LOCATIONS},
        "Type": {"type": "string", "enum": ITEM_TYPES},
        "Count": {"type": "integer"},
        "Container Type": {"type": "string", "enum": CONTAINER_TYPES},
        "Count per Units": {"type": "string"},
        "Unit Size": {"type": "string"},
        "Reorder Status": {"type": "string"},
        "Expiration Date": {"type": "string"},
        "Notes": {"type": "string"},
    },
    "required": list(SHEET_HEADERS),
}

SEARCH_SYSTEM_INSTRUCTION = (
    "You help match a household medicine inventory to a user query.\n"
    "The query may be:\n"
    "1) a brand / item name (e.g. 'Nexpro', 'Doliprane'),\n"
    "2) an active ingredient / formula (e.g. 'paracetamol', 'pantoprazole'), or\n"
    "3) a symptom or condition (e.g. 'headache', 'fever', 'acid reflux', "
    "'allergy', 'constipation', 'bloating', 'pain').\n"
    "For symptoms, use Notes (what the medicine is for), Formula, Item Name, "
    "and Type to recommend suitable items already in the inventory. "
    "Only return items that reasonably treat or relate to the query — "
    "do not invent medicines that are not in the inventory list.\n"
    "Respond with JSON: "
    '{"matches": [{"row_index": <int>, "reason": <short why it matches>}]}. '
    "row_index must be the Google Sheets 1-based row number from the inventory. "
    "If nothing matches, return {\"matches\": []}."
)


def _coerce_choice(value: Any, allowed: list[str], default: str) -> str:
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


def _is_bad_name(value: Any) -> bool:
    return str(value or "").strip().casefold() in BAD_ITEM_NAMES


def is_non_item_message(text: str) -> bool:
    cleaned = re.sub(r"[!?.,🙂😀😊👋🙏❤️]+", " ", (text or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip().casefold()
    return cleaned in CHAT_MESSAGES


def _names_related(a: str, b: str) -> bool:
    left = re.sub(r"[^a-z0-9]+", " ", (a or "").casefold()).strip()
    right = re.sub(r"[^a-z0-9]+", " ", (b or "").casefold()).strip()
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.55


def parse_add_text(text: str) -> tuple[dict[str, Any], set[str]]:
    """Lightweight offline parse for `/add …` text."""
    raw = (text or "").strip()
    provided: set[str] = set()
    item = dict(DEFAULTS)

    if not raw:
        return item, provided

    count_match = re.search(r"\b(?:x|×)?\s*(\d+)\s*(?:packs?|strips?|boxes?)?\b", raw, re.I)
    # Prefer explicit "count N" / "N strips"
    explicit_count = re.search(
        r"\b(?:count|qty|quantity)\s*[:=]?\s*(\d+)\b", raw, re.I
    ) or re.search(r"\b(\d+)\s*(?:packs?|strips?|boxes?)\b", raw, re.I)
    if explicit_count:
        item["Count"] = max(1, int(explicit_count.group(1)))
        provided.add("Count")
    elif count_match and re.search(r"\b\d+\b", raw):
        # lone leading number like "2 nexpro"
        leading = re.match(r"^(\d+)\s+(.+)$", raw)
        if leading:
            item["Count"] = max(1, int(leading.group(1)))
            raw = leading.group(2).strip()
            provided.add("Count")

    for loc in STORAGE_LOCATIONS:
        if loc.casefold() in raw.casefold():
            item["Storage Location"] = loc
            provided.add("Storage Location")
            break

    for typ in ITEM_TYPES:
        if typ.casefold() in raw.casefold():
            item["Type"] = typ
            provided.add("Type")
            break

    for container in CONTAINER_TYPES:
        if container.casefold() in raw.casefold():
            item["Container Type"] = container
            provided.add("Container Type")
            break

    strength = re.search(r"\b(\d+(?:\.\d+)?\s*mg)\b", raw, re.I)
    if strength:
        item["Unit Size"] = strength.group(1).replace(" ", "")
        provided.add("Unit Size")

    name = raw
    for noise in STORAGE_LOCATIONS + ITEM_TYPES + CONTAINER_TYPES:
        name = re.sub(re.escape(noise), " ", name, flags=re.I)
    name = re.sub(r"\b(?:count|qty|quantity)\s*[:=]?\s*\d+\b", " ", name, flags=re.I)
    name = re.sub(r"\b\d+\s*(?:packs?|strips?|boxes?)\b", " ", name, flags=re.I)
    name = re.sub(r"^\d+\s+", "", name.strip())
    name = re.sub(r"\s+", " ", name).strip(" -,\t")
    if name and not _is_bad_name(name):
        item["Item Name"] = name.title() if name.islower() else name
        provided.add("Item Name")

    return item, provided


def missing_clarify_fields(provided: set[str]) -> list[str]:
    return [field for field in CLARIFY_FIELDS if field not in provided]


def local_search(
    query: str, inventory: list[dict[str, Any]], *, limit: int = 12
) -> list[dict[str, Any]]:
    """Fallback name/formula/notes token search when Gemini is unavailable."""
    q = (query or "").strip().casefold()
    if not q or not inventory:
        return []

    tokens = [t for t in re.split(r"[^a-z0-9]+", q) if t]
    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in inventory:
        name = str(entry.get("Item Name", "")).casefold()
        formula = str(entry.get("Formula", "")).casefold()
        notes = str(entry.get("Notes", "")).casefold()
        blob = f"{name} {formula} {notes}"
        score = 0.0
        if q in name:
            score += 200
        if q in formula:
            score += 160
        if q in notes:
            score += 120
        for token in tokens:
            if token in name:
                score += 40
            if token in formula:
                score += 35
            if token in notes:
                score += 25
        score += 40 * SequenceMatcher(None, q, name).ratio()
        score += 25 * SequenceMatcher(None, q, formula).ratio()
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


def require_config() -> dict[str, str]:
    """Load medicine-specific settings (`MEDICINE_*` preferred)."""
    token, token_key = _env_first(
        "MEDICINE_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"
    )
    gemini_key, gemini_key_name = _env_first(
        "MEDICINE_GEMINI_API_KEY", "GEMINI_API_KEY"
    )
    spreadsheet_id, sheet_key = _env_first(
        "MEDICINE_SPREADSHEET_ID", "SPREADSHEET_ID"
    )
    credentials_path, creds_key = _env_first(
        "MEDICINE_CREDENTIALS_PATH",
        "CREDENTIALS_PATH",
        default="credentials.json",
    )
    if not credentials_path:
        credentials_path = "credentials.json"
        creds_key = "MEDICINE_CREDENTIALS_PATH"

    worksheet_name, _ = _env_first("MEDICINE_WORKSHEET", default="")
    worksheet_gid_raw, _ = _env_first("MEDICINE_WORKSHEET_GID", default="")
    worksheet_gid = ""
    if worksheet_gid_raw:
        try:
            worksheet_gid = str(int(worksheet_gid_raw))
        except ValueError as exc:
            raise RuntimeError(
                "MEDICINE_WORKSHEET_GID must be an integer "
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
            + " (prefer MEDICINE_* names per automation)"
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


class MedicineBotRuntime:
    """Owns Telegram Application + Sheets/Gemini clients for medicine inventory."""

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

        item["Type"] = _coerce_choice(item["Type"], ITEM_TYPES, DEFAULTS["Type"])
        item["Storage Location"] = _coerce_choice(
            item["Storage Location"],
            STORAGE_LOCATIONS,
            DEFAULTS["Storage Location"],
        )
        item["Container Type"] = _coerce_choice(
            item["Container Type"], CONTAINER_TYPES, DEFAULTS["Container Type"]
        )

        expiration = str(item["Expiration Date"]).strip()
        if expiration.upper() in {"", "NONE", "NULL", "UNKNOWN"}:
            expiration = "N/A"
        item["Expiration Date"] = expiration

        if not str(item["Reorder Status"]).strip():
            item["Reorder Status"] = DEFAULTS["Reorder Status"]
        if not str(item["Formula"]).strip():
            item["Formula"] = DEFAULTS["Formula"]

        return item

    def sheet_append_item(self, item: dict[str, Any]) -> list[Any]:
        assert self.worksheet is not None
        row = [item[header] for header in SHEET_HEADERS]
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
            "Extract the medicine/supplement details from the provided input.",
        ]
        if text and text.strip():
            prompt_bits.append(f"User text/caption:\n{text.strip()}")
        elif not image_bytes:
            raise ValueError("Either text or image_bytes must be provided.")
        else:
            prompt_bits.append(
                "No caption was provided; infer details from the image/label."
            )

        parts.append("\n".join(prompt_bits))

        response = await self.genai_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=parts,
            config=types.GenerateContentConfig(
                system_instruction=EXTRACTION_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_json_schema=ITEM_RESPONSE_SCHEMA,
                temperature=0.2,
            ),
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
        local_item, provided = parse_add_text(text)
        try:
            gemini_item = await self.gemini_extract_item(text=text)
        except Exception:
            logger.exception("Gemini extract failed; using local parse")
            if _is_bad_name(local_item.get("Item Name")):
                raise
            return self._normalize_item(local_item), provided

        merged = dict(gemini_item)
        for key in provided:
            if key in local_item:
                merged[key] = local_item[key]

        if _is_bad_name(merged.get("Item Name")) or (
            local_item.get("Item Name")
            and not _names_related(
                str(merged.get("Item Name", "")),
                str(local_item.get("Item Name", "")),
            )
        ):
            if not _is_bad_name(local_item.get("Item Name")):
                merged["Item Name"] = local_item["Item Name"]

        return self._normalize_item(merged, fallback_name=text), provided

    async def gemini_search_matches(
        self,
        query: str,
        inventory: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Symptom / name / formula matching via Gemini.

        Uses Notes + Formula so queries like "headache" or "acid reflux"
        can find the right medicines.
        """
        assert self.genai_client is not None
        if not inventory:
            return []

        compact = [
            {
                "row_index": entry["row_index"],
                "Item Name": entry.get("Item Name", ""),
                "Formula": entry.get("Formula", ""),
                "Type": entry.get("Type", ""),
                "Unit Size": entry.get("Unit Size", ""),
                "Notes": entry.get("Notes", ""),
                "Expiration Date": entry.get("Expiration Date", ""),
            }
            for entry in inventory
        ]

        prompt = (
            f"User query: {query}\n\n"
            f"Inventory JSON:\n{json.dumps(compact, ensure_ascii=False)}"
        )

        response = await self.genai_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SEARCH_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )

        raw_text = (response.text or "").strip()
        if not raw_text:
            return []

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("Medicine search JSON parse failed: %s", raw_text[:300])
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

    def format_item_markdown(
        self, item: dict[str, Any], *, row_index: int | None = None
    ) -> str:
        lines = [
            f"*💊 {_escape_md(item.get('Item Name', 'Unknown'))}*",
            f"• Formula: `{_escape_md(item.get('Formula', 'N/A'))}`",
            f"• Type: `{_escape_md(item.get('Type', 'N/A'))}`",
            f"• Storage: `{_escape_md(item.get('Storage Location', 'N/A'))}`",
            f"• Count: `{_escape_md(item.get('Count', 0))}`",
            f"• Container: `{_escape_md(item.get('Container Type', 'N/A'))}`",
            f"• Per pack: `{_escape_md(item.get('Count per Units', 'N/A'))}`",
            f"• Strength: `{_escape_md(item.get('Unit Size', 'N/A'))}`",
            f"• Reorder: `{_escape_md(item.get('Reorder Status', 'OK'))}`",
            f"• Expires: `{_escape_md(item.get('Expiration Date', 'N/A'))}`",
        ]
        notes = item.get("Notes")
        if notes:
            lines.append(f"• Notes: _{_escape_md(notes)}_")
        reason = item.get("match_reason")
        if reason:
            lines.append(f"• Why matched: _{_escape_md(reason)}_")
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
        logger.info("Medicine bot polling started (model=%s)", GEMINI_MODEL)

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
        logger.info("Medicine bot stopped")

    # --- Telegram handlers -------------------------------------------------

    async def start_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return
        await update.message.reply_text(
            self._options_message(greeting=True),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.start_command(update, context)

    @staticmethod
    def _options_message(*, greeting: bool) -> str:
        intro = "Hi — this is your *Medicine Inventory* bot.\n\n" if greeting else ""
        return (
            f"{intro}"
            "Commands:\n"
            "• `/add <name>` — add a medicine (photo also works)\n"
            "• `/search <name or symptom>` — e.g. `/search headache` "
            "or `/search acid reflux`\n"
            "• `/edit <query>` — adjust count or delete\n"
            "• `/list` — show recent items\n"
            "• `/cancel` — cancel an in-progress add\n"
        )

    def _clear_pending(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop(PENDING_ADD_KEY, None)

    def _get_pending(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> dict[str, Any] | None:
        pending = context.user_data.get(PENDING_ADD_KEY)
        return pending if isinstance(pending, dict) else None

    def _set_pending(
        self, context: ContextTypes.DEFAULT_TYPE, pending: dict[str, Any]
    ) -> None:
        context.user_data[PENDING_ADD_KEY] = pending

    def _clarify_keyboard(self, field: str) -> InlineKeyboardMarkup | None:
        choices = FIELD_CHOICES.get(field)
        if not choices:
            return None
        key = FIELD_CALLBACK_KEYS.get(field)
        if not key:
            return None
        rows: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for idx, label in enumerate(choices):
            row.append(
                InlineKeyboardButton(
                    label, callback_data=f"cf:v:{key}:{idx}"
                )
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(
            [InlineKeyboardButton("Cancel", callback_data="cf:cancel")]
        )
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _confirm_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Save", callback_data="cf:save"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cf:cancel"),
                ]
            ]
        )

    async def _prompt_next_clarification(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        edit_message: bool = False,
    ) -> None:
        pending = self._get_pending(context)
        if not pending or not update.effective_message:
            return

        item = pending["item"]
        remaining = missing_clarify_fields(set(pending.get("provided", [])))
        if not remaining:
            pending["awaiting"] = "confirm"
            self._set_pending(context, pending)
            text = (
                "Please confirm this medicine:\n\n"
                + self.format_item_markdown(item)
                + "\n\nSave to the sheet?"
            )
            markup = self._confirm_keyboard()
        else:
            field = remaining[0]
            pending["awaiting"] = field
            self._set_pending(context, pending)
            prompt = FIELD_PROMPTS.get(field, f"Provide *{field}*:")
            text = (
                f"Adding *{_escape_md(item.get('Item Name', 'item'))}*\n\n"
                f"{prompt}"
            )
            markup = self._clarify_keyboard(field)

        if edit_message and update.callback_query:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
            )
        else:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
            )

    async def _begin_add_flow(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        item: dict[str, Any],
        provided: set[str],
    ) -> None:
        self._set_pending(
            context,
            {
                "item": item,
                "provided": list(provided),
                "awaiting": None,
            },
        )
        await self._prompt_next_clarification(update, context)

    async def _apply_field_answer(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        field: str,
        value: str,
    ) -> dict[str, Any]:
        pending = self._get_pending(context)
        assert pending is not None
        item = dict(pending["item"])
        provided = set(pending.get("provided", []))

        if field == "Count":
            try:
                item["Count"] = max(0, int(str(value).strip()))
            except ValueError as exc:
                raise ValueError("Count must be a number.") from exc
        elif field == "Type":
            item["Type"] = _coerce_choice(value, ITEM_TYPES, item["Type"])
        elif field == "Storage Location":
            item["Storage Location"] = _coerce_choice(
                value, STORAGE_LOCATIONS, item["Storage Location"]
            )
        elif field == "Container Type":
            item["Container Type"] = _coerce_choice(
                value, CONTAINER_TYPES, item["Container Type"]
            )
        elif field == "Unit Size":
            item["Unit Size"] = str(value).strip() or "N/A"
        elif field == "Formula":
            item["Formula"] = str(value).strip() or "N/A"
        else:
            item[field] = value

        provided.add(field)
        pending["item"] = self._normalize_item(item)
        pending["provided"] = list(provided)
        self._set_pending(context, pending)
        return pending

    async def _save_pending_item(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> dict[str, Any]:
        pending = self._get_pending(context)
        assert pending is not None
        item = self._normalize_item(pending["item"])
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
            await update.message.reply_text("Cancelled the current add.")
        else:
            await update.message.reply_text("Nothing to cancel.")

    async def add_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return
        text = " ".join(context.args).strip() if context.args else ""
        if not text:
            await update.message.reply_text(
                "Usage: `/add <medicine name>`\nExample: `/add nexpro 20mg`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        status = await update.message.reply_text(
            "🔍 Preparing medicine details…"
        )
        try:
            item, provided = await self.resolve_item_from_text(text)
        except Exception:
            logger.exception("Add failed for text=%r", text)
            await status.edit_text(
                "❌ Could not understand that medicine. "
                "Try `/add <brand name>` with strength if you know it."
            )
            return

        await status.delete()
        # User typed the name via /add
        provided = set(provided) | {"Item Name"}
        await self._begin_add_flow(update, context, item, provided)

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
            "🔍 Reading medicine label with Gemini…"
        )
        caption = update.message.caption or ""
        try:
            image_bytes = await self._download_best_photo(update, context)
            item = await self.gemini_extract_item(
                text=caption or None, image_bytes=image_bytes
            )
            provided: set[str] = {"Item Name"}
            if caption.strip():
                _, caption_provided = parse_add_text(caption)
                provided |= caption_provided
        except Exception:
            logger.exception("Photo medicine extraction failed")
            await status.edit_text(
                "❌ Could not read that label. Try a clearer photo or `/add <name>`."
            )
            return

        await status.delete()
        await self._begin_add_flow(update, context, item, provided)

    async def handle_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
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
                        logger.exception("Save pending medicine failed")
                        await update.message.reply_text(
                            "❌ Failed to save to Google Sheets."
                        )
                        return
                    await update.message.reply_text(
                        "✅ *Saved*\n\n" + self.format_item_markdown(item),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                if lowered in {"no", "n", "cancel", "stop"}:
                    self._clear_pending(context)
                    await update.message.reply_text("Cancelled.")
                    return
                await update.message.reply_text(
                    "Reply `yes` to save or `no` to cancel.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

            if awaiting in CLARIFY_FIELDS:
                try:
                    await self._apply_field_answer(context, awaiting, text)
                except ValueError as exc:
                    await update.message.reply_text(f"❌ {exc}")
                    return
                await self._prompt_next_clarification(update, context)
                return

        if is_non_item_message(text):
            await update.message.reply_text(
                self._options_message(greeting=True),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await update.message.reply_text(
            self._options_message(greeting=False),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _reply_search_results(
        self,
        update: Update,
        *,
        query: str,
        matches: list[dict[str, Any]],
        status_message,
    ) -> None:
        if not update.message:
            return
        if not matches:
            await status_message.edit_text(
                f"No matches for *{_escape_md(query)}*.\n"
                "Try a brand name, formula, or symptom "
                "(e.g. `headache`, `acid reflux`).",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await status_message.edit_text(
            f"Found *{len(matches)}* match(es) for *{_escape_md(query)}*:",
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
                "Usage: `/search <name|formula|symptom>`\n"
                "Examples: `/search headache` · `/search paracetamol` · `/search nexpro`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        status = await update.message.reply_text(
            f"🔎 Matching *{_escape_md(query)}* "
            "(name, formula, or symptom)…",
            parse_mode=ParseMode.MARKDOWN,
        )

        try:
            inventory = await asyncio.to_thread(self.sheet_get_inventory)
            try:
                matches = await self.gemini_search_matches(query, inventory)
            except Exception:
                logger.exception("Gemini medicine search failed; local fallback")
                matches = local_search(query, inventory)
            if not matches:
                matches = local_search(query, inventory)
        except Exception:
            logger.exception("Search failed for query=%r", query)
            await status.edit_text(
                "❌ Search failed while reading the sheet or calling Gemini."
            )
            return

        await self._reply_search_results(
            update, query=query, matches=matches, status_message=status
        )

    async def edit_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Same lookup as search, focused on count/delete actions."""
        if not update.message:
            return
        query = " ".join(context.args).strip() if context.args else ""
        if not query:
            await update.message.reply_text(
                "Usage: `/edit <name or symptom>`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        # Reuse search path
        context.args = query.split()
        await self.search_command(update, context)

    async def list_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return
        status = await update.message.reply_text("📋 Loading medicines…")
        try:
            inventory = await asyncio.to_thread(self.sheet_get_inventory)
        except Exception:
            logger.exception("List failed")
            await status.edit_text("❌ Could not read the medicine sheet.")
            return

        if not inventory:
            await status.edit_text("Inventory is empty.")
            return

        recent = inventory[-10:]
        await status.edit_text(
            f"Showing last *{len(recent)}* of *{len(inventory)}* items:",
            parse_mode=ParseMode.MARKDOWN,
        )
        for entry in reversed(recent):
            row_index = int(entry["row_index"])
            await update.message.reply_text(
                self.format_item_markdown(entry, row_index=row_index),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.action_keyboard(row_index),
            )

    async def callback_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not query.data:
            return

        await query.answer()
        data = query.data

        if data == "cf:cancel":
            self._clear_pending(context)
            await query.edit_message_text("Cancelled.")
            return

        if data == "cf:save":
            try:
                item = await self._save_pending_item(context)
            except Exception:
                logger.exception("Callback save failed")
                await query.edit_message_text("❌ Failed to save to Google Sheets.")
                return
            await query.edit_message_text(
                "✅ *Saved*\n\n" + self.format_item_markdown(item),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        choice_match = re.fullmatch(r"cf:v:([a-z]+):(\d+)", data)
        if choice_match:
            field_key, idx_str = choice_match.groups()
            field = CALLBACK_KEY_TO_FIELD.get(field_key)
            pending = self._get_pending(context)
            if not field or not pending:
                await query.edit_message_text("❌ No active add in progress.")
                return
            choices = FIELD_CHOICES.get(field, [])
            idx = int(idx_str)
            if idx < 0 or idx >= len(choices):
                await query.edit_message_text("❌ Invalid choice.")
                return
            await self._apply_field_answer(context, field, choices[idx])
            await self._prompt_next_clarification(
                update, context, edit_message=True
            )
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
                    "try `/search` again."
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

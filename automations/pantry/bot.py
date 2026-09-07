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

logger = logging.getLogger("automations.pantry")

GEMINI_MODEL = "gemini-3.6-flash"

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
]

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
    """
    Build a pantry row from plain user text without Gemini.

    Supports: "pasta", "pasta 2", "2x pasta", "pasta x2", "add pasta".
    """
    raw = re.sub(r"\s+", " ", (text or "").strip())
    raw = re.sub(r"^(?:add|/add)\s+", "", raw, flags=re.IGNORECASE).strip()
    if not raw:
        raise ValueError("Empty item description.")
    if is_non_item_message(raw):
        raise ValueError("That looks like a chat message, not a pantry item.")

    count = 1
    name = raw

    patterns = [
        r"^(?P<count>\d+)\s*[x×]\s*(?P<name>.+)$",
        r"^(?P<name>.+?)\s*[x×]\s*(?P<count>\d+)$",
        r"^(?P<name>.+?)\s+(?P<count>\d+)$",
        r"^(?P<count>\d+)\s+(?P<name>.+)$",
    ]
    for pattern in patterns:
        match = re.fullmatch(pattern, raw, flags=re.IGNORECASE)
        if match:
            name = match.group("name").strip(" -,:;")
            count = max(1, int(match.group("count")))
            break

    name = name.strip(" -,:;")
    if _is_bad_name(name):
        raise ValueError("Could not determine an item name from that text.")

    item = dict(DEFAULTS)
    item["Item Name"] = name.title() if name.islower() else name
    item["Count"] = count
    item["Category"] = _guess_category(name)
    return item


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

    def init_sheet(self, credentials_path: str, spreadsheet_id: str) -> gspread.Worksheet:
        client = gspread.service_account(filename=credentials_path)
        spreadsheet = client.open_by_key(spreadsheet_id)
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
        if expiration.upper() in {"", "NONE", "NULL", "UNKNOWN"}:
            expiration = "N/A"
        item["Expiration Date"] = expiration

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

        response = await self.genai_client.aio.models.generate_content(
            model=GEMINI_MODEL,
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

    async def resolve_item_from_text(self, text: str) -> dict[str, Any]:
        """Prefer Gemini enrichment; always fall back to local parsing."""
        local_item = item_from_text(text)
        try:
            gemini_item = await self.gemini_extract_item(text=text)
            gemini_name = str(gemini_item.get("Item Name") or "")
            if _is_bad_name(gemini_name) or not _names_related(
                str(local_item["Item Name"]), gemini_name
            ):
                # Keep the user's words — never let Gemini invent another product.
                gemini_item["Item Name"] = local_item["Item Name"]
                gemini_item["Count"] = local_item["Count"]
                gemini_item["Category"] = local_item["Category"]
            elif not gemini_item.get("Count"):
                gemini_item["Count"] = local_item["Count"]

            notes = str(gemini_item.get("Notes") or "")
            if re.search(
                r"no item|not provided|unknown|n/?a", notes, flags=re.IGNORECASE
            ):
                gemini_item["Notes"] = ""
            return gemini_item
        except Exception:
            logger.warning(
                "Gemini extract failed; using local parse for %r",
                text,
                exc_info=True,
            )
            return local_item

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
            (
                "*Pantry Inventory Bot*\n\n"
                "Add items with `/add pasta`, a photo, or plain text.\n\n"
                "Commands:\n"
                "• `/add <item>` – add an item (e.g. `/add pasta 2`)\n"
                "• `/search <query>` – find items\n"
                "• `/edit <query>` – change count or delete\n"
                "• `/list` – show recent items\n"
                "• `/help` – show this help\n"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.start_command(update, context)

    async def _save_text_item(
        self, update: Update, text: str, *, status_prefix: str
    ) -> None:
        assert update.message
        status = await update.message.reply_text(status_prefix)

        try:
            item = await self.resolve_item_from_text(text)
            await asyncio.to_thread(self.sheet_append_item, item)
        except Exception:
            logger.exception("Text item creation failed for %r", text)
            await status.edit_text(
                "❌ Could not save that item. Try `/add pasta` or "
                "`/add pasta 2`."
            )
            return

        await status.edit_text(
            "✅ *Item added to pantry*\n\n" + self.format_item_markdown(item),
            parse_mode=ParseMode.MARKDOWN,
        )

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
                "• `/add 2x olive oil`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await self._save_text_item(
            update,
            text,
            status_prefix="➕ Adding item…",
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
            try:
                item = await self.gemini_extract_item(
                    text=caption or None, image_bytes=image_bytes
                )
            except Exception:
                if caption.strip():
                    logger.warning(
                        "Gemini photo extract failed; falling back to caption",
                        exc_info=True,
                    )
                    item = item_from_text(caption)
                else:
                    raise
            await asyncio.to_thread(self.sheet_append_item, item)
        except Exception:
            logger.exception("Photo item creation failed")
            await status.edit_text(
                "❌ Could not extract or save the item from that photo. "
                "Please try again with a clearer image or add a caption "
                "(or use `/add <item>`)."
            )
            return

        await status.edit_text(
            "✅ *Item added to pantry*\n\n" + self.format_item_markdown(item),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def handle_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.message.text:
            return

        text = update.message.text.strip()
        if is_non_item_message(text):
            await update.message.reply_text(
                "👋 Hi! I only add pantry items.\n\n"
                "Try:\n"
                "• `/add pasta`\n"
                "• `/search honey`\n"
                "• `/edit rice`\n"
                "• or send a product photo",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await self._save_text_item(
            update,
            text,
            status_prefix="➕ Adding item…",
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

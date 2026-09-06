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
from typing import Any

import gspread
from google import genai
from google.genai import types
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger("automations.pantry")

GEMINI_MODEL = "gemini-2.5-flash"

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
# Category list may be incomplete (sheet UI was scrolled); add more as needed.
CATEGORIES = [
    "Grains & Rice",
    "Oils & Condiments",
    "Canned Goods",
    "Pasta & Noodles",
    "Baking",
    "Spreads & Jams",
    "Seasonings & Spices",
    "Beverages",  # present in existing sheet rows
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
    "Item Name": "Unknown Item",
    "Category": "Canned Goods",
    "Storage Location": "Kitchen Cabinet",
    "Count": 1,
    "Container Type": "Packages",
    "Unit Size": "N/A",
    "Reorder Status": "OK",
    "Expiration Date": "N/A",
    "Notes": "",
}

EXTRACTION_SYSTEM_INSTRUCTION = (
    "Extract pantry item details into a JSON object with keys: Item Name, "
    "Category, Storage Location, Count, Container Type, Unit Size, "
    "Reorder Status, Expiration Date, Notes.\n"
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


SEARCH_SYSTEM_INSTRUCTION = (
    "You match pantry search queries against an inventory list. Prefer fuzzy "
    "and natural-language matches (e.g. 'honey' → 'Liquid Forest Honey', "
    "'cinnamon' → 'Ground Cinnamon'). Return only relevant matches. "
    "Respond with JSON: {\"matches\": [{\"row_index\": <int>, \"reason\": <str>}]}. "
    "row_index must be the Google Sheets 1-based row number provided in the "
    "inventory. If nothing matches, return {\"matches\": []}."
)


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

    def _normalize_item(self, raw: dict[str, Any]) -> dict[str, Any]:
        item: dict[str, Any] = {}
        for key in SHEET_HEADERS:
            value = raw.get(key, DEFAULTS[key])
            if value is None or (isinstance(value, str) and not value.strip()):
                value = DEFAULTS[key]
            item[key] = value

        try:
            item["Count"] = max(0, int(item["Count"]))
        except (TypeError, ValueError):
            item["Count"] = DEFAULTS["Count"]

        item["Category"] = _coerce_choice(
            item["Category"], CATEGORIES, DEFAULTS["Category"]
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

        return self._normalize_item(parsed)

    async def gemini_search_matches(
        self,
        query: str,
        inventory: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        assert self.genai_client is not None

        if not inventory:
            return []

        compact = [
            {
                "row_index": entry["row_index"],
                "Item Name": entry.get("Item Name", ""),
                "Category": entry.get("Category", ""),
                "Storage Location": entry.get("Storage Location", ""),
                "Notes": entry.get("Notes", ""),
            }
            for entry in inventory
        ]

        prompt = (
            f"Search query: {query}\n\n"
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
            logger.warning("Search JSON parse failed: %s", raw_text[:300])
            return []

        match_rows = {
            int(m["row_index"])
            for m in parsed.get("matches", [])
            if isinstance(m, dict) and "row_index" in m
        }

        return [entry for entry in inventory if entry["row_index"] in match_rows]

    @staticmethod
    def format_item_markdown(
        item: dict[str, Any], *, row_index: int | None = None
    ) -> str:
        lines = [
            f"*📦 {_escape_md(item.get('Item Name', 'Unknown'))}*",
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
        application = (
            Application.builder()
            .token(token)
            .concurrent_updates(True)
            .build()
        )

        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("search", self.search_command))
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
                "Send a *photo* of a product (optional caption) or a *text* "
                "description to add an item.\n\n"
                "Commands:\n"
                "• `/search <query>` – fuzzy find items and manage counts\n"
                "• `/start` – show this help\n"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.start_command(update, context)

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
            item = await self.gemini_extract_item(
                text=caption or None, image_bytes=image_bytes
            )
            await asyncio.to_thread(self.sheet_append_item, item)
        except Exception:
            logger.exception("Photo item creation failed")
            await status.edit_text(
                "❌ Could not extract or save the item from that photo. "
                "Please try again with a clearer image or add a caption."
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

        status = await update.message.reply_text(
            "🔍 Extracting item details with Gemini…"
        )

        try:
            item = await self.gemini_extract_item(text=update.message.text)
            await asyncio.to_thread(self.sheet_append_item, item)
        except Exception:
            logger.exception("Text item creation failed")
            await status.edit_text(
                "❌ Could not extract or save the item from that message. "
                "Try a clearer description (name, size, count, location)."
            )
            return

        await status.edit_text(
            "✅ *Item added to pantry*\n\n" + self.format_item_markdown(item),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def search_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return

        query = " ".join(context.args).strip() if context.args else ""
        if not query:
            await update.message.reply_text(
                "Usage: `/search <item_name_or_category>`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        status = await update.message.reply_text(
            f"🔎 Searching pantry for *{_escape_md(query)}*…",
            parse_mode=ParseMode.MARKDOWN,
        )

        try:
            inventory = await asyncio.to_thread(self.sheet_get_inventory)
            matches = await self.gemini_search_matches(query, inventory)
        except Exception:
            logger.exception("Search failed for query=%r", query)
            await status.edit_text(
                "❌ Search failed while reading the sheet or calling Gemini."
            )
            return

        if not matches:
            await status.edit_text(
                f"No matches found for *{_escape_md(query)}*.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await status.edit_text(
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

"""Pantry inventory automation — registered in the Relay hub."""

from __future__ import annotations

from automations.base import Automation


class PantryAutomation(Automation):
    id = "pantry"
    name = "Pantry Inventory"
    description = (
        "Telegram bot with /add, /search, /edit, /list. Adds items from "
        "text or photos (Gemini when available, local fallback otherwise), "
        "stores them in Google Sheets, and supports fuzzy search with "
        "inline count and delete controls."
    )
    tags = ["telegram", "gemini", "google-sheets", "inventory"]

    def __init__(self) -> None:
        super().__init__()
        self._runtime = None

    def _get_runtime(self):
        if self._runtime is None:
            # Lazy import so the hub UI loads even before bot deps are installed.
            from automations.pantry.bot import PantryBotRuntime

            self._runtime = PantryBotRuntime()
        return self._runtime

    async def _start(self) -> None:
        await self._get_runtime().start_polling()

    async def _stop(self) -> None:
        if self._runtime is not None:
            await self._runtime.stop_polling()

"""Unified household inventory automation — registered in the Relay hub."""

from __future__ import annotations

from automations.base import Automation


class InventoryAutomation(Automation):
    id = "inventory"
    name = "Household Inventory"
    description = (
        "One Telegram bot for pantry groceries and medicine/supplements in a "
        "single Google Sheet. Add items via free text or photo and Gemini "
        "figures out the category; /search matches by name, brand, category, "
        "or (for medicine) symptoms. Adding is a conversation: Gemini fills "
        "what it can, the bot asks only for blank or important fields, then "
        "you confirm a recap (type 'change location' to reopen that picker)."
    )
    tags = ["telegram", "gemini", "google-sheets", "inventory", "medicine", "pantry"]

    def __init__(self) -> None:
        super().__init__()
        self._runtime = None

    def _get_runtime(self):
        if self._runtime is None:
            # Lazy import so the hub UI loads even before bot deps are installed.
            from automations.inventory.bot import InventoryBotRuntime

            self._runtime = InventoryBotRuntime()
        return self._runtime

    async def _start(self) -> None:
        await self._get_runtime().start_polling()

    async def _stop(self) -> None:
        if self._runtime is not None:
            await self._runtime.stop_polling()

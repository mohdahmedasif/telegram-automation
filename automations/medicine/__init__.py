"""Medicine inventory automation — registered in the Relay hub."""

from __future__ import annotations

from automations.base import Automation


class MedicineAutomation(Automation):
    id = "medicine"
    name = "Medicine Inventory"
    description = (
        "Telegram bot for medicine/supplement stock in Google Sheets. "
        "/search uses Gemini to match by brand, formula, or symptoms "
        "(e.g. headache → paracetamol) using Notes and Formula."
    )
    tags = ["telegram", "gemini", "google-sheets", "medicine", "symptoms"]

    def __init__(self) -> None:
        super().__init__()
        self._runtime = None

    def _get_runtime(self):
        if self._runtime is None:
            from automations.medicine.bot import MedicineBotRuntime

            self._runtime = MedicineBotRuntime()
        return self._runtime

    async def _start(self) -> None:
        await self._get_runtime().start_polling()

    async def _stop(self) -> None:
        if self._runtime is not None:
            await self._runtime.stop_polling()

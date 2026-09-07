"""Central registry of available automations."""

from __future__ import annotations

import logging

from automations.base import Automation

logger = logging.getLogger("relay.registry")


def _load_automations() -> list[Automation]:
    """
    Instantiate registered automations.

    Add new automations here — they appear automatically in the UI.
    """
    items: list[Automation] = []

    try:
        from automations.pantry import PantryAutomation

        items.append(PantryAutomation())
    except Exception:
        logger.exception("Failed to register Pantry automation")

    try:
        from automations.medicine import MedicineAutomation

        items.append(MedicineAutomation())
    except Exception:
        logger.exception("Failed to register Medicine automation")

    return items


AUTOMATIONS: list[Automation] = _load_automations()


def get_automation(automation_id: str) -> Automation | None:
    for automation in AUTOMATIONS:
        if automation.id == automation_id:
            return automation
    return None


def list_automations() -> list[Automation]:
    return list(AUTOMATIONS)

"""Shared automation contracts for the Relay control hub."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# Telegram bot tokens look like 123456:AA... — never expose them in UI/API.
_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")


def _safe_error(exc: BaseException) -> str:
    message = str(exc)
    return _TOKEN_RE.sub("[redacted-token]", message)


class AutomationStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass(slots=True)
class AutomationInfo:
    """Serializable metadata shown in the control UI."""

    id: str
    name: str
    description: str
    status: AutomationStatus
    tags: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


class Automation(ABC):
    """
    One runnable automation (Telegram bots, schedulers, webhooks, …).

    Subclass this and register an instance in `app.registry`.
    """

    id: str
    name: str
    description: str
    tags: list[str] = []

    def __init__(self) -> None:
        self._status = AutomationStatus.STOPPED
        self._error: str | None = None

    @property
    def status(self) -> AutomationStatus:
        return self._status

    @property
    def error(self) -> str | None:
        return self._error

    def info(self) -> AutomationInfo:
        return AutomationInfo(
            id=self.id,
            name=self.name,
            description=self.description,
            status=self._status,
            tags=list(self.tags),
            error=self._error,
        )

    async def start(self) -> None:
        if self._status is AutomationStatus.RUNNING:
            return
        self._status = AutomationStatus.STARTING
        self._error = None
        try:
            await self._start()
            self._status = AutomationStatus.RUNNING
        except Exception as exc:
            self._status = AutomationStatus.ERROR
            self._error = _safe_error(exc)
            raise

    async def stop(self) -> None:
        if self._status is AutomationStatus.STOPPED:
            return
        self._status = AutomationStatus.STOPPING
        try:
            await self._stop()
            self._status = AutomationStatus.STOPPED
            self._error = None
        except Exception as exc:
            self._status = AutomationStatus.ERROR
            self._error = _safe_error(exc)
            raise

    @abstractmethod
    async def _start(self) -> None:
        """Begin background work (polling, schedules, listeners)."""

    @abstractmethod
    async def _stop(self) -> None:
        """Tear down background work cleanly."""

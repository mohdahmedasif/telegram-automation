"""FastAPI status surface for Relay automations (auto-started on boot)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.registry import list_automations
from automations.base import AutomationStatus

logger = logging.getLogger("relay.web")

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


async def _start_all() -> None:
    async def _one(automation) -> None:
        try:
            await automation.start()
            logger.info("Started automation %s", automation.id)
        except Exception:
            logger.exception("Failed to start automation %s", automation.id)

    await asyncio.gather(*[_one(a) for a in list_automations()])


async def _stop_all() -> None:
    for automation in list_automations():
        if automation.status is AutomationStatus.RUNNING:
            try:
                await automation.stop()
            except Exception:
                logger.exception(
                    "Error stopping %s during shutdown", automation.id
                )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await _start_all()
    yield
    await _stop_all()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Relay",
        description="Personal automation status hub",
        lifespan=lifespan,
    )
    app.mount(
        "/static",
        StaticFiles(directory=str(WEB_DIR / "static")),
        name="static",
    )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        automations = [a.info().to_dict() for a in list_automations()]
        running = sum(
            1 for a in automations if a["status"] == AutomationStatus.RUNNING.value
        )
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "automations": automations,
                "running_count": running,
                "total_count": len(automations),
            },
        )

    @app.get("/api/automations")
    async def api_list() -> JSONResponse:
        return JSONResponse(
            [a.info().to_dict() for a in list_automations()]
        )

    return app

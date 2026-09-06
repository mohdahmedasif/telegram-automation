"""FastAPI control surface for listing and starting/stopping automations."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.registry import get_automation, list_automations
from automations.base import AutomationStatus

logger = logging.getLogger("relay.web")

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    for automation in list_automations():
        if automation.status is AutomationStatus.RUNNING:
            try:
                await automation.stop()
            except Exception:
                logger.exception(
                    "Error stopping %s during shutdown", automation.id
                )


def create_app() -> FastAPI:
    app = FastAPI(
        title="Relay",
        description="Personal automation control hub",
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
            "index.html",
            {
                "request": request,
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

    @app.post("/api/automations/{automation_id}/start")
    async def api_start(automation_id: str) -> JSONResponse:
        automation = get_automation(automation_id)
        if automation is None:
            raise HTTPException(status_code=404, detail="Automation not found")
        try:
            await automation.start()
        except Exception as exc:
            logger.exception("Failed to start %s", automation_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse(automation.info().to_dict())

    @app.post("/api/automations/{automation_id}/stop")
    async def api_stop(automation_id: str) -> JSONResponse:
        automation = get_automation(automation_id)
        if automation is None:
            raise HTTPException(status_code=404, detail="Automation not found")
        try:
            await automation.stop()
        except Exception as exc:
            logger.exception("Failed to stop %s", automation_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse(automation.info().to_dict())

    return app

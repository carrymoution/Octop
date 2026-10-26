"""HTTP adapter for administrator setup and employee ChartForge OAuth."""

from __future__ import annotations

import logging
from html import escape
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from octop.api.common.public_base import resolve_public_base
from octop.api.deps import current_user, get_server, require_admin
from octop.i18n import tr
from octop.infra.connectors.chartforge import ChartForgeConnector
from octop.infra.errors import OctopError
from octop.infra.utils.locale import resolve_request_locale

router = APIRouter()
logger = logging.getLogger(__name__)


class ChartForgeSettings(BaseModel):
    base_url: str = Field(description="ChartForge public platform origin, without a path.")
    frontend_url: str = Field(
        default="", description="Platform frontend origin for the employee link."
    )
    enabled: bool = False
    allow_http: bool = Field(
        default=False, description="Explicitly allow HTTP for the configured platform origin."
    )


@router.get("/connectors/chartforge", summary="ChartForge configuration and personal connection")
async def status(
    user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> dict[str, Any]:
    return ChartForgeConnector(server.services).status(user.id)


@router.put(
    "/connectors/chartforge/settings", summary="Configure the organization ChartForge origin"
)
async def settings(
    body: ChartForgeSettings,
    user: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> ChartForgeSettings:
    connector = ChartForgeConnector(server.services)
    config = body.model_dump()
    if body.enabled:
        config.update(await connector.check(config))
    result = connector.save_settings(config)
    server.services.audit_repo.write(
        actor=user.username, action="chartforge.configure", target=result["base_url"]
    )
    return ChartForgeSettings(**result)


@router.post("/connectors/chartforge/check", summary="Check the configured platform without saving")
async def check(
    body: ChartForgeSettings,
    user: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, str]:
    return await ChartForgeConnector(server.services).check(body.model_dump())


@router.post(
    "/connectors/chartforge/authorize", summary="Authorize this employee's ChartForge account"
)
async def authorize(
    request: Request, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> dict[str, str]:
    callback = resolve_public_base(request) + "/api/connectors/oauth/callback"
    return await ChartForgeConnector(server.services).start(user.id, callback)


@router.delete(
    "/connectors/chartforge", summary="Revoke the current employee's ChartForge connection"
)
async def disconnect(
    user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> dict[str, bool]:
    await ChartForgeConnector(server.services).disconnect(user.id)
    if server.app_runtime is not None:
        from octop.api.routers.connectors import _schedule_connector_reload

        _schedule_connector_reload(server, user.id)
    return {"ok": True}


async def complete_callback(request: Request, server: Any, row: Any, code: str) -> HTMLResponse:
    locale = resolve_request_locale(request)
    try:
        await ChartForgeConnector(server.services).complete(row, code)
        if server.app_runtime is not None:
            from octop.api.routers.connectors import _schedule_connector_reload

            _schedule_connector_reload(server, row.user_id)
        text = escape(tr("chartforge.connected", locale))
        # This flow stores credentials server-side, never in a pending browser payload.
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><p>" + text + "</p><script>"
            "if(window.opener){window.opener.postMessage({type:'octop:chartforge-oauth'},window.location.origin);window.close();}"
            "else{window.location.replace('/connectors');}</script>"
        )
    except Exception as error:
        logger.warning("ChartForge authorization failed for user %s", row.user_id, exc_info=True)
        reason_key = error.details.get("reason_key") if isinstance(error, OctopError) else None
        known_reasons = {
            "chartforge.disabled",
            "chartforge.reauthorize",
            "chartforge.identityMismatch",
            "chartforge.connectionFailed",
            "chartforge.invalidOrigin",
            "chartforge.httpNotAllowed",
            "chartforge.unsafeAddress",
            "chartforge.redirectNotAllowed",
        }
        message_key = (
            reason_key if reason_key in known_reasons else "chartforge.authorizationFailed"
        )
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><p>" + escape(tr(message_key, locale)) + "</p>",
            status_code=400 if reason_key in known_reasons else 500,
        )

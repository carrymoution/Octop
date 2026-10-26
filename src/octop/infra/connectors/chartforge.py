"""ChartForge: one administrator-managed origin, one OAuth grant per employee."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import secrets
import socket
import time
from contextlib import suppress
from typing import Any
from urllib.parse import urlencode, urlsplit
from weakref import WeakKeyDictionary

import httpx

from octop.i18n import tr
from octop.infra.connectors.crypto import decrypt_credentials, encrypt_credentials
from octop.infra.connectors.oauth.pkce import new_pkce_pair
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.locale import DEFAULT_LOCALE
from octop.infra.utils.ssrf_guard import PinnedIPTransport
from octop.infra.utils.ulid import new_ulid

SETTINGS_KEY = "chartforge.settings"
CONTEXT_KEY = "chartforge.oauth."
_LOCKS: WeakKeyDictionary[Any, dict[int, asyncio.Lock]] = WeakKeyDictionary()


def failure(key: str, **params: str) -> OctopError:
    reason_key = f"chartforge.{key}"
    reason = tr(reason_key, DEFAULT_LOCALE, **params)
    return OctopError(
        ErrorCode.CONNECTOR_INVALID_CREDENTIALS,
        reason,
        details={"reason": reason, "reason_key": reason_key, **params},
    )


def response_object(response: httpx.Response, error_key: str) -> dict[str, Any]:
    if not response.is_success:
        raise failure(error_key)
    try:
        body = response.json()
    except ValueError as error:
        raise failure(error_key) from error
    if not isinstance(body, dict):
        raise failure(error_key)
    if isinstance(body.get("code"), int) and 400 <= body["code"] <= 599:
        raise failure(error_key)
    return body


def normalize_origin(raw: str, allow_http: bool) -> str:
    try:
        parsed = urlsplit(raw.strip())
        port = parsed.port
    except ValueError as error:
        raise failure("invalidOrigin") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise failure("invalidOrigin")
    if parsed.scheme == "http" and not allow_http:
        raise failure("httpNotAllowed")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise failure("invalidOrigin") from error
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        netloc += f":{port}"
    return f"{parsed.scheme}://{netloc}"


async def request(config: dict[str, Any], method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Pin the explicitly configured origin; never follow upstream redirects."""
    origin = normalize_origin(str(config.get("base_url") or ""), bool(config.get("allow_http")))
    if not path.startswith("/") or path.startswith("//") or urlsplit(path).netloc:
        raise failure("invalidOrigin")
    parsed = urlsplit(origin)
    host = str(parsed.hostname)
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except OSError as error:
        raise failure("connectionFailed") from error
    if not addresses:
        raise failure("invalidOrigin")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if (
            ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or (ip.is_reserved and not ip.is_loopback)
        ):
            raise failure("unsafeAddress")
    # Prefer IPv4 when localhost resolves to both families but the server binds only IPv4.
    pinned = next((address for address in addresses if address[0] == socket.AF_INET), addresses[0])
    try:
        async with httpx.AsyncClient(
            transport=PinnedIPTransport(host, pinned[4][0]), timeout=35, follow_redirects=False
        ) as client:
            response = await client.request(method, origin + path, **kwargs)
    except httpx.HTTPError as error:
        raise failure("connectionFailed") from error
    if response.is_redirect:
        raise failure("redirectNotAllowed")
    return response


class ChartForgeConnector:
    def __init__(self, services: Any) -> None:
        self.services = services
        self.repo = services.repos.connector_repo

    def _lock(self, user_id: int) -> asyncio.Lock:
        return _LOCKS.setdefault(self.repo, {}).setdefault(user_id, asyncio.Lock())

    def settings(self) -> dict[str, Any]:
        raw = self.services.settings_repo.get(SETTINGS_KEY)
        return (
            dict(json.loads(raw))
            if raw
            else {"enabled": False, "base_url": "", "allow_http": False}
        )

    def save_settings(self, config: dict[str, Any]) -> dict[str, Any]:
        result = {
            "base_url": normalize_origin(
                str(config.get("base_url") or ""), bool(config.get("allow_http"))
            ),
            "allow_http": bool(config.get("allow_http")),
            "enabled": bool(config.get("enabled")),
            "frontend_url": normalize_origin(str(config["frontend_url"]), True)
            if config.get("frontend_url")
            else "",
        }
        self.services.settings_repo.set(SETTINGS_KEY, json.dumps(result))
        return result

    def status(self, user_id: int) -> dict[str, Any]:
        config = self.settings()
        instance = self.repo.get_by_user_kind(user_id, "chartforge")
        bound = {}
        if instance is not None and instance.has_credentials:
            bound = decrypt_credentials(self.services.secret_repo, instance.credential_blob)
        user = self.services.user_repo.get(user_id)
        connected = bool(
            config.get("enabled")
            and instance
            and instance.status == "active"
            and not instance.shared
            and bound.get("base_url") == config.get("base_url")
            and user is not None
            and not user.disabled
            and user.username == bound.get("employee_no")
        )
        return {
            **config,
            "connected": connected,
            "needs_reauthorization": bool(config.get("enabled") and bound and not connected),
            "employee_no": bound.get("employee_no"),
        }

    def _enabled_settings(self) -> dict[str, Any]:
        config = self.settings()
        if not config.get("enabled"):
            raise failure("disabled")
        return config

    async def check(self, config: dict[str, Any]) -> dict[str, str]:
        origin = normalize_origin(str(config.get("base_url") or ""), bool(config.get("allow_http")))
        checked = {**config, "base_url": origin}
        response = await request(checked, "GET", "/.well-known/oauth-authorization-server")
        metadata = response_object(response, "invalidServer")
        if metadata.get("issuer") != origin:
            raise failure("issuerMismatch")
        frontend = metadata.get("chartforge_frontend_url") or config.get("frontend_url")
        frontend_url = normalize_origin(str(frontend), True) if frontend else ""
        authorization_endpoint = str(metadata.get("authorization_endpoint") or "")
        allowed_authorization_endpoints = {origin + "/oauth2/authorize"}
        if frontend_url:
            allowed_authorization_endpoints.add(frontend_url + "/oauth2/authorize")
        if authorization_endpoint not in allowed_authorization_endpoints:
            raise failure("endpointMismatch")
        for field, expected in (
            ("token_endpoint", "/oauth2/token"),
            ("registration_endpoint", "/gateway/oauth/register"),
        ):
            if metadata.get(field) != origin + expected:
                raise failure("endpointMismatch")
        return {
            "base_url": origin,
            "frontend_url": frontend_url,
            "authorization_endpoint": authorization_endpoint,
        }

    async def start(self, user_id: int, redirect_uri: str) -> dict[str, str]:
        config = self._enabled_settings()
        checked = await self.check(config)
        origin = checked["base_url"]
        config = {**config, **checked}
        response = await request(
            config,
            "POST",
            "/gateway/oauth/register",
            json={
                "client_name": "Octop AI报表查询",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            },
        )
        if response.status_code == 400:
            try:
                error_body = response.json()
            except ValueError:
                error_body = {}
            if isinstance(error_body, dict) and error_body.get("error") == "invalid_redirect_uri":
                raise failure("callbackNotAllowed", url=redirect_uri)
        registered = response_object(response, "registrationFailed")
        if not all(
            isinstance(registered.get(k), str) and registered[k]
            for k in ("client_id", "client_secret")
        ):
            raise failure("registrationFailed")
        verifier, challenge = new_pkce_pair()
        state, state_id = secrets.token_urlsafe(32), new_ulid()
        context = {
            **config,
            "client_id": registered["client_id"],
            "client_secret": registered["client_secret"],
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        }
        blob = encrypt_credentials(self.services.secret_repo, context)
        self.services.settings_repo.set(
            CONTEXT_KEY + state_id, base64.b64encode(blob).decode("ascii")
        )
        self.repo.create_oauth_state(
            state_id=state_id,
            state=state,
            user_id=user_id,
            kind="chartforge",
            code_verifier=verifier,
            redirect_after="/connectors",
        )
        return {
            "state_id": state_id,
            "authorize_url": checked["authorization_endpoint"]
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": registered["client_id"],
                    "redirect_uri": redirect_uri,
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "scope": "gateway:read",
                    "resource": origin + "/mcp/sql",
                }
            ),
        }

    async def complete(self, row: Any, code: str) -> None:
        raw = self.services.settings_repo.get(CONTEXT_KEY + row.state_id)
        self.services.settings_repo.delete(CONTEXT_KEY + row.state_id)
        if not raw:
            raise failure("reauthorize")
        context = decrypt_credentials(self.services.secret_repo, base64.b64decode(raw))
        config = self._enabled_settings()
        if (
            context["base_url"] != config["base_url"]
            or time.time() - float(context["created_at"]) > 600
        ):
            raise failure("reauthorize")
        response = await request(
            config,
            "POST",
            "/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": row.code_verifier,
                "client_id": context["client_id"],
                "client_secret": context["client_secret"],
                "redirect_uri": context["redirect_uri"],
                "resource": config["base_url"] + "/mcp/sql",
            },
        )
        tokens = response_object(response, "reauthorize")
        if not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
            raise failure("reauthorize")
        creds = {
            **tokens,
            "base_url": config["base_url"],
            "oauth_client_id": context["client_id"],
            "oauth_client_secret": context["client_secret"],
            "expires_at": int(time.time()) + int(tokens.get("expires_in", 1800)),
        }
        try:
            await self._check_identity(config, creds, row.user_id)
        except Exception:
            await self._revoke(config, creds)
            raise
        async with self._lock(row.user_id):
            existing = self.repo.get_by_user_kind(row.user_id, "chartforge")
            instance_id = existing.instance_id if existing else new_ulid()
            previous = (
                decrypt_credentials(self.services.secret_repo, existing.credential_blob)
                if existing and existing.has_credentials
                else {}
            )
            if existing:
                self.repo.update_status(instance_id, "active")
                self.repo.update_metadata(instance_id, display_name="AI报表查询", shared=False)
            else:
                self.repo.create(
                    instance_id=instance_id,
                    user_id=row.user_id,
                    kind="chartforge",
                    display_name="AI报表查询",
                    mcp_server_name="chartforge",
                    shared=False,
                    config_json=json.dumps({"default_open": True}),
                )
            self.repo.upsert_credentials(
                instance_id=instance_id,
                blob=encrypt_credentials(self.services.secret_repo, creds),
                expires_at=creds["expires_at"],
            )
            await self._revoke(config, previous)

    async def _check_identity(
        self, config: dict[str, Any], creds: dict[str, Any], user_id: int
    ) -> None:
        user = self.services.user_repo.get(user_id)
        response = await request(
            config,
            "GET",
            "/gateway/me",
            headers={"Authorization": f"Bearer {creds['access_token']}"},
        )
        employee = response_object(response, "connectionFailed").get("employeeNo")
        if not isinstance(employee, str) or not employee:
            raise failure("connectionFailed")
        if user is None or user.disabled or user.username != employee:
            raise failure("identityMismatch")
        creds["employee_no"] = employee

    async def _revoke(self, config: dict[str, Any], creds: dict[str, Any]) -> None:
        if creds.get("base_url") != config.get("base_url"):
            return
        if not creds.get("access_token"):
            return
        for token in (creds.get("refresh_token"), creds.get("access_token")):
            if token:
                with suppress(httpx.HTTPError, OctopError):
                    await request(
                        config,
                        "POST",
                        "/oauth2/revoke",
                        data={
                            "token": token,
                            "client_id": creds.get("oauth_client_id"),
                            "client_secret": creds.get("oauth_client_secret"),
                        },
                    )

    async def disconnect(self, user_id: int) -> None:
        async with self._lock(user_id):
            row = self.repo.get_by_user_kind(user_id, "chartforge")
            if row:
                creds = (
                    decrypt_credentials(self.services.secret_repo, row.credential_blob)
                    if row.credential_blob
                    else {}
                )
                if creds.get("base_url") == self.settings().get("base_url"):
                    await self._revoke(self.settings(), creds)
                self.repo.delete(row.instance_id)

    async def call(self, user_id: int, name: str, arguments: dict[str, Any]) -> str:
        config = self._enabled_settings()
        async with self._lock(user_id):
            row = self.repo.get_by_user_kind(user_id, "chartforge")
            if row is None or row.status != "active" or row.shared or not row.credential_blob:
                raise failure("reauthorize")
            creds = decrypt_credentials(self.services.secret_repo, row.credential_blob)
            user = self.services.user_repo.get(user_id)
            if (
                creds.get("base_url") != config["base_url"]
                or user is None
                or user.disabled
                or user.username != creds.get("employee_no")
            ):
                raise failure("identityMismatch")
            if int(creds.get("expires_at", 0)) <= time.time() + 120:
                response = await request(
                    config,
                    "POST",
                    "/oauth2/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": creds.get("refresh_token"),
                        "client_id": creds["oauth_client_id"],
                        "client_secret": creds["oauth_client_secret"],
                        "resource": config["base_url"] + "/mcp/sql",
                    },
                )
                if response.status_code >= 400:
                    if response.status_code in (400, 401):
                        self.repo.update_status(row.instance_id, "error")
                    raise failure("reauthorize")
                tokens = response_object(response, "reauthorize")
                if not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
                    raise failure("reauthorize")
                creds.update(tokens)
                creds["expires_at"] = int(time.time()) + int(tokens.get("expires_in", 1800))
                try:
                    await self._check_identity(config, creds, user_id)
                except Exception:
                    await self._revoke(config, creds)
                    raise
                self.repo.upsert_credentials(
                    instance_id=row.instance_id,
                    blob=encrypt_credentials(self.services.secret_repo, creds),
                    expires_at=creds["expires_at"],
                )
            response = await request(
                config,
                "POST",
                "/mcp/sql",
                headers={
                    "Authorization": f"Bearer {creds['access_token']}",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-03-26",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                timeout=httpx.Timeout(360, connect=5),
            )
        if response.status_code == 401:
            self.repo.update_status(row.instance_id, "error")
            raise failure("reauthorize")
        body = response_object(response, "queryFailed")
        if body.get("error"):
            raise failure("queryFailed")
        return json.dumps(body.get("result", {}), ensure_ascii=False)


def build_tools(services: Any) -> list[Any]:
    """Resolve identity at invocation time, including when an agent is shared."""
    from langchain_core.tools import StructuredTool
    from langgraph.config import get_config
    from octop_harness.mcp import mcp_args_model

    from octop.infra.utils.browser_media import parse_octop_user_id

    definitions = [
        ("list_connections", {}, []),
        ("list_schemas", {"connectionId": {"type": "integer"}}, ["connectionId"]),
        ("list_objects", {"connectionId": {"type": "integer"}}, ["connectionId"]),
        ("describe_object", {"objectId": {"type": "integer"}}, ["objectId"]),
        (
            "query_sql",
            {
                "connectionId": {"type": "integer"},
                "sql": {"type": "string"},
                "defaultSchema": {"type": "string"},
                "parameters": {"type": "object"},
                "maxRows": {"type": "integer"},
            },
            ["connectionId", "sql"],
        ),
        ("list_saved_queries", {"connectionId": {"type": "integer"}}, ["connectionId"]),
        (
            "run_saved_query",
            {
                "queryId": {"type": "integer"},
                "parameters": {"type": "object"},
                "maxRows": {"type": "integer"},
            },
            ["queryId"],
        ),
    ]

    def invoke(tool_name: str) -> Any:
        async def run(**arguments: Any) -> str:
            uid = parse_octop_user_id(dict(get_config().get("configurable") or {}).get("user"))
            if uid is None:
                raise failure("identityMismatch")
            connector = ChartForgeConnector(services() if callable(services) else services)
            return await connector.call(
                uid, tool_name, {k: v for k, v in arguments.items() if v is not None}
            )

        return run

    return [
        StructuredTool.from_function(
            coroutine=invoke(name),
            name=f"chartforge_{name}",
            description=tr(f"chartforge.tools.{name}"),
            args_schema=mcp_args_model(
                f"chartforge_{name}",
                {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
            ),
        )
        for name, props, required in definitions
    ]

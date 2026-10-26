from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from octop_harness.mcp import filter_tools_for_mcp_servers, mcp_tool_names

from octop.infra.connectors import chartforge as cf
from octop.infra.connectors.crypto import decrypt_credentials, encrypt_credentials
from octop.infra.errors import OctopError


class Repo:
    def __init__(self) -> None:
        self.rows: dict[int, SimpleNamespace] = {}

    def get_by_user_kind(self, uid: int, kind: str) -> SimpleNamespace | None:
        return self.rows.get(uid)

    def upsert_credentials(self, **kwargs: object) -> None:
        for row in self.rows.values():
            if row.instance_id == kwargs["instance_id"]:
                row.credential_blob = kwargs["blob"]

    def update_status(self, instance_id: str, status: str) -> None:
        for row in self.rows.values():
            if row.instance_id == instance_id:
                row.status = status


@pytest.fixture
def connector() -> cf.ChartForgeConnector:
    values: dict[str, str] = {
        cf.SETTINGS_KEY: json.dumps(
            {"base_url": "http://10.0.0.9:8080", "allow_http": True, "enabled": True}
        )
    }
    key = Fernet.generate_key()
    services = SimpleNamespace(
        repos=SimpleNamespace(connector_repo=Repo()),
        settings_repo=SimpleNamespace(
            get=values.get, set=values.__setitem__, delete=lambda k: values.pop(k, None)
        ),
        secret_repo=SimpleNamespace(get_or_create=lambda *_: key),
        user_repo=SimpleNamespace(
            get=lambda uid: SimpleNamespace(username=f"{uid:04}", disabled=0)
        ),
    )
    result = cf.ChartForgeConnector(services)
    for uid in (7, 8):
        creds = {
            "base_url": "http://10.0.0.9:8080",
            "access_token": f"token-{uid}",
            "employee_no": f"{uid:04}",
            "expires_at": int(time.time()) + 1000,
            "refresh_token": f"refresh-{uid}",
            "oauth_client_id": "client",
            "oauth_client_secret": "secret",
        }
        result.repo.rows[uid] = SimpleNamespace(
            instance_id=str(uid),
            status="active",
            shared=False,
            has_credentials=True,
            credential_blob=encrypt_credentials(services.secret_repo, creds),
        )
    return result


def response(body: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("POST", "http://10.0.0.9:8080"))


def test_explicit_http_and_origin_validation() -> None:
    assert cf.normalize_origin("http://10.0.0.9:8080/", True) == "http://10.0.0.9:8080"
    for url, enabled in [
        ("http://10.0.0.9", False),
        ("http://u:p@10.0.0.9", True),
        ("https://example.com/path", False),
        ("https://example.com?x=1", False),
    ]:
        with pytest.raises(OctopError):
            cf.normalize_origin(url, enabled)


def metadata(origin: str) -> dict[str, str]:
    return {
        "issuer": origin,
        "authorization_endpoint": origin + "/oauth2/authorize",
        "token_endpoint": origin + "/oauth2/token",
        "registration_endpoint": origin + "/gateway/oauth/register",
    }


@pytest.mark.asyncio
async def test_check_discovers_frontend_link_without_changing_token_origin(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = connector.settings()["base_url"]
    monkeypatch.setattr(
        cf,
        "request",
        AsyncMock(
            return_value=response(
                {**metadata(origin), "chartforge_frontend_url": "https://portal.example/"}
            )
        ),
    )
    checked = await connector.check(connector.settings())
    assert checked == {
        "base_url": origin,
        "frontend_url": "https://portal.example",
        "authorization_endpoint": origin + "/oauth2/authorize",
    }
    saved = connector.save_settings({**connector.settings(), **checked})
    assert saved["base_url"] == origin
    assert connector.status(7)["connected"] is True
    assert connector.status(7)["frontend_url"] == "https://portal.example"


@pytest.mark.asyncio
async def test_browser_authorization_uses_discovered_frontend_with_backend_resource(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = connector.settings()["base_url"]
    frontend = "https://portal.example"
    request = AsyncMock(
        side_effect=[
            response(
                {
                    **metadata(origin),
                    "chartforge_frontend_url": frontend,
                    "authorization_endpoint": frontend + "/oauth2/authorize",
                }
            ),
            response({"client_id": "client", "client_secret": "secret"}),
        ]
    )
    monkeypatch.setattr(cf, "request", request)
    monkeypatch.setattr(connector.repo, "create_oauth_state", lambda **kwargs: None, raising=False)
    result = await connector.start(7, "https://octop.example/api/connectors/oauth/callback")
    url = urlsplit(result["authorize_url"])
    assert url.scheme + "://" + url.netloc + url.path == frontend + "/oauth2/authorize"
    assert parse_qs(url.query)["resource"] == [origin + "/mcp/sql"]
    assert all(call.args[0]["base_url"] == origin for call in request.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    [
        "https://unrelated.example/oauth2/authorize",
        "https://portal.example/redirect",
    ],
)
async def test_authorization_endpoint_must_match_a_known_origin_and_exact_path(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    origin = connector.settings()["base_url"]
    monkeypatch.setattr(
        cf,
        "request",
        AsyncMock(
            return_value=response(
                {
                    **metadata(origin),
                    "chartforge_frontend_url": "https://portal.example",
                    "authorization_endpoint": endpoint,
                }
            )
        ),
    )
    with pytest.raises(OctopError) as caught:
        await connector.check(connector.settings())
    assert caught.value.details["reason_key"] == "chartforge.endpointMismatch"


@pytest.mark.asyncio
async def test_discovered_frontend_link_cannot_use_an_unsafe_scheme(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = connector.settings()["base_url"]
    monkeypatch.setattr(
        cf,
        "request",
        AsyncMock(
            return_value=response(
                {**metadata(origin), "chartforge_frontend_url": "javascript:alert(1)"}
            )
        ),
    )
    with pytest.raises(OctopError):
        await connector.check(connector.settings())


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["<html>frontend</html>", "", "[]", "null"])
async def test_frontend_or_invalid_metadata_gives_actionable_error(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    request = AsyncMock(return_value=httpx.Response(200, text=body))
    monkeypatch.setattr(cf, "request", request)
    before = connector.settings()
    with pytest.raises(OctopError) as caught:
        await connector.start(7, "http://octop.internal/api/connectors/oauth/callback")
    assert caught.value.status == 400
    assert caught.value.details["reason_key"] == "chartforge.invalidServer"
    assert connector.settings() == before
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_callback_rejection_includes_exact_callback(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = connector.settings()["base_url"]
    monkeypatch.setattr(
        cf,
        "request",
        AsyncMock(
            side_effect=[
                response(metadata(origin)),
                response({"error": "invalid_redirect_uri"}, 400),
            ]
        ),
    )
    callback = "http://127.0.0.1:5173/api/connectors/oauth/callback"
    with pytest.raises(OctopError) as caught:
        await connector.start(7, callback)
    assert caught.value.details["reason_key"] == "chartforge.callbackNotAllowed"
    assert caught.value.details["url"] == callback


@pytest.mark.asyncio
async def test_issuer_mismatch_does_not_register_a_client(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = AsyncMock(return_value=response(metadata("http://another.internal")))
    monkeypatch.setattr(cf, "request", request)
    with pytest.raises(OctopError) as caught:
        await connector.check(connector.settings())
    assert caught.value.details["reason_key"] == "chartforge.issuerMismatch"
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_localhost_ipv6_loopback_is_allowed_and_ipv4_is_preferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import socket

    addresses = [
        (socket.AF_INET6, 1, 6, "", ("::1", 8080, 0, 0)),
        (socket.AF_INET, 1, 6, "", ("127.0.0.1", 8080)),
    ]
    monkeypatch.setattr(
        asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=addresses)
    )
    pinned: list[str] = []

    def transport(host: str, address: str) -> httpx.MockTransport:
        pinned.append(address)
        return httpx.MockTransport(lambda req: httpx.Response(200, json={}))

    monkeypatch.setattr(cf, "PinnedIPTransport", transport)
    await cf.request(
        {"base_url": "http://localhost:8080", "allow_http": True}, "GET", "/gateway/me"
    )
    assert pinned == ["127.0.0.1"]
    addresses.pop()
    await cf.request({"base_url": "http://[::1]:8080", "allow_http": True}, "GET", "/gateway/me")
    assert pinned[-1] == "::1"


@pytest.mark.asyncio
async def test_network_failure_is_not_an_internal_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    monkeypatch.setattr(
        asyncio.get_running_loop(), "getaddrinfo", AsyncMock(side_effect=OSError("dns"))
    )
    with pytest.raises(OctopError) as caught:
        await cf.request(
            {"base_url": "http://missing.internal", "allow_http": True}, "GET", "/gateway/me"
        )
    assert caught.value.details["reason_key"] == "chartforge.connectionFailed"


@pytest.mark.asyncio
async def test_concurrent_employees_use_separate_tokens(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    request = AsyncMock(return_value=response({"result": {"content": []}}))
    monkeypatch.setattr(cf, "request", request)
    await asyncio.gather(
        connector.call(7, "list_connections", {}), connector.call(8, "list_connections", {})
    )
    headers = {call.kwargs["headers"]["Authorization"] for call in request.call_args_list}
    assert headers == {"Bearer token-7", "Bearer token-8"}


@pytest.mark.asyncio
async def test_shared_credentials_are_never_used(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector.repo.rows[7].shared = True
    request = AsyncMock()
    monkeypatch.setattr(cf, "request", request)
    with pytest.raises(OctopError):
        await connector.call(7, "list_connections", {})
    request.assert_not_called()


@pytest.mark.asyncio
async def test_no_fallback_to_another_employee(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = AsyncMock()
    monkeypatch.setattr(cf, "request", request)
    with pytest.raises(OctopError):
        await connector.call(9, "list_connections", {})
    request.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_rotates_grant_before_query(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = connector.repo.rows[7]
    creds = decrypt_credentials(connector.services.secret_repo, row.credential_blob)
    creds["expires_at"] = 0
    row.credential_blob = encrypt_credentials(connector.services.secret_repo, creds)
    request = AsyncMock(
        side_effect=[
            response({"access_token": "new", "refresh_token": "rotated", "expires_in": 1800}),
            response({"employeeNo": "0007"}),
            response({"result": {}}),
        ]
    )
    monkeypatch.setattr(cf, "request", request)
    await connector.call(7, "list_connections", {})
    assert request.call_args_list[-1].kwargs["headers"]["Authorization"] == "Bearer new"
    stored = decrypt_credentials(connector.services.secret_repo, row.credential_blob)
    assert stored["refresh_token"] == "rotated"
    assert request.call_args_list[-1].kwargs["timeout"].read > 330


def test_status_requires_enabled_server_and_current_employee(
    connector: cf.ChartForgeConnector,
) -> None:
    assert connector.status(7)["connected"] is True
    connector.services.user_repo.get = lambda uid: SimpleNamespace(username="changed", disabled=0)
    assert connector.status(7)["connected"] is False
    assert connector.status(7)["needs_reauthorization"] is True
    connector.save_settings({**connector.settings(), "enabled": False})
    assert connector.status(7)["connected"] is False


@pytest.mark.asyncio
async def test_reauthorization_updates_real_repository_and_replaces_encrypted_grant(
    connector: cf.ChartForgeConnector, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.connectors import ConnectorRepo

    pool = SqlitePool(tmp_path / "chartforge.db")
    try:
        run_migrations(pool)
        with pool.transaction() as db:
            db.execute(
                "INSERT INTO users(id,username,password_hash,role,created_at) VALUES(7,'0007','test','user',1)"
            )
        repo = ConnectorRepo(pool)
        old = decrypt_credentials(
            connector.services.secret_repo, connector.repo.rows[7].credential_blob
        )
        repo.create(
            instance_id="existing",
            user_id=7,
            kind="chartforge",
            display_name="Old name",
            mcp_server_name="chartforge",
        )
        repo.upsert_credentials(
            instance_id="existing", blob=encrypt_credentials(connector.services.secret_repo, old)
        )
        connector.repo = repo
        context = {
            **connector.settings(),
            "client_id": "client",
            "client_secret": "secret",
            "redirect_uri": "http://octop.internal/api/connectors/oauth/callback",
            "created_at": time.time(),
        }
        connector.services.settings_repo.set(
            cf.CONTEXT_KEY + "state",
            base64.b64encode(encrypt_credentials(connector.services.secret_repo, context)).decode(
                "ascii"
            ),
        )
        row = SimpleNamespace(state_id="state", user_id=7, code_verifier="verifier")
        request = AsyncMock(
            side_effect=[
                response(
                    {"access_token": "new", "refresh_token": "new-refresh", "expires_in": 1800}
                ),
                response({"employeeNo": "0007"}),
                response({}),
                response({}),
            ]
        )
        monkeypatch.setattr(cf, "request", request)
        await connector.complete(row, "code")
        saved = repo.get_by_user_kind(7, "chartforge")
        assert saved is not None and saved.display_name == "AI报表查询" and not saved.shared
        assert (
            decrypt_credentials(connector.services.secret_repo, saved.credential_blob)[
                "access_token"
            ]
            == "new"
        )
        assert request.call_args_list[-1].args[2] == "/oauth2/revoke"
    finally:
        pool.close()


@pytest.mark.asyncio
async def test_identity_preserves_leading_zero_and_rejects_mismatch(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = {"access_token": "x"}
    monkeypatch.setattr(cf, "request", AsyncMock(return_value=response({"employeeNo": "7"})))
    with pytest.raises(OctopError):
        await connector._check_identity(connector.settings(), creds, 7)


@pytest.mark.asyncio
async def test_changed_origin_does_not_receive_old_token(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector.save_settings({"base_url": "http://10.0.0.10", "enabled": True, "allow_http": True})
    request = AsyncMock()
    monkeypatch.setattr(cf, "request", request)
    with pytest.raises(OctopError):
        await connector.call(7, "list_connections", {})
    request.assert_not_called()


@pytest.mark.asyncio
async def test_metadata_and_reserved_addresses_are_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import socket

    for address in ("169.254.169.254", "0.0.0.0", "224.0.0.1", "240.0.0.1"):
        monkeypatch.setattr(
            asyncio.get_running_loop(),
            "getaddrinfo",
            AsyncMock(return_value=[(socket.AF_INET, 1, 6, "", (address, 80))]),
        )
        with pytest.raises(OctopError):
            await cf.request(
                {"base_url": "http://configured.internal", "allow_http": True}, "GET", "/gateway/me"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_public_platform_domain_uses_pinned_origin_for_all_connector_endpoints(
    monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    import asyncio
    import socket

    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(return_value=[(socket.AF_INET, 1, 6, "", ("183.239.187.105", 5000))]),
    )
    seen: list[httpx.Request] = []
    pinned: list[tuple[str, str]] = []

    def transport(host: str, address: str) -> httpx.MockTransport:
        pinned.append((host, address))

        def handle(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json={})

        return httpx.MockTransport(handle)

    monkeypatch.setattr(cf, "PinnedIPTransport", transport)
    origin = f"{scheme}://ai.dgkaijin.com:5000"
    config = {"base_url": origin, "allow_http": scheme == "http"}
    for method, path in (
        ("GET", "/.well-known/oauth-authorization-server"),
        ("POST", "/gateway/oauth/register"),
        ("POST", "/oauth2/token"),
        ("GET", "/gateway/me"),
        ("POST", "/mcp/sql"),
        ("POST", "/oauth2/revoke"),
    ):
        await cf.request(config, method, path)
        assert str(seen[-1].url) == origin + path
        assert seen[-1].headers["Host"] == "ai.dgkaijin.com:5000"
    assert pinned == [("ai.dgkaijin.com", "183.239.187.105")] * 6


@pytest.mark.asyncio
async def test_public_http_still_requires_admin_opt_in_and_cannot_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import socket

    resolver = AsyncMock(return_value=[(socket.AF_INET, 1, 6, "", ("183.239.187.105", 5000))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    config = {"base_url": "http://ai.dgkaijin.com:5000", "allow_http": False}
    with pytest.raises(OctopError) as denied:
        await cf.request(config, "GET", "/gateway/me")
    assert denied.value.details["reason_key"] == "chartforge.httpNotAllowed"
    resolver.assert_not_awaited()
    monkeypatch.setattr(
        cf,
        "PinnedIPTransport",
        lambda *_: httpx.MockTransport(
            lambda req: httpx.Response(302, headers={"Location": "http://other.example"})
        ),
    )
    with pytest.raises(OctopError) as redirected:
        await cf.request({**config, "allow_http": True}, "GET", "/gateway/me")
    assert redirected.value.details["reason_key"] == "chartforge.redirectNotAllowed"


@pytest.mark.asyncio
async def test_shared_agent_tools_resolve_current_turn_identity(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from contextvars import ContextVar

    import langgraph.config

    current: ContextVar[int] = ContextVar("test_chartforge_user")
    monkeypatch.setattr(
        langgraph.config, "get_config", lambda: {"configurable": {"user": current.get()}}
    )
    request = AsyncMock(return_value=response({"result": {"content": []}}))
    monkeypatch.setattr(cf, "request", request)
    tool = cf.build_tools(connector.services)[0]

    async def invoke(uid: int) -> None:
        token = current.set(uid)
        try:
            await tool.ainvoke({})
        finally:
            current.reset(token)

    await asyncio.gather(invoke(7), invoke(8))
    assert {call.kwargs["headers"]["Authorization"] for call in request.call_args_list} == {
        "Bearer token-7",
        "Bearer token-8",
    }


@pytest.mark.asyncio
async def test_revoke_never_sends_old_credentials_to_new_origin(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = AsyncMock()
    monkeypatch.setattr(cf, "request", request)
    creds = decrypt_credentials(
        connector.services.secret_repo, connector.repo.rows[7].credential_blob
    )
    await connector._revoke({"base_url": "http://10.0.0.10"}, creds)
    request.assert_not_called()


@pytest.mark.asyncio
async def test_missing_turn_identity_is_rejected(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    import langgraph.config

    monkeypatch.setattr(langgraph.config, "get_config", lambda: {})
    request = AsyncMock()
    monkeypatch.setattr(cf, "request", request)
    with pytest.raises(OctopError):
        await cf.build_tools(connector.services)[0].ainvoke({})
    request.assert_not_called()


@pytest.mark.asyncio
async def test_expired_refresh_grant_marks_connection_as_needing_authorization(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = connector.repo.rows[7]
    creds = decrypt_credentials(connector.services.secret_repo, row.credential_blob)
    creds["expires_at"] = 0
    row.credential_blob = encrypt_credentials(connector.services.secret_repo, creds)
    monkeypatch.setattr(
        cf, "request", AsyncMock(return_value=response({"error": "invalid_grant"}, 400))
    )
    with pytest.raises(OctopError):
        await connector.call(7, "list_connections", {})
    assert connector.status(7)["connected"] is False
    assert connector.status(7)["needs_reauthorization"] is True


@pytest.mark.asyncio
async def test_platform_error_envelope_is_not_reported_as_wrong_employee(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cf, "request", AsyncMock(return_value=response({"code": 500, "msg": "unavailable"}))
    )
    with pytest.raises(OctopError) as caught:
        await connector._check_identity(connector.settings(), {"access_token": "test"}, 7)
    assert caught.value.details["reason_key"] == "chartforge.connectionFailed"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_first_employee", [False, True])
async def test_multiple_employee_grants_preserve_namespace_and_isolate_credentials(
    connector: cf.ChartForgeConnector,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_first_employee: bool,
) -> None:
    import asyncio

    from octop.config import OctopConfig
    from octop.infra.agents.manager import AgentManager
    from octop.infra.connectors.builder import (
        build_mcp_server_configs_for_user,
        gateway_mcp_server_names,
    )
    from octop.infra.connectors.service import ConnectorService
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.connectors import ConnectorRepo

    pool = SqlitePool(tmp_path / "employees.db")
    try:
        run_migrations(pool)
        with pool.transaction() as db:
            for uid in (7, 8, 9):
                db.execute(
                    "INSERT INTO users(id,username,password_hash,role,created_at) VALUES(?,?,'test','user',1)",
                    (uid, f"{uid:04}"),
                )
        repo = ConnectorRepo(pool)
        if legacy_first_employee:
            repo.create(
                instance_id="legacy",
                user_id=7,
                kind="chartforge",
                display_name="AI报表查询",
                mcp_server_name="chartforge",
                config_json=json.dumps({"default_open": True}),
            )
            repo.upsert_credentials(
                instance_id="legacy", blob=connector.repo.rows[7].credential_blob
            )
        connector.repo = repo
        connector.services.repos = SimpleNamespace(
            connector_repo=repo,
            secret_repo=connector.services.secret_repo,
            settings_repo=connector.services.settings_repo,
            user_repo=connector.services.user_repo,
        )

        async def platform_request(
            config: dict[str, Any], method: str, path: str, **kwargs: Any
        ) -> httpx.Response:
            if path == "/oauth2/token":
                code = kwargs["data"]["code"]
                return response(
                    {"access_token": f"token-{code}", "refresh_token": f"refresh-{code}"}
                )
            if path == "/gateway/me":
                uid = int(kwargs["headers"]["Authorization"].split("-")[1])
                return response({"employeeNo": f"{uid:04}"})
            if path == "/mcp/sql":
                return response({"result": {"content": []}})
            assert path == "/oauth2/revoke"
            return response({})

        request = AsyncMock(side_effect=platform_request)
        monkeypatch.setattr(cf, "request", request)

        async def authorize(uid: int, suffix: str = "first") -> None:
            state_id = f"{uid}-{suffix}"
            context = {
                **connector.settings(),
                "client_id": f"client-{uid}",
                "client_secret": f"secret-{uid}",
                "redirect_uri": "http://octop.internal/api/connectors/oauth/callback",
                "created_at": time.time(),
            }
            connector.services.settings_repo.set(
                cf.CONTEXT_KEY + state_id,
                base64.b64encode(
                    encrypt_credentials(connector.services.secret_repo, context)
                ).decode("ascii"),
            )
            await connector.complete(
                SimpleNamespace(state_id=state_id, user_id=uid, code_verifier="verifier"),
                f"{uid}-{suffix}",
            )

        if not legacy_first_employee:
            await authorize(7)
        first = repo.get_by_user_kind(7, "chartforge")
        assert first is not None
        # Both new installations and existing single-user installations must work.
        await asyncio.gather(authorize(8), authorize(9))
        assert repo.get_by_user_kind(7, "chartforge") == first
        rows = repo.list_by_kind("chartforge")
        assert len(rows) == len({row.mcp_server_name for row in rows}) == 3
        assert all(not row.shared for row in rows)

        config = OctopConfig()
        service = ConnectorService(
            repo=repo,
            secret_repo=connector.services.secret_repo,
            settings_repo=connector.services.settings_repo,
            config=config,
        )
        for uid in (7, 8, 9):
            assert connector.status(uid)["connected"] is True
            assert service.list_active_mcp_server_names(uid) == ["chartforge"]
            assert service.list_default_open_mcp_server_names(uid) == ["chartforge"]
            assert service.merge_turn_mcp_servers(uid, None) == ["chartforge"]
            assert service.merge_turn_mcp_servers(uid, [], apply_defaults=False) is None
            assert service.merge_turn_mcp_servers(uid, ["chartforge"], apply_defaults=False) == [
                "chartforge"
            ]
            assert service.validate_mcp_servers_for_user(uid, ["chartforge"]) == ["chartforge"]
            assert service.reserved_builtin_mcp_names(uid) == {"chartforge"}
            instances = service.list_instances_for_api(uid)
            assert len(instances) == 1 and instances[0]["mcp_server_name"] == "chartforge"
            assert instances[0]["owner_user_id"] == uid
            configs = build_mcp_server_configs_for_user(
                svc=service,
                connector_repo=repo,
                user_id=uid,
                agent_id="test",
                agent_user_id=uid,
                config=config,
                log=False,
            )
            assert configs == {"chartforge": {}}
            assert await service.mcp_configs_for_user(uid) == {"chartforge": {}}
            assert gateway_mcp_server_names(connector_repo=repo, user_id=uid) == {"chartforge"}
            tools: list[Any] = []
            agent = SimpleNamespace(_mcp_tools=tools, inject_mcp_tools=tools.extend)
            manager = SimpleNamespace(_repos=connector.services.repos)
            AgentManager._attach_chartforge_tools(manager, agent, configs)
            assert len(tools) == 7
            assert {tool.name for tool in tools} == {
                tool.name for tool in cf.build_tools(connector.services)
            }
            assert (
                filter_tools_for_mcp_servers(
                    tools,
                    mcp_tool_names=mcp_tool_names(tools),
                    server_names=frozenset(configs),
                    active_servers=["chartforge"],
                )
                == tools
            )
            assert (
                filter_tools_for_mcp_servers(
                    tools,
                    mcp_tool_names=mcp_tool_names(tools),
                    server_names=frozenset(configs),
                    active_servers=[],
                )
                == []
            )
            AgentManager._attach_chartforge_tools(manager, agent, configs)
            assert len(tools) == 7

        second = repo.get_by_user_kind(8, "chartforge")
        await authorize(8, "again")
        updated = repo.get_by_user_kind(8, "chartforge")
        assert second is not None and updated is not None
        assert updated.instance_id == second.instance_id
        assert updated.mcp_server_name == second.mcp_server_name
        assert len(repo.list_by_kind("chartforge")) == 3
        assert repo.get_by_user_kind(7, "chartforge") == first
        request.reset_mock()
        await asyncio.gather(*(connector.call(uid, "list_connections", {}) for uid in (7, 8, 9)))
        assert {call.kwargs["headers"]["Authorization"] for call in request.call_args_list} == {
            "Bearer token-7" if legacy_first_employee else "Bearer token-7-first",
            "Bearer token-8-again",
            "Bearer token-9-first",
        }
        request.reset_mock()
        await connector.disconnect(8)
        assert repo.get_by_user_kind(8, "chartforge") is None
        assert repo.get_by_user_kind(7, "chartforge") == first
        assert connector.status(9)["connected"] is True
        assert {call.kwargs["data"]["token"] for call in request.call_args_list} == {
            "token-8-again",
            "refresh-8-again",
        }
    finally:
        pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["zh", "en"])
@pytest.mark.parametrize("reason", ["identityMismatch", "connectionFailed", None])
async def test_callback_distinguishes_employee_and_server_errors(
    connector: cf.ChartForgeConnector,
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
    reason: str | None,
) -> None:
    from fastapi import Request

    from octop.api.routers.chartforge import complete_callback
    from octop.i18n import tr

    error = cf.failure(reason) if reason else RuntimeError("private database details <secret>")
    monkeypatch.setattr(cf.ChartForgeConnector, "complete", AsyncMock(side_effect=error))
    result = await complete_callback(
        Request({"type": "http", "headers": [(b"accept-language", locale.encode())]}),
        SimpleNamespace(services=connector.services, app_runtime=None),
        SimpleNamespace(user_id=8),
        "code",
    )
    assert result.status_code == (400 if reason else 500)
    html = bytes(result.body).decode()
    assert tr(f"chartforge.{reason or 'authorizationFailed'}", locale) in html
    assert "private database details" not in html


@pytest.mark.asyncio
async def test_failed_persistence_revokes_only_the_new_grant(
    connector: cf.ChartForgeConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = {
        **connector.settings(),
        "client_id": "client",
        "client_secret": "secret",
        "redirect_uri": "http://octop.internal/api/connectors/oauth/callback",
        "created_at": time.time(),
    }
    connector.services.settings_repo.set(
        cf.CONTEXT_KEY + "state",
        base64.b64encode(encrypt_credentials(connector.services.secret_repo, context)).decode(
            "ascii"
        ),
    )
    original = connector.repo.rows[7].credential_blob
    monkeypatch.setattr(connector.repo, "get_by_user_kind", lambda *_: None)
    monkeypatch.setattr(
        connector.repo,
        "create",
        Mock(side_effect=RuntimeError("database unavailable")),
        raising=False,
    )
    request = AsyncMock(
        side_effect=[
            response({"access_token": "new-token", "refresh_token": "new-refresh"}),
            response({"employeeNo": "0007"}),
            response({}),
            response({}),
        ]
    )
    monkeypatch.setattr(cf, "request", request)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await connector.complete(
            SimpleNamespace(state_id="state", user_id=7, code_verifier="v"), "code"
        )
    assert connector.repo.rows[7].credential_blob == original
    assert {call.kwargs["data"]["token"] for call in request.call_args_list[2:]} == {
        "new-token",
        "new-refresh",
    }

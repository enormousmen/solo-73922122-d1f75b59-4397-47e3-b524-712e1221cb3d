"""End-to-end tests for protection mode: download grants at the proxy.

Runs the real relay over TCP with RELAY_PROTECT_DOWNLOADS=1 and drives it
with an HTTP client, exactly like the unprotected integration tests.
"""

from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest

from .test_proxy_integration import stack, status, switch

pytestmark = pytest.mark.asyncio

ADMIN_KEY = "test-admin-key"
PROTECT_ENV = {"RELAY_PROTECT_DOWNLOADS": "1", "RELAY_ADMIN_KEY": ADMIN_KEY}


async def grant(
    base: str,
    path: str,
    query: str,
    *,
    method: str = "GET",
    ttl: int = 600,
    key: str | None = ADMIN_KEY,
) -> tuple[int, dict]:
    headers = {"authorization": f"Bearer {key}"} if key is not None else {}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            base + "/api/download-grants",
            json={"path": path, "query": query, "method": method, "ttl_seconds": ttl},
            headers=headers,
        ) as response:
            return response.status, await response.json()


def bearer(payload: dict) -> dict[str, str]:
    return {"authorization": payload["authorization"]}


async def upstream_stats(base: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(base + "/_state") as response:
            return (await response.json())["stats"]


async def test_grants_endpoint_issues_token_bound_to_current_generation():
    async with stack(extra_env=PROTECT_ENV) as s:
        code, payload = await grant(s["base"], "/artifact", "bytes=64&chunk=32", ttl=120)
        assert code == 200
        assert payload["generation"] == 1
        assert payload["method"] == "GET"
        assert payload["path"] == "/artifact"
        assert payload["query"] == "bytes=64&chunk=32"
        assert payload["expires_in_seconds"] == 120
        assert payload["token_type"] == "bearer"
        assert payload["grant"].startswith("grv1.")
        assert payload["authorization"] == "Bearer " + payload["grant"]


async def test_grants_endpoint_requires_admin_key():
    async with stack(extra_env=PROTECT_ENV) as s:
        code, _ = await grant(s["base"], "/artifact", "", key=None)
        assert code == 401
        code, _ = await grant(s["base"], "/artifact", "", key="wrong-key")
        assert code == 401


async def test_grants_endpoint_validates_request():
    async with stack(extra_env=PROTECT_ENV) as s:
        code, _ = await grant(s["base"], "/artifact", "", ttl=601)
        assert code == 422  # over the 10 minute cap
        code, _ = await grant(s["base"], "/artifact", "", ttl=0)
        assert code == 422
        async with aiohttp.ClientSession() as session:
            async with session.post(
                s["base"] + "/api/download-grants",
                json={"path": "no-slash", "query": "", "method": "GET"},
                headers={"authorization": f"Bearer {ADMIN_KEY}"},
            ) as response:
                assert response.status == 422
            async with session.post(
                s["base"] + "/api/download-grants",
                json={"path": "/artifact", "query": "", "method": "POST"},
                headers={"authorization": f"Bearer {ADMIN_KEY}"},
            ) as response:
                assert response.status == 422


async def test_grants_endpoint_disabled_without_admin_key():
    async with stack() as s:  # no RELAY_ADMIN_KEY configured
        code, payload = await grant(s["base"], "/artifact", "")
        assert code == 503
        assert "disabled" in payload["detail"]


async def test_missing_grant_is_401_and_upstream_is_not_touched():
    async with stack(extra_env=PROTECT_ENV) as s:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{s['base']}/artifact?bytes=100&chunk=10") as response:
                assert response.status == 401
                assert response.headers["www-authenticate"].startswith("Bearer")
                assert (await response.json())["error"] == "grant_required"
            # The control API itself is not behind grants.
            async with session.get(s["base"] + "/api/status") as response:
                assert response.status == 200
                assert (await response.json())["protection"]["enabled"] is True
        stats = await upstream_stats(s["blue"])
        assert stats["started"] == 0  # nothing was forwarded upstream


async def test_valid_grant_downloads_intact_body():
    async with stack(extra_env=PROTECT_ENV) as s:
        query = "bytes=200000&chunk=8192"
        code, payload = await grant(s["base"], "/artifact", query)
        assert code == 200
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{s['base']}/artifact?{query}", headers=bearer(payload)
            ) as response:
                assert response.status == 200
                assert response.headers["x-relay-target"] == "blue"
                body = await response.read()
        assert len(body) == 200000
        assert body == (b"0123456789abcdef" * (200000 // 16 + 1))[:200000]


async def test_tampered_grant_is_401():
    async with stack(extra_env=PROTECT_ENV) as s:
        _, payload = await grant(s["base"], "/artifact", "bytes=64&chunk=32")
        bad = payload["grant"][:-1] + ("A" if payload["grant"][-1] != "A" else "B")
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{s['base']}/artifact?bytes=64&chunk=32",
                headers={"authorization": f"Bearer {bad}"},
            ) as response:
                assert response.status == 401
                assert (await response.json())["error"] == "bad_signature"


async def test_expired_grant_is_401():
    async with stack(extra_env=PROTECT_ENV) as s:
        _, payload = await grant(s["base"], "/artifact", "bytes=64&chunk=32", ttl=1)
        await asyncio.sleep(1.2)  # exp is whole seconds; ttl=1 lapses within 1s
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{s['base']}/artifact?bytes=64&chunk=32", headers=bearer(payload)
            ) as response:
                assert response.status == 401
                assert (await response.json())["error"] == "expired"


async def test_request_must_match_grant_exactly():
    async with stack(extra_env=PROTECT_ENV) as s:
        _, payload = await grant(s["base"], "/artifact", "bytes=100&chunk=10")
        async with aiohttp.ClientSession() as session:
            # Different query string.
            async with session.get(
                f"{s['base']}/artifact?bytes=200&chunk=10", headers=bearer(payload)
            ) as response:
                assert response.status == 403
                assert (await response.json())["error"] == "target_mismatch"
            # Different path.
            async with session.get(f"{s['base']}/fixed", headers=bearer(payload)) as response:
                assert response.status == 403
            # Different method (grant is GET, request is HEAD).
            async with session.head(
                f"{s['base']}/artifact?bytes=100&chunk=10", headers=bearer(payload)
            ) as response:
                assert response.status == 403  # HEAD has no body to inspect
        # A HEAD grant covers HEAD exactly.
        _, head_grant = await grant(s["base"], "/artifact", "bytes=100&chunk=10", method="HEAD")
        async with aiohttp.ClientSession() as session:
            async with session.head(
                f"{s['base']}/artifact?bytes=100&chunk=10", headers=bearer(head_grant)
            ) as response:
                assert response.status == 200
                assert await response.read() == b""


async def test_grant_dies_with_its_generation():
    async with stack(extra_env=PROTECT_ENV) as s:
        query = "bytes=64&chunk=32"
        _, old = await grant(s["base"], "/artifact", query)
        assert old["generation"] == 1
        code, _ = await switch(s["base"], "green")
        assert code == 200
        _, fresh = await grant(s["base"], "/artifact", query)
        assert fresh["generation"] == 2
        async with aiohttp.ClientSession() as session:
            # The pre-switch grant is rejected on the new generation.
            async with session.get(
                f"{s['base']}/artifact?{query}", headers=bearer(old)
            ) as response:
                assert response.status == 403
                assert (await response.json())["error"] == "generation_mismatch"
            # A grant minted after the switch works and routes to green.
            async with session.get(
                f"{s['base']}/artifact?{query}", headers=bearer(fresh)
            ) as response:
                assert response.status == 200
                assert response.headers["x-relay-target"] == "green"
                assert response.headers["x-relay-generation"] == "2"
                await response.read()


async def test_grant_is_not_forwarded_upstream():
    async with stack(extra_env=PROTECT_ENV) as s:
        _, payload = await grant(s["base"], "/_echo", "")
        headers = {**bearer(payload), "x-keep-me": "yes"}
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{s['base']}/_echo", headers=headers) as response:
                assert response.status == 200
                echoed = (await response.json())["headers"]
        assert "authorization" not in echoed  # the grant stays at the relay
        assert echoed.get("x-keep-me") == "yes"  # ordinary headers still pass


async def test_in_flight_download_drains_normally_when_protected():
    """A download accepted before the switch keeps its generation's 5s-style
    drain semantics even though its grant is stale the moment after."""
    async with stack(drain_seconds=1.0, extra_env=PROTECT_ENV) as s:
        query = "bytes=1200000&chunk=4000&delay=0.02"
        _, payload = await grant(s["base"], "/artifact", query)
        async with aiohttp.ClientSession() as session:
            response = await session.get(
                f"{s['base']}/artifact?{query}", headers=bearer(payload)
            )
            assert response.status == 200
            assert response.headers["x-relay-generation"] == "1"
            await asyncio.sleep(0.1)

            switch_started = time.monotonic()
            code, _ = await switch(s["base"], "green")
            assert code == 200

            # The same grant is useless for *new* requests now...
            async with session.get(
                f"{s['base']}/artifact?{query}", headers=bearer(payload)
            ) as stale:
                assert stale.status == 403

            # ...but the already-accepted download keeps streaming on the old
            # generation until the drain window expires.
            chunk = await asyncio.wait_for(response.content.readany(), timeout=0.6)
            assert chunk
            with pytest.raises(aiohttp.ClientError):
                async for _ in response.content.iter_any():
                    pass
            assert time.monotonic() - switch_started < 3.0
            response.close()

            snap = await status(s["base"])
            old = [g for g in snap["generations"] if g["id"] == 1][0]
            assert old["state"] == "closed"
            assert old["in_flight"] == 0


async def test_openapi_and_demo_cover_the_grants_flow():
    async with stack(extra_env=PROTECT_ENV) as s:
        async with aiohttp.ClientSession() as session:
            async with session.get(s["base"] + "/openapi.json") as response:
                spec = await response.json()
            async with session.get(s["base"] + "/demo") as response:
                page = await response.text()
        post = spec["paths"]["/api/download-grants"]["post"]
        assert post["security"], "grants endpoint must show its bearer scheme in /docs"
        schemes = spec["components"]["securitySchemes"]
        assert any(s.get("scheme") == "bearer" for s in schemes.values())
        assert "download-grants" in page  # the demo page wires the grant flow

"""
WM800 UCP Mock Server — test suite.

Run:
    cd adapters/ucp/mock
    pytest test_server.py -v
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import pytest
import httpx
from httpx import ASGITransport
from fastapi.testclient import TestClient

import server
from server import (
    app, CLIENT_ID, CLIENT_SECRET, CONTINUE_URL, CATALOG,
    _checkouts, _order_events, _TERMINAL,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean():
    """Wipe all state before every test."""
    _checkouts.clear()
    _order_events.clear()
    yield
    _checkouts.clear()
    _order_events.clear()

@pytest.fixture
def http():
    return TestClient(app)

@pytest.fixture
def tok(http):
    r = http.post("/oauth/token", data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    assert r.status_code == 200
    return r.json()["access_token"]

@pytest.fixture
def auth(tok):
    return {"Authorization": f"Bearer {tok}"}

def checkout_and_complete(http, auth, lane: int) -> tuple[dict, dict]:
    """Helper: create checkout + complete it in one call."""
    chk_r = http.post("/checkout-sessions", headers=auth, json={
        "line_items": [{"id": f"lane_{lane}", "quantity": 1}],
        "payment": {"handler_id": "prepaid", "instrument": {"token": "pay-test"}},
    })
    assert chk_r.status_code == 201  # UCP spec: 201 Created for new checkout
    chk = chk_r.json()

    cmp_r = http.post(f"/checkout-sessions/{chk['id']}/complete", headers=auth)
    assert cmp_r.status_code == 200
    return chk, cmp_r.json()

# ── 1. UCP Discovery ──────────────────────────────────────────────────────────

class TestDiscovery:
    def test_ucp_profile_shape(self, http):
        r = http.get("/.well-known/ucp")
        assert r.status_code == 200
        d = r.json()
        assert d["ucp_version"] == "2026-04-08"
        assert "dev.ucp.shopping.cart"     in d["capabilities"]
        assert "dev.ucp.shopping.checkout" in d["capabilities"]
        assert "dev.ucp.shopping.order"    in d["capabilities"]
        assert len(d["payment_handlers"])  >= 1   # prepaid + optional alipay/gpay handlers
        assert d["payment_handlers"][0]["handler_id"] == "prepaid"

    def test_oauth_discovery(self, http):
        r = http.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        d = r.json()
        assert "client_credentials" in d["grant_types_supported"]
        assert d["token_endpoint"].endswith("/oauth/token")

    def test_discovery_no_auth_required(self, http):
        """Profile endpoints must be public per UCP spec."""
        assert http.get("/.well-known/ucp").status_code == 200
        assert http.get("/.well-known/oauth-authorization-server").status_code == 200

# ── 2. OAuth 2.0 ─────────────────────────────────────────────────────────────

class TestAuth:
    def test_valid_credentials(self, http):
        r = http.post("/oauth/token", data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
        assert r.status_code == 200
        d = r.json()
        assert "access_token" in d
        assert d["token_type"] == "bearer"
        assert d["expires_in"] == 3600

    def test_wrong_secret(self, http):
        r = http.post("/oauth/token", data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": "wrong",
        })
        assert r.status_code == 401

    def test_unsupported_grant_type(self, http):
        r = http.post("/oauth/token", data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
        assert r.status_code == 400

    def test_protected_endpoint_no_token(self, http):
        assert http.post("/cart-sessions").status_code == 401

    def test_protected_endpoint_bad_token(self, http):
        r = http.post("/cart-sessions", headers={"Authorization": "Bearer bad.jwt.token"})
        assert r.status_code == 401

    def test_token_is_valid_jwt(self, http, tok):
        import jwt as _jwt
        claims = _jwt.decode(tok, server.JWT_SECRET, algorithms=["HS256"])
        assert claims["sub"] == CLIENT_ID

# ── 3. UCP Envelope ───────────────────────────────────────────────────────────

class TestUcpEnvelope:
    """Every UCP response must carry the envelope + continue_url."""

    def _assert_envelope(self, body: dict, resource: str):
        assert "ucp" in body, "missing ucp envelope"
        assert body["ucp"]["version"] == "2026-04-08"
        assert f"dev.ucp.shopping.{resource}" in body["ucp"]["capabilities"]
        assert "continue_url" in body, "continue_url must always be present"
        assert body["continue_url"] == CONTINUE_URL

    def test_cart_has_envelope(self, http, auth):
        self._assert_envelope(http.post("/cart-sessions", headers=auth).json(), "cart")

    def test_checkout_has_envelope(self, http, auth):
        r = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_100", "quantity": 1}],
        })
        self._assert_envelope(r.json(), "checkout")

    def test_order_has_envelope(self, http, auth):
        _, cmp = checkout_and_complete(http, auth, lane=100)
        oid = cmp["order_id"]
        # Inject a terminal event so the order exists in the event store
        _order_events[oid] = [{"ts": time.time(), "action": 0x03, "name": "goods_taken"}]
        r = http.get(f"/orders/{oid}", headers=auth)
        self._assert_envelope(r.json(), "order")

    def test_error_response_has_envelope_and_continue_url(self, http, auth):
        """Error responses must still carry continue_url — the fallback."""
        # Lane 901 is offline → checkout complete returns error
        chk = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_901", "quantity": 1}],
            "payment": {"handler_id": "prepaid", "instrument": {"token": "x"}},
        }).json()
        err = http.post(f"/checkout-sessions/{chk['id']}/complete", headers=auth).json()

        assert err["status"] == "error"
        assert "continue_url" in err
        assert err["continue_url"] == CONTINUE_URL
        assert len(err["messages"]) > 0
        assert err["messages"][0]["type"] == "error"

# ── 4. Cart ───────────────────────────────────────────────────────────────────

class TestCart:
    def test_returns_all_catalog_lanes(self, http, auth):
        d = http.post("/cart-sessions", headers=auth).json()
        ids = {item["id"] for item in d["line_items"]}
        assert ids == {f"lane_{k}" for k in CATALOG}

    def test_item_shape(self, http, auth):
        items = http.post("/cart-sessions", headers=auth).json()["line_items"]
        for item in items:
            assert "id"    in item
            assert "name"  in item
            assert "price" in item
            assert "amount"   in item["price"]
            assert "currency" in item["price"]
            assert "quantity_available" in item

    def test_empty_lane_quantity_zero(self, http, auth):
        items = http.post("/cart-sessions", headers=auth).json()["line_items"]
        lane_900 = next(i for i in items if i["id"] == "lane_900")
        assert lane_900["quantity_available"] == 0

    def test_status_is_incomplete(self, http, auth):
        assert http.post("/cart-sessions", headers=auth).json()["status"] == "incomplete"

# ── 5. Checkout ───────────────────────────────────────────────────────────────

class TestCheckout:
    def test_create_returns_201(self, http, auth):
        """UCP spec: POST /checkout-sessions must return 201 Created."""
        r = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_100", "quantity": 1}],
            "payment": {"handler_id": "prepaid", "instrument": {"token": "t"}},
        })
        assert r.status_code == 201

    def test_checkout_has_currency(self, http, auth):
        """UCP required field: currency (ISO 4217)."""
        r = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_100", "quantity": 1}],
        }).json()
        assert "currency" in r
        assert len(r["currency"]) == 3  # ISO 4217 is 3 chars

    def test_checkout_has_totals(self, http, auth):
        """UCP required field: totals with subtotal + total entries."""
        r = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_100", "quantity": 1}],
        }).json()
        assert "totals" in r
        types = {t["type"] for t in r["totals"]}
        assert "subtotal" in types
        assert "total" in types

    def test_totals_reflect_catalog_price(self, http, auth):
        r = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_100", "quantity": 1}],  # price=200 fen
        }).json()
        total = next(t for t in r["totals"] if t["type"] == "total")
        assert total["amount"] == 200

    def test_get_checkout_by_id(self, http, auth):
        """GET /checkout-sessions/{id} must return current checkout state."""
        chk = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_101", "quantity": 1}],
        }).json()
        fetched = http.get(f"/checkout-sessions/{chk['id']}", headers=auth).json()
        assert fetched["id"] == chk["id"]
        assert fetched["status"] == chk["status"]

    def test_get_unknown_checkout_is_404(self, http, auth):
        assert http.get("/checkout-sessions/chk_unknown", headers=auth).status_code == 404

    def test_no_payment_gives_incomplete(self, http, auth):
        r = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_100", "quantity": 1}],
        })
        assert r.json()["status"] == "incomplete"

    def test_with_payment_gives_ready_for_complete(self, http, auth):
        r = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_100", "quantity": 1}],
            "payment": {"handler_id": "prepaid", "instrument": {"token": "t"}},
        })
        assert r.json()["status"] == "ready_for_complete"

    def test_empty_payment_token_stays_incomplete(self, http, auth):
        r = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_100", "quantity": 1}],
            "payment": {"handler_id": "prepaid", "instrument": {"token": ""}},
        })
        assert r.json()["status"] == "incomplete"

    def test_no_line_items_is_400(self, http, auth):
        r = http.post("/checkout-sessions", headers=auth, json={"line_items": []})
        assert r.status_code == 400

    def test_invalid_item_id_is_400(self, http, auth):
        r = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "not-a-lane", "quantity": 1}],
        })
        assert r.status_code == 400

    def test_checkout_has_unique_ids(self, http, auth):
        def make():
            return http.post("/checkout-sessions", headers=auth, json={
                "line_items": [{"id": "lane_100", "quantity": 1}],
            }).json()["id"]
        assert make() != make()

    def test_complete_not_ready_is_422(self, http, auth):
        chk = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_100", "quantity": 1}],
            # no payment → status stays "incomplete"
        }).json()
        r = http.post(f"/checkout-sessions/{chk['id']}/complete", headers=auth)
        assert r.status_code == 422

    def test_complete_unknown_checkout_is_404(self, http, auth):
        assert http.post("/checkout-sessions/chk_doesnotexist/complete",
                         headers=auth).status_code == 404

    def test_complete_returns_order_id(self, http, auth):
        _, cmp = checkout_and_complete(http, auth, lane=100)
        assert cmp["status"] == "completed"
        assert "order_id" in cmp
        assert len(cmp["order_id"]) == 16

    def test_complete_is_idempotent(self, http, auth):
        chk, cmp1 = checkout_and_complete(http, auth, lane=100)
        cmp2 = http.post(f"/checkout-sessions/{chk['id']}/complete", headers=auth).json()
        assert cmp1["order_id"] == cmp2["order_id"]
        assert cmp2["status"] == "completed"

# ── 6. Error scenarios ────────────────────────────────────────────────────────

class TestErrorScenarios:
    def test_offline_lane_returns_device_unavailable(self, http, auth):
        _, cmp = checkout_and_complete(http, auth, lane=901)
        assert cmp["status"] == "error"
        assert cmp["messages"][0]["code"] == "device_unavailable"

    @pytest.mark.anyio
    async def test_empty_lane_order_shows_dispense_failed(self):
        """
        Background task runs in the asyncio event loop; we must await to let it fire.
        Uses httpx.AsyncClient so that await asyncio.sleep() yields control to tasks.
        """
        _checkouts.clear()
        _order_events.clear()
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/oauth/token", data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            })
            tok = r.json()["access_token"]
            hdrs = {"Authorization": f"Bearer {tok}"}

            chk = (await client.post("/checkout-sessions", headers=hdrs, json={
                "line_items": [{"id": "lane_900", "quantity": 1}],
                "payment": {"handler_id": "prepaid", "instrument": {"token": "x"}},
            })).json()
            cmp = (await client.post(f"/checkout-sessions/{chk['id']}/complete",
                                     headers=hdrs)).json()
            oid = cmp["order_id"]

            # Await in small increments so the event loop can run the background task.
            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                if any(e["action"] == 0x08 for e in _order_events.get(oid, [])):
                    break
                await asyncio.sleep(0.1)

            r = (await client.get(f"/orders/{oid}", headers=hdrs)).json()
        assert r["status"] == "error"
        assert r["messages"][0]["code"] == "dispense_failed"
        assert "continue_url" in r

    def test_continue_url_present_on_all_errors(self, http, auth):
        # offline lane
        chk = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_901", "quantity": 1}],
            "payment": {"handler_id": "prepaid", "instrument": {"token": "x"}},
        }).json()
        err = http.post(f"/checkout-sessions/{chk['id']}/complete", headers=auth).json()
        assert err.get("continue_url") == CONTINUE_URL

# ── 7. Order tracking ─────────────────────────────────────────────────────────

class TestOrder:
    def test_unknown_order_is_404(self, http, auth):
        assert http.get("/orders/doesnotexist", headers=auth).status_code == 404

    def test_order_starts_incomplete(self, http, auth):
        _, cmp = checkout_and_complete(http, auth, lane=100)
        oid = cmp["order_id"]
        r = http.get(f"/orders/{oid}", headers=auth).json()
        # No events injected yet — should be incomplete
        assert r["status"] == "incomplete"
        assert r["fulfillment"]["method"] == "pickup"

    def test_order_complete_after_goods_taken(self, http, auth):
        _, cmp = checkout_and_complete(http, auth, lane=100)
        oid = cmp["order_id"]
        # Inject terminal event directly
        _order_events[oid] = [{"ts": time.time(), "action": 0x03, "name": "goods_taken"}]
        r = http.get(f"/orders/{oid}", headers=auth).json()
        assert r["status"] == "completed"

    def test_order_events_appear_in_response(self, http, auth):
        _, cmp = checkout_and_complete(http, auth, lane=100)
        oid = cmp["order_id"]
        _order_events[oid] = [
            {"ts": time.time(), "action": 0x01, "name": "door_open"},
            {"ts": time.time(), "action": 0x03, "name": "goods_taken"},
        ]
        events = http.get(f"/orders/{oid}", headers=auth).json()["fulfillment"]["events"]
        names = [e["type"] for e in events]
        assert "door_open"   in names
        assert "goods_taken" in names

    def test_order_has_currency(self, http, auth):
        """UCP required field: currency in order response."""
        _, cmp = checkout_and_complete(http, auth, lane=100)
        oid = cmp["order_id"]
        r = http.get(f"/orders/{oid}", headers=auth).json()
        assert "currency" in r
        assert len(r["currency"]) == 3

    def test_order_has_totals(self, http, auth):
        """UCP required field: totals in order response."""
        _, cmp = checkout_and_complete(http, auth, lane=101)  # price=500
        oid = cmp["order_id"]
        r = http.get(f"/orders/{oid}", headers=auth).json()
        assert "totals" in r
        types = {t["type"] for t in r["totals"]}
        assert "subtotal" in types and "total" in types
        total = next(t for t in r["totals"] if t["type"] == "total")
        assert total["amount"] == 500

    def test_error_message_uses_content_field(self, http, auth):
        """UCP spec: messages[].content not messages[].message."""
        chk = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_901", "quantity": 1}],
            "payment": {"handler_id": "prepaid", "instrument": {"token": "x"}},
        }).json()
        err = http.post(f"/checkout-sessions/{chk['id']}/complete", headers=auth).json()
        assert err["status"] == "error"
        msg = err["messages"][0]
        assert "content" in msg, "UCP spec requires 'content' field, not 'message'"
        assert "message" not in msg, "'message' field is not UCP-compliant"

    def test_order_links_back_to_checkout(self, http, auth):
        chk, cmp = checkout_and_complete(http, auth, lane=100)
        oid = cmp["order_id"]
        r = http.get(f"/orders/{oid}", headers=auth).json()
        assert r["checkout_id"] == chk["id"]

# ── 8. Simulation timing ──────────────────────────────────────────────────────

class TestSimulation:
    """
    Background tasks (asyncio.create_task) only run when the event loop gets
    control. Sync time.sleep() starves them; async tests + await asyncio.sleep()
    yield control so events fire between checks.

    Each test is fully self-contained: one AsyncClient, one async with block.
    """

    @staticmethod
    async def _client_and_token():
        """Open an AsyncClient and return (client, auth_headers). Caller owns it."""
        client = httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        )
        await client.__aenter__()
        r = await client.post("/oauth/token", data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        })
        return client, {"Authorization": f"Bearer {r.json()['access_token']}"}

    @staticmethod
    async def _buy(client, hdrs, lane: int) -> str:
        chk = (await client.post("/checkout-sessions", headers=hdrs, json={
            "line_items": [{"id": f"lane_{lane}", "quantity": 1}],
            "payment": {"handler_id": "prepaid", "instrument": {"token": "t"}},
        })).json()
        return (await client.post(
            f"/checkout-sessions/{chk['id']}/complete", headers=hdrs
        )).json()["order_id"]

    @staticmethod
    async def _wait_for(oid: str, action: int, timeout: float) -> bool:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if any(e["action"] == action for e in _order_events.get(oid, [])):
                return True
            await asyncio.sleep(0.1)
        return False

    @pytest.mark.anyio
    async def test_normal_scenario_emits_accepted(self):
        _checkouts.clear(); _order_events.clear()
        client, hdrs = await self._client_and_token()
        try:
            oid = await self._buy(client, hdrs, 100)
            assert await self._wait_for(oid, 0x00, 2.0), "accepted event never arrived"
        finally:
            await client.__aexit__(None, None, None)

    @pytest.mark.anyio
    async def test_normal_scenario_emits_door_open(self):
        _checkouts.clear(); _order_events.clear()
        client, hdrs = await self._client_and_token()
        try:
            oid = await self._buy(client, hdrs, 100)
            assert await self._wait_for(oid, 0x01, 6.0), "door_open event never arrived"
        finally:
            await client.__aexit__(None, None, None)

    @pytest.mark.anyio
    async def test_normal_scenario_reaches_complete(self):
        _checkouts.clear(); _order_events.clear()
        client, hdrs = await self._client_and_token()
        try:
            oid = await self._buy(client, hdrs, 100)
            assert await self._wait_for(oid, 0x03, 12.0), "goods_taken event never arrived"
            r = (await client.get(f"/orders/{oid}", headers=hdrs)).json()
            assert r["status"] == "completed"
        finally:
            await client.__aexit__(None, None, None)

    @pytest.mark.anyio
    async def test_empty_scenario_emits_rejected(self):
        _checkouts.clear(); _order_events.clear()
        client, hdrs = await self._client_and_token()
        try:
            oid = await self._buy(client, hdrs, 900)
            assert await self._wait_for(oid, 0x08, 3.0), "rejected_0x08 event never arrived"
        finally:
            await client.__aexit__(None, None, None)

    @pytest.mark.anyio
    async def test_event_order_is_chronological(self):
        _checkouts.clear(); _order_events.clear()
        client, hdrs = await self._client_and_token()
        try:
            oid = await self._buy(client, hdrs, 100)
            await self._wait_for(oid, 0x04, 15.0)  # wait for terminal event
        finally:
            await client.__aexit__(None, None, None)
        evs = _order_events.get(oid, [])
        assert len(evs) > 0, "no events arrived"
        ts_list = [e["ts"] for e in evs]
        assert ts_list == sorted(ts_list), "events not in chronological order"

# ── 9. SSE stream ─────────────────────────────────────────────────────────────

class TestSSE:
    def test_sse_requires_auth(self, http):
        r = http.get("/orders/fakeid/stream")
        assert r.status_code == 401

    def test_sse_accepts_token_query_param(self, http, tok):
        """EventSource can't set headers — token via ?token= must work."""
        _, cmp = checkout_and_complete(http, {"Authorization": f"Bearer {tok}"}, lane=100)
        oid = cmp["order_id"]
        # Inject done event so the stream terminates immediately
        _order_events[oid] = [{"ts": time.time(), "action": 0x03, "name": "goods_taken"}]
        with http.stream("GET", f"/orders/{oid}/stream?token={tok}") as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]

    def test_sse_emits_json_lines(self, http, tok):
        auth = {"Authorization": f"Bearer {tok}"}
        _, cmp = checkout_and_complete(http, auth, lane=100)
        oid = cmp["order_id"]
        _order_events[oid] = [
            {"ts": time.time(), "action": 0x01, "name": "door_open"},
            {"ts": time.time(), "action": 0x03, "name": "goods_taken"},
        ]
        collected = []
        with http.stream("GET", f"/orders/{oid}/stream?token={tok}") as r:
            for line in r.iter_lines():
                if line.startswith("data:"):
                    import json
                    collected.append(json.loads(line[5:].strip()))
                    if collected[-1].get("event") == "done":
                        break
        event_names = [e.get("name") for e in collected if "name" in e]
        assert "door_open"   in event_names
        assert "goods_taken" in event_names

# ── 10. Admin ─────────────────────────────────────────────────────────────────

class TestAdmin:
    def test_reset_clears_checkouts(self, http, auth):
        checkout_and_complete(http, auth, lane=100)
        assert len(_checkouts) > 0
        http.post("/admin/reset")
        assert len(_checkouts) == 0

    def test_reset_clears_order_events(self, http, auth):
        _, cmp = checkout_and_complete(http, auth, lane=100)
        _order_events[cmp["order_id"]] = [{"ts": 0, "action": 1, "name": "x"}]
        http.post("/admin/reset")
        assert len(_order_events) == 0

    def test_fallback_page_accessible(self, http):
        r = http.get("/fallback")
        assert r.status_code == 200
        assert "fallback" in r.text.lower()

    def test_fallback_page_contains_continue_url_hint(self, http):
        r = http.get("/fallback")
        assert r.status_code == 200
        # Page should explain why the user landed here
        assert any(kw in r.text.lower() for kw in ["ucp", "fallback", "complete"])


# ── 11. continue_url contract ─────────────────────────────────────────────────

class TestContinueUrlContract:
    """
    Per UCP spec, continue_url must appear in every response and be a usable
    absolute URL. These tests pin the contract so regressions are caught early.
    """

    def test_equals_configured_constant(self, http, auth):
        url = http.post("/cart-sessions", headers=auth).json()["continue_url"]
        assert url == CONTINUE_URL, f"expected {CONTINUE_URL!r}, got {url!r}"

    def test_is_absolute_http_url(self, http, auth):
        for resp_fn in [
            lambda: http.post("/cart-sessions", headers=auth).json(),
            lambda: http.post("/checkout-sessions", headers=auth, json={
                "line_items": [{"id": "lane_100", "quantity": 1}]
            }).json(),
        ]:
            url = resp_fn()["continue_url"]
            assert url.startswith("http"), f"continue_url must be absolute URL, got: {url!r}"

    def test_identical_across_cart_checkout_order(self, http, auth):
        cart_url = http.post("/cart-sessions", headers=auth).json()["continue_url"]

        chk_url = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_100", "quantity": 1}],
        }).json()["continue_url"]

        _, cmp = checkout_and_complete(http, auth, lane=100)
        oid = cmp["order_id"]
        _order_events[oid] = [{"ts": time.time(), "action": 0x03, "name": "goods_taken"}]
        order_url = http.get(f"/orders/{oid}", headers=auth).json()["continue_url"]

        assert cart_url == chk_url == order_url == CONTINUE_URL


# ── 12. Multiple / concurrent orders ─────────────────────────────────────────

class TestMultipleOrders:

    def test_two_purchases_have_different_order_ids(self, http, auth):
        _, c1 = checkout_and_complete(http, auth, lane=100)
        _, c2 = checkout_and_complete(http, auth, lane=101)
        assert c1["order_id"] != c2["order_id"]

    def test_events_tracked_independently_per_order(self, http, auth):
        """Injecting events for order A must not affect order B."""
        _, c1 = checkout_and_complete(http, auth, lane=100)
        _, c2 = checkout_and_complete(http, auth, lane=101)
        oid1, oid2 = c1["order_id"], c2["order_id"]

        _order_events[oid1] = [{"ts": time.time(), "action": 0x03, "name": "goods_taken"}]
        # oid2 gets no events

        r1 = http.get(f"/orders/{oid1}", headers=auth).json()
        r2 = http.get(f"/orders/{oid2}", headers=auth).json()
        assert r1["status"] == "completed"
        assert r2["status"] == "incomplete"

    def test_orders_list_shows_all_checkouts(self, http, auth):
        for lane in (100, 101, 102):
            checkout_and_complete(http, auth, lane=lane)
        orders = http.get("/orders", headers=auth).json()
        assert len(orders) == 3

    def test_orders_list_empty_after_reset(self, http, auth):
        checkout_and_complete(http, auth, lane=100)
        http.post("/admin/reset")
        orders = http.get("/orders", headers=auth).json()
        assert len(orders) == 0

    def test_orders_list_includes_lane_number(self, http, auth):
        checkout_and_complete(http, auth, lane=102)
        orders = http.get("/orders", headers=auth).json()
        assert any(o["lane"] == 102 for o in orders)


# ── 13. Order event timeline ──────────────────────────────────────────────────

class TestOrderTimeline:

    def test_goods_taken_absent_immediately_after_complete(self, http, auth):
        """goods_taken fires at 8 s; polling instantly should not see it."""
        _, cmp = checkout_and_complete(http, auth, lane=100)
        r = http.get(f"/orders/{cmp['order_id']}", headers=auth).json()
        names = [e["type"] for e in r["fulfillment"]["events"]]
        assert "goods_taken" not in names

    def test_injected_events_appear_in_order_response(self, http, auth):
        _, cmp = checkout_and_complete(http, auth, lane=100)
        oid = cmp["order_id"]
        _order_events[oid] = [
            {"ts": 1000.0, "action": 0x00, "name": "accepted"},
            {"ts": 1003.0, "action": 0x01, "name": "door_open"},
            {"ts": 1008.0, "action": 0x03, "name": "goods_taken"},
        ]
        events = http.get(f"/orders/{oid}", headers=auth).json()["fulfillment"]["events"]
        names = [e["type"] for e in events]
        assert names == ["accepted", "door_open", "goods_taken"]

    def test_door_open_without_goods_taken_is_still_incomplete(self, http, auth):
        _, cmp = checkout_and_complete(http, auth, lane=100)
        oid = cmp["order_id"]
        _order_events[oid] = [
            {"ts": time.time(),     "action": 0x00, "name": "accepted"},
            {"ts": time.time() + 3, "action": 0x01, "name": "door_open"},
        ]
        r = http.get(f"/orders/{oid}", headers=auth).json()
        assert r["status"] == "incomplete"


# ── 14. Edge cases ────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_expired_jwt_returns_401(self, http):
        import jwt as _jwt
        expired = _jwt.encode(
            {"sub": CLIENT_ID, "iat": int(time.time()) - 7200,
             "exp": int(time.time()) - 3600},
            server.JWT_SECRET, algorithm="HS256",
        )
        r = http.post("/cart-sessions", headers={"Authorization": f"Bearer {expired}"})
        assert r.status_code == 401

    def test_buyer_field_preserved_in_checkout_response(self, http, auth):
        buyer = {"email": "agent@example.com", "name": "Test Agent"}
        chk = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_100", "quantity": 1}],
            "buyer": buyer,
            "payment": {"handler_id": "prepaid", "instrument": {"token": "t"}},
        }).json()
        assert chk["buyer"]["email"] == buyer["email"]
        assert chk["buyer"]["name"] == buyer["name"]

    def test_checkout_without_buyer_still_accepted(self, http, auth):
        r = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_100", "quantity": 1}],
            "payment": {"handler_id": "prepaid", "instrument": {"token": "t"}},
        })
        assert r.status_code == 201   # UCP: 201 Created
        assert r.json()["status"] == "ready_for_complete"

    def test_wrong_payment_handler_id_leaves_checkout_incomplete(self, http, auth):
        r = http.post("/checkout-sessions", headers=auth, json={
            "line_items": [{"id": "lane_100", "quantity": 1}],
            "payment": {"handler_id": "credit_card", "instrument": {"token": "t"}},
        }).json()
        assert r["status"] == "incomplete"

    def test_same_payment_token_used_twice_creates_two_checkouts(self, http, auth):
        """Mock does not enforce payment-token uniqueness — caller's responsibility."""
        body = {
            "line_items": [{"id": "lane_100", "quantity": 1}],
            "payment": {"handler_id": "prepaid", "instrument": {"token": "repeated-token"}},
        }
        r1 = http.post("/checkout-sessions", headers=auth, json=body).json()
        r2 = http.post("/checkout-sessions", headers=auth, json=body).json()
        assert r1["id"] != r2["id"]
        assert r1["status"] == r2["status"] == "ready_for_complete"

    def test_order_id_is_16_hex_characters(self, http, auth):
        _, cmp = checkout_and_complete(http, auth, lane=100)
        oid = cmp["order_id"]
        assert len(oid) == 16
        assert all(c in "0123456789abcdef" for c in oid)

    def test_offline_lane_listed_in_catalog_with_quantity_one(self, http, auth):
        """Lane 901 appears in cart (it fails at complete time, not discovery time)."""
        items = http.post("/cart-sessions", headers=auth).json()["line_items"]
        lane_901 = next((i for i in items if i["id"] == "lane_901"), None)
        assert lane_901 is not None
        assert lane_901["quantity_available"] == 1


# ── 15. Scenario timing (async) ───────────────────────────────────────────────

class TestScenarioTiming:
    """
    Verify the dispense-event timings defined in _NORMAL / _SLOW / _EMPTY
    are respected at runtime.
    """

    @pytest.mark.anyio
    async def test_normal_goods_taken_within_12_seconds(self):
        _checkouts.clear(); _order_events.clear()
        client, hdrs = await TestSimulation._client_and_token()
        try:
            oid = await TestSimulation._buy(client, hdrs, 100)
            t0 = asyncio.get_event_loop().time()
            found = await TestSimulation._wait_for(oid, 0x03, timeout=12.0)
            elapsed = asyncio.get_event_loop().time() - t0
        finally:
            await client.__aexit__(None, None, None)
        assert found, "goods_taken never arrived within 12 s"
        assert elapsed < 12.0, f"took too long: {elapsed:.1f}s"

    @pytest.mark.anyio
    async def test_slow_door_open_after_at_least_9_seconds(self):
        """Lane 200 slow scenario: door_open must NOT arrive before 9 s."""
        _checkouts.clear(); _order_events.clear()
        client, hdrs = await TestSimulation._client_and_token()
        try:
            oid = await TestSimulation._buy(client, hdrs, 200)
            t0 = asyncio.get_event_loop().time()
            # Check at 8 s: door_open (fires at 10 s) should not be there yet
            await asyncio.sleep(8.0)
            events_at_8s = [e["name"] for e in _order_events.get(oid, [])]
            # Continue waiting until door_open actually arrives
            found = await TestSimulation._wait_for(oid, 0x01, timeout=6.0)
            elapsed = asyncio.get_event_loop().time() - t0
        finally:
            await client.__aexit__(None, None, None)
        assert "door_open" not in events_at_8s, "door_open arrived too early for slow scenario"
        assert found, "door_open never arrived for slow scenario"
        assert elapsed >= 9.0, f"door_open came too early: {elapsed:.1f}s"

    @pytest.mark.anyio
    async def test_empty_lane_rejected_within_1_second(self):
        _checkouts.clear(); _order_events.clear()
        client, hdrs = await TestSimulation._client_and_token()
        try:
            oid = await TestSimulation._buy(client, hdrs, 900)
            t0 = asyncio.get_event_loop().time()
            found = await TestSimulation._wait_for(oid, 0x08, timeout=2.0)
            elapsed = asyncio.get_event_loop().time() - t0
        finally:
            await client.__aexit__(None, None, None)
        assert found, "rejected_0x08 never arrived"
        assert elapsed < 2.0


# ── 16. SSE advanced (async) ──────────────────────────────────────────────────

class TestSSEAdvanced:

    @pytest.mark.anyio
    async def test_sse_emits_done_sentinel_after_terminal_event(self):
        """SSE stream must close with {"event":"done"} after goods_taken."""
        _checkouts.clear(); _order_events.clear()
        client, hdrs = await TestSimulation._client_and_token()
        try:
            chk = (await client.post("/checkout-sessions", headers=hdrs, json={
                "line_items": [{"id": "lane_100", "quantity": 1}],
                "payment": {"handler_id": "prepaid", "instrument": {"token": "t"}},
            })).json()
            cmp = (await client.post(
                f"/checkout-sessions/{chk['id']}/complete", headers=hdrs
            )).json()
            oid = cmp["order_id"]
            # Pre-inject terminal event so SSE terminates immediately
            _order_events[oid] = [{"ts": time.time(), "action": 0x03, "name": "goods_taken"}]

            import json as _json
            collected = []
            async with client.stream("GET", f"/orders/{oid}/stream",
                                     headers=hdrs) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        ev = _json.loads(line[5:].strip())
                        collected.append(ev)
                        if ev.get("event") == "done":
                            break
        finally:
            await client.__aexit__(None, None, None)

        assert any(e.get("event") == "done" for e in collected), "done sentinel missing"
        names = [e.get("name") for e in collected if "name" in e]
        assert "goods_taken" in names

    @pytest.mark.anyio
    async def test_sse_delivers_buffered_events_on_connect(self):
        """Connecting to SSE after events have already fired delivers them immediately."""
        _checkouts.clear(); _order_events.clear()
        client, hdrs = await TestSimulation._client_and_token()
        try:
            oid = await TestSimulation._buy(client, hdrs, 100)
            # Wait until accepted arrives (0.3 s)
            assert await TestSimulation._wait_for(oid, 0x00, 2.0), "accepted never arrived"

            import json as _json
            collected = []
            async with client.stream("GET", f"/orders/{oid}/stream",
                                     headers=hdrs) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        ev = _json.loads(line[5:].strip())
                        collected.append(ev)
                        if len(collected) >= 1:
                            break  # got at least the buffered event
        finally:
            await client.__aexit__(None, None, None)

        assert len(collected) >= 1
        assert any("name" in e for e in collected), "no named events received"

    @pytest.mark.anyio
    async def test_sse_for_rejected_order_closes_with_done(self):
        """Empty-lane rejection is a terminal event; SSE must emit done and close."""
        _checkouts.clear(); _order_events.clear()
        client, hdrs = await TestSimulation._client_and_token()
        try:
            oid = await TestSimulation._buy(client, hdrs, 900)
            assert await TestSimulation._wait_for(oid, 0x08, 3.0), "rejected event never arrived"

            import json as _json
            collected = []
            async with client.stream("GET", f"/orders/{oid}/stream",
                                     headers=hdrs) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        ev = _json.loads(line[5:].strip())
                        collected.append(ev)
                        if ev.get("event") == "done":
                            break
        finally:
            await client.__aexit__(None, None, None)

        assert any(e.get("event") == "done" for e in collected)
        names = [e.get("name") for e in collected if "name" in e]
        assert "rejected_0x08" in names

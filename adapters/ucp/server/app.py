"""
UCP Adapter for WM800 vending machine.

Exposes UCP-compliant REST endpoints over the WM800 serial protocol.

Required environment variables:
    WM800_PORT          — serial port, e.g. /dev/tty.usbserial-XXXX
    WM800_ADDR          — hex device address (default: 0x00)

Optional:
    CATALOG_PATH        — path to catalog.json (default: ../catalog.json)
    ADAPTER_BASE_URL    — public HTTPS base URL (default: http://localhost:8080)
    UCP_CONTINUE_URL    — fallback URL included in every UCP response; the UCP
                          client navigates here when the programmatic flow cannot
                          complete (e.g. device offline, dispense error).
                          Per UCP spec, continue_url is a fallback — not the
                          primary path. Primary path is always the API.
    UCP_CLIENT_ID / UCP_CLIENT_SECRET / UCP_JWT_SECRET

Run:
    WM800_PORT=/dev/tty.usbserial-XXXX python app.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Sibling modules in the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth
import gateway

# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

# lane_str → {name, price (fen/cents), currency}
_catalog: dict[str, dict] = {}


def _load_catalog() -> None:
    path = os.environ.get(
        "CATALOG_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "../catalog.json"),
    )
    if os.path.exists(path):
        with open(path) as f:
            _catalog.update(json.load(f).get("lanes", {}))


# ---------------------------------------------------------------------------
# In-memory checkout store (process lifetime only)
# ---------------------------------------------------------------------------

_checkouts: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("ADAPTER_BASE_URL", "http://localhost:8080")
# Fallback URL embedded in every UCP response. UCP clients use this only when
# the programmatic flow fails — it is never the primary path.
CONTINUE_URL = os.environ.get("UCP_CONTINUE_URL", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    port = os.environ.get("WM800_PORT")
    if not port:
        raise RuntimeError("WM800_PORT environment variable is required")
    addr = int(os.environ.get("WM800_ADDR", "0x00"), 16)
    gateway.setup(port, addr)
    _load_catalog()
    yield
    gateway.teardown()


app = FastAPI(title="WM800 UCP Adapter", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Discovery (no auth required — public per UCP spec)
# ---------------------------------------------------------------------------

@app.get("/.well-known/ucp")
def ucp_profile():
    return {
        "ucp_version": "2026-04-08",
        "services": [{"type": "rest", "base_url": BASE_URL}],
        "capabilities": {
            "dev.ucp.shopping.cart":     [{"version": "2026-04-08"}],
            "dev.ucp.shopping.checkout": [{"version": "2026-04-08"}],
            "dev.ucp.shopping.order":    [{"version": "2026-04-08"}],
        },
        "payment_handlers": [{
            "handler_id": "prepaid",
            "available_instruments": [{"type": "dev.ucp.vending.prepaid_token"}],
            "config": {
                "note": "Caller has already collected payment. "
                        "Submit any non-empty token as proof of payment."
            },
        }],
        # HTTP Message Signatures not yet implemented — add signing_keys when ready
        "signing_keys": [],
    }


@app.get("/.well-known/oauth-authorization-server")
def oauth_metadata():
    return {
        "issuer": BASE_URL,
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "grant_types_supported": ["client_credentials"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.post("/oauth/token")
def token(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    return auth.handle_token_request(client_id, client_secret, grant_type)


# ---------------------------------------------------------------------------
# UCP: Cart
# ---------------------------------------------------------------------------

@app.post("/v1/cart")
async def create_cart(_: dict = Depends(auth.require_auth)):
    """
    Returns available items from the catalog, filtered to lanes that physically
    exist on the device (via 0x2B step table).

    stock quantity_available is always 1 — WM800 can report exact stock only
    after a full 0x24 scan (~200 s). Use the /scan endpoint to refresh.
    """
    try:
        lanes = await gateway.get_lanes()
    except Exception as exc:
        # Device unreachable — return UCP error with continue_url so the agent
        # can fall back to a human-operated channel rather than hard-failing.
        return _ucp_error("cart", "device_unavailable", str(exc))

    existing_lanes = {str(l["lane"]) for l in lanes}
    items = []
    for lane_str, product in _catalog.items():
        if lane_str not in existing_lanes:
            continue
        items.append({
            "id": f"lane_{lane_str}",
            "name": product.get("name", f"Lane {lane_str}"),
            "price": {
                "amount": product.get("price", 0),
                "currency": product.get("currency", "CNY"),
            },
            "quantity_available": 1,
        })

    return _ucp_wrap("cart", {"status": "incomplete", "line_items": items})


# ---------------------------------------------------------------------------
# UCP: Checkout
# ---------------------------------------------------------------------------

class CheckoutBody(BaseModel):
    line_items: list[dict]
    buyer: Optional[dict] = None
    payment: Optional[dict] = None


@app.post("/v1/checkout")
async def create_checkout(
    body: CheckoutBody,
    _: dict = Depends(auth.require_auth),
):
    if not body.line_items:
        raise HTTPException(400, "line_items required")

    lane = _parse_lane(body.line_items[0]["id"])
    chk_id = f"chk_{uuid.uuid4().hex[:12]}"
    oid_hex = uuid.uuid4().hex[:16]   # 8 bytes for WM800

    has_payment = (
        body.payment is not None
        and body.payment.get("handler_id") == "prepaid"
        and body.payment.get("instrument", {}).get("token")
    )

    _checkouts[chk_id] = {
        "id": chk_id,
        "order_id_hex": oid_hex,
        "lane": lane,
        "line_items": body.line_items,
        "buyer": body.buyer,
        "payment": body.payment,
        "status": "ready_for_complete" if has_payment else "incomplete",
        "created_at": time.time(),
    }

    return _ucp_wrap("checkout", _to_ucp_checkout(_checkouts[chk_id]))


@app.post("/v1/checkout/{checkout_id}/complete")
async def complete_checkout(
    checkout_id: str,
    _: dict = Depends(auth.require_auth),
):
    chk = _checkouts.get(checkout_id)
    if not chk:
        raise HTTPException(404, "checkout not found")
    if chk["status"] == "complete":
        return _ucp_wrap("checkout", _to_ucp_checkout(chk))
    if chk["status"] != "ready_for_complete":
        raise HTTPException(422, "checkout is not ready_for_complete")

    await gateway.start_dispense(chk["lane"], chk["order_id_hex"])
    chk["status"] = "complete"

    return _ucp_wrap("checkout", _to_ucp_checkout(chk))


# ---------------------------------------------------------------------------
# UCP: Order
# ---------------------------------------------------------------------------

@app.get("/v1/order/{order_id}")
async def get_order(order_id: str, _: dict = Depends(auth.require_auth)):
    chk = _find_checkout_by_order(order_id)
    if not chk:
        raise HTTPException(404, "order not found")

    evs = gateway.get_order_events(order_id)
    goods_taken = any(e["action"] == 0x03 for e in evs)
    rejected = any(
        e["action"] == 0x00 and e.get("status_code", 0) != 0 for e in evs
    )

    if rejected:
        reject_ev = next(e for e in evs if e["action"] == 0x00)
        return _ucp_error(
            "order",
            "dispense_failed",
            f"WM800 rejected dispense: {reject_ev.get('name')}",
            extra={
                "id": order_id,
                "checkout_id": chk["id"],
                "fulfillment": {"method": "pickup", "events": evs},
            },
        )

    return _ucp_wrap("order", {
        "id": order_id,
        "checkout_id": chk["id"],
        "status": "complete" if goods_taken else "incomplete",
        "fulfillment": {
            "method": "pickup",
            "events": [{"type": e["name"], "ts": e["ts"]} for e in evs],
        },
    })


@app.get("/v1/order/{order_id}/stream")
async def stream_order(order_id: str, _: dict = Depends(auth.require_auth)):
    """SSE stream of WM800 0xE1 fulfillment events for this order."""
    async def generate():
        seen = 0
        while True:
            evs = gateway.get_order_events(order_id)
            for ev in evs[seen:]:
                yield f"data: {json.dumps(ev)}\n\n"
                seen += 1
            if gateway.order_is_done(order_id):
                yield 'data: {"event":"done"}\n\n'
                break
            await asyncio.sleep(0.5)
    return StreamingResponse(generate(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ucp_wrap(resource: str, body: dict) -> dict:
    return {
        "ucp": {
            "version": "2026-04-08",
            "capabilities": {
                f"dev.ucp.shopping.{resource}": [{"version": "2026-04-08"}]
            },
        },
        # continue_url is always present per UCP spec — it is a fallback for
        # when the programmatic API flow cannot complete, not the primary path.
        "continue_url": CONTINUE_URL,
        **body,
    }


def _ucp_error(resource: str, code: str, message: str,
               extra: Optional[dict] = None) -> dict:
    body: dict = {
        "status": "error",
        "messages": [{"type": "error", "code": code, "message": message}],
    }
    if extra:
        body.update(extra)
    return _ucp_wrap(resource, body)


def _to_ucp_checkout(chk: dict) -> dict:
    out: dict = {
        "id": chk["id"],
        "status": chk["status"],
        "line_items": chk["line_items"],
    }
    if chk.get("buyer"):
        out["buyer"] = chk["buyer"]
    if chk.get("payment"):
        out["payment"] = chk["payment"]
    out["fulfillment"] = {"method": "pickup"}
    if chk["status"] == "complete":
        out["order_id"] = chk["order_id_hex"]
    return out


def _parse_lane(item_id: str) -> int:
    # item_id format from cart: "lane_100"
    try:
        return int(item_id.split("_", 1)[1])
    except (IndexError, ValueError):
        raise HTTPException(400, f"invalid item id: {item_id!r} — expected lane_<number>")


def _find_checkout_by_order(order_id_hex: str) -> Optional[dict]:
    return next(
        (c for c in _checkouts.values() if c["order_id_hex"] == order_id_hex),
        None,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=False)

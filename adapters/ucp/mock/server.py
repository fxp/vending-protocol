"""
WM800 UCP Mock Server

Standalone mock implementing the full UCP protocol. No hardware required.
Simulates realistic vending machine dispense timing with configurable scenarios.

Run:
    pip install fastapi uvicorn pyjwt
    python server.py
    open http://localhost:8080

Scenarios (controlled by lane number in catalog):
    100–199  normal     door_open 3 s → goods_taken 8 s → platform_home 12 s
    200–299  slow       door_open 10 s → goods_taken 25 s → platform_home 30 s
    900      empty      immediate reject (WM800 status 0x08 — lane empty)
    901      offline    device_unavailable error + continue_url, no dispense
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import jwt
import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

PORT         = int(os.environ.get("PORT", 8080))
BASE_URL     = os.environ.get("BASE_URL", f"http://localhost:{PORT}")
CONTINUE_URL = os.environ.get("CONTINUE_URL", f"{BASE_URL}/fallback")
CLIENT_ID    = os.environ.get("UCP_CLIENT_ID",     "demo")
CLIENT_SECRET= os.environ.get("UCP_CLIENT_SECRET", "demo")
JWT_SECRET   = os.environ.get("JWT_SECRET",        "mock-secret-change-me")
TOKEN_TTL    = 3600

# ── Fake catalog ──────────────────────────────────────────────────────────────

CATALOG: dict[str, dict] = {
    "100": {"name": "Mineral Water 550ml",        "price": 200},
    "101": {"name": "Coca Cola 330ml",             "price": 500},
    "102": {"name": "Green Tea 500ml",             "price": 400},
    "200": {"name": "Lay's Chips 40g  [slow 25s]", "price": 800},
    "900": {"name": "Empty Lane  [test: fail]",    "price": 100},
    "901": {"name": "Offline Lane  [test: error]", "price": 100},
}
CURRENCY = "CNY"

# ── Dispense scenarios ────────────────────────────────────────────────────────

# (seconds_from_start, event_name, 0xE1_action_code)
_NORMAL: list[tuple[float, str, int]] = [
    (0.3,  "accepted",      0x00),
    (3.0,  "door_open",     0x01),
    (8.0,  "goods_taken",   0x03),
    (12.0, "platform_home", 0x04),
]
_SLOW: list[tuple[float, str, int]] = [
    (0.3,  "accepted",      0x00),
    (10.0, "door_open",     0x01),
    (25.0, "goods_taken",   0x03),
    (30.0, "platform_home", 0x04),
]
_EMPTY: list[tuple[float, str, int]] = [
    (0.5, "rejected_0x08", 0x08),  # lane empty
]

_TERMINAL = {0x03, 0x04, 0x08}

def _scenario(lane: int) -> list[tuple[float, str, int]]:
    if lane == 900:          return _EMPTY
    if 200 <= lane <= 299:   return _SLOW
    return _NORMAL

def _is_offline(lane: int) -> bool:
    return lane == 901

# ── State ─────────────────────────────────────────────────────────────────────

_checkouts:    dict[str, dict]        = {}   # checkout_id → checkout
_order_events: dict[str, list[dict]]  = {}   # order_id_hex → events

# Alipay AI Pay mock state
_alipay_orders: dict[str, dict] = {}  # alipay_order_id → {checkout_id, amount, currency, status, product_name}

# AP2: mock merchant signing key (HMAC-SHA256 for simplicity; real impl uses ES256 JWK)
import hashlib, hmac as _hmac, base64 as _b64
AP2_MOCK_KEY = os.environ.get("AP2_MOCK_KEY", "wm800-mock-ap2-key-2026")

def _ap2_sign(payload_dict: dict) -> str:
    """Mock AP2 merchant_authorization: base64(HMAC-SHA256(JCS-canonical-json))"""
    import json as _json
    # Exclude ap2 field per spec
    payload = {k: v for k, v in payload_dict.items() if k != "ap2"}
    canonical = _json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    sig = _hmac.new(AP2_MOCK_KEY.encode(), canonical, hashlib.sha256).digest()
    encoded_header = _b64.urlsafe_b64encode(b'{"alg":"HS256","kid":"wm800-mock-2026"}').rstrip(b"=").decode()
    encoded_sig = _b64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{encoded_header}..{encoded_sig}"

# ── Auth ──────────────────────────────────────────────────────────────────────

_bearer = OAuth2PasswordBearer(tokenUrl="/oauth/token", auto_error=False)

def _decode(token: Optional[str]) -> dict:
    if not token:
        raise HTTPException(401, "missing token")
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token_expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "invalid_token")

def _require(bearer: Optional[str] = Depends(_bearer)) -> dict:
    return _decode(bearer)

def _require_sse(
    bearer: Optional[str] = Depends(_bearer),
    token:  Optional[str] = Query(None),    # EventSource can't set headers
) -> dict:
    return _decode(bearer or token)

# ── UCP response helpers ──────────────────────────────────────────────────────

def _ucp(resource: str, body: dict) -> dict:
    """Wrap a response body with the UCP envelope + continue_url."""
    return {
        "ucp": {
            "version": "2026-04-08",
            "capabilities": {
                f"dev.ucp.shopping.{resource}": [{"version": "2026-04-08"}]
            },
        },
        # Per UCP spec: continue_url is always present as a fallback.
        # The primary flow is the API; the client uses this URL only when
        # it cannot complete the interaction programmatically.
        "continue_url": CONTINUE_URL,
        **body,
    }

def _ucp_err(resource: str, code: str, msg: str, extra: dict | None = None) -> dict:
    body: dict = {
        "status": "error",
        # UCP spec: field is "content", not "message"
        "messages": [{"type": "error", "code": code, "content": msg,
                      "severity": "unrecoverable"}],
    }
    if extra:
        body.update(extra)
    return _ucp(resource, body)


def _checkout_total(line_items: list[dict]) -> int:
    """Sum prices from CATALOG for the given line_items (returns fen/cents)."""
    total = 0
    for item in line_items:
        lane_id = str(item.get("id", "")).replace("lane_", "")
        price = CATALOG.get(lane_id, {}).get("price", 0)
        qty = int(item.get("quantity", 1))
        total += price * qty
    return total

# ── Simulation ────────────────────────────────────────────────────────────────

async def _run_scenario(oid: str, lane: int) -> None:
    events = _scenario(lane)
    t0 = time.monotonic()
    for target, name, action in events:
        wait = target - (time.monotonic() - t0)
        if wait > 0:
            await asyncio.sleep(wait)
        _order_events.setdefault(oid, []).append({
            "ts": time.time(), "action": action, "name": name,
        })

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="WM800 UCP Mock", docs_url="/api-docs")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Discovery ─────────────────────────────────────────────────────────────────

@app.get("/.well-known/ucp")
def ucp_profile():
    return {
        "ucp_version": "2026-04-08",
        # UCP spec: services is an object keyed by service name
        "services": {
            "dev.ucp.shopping": [{
                "version": "2026-04-08",
                "spec": "https://ucp.dev/latest/specification/checkout-capability/overview/",
                "transport": "rest",
                "endpoint": f"{BASE_URL}/checkout-sessions",
            }]
        },
        "capabilities": {
            "dev.ucp.shopping.cart":     [{"version": "2026-04-08"}],
            "dev.ucp.shopping.checkout": [{"version": "2026-04-08"}],
            "dev.ucp.shopping.order":    [{"version": "2026-04-08"}],
            "dev.ucp.shopping.ap2_mandate": [{
                "version": "2026-04-08",
                "spec": "https://ucp.dev/2026-04-08/specification/ap2-mandates",
                "schema": "https://ucp.dev/2026-04-08/schemas/shopping/ap2_mandate.json",
                "extends": "dev.ucp.shopping.checkout",
                "config": {"vp_formats_supported": {"dc+sd-jwt": {}}},
            }],
        },
        "payment_handlers": [
            {
                "handler_id": "prepaid",
                "available_instruments": [{"type": "dev.ucp.vending.prepaid_token"}],
                "config": {"note": "Any non-empty token is accepted as payment proof."},
            },
            {
                "handler_id": "alipay_aipay",
                "name": "com.alipay.aipay",
                "version": "2026-04-08",
                "spec": "https://open.alipay.com/api/ucp/handler",
                "available_instruments": [{"type": "alipay_token"}],
                "config": {
                    "app_id": "mock_alipay_app_2026",
                    "environment": "sandbox",
                    "cashier_base": f"{BASE_URL}/alipay/cashier",
                    "verify_base": f"{BASE_URL}/alipay/query-order",
                    "supported_auth_methods": ["biometric", "password", "voice"],
                    "act_protocol": "ACT/1.0",
                    "mandate_type": "alipay_intent_credential",
                },
            },
            {
                "handler_id": "google_pay",
                "name": "com.google.pay",
                "version": "2026-04-08",
                "spec": "https://pay.google.com/gp/p/ucp/2026-01-23/",
                "available_instruments": [{"type": "card"}],
                "config": {
                    "environment": "TEST",
                    "api_version": 2,
                    "api_version_minor": 0,
                    "merchant_info": {
                        "merchant_name": "WM800 Vending",
                        "merchant_id": "TEST",
                    },
                    "allowed_payment_methods": [{
                        "type": "CARD",
                        "parameters": {
                            "allowedAuthMethods": ["PAN_ONLY", "CRYPTOGRAM_3DS"],
                            "allowedCardNetworks": ["MASTERCARD", "VISA"],
                        },
                        "tokenizationSpecification": {
                            "type": "PAYMENT_GATEWAY",
                            "parameters": {
                                "gateway": "example",
                                "gatewayMerchantId": "exampleGatewayMerchantId",
                            },
                        },
                    }],
                },
            },
        ],
        "signing_keys": [
            {
                "kid": "wm800-mock-2026",
                "alg": "HS256",
                "note": "Mock key — not cryptographically secure. Use ES256 in production.",
            }
        ],
    }

@app.get("/.well-known/oauth-authorization-server")
def oauth_meta():
    return {
        "issuer": BASE_URL,
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "grant_types_supported": ["client_credentials"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.post("/oauth/token")
def token_endpoint(
    grant_type:    str = Form(...),
    client_id:     str = Form(...),
    client_secret: str = Form(...),
):
    if grant_type != "client_credentials":
        raise HTTPException(400, "unsupported_grant_type")
    if client_id != CLIENT_ID or client_secret != CLIENT_SECRET:
        raise HTTPException(401, "invalid_client")
    now = int(time.time())
    tok = jwt.encode(
        {"sub": client_id, "iat": now, "exp": now + TOKEN_TTL},
        JWT_SECRET, algorithm="HS256",
    )
    return {"access_token": tok, "token_type": "bearer", "expires_in": TOKEN_TTL}

# ── UCP: Cart ─────────────────────────────────────────────────────────────────

@app.post("/cart-sessions")
def create_cart(_: dict = Depends(_require)):
    items = [
        {
            "id": f"lane_{lane}",
            "name": info["name"],
            "price": {"amount": info["price"], "currency": CURRENCY},
            # quantity_available: 0 for the empty-lane scenario
            "quantity_available": 0 if lane == "900" else 1,
        }
        for lane, info in CATALOG.items()
    ]
    return _ucp("cart", {"status": "incomplete", "line_items": items})

# ── UCP: Checkout ─────────────────────────────────────────────────────────────

class CheckoutBody(BaseModel):
    line_items: list[dict]
    buyer:   Optional[dict] = None
    payment: Optional[dict] = None

@app.post("/checkout-sessions", status_code=201)
def create_checkout(body: CheckoutBody, _: dict = Depends(_require)):
    if not body.line_items:
        raise HTTPException(400, "line_items required")

    item_id = body.line_items[0].get("id", "")
    try:
        lane = int(item_id.split("_", 1)[1])
    except (IndexError, ValueError):
        raise HTTPException(400, f"invalid item id: {item_id!r} — expected lane_<number>")

    handler_id = (body.payment or {}).get("handler_id", "")
    has_payment = bool(
        handler_id == "prepaid"
        and (body.payment or {}).get("instrument", {}).get("token")
        or handler_id in ("alipay_aipay", "google_pay")
    )

    chk_id = f"chk_{uuid.uuid4().hex[:12]}"
    oid    = uuid.uuid4().hex[:16]

    _checkouts[chk_id] = {
        "id":           chk_id,
        "order_id_hex": oid,
        "lane":         lane,
        "line_items":   body.line_items,
        "buyer":        body.buyer,
        "payment":      body.payment,
        "status":       "ready_for_complete" if has_payment else "incomplete",
        "created_at":   time.time(),
    }

    # AP2: sign checkout response (merchant_authorization)
    checkout_data = _fmt_checkout(_checkouts[chk_id])
    ap2_sig = _ap2_sign(checkout_data)
    checkout_data["ap2"] = {"merchant_authorization": ap2_sig}
    return _ucp("checkout", checkout_data)

class CompleteBody(BaseModel):
    payment: Optional[dict] = None
    ap2: Optional[dict] = None  # AP2 mandate (checkout_mandate + payment credentials)

@app.get("/checkout-sessions/{checkout_id}")
def get_checkout(checkout_id: str, _: dict = Depends(_require)):
    chk = _checkouts.get(checkout_id)
    if not chk:
        raise HTTPException(404, "checkout not found")
    return _ucp("checkout", _fmt_checkout(chk))


@app.post("/checkout-sessions/{checkout_id}/complete")
async def complete_checkout(checkout_id: str, body: CompleteBody = CompleteBody(), _: dict = Depends(_require)):
    chk = _checkouts.get(checkout_id)
    if not chk:
        raise HTTPException(404, "checkout not found")
    if chk["status"] == "completed":
        return _ucp("checkout", _fmt_checkout(chk))
    if chk["status"] != "ready_for_complete":
        raise HTTPException(422, "checkout is not ready_for_complete — add payment first")

    # AP2: accept checkout_mandate if present (mock: log it, don't cryptographically verify)
    if body.ap2 and body.ap2.get("checkout_mandate"):
        chk["ap2_mandate_received"] = True

    # Alipay: verify payment was actually confirmed before completing
    handler_id = (chk.get("payment") or {}).get("handler_id", "")
    if handler_id == "alipay_aipay":
        alipay_order_id = (chk.get("payment") or {}).get("alipay_order_id")
        if alipay_order_id:
            alipay_order = _alipay_orders.get(alipay_order_id)
            if not alipay_order or alipay_order.get("status") != "paid":
                return _ucp_err("checkout", "payment_required",
                    "Alipay payment not confirmed — user must complete payment first")

    lane = chk["lane"]
    oid  = chk["order_id_hex"]

    if _is_offline(lane):
        return _ucp_err(
            "checkout", "device_unavailable",
            f"Lane {lane} is offline (mock scenario 901)",
            extra={"id": checkout_id},
        )

    chk["status"] = "completed"   # UCP spec uses "completed"
    # Fire-and-forget: simulate the physical dispense cycle.
    asyncio.create_task(_run_scenario(oid, lane))
    return _ucp("checkout", _fmt_checkout(chk))

# ── UCP: Order ────────────────────────────────────────────────────────────────

@app.get("/orders/{order_id}")
def get_order(order_id: str, _: dict = Depends(_require)):
    chk = _find_checkout(order_id)
    if not chk:
        raise HTTPException(404, "order not found")

    evs         = _order_events.get(order_id, [])
    goods_taken = any(e["action"] == 0x03 for e in evs)
    rejected    = any(e["action"] == 0x08 for e in evs)
    amount      = _checkout_total(chk.get("line_items", []))

    if rejected:
        return _ucp_err(
            "order", "dispense_failed",
            "WM800 rejected dispense: lane empty (0x08)",
            extra={
                "id": order_id, "checkout_id": chk["id"],
                "currency": CURRENCY,
                "totals": [
                    {"type": "subtotal", "amount": amount, "currency": CURRENCY},
                    {"type": "total",    "amount": amount, "currency": CURRENCY},
                ],
                "fulfillment": {"method": "pickup", "events": _fmt_events(evs)},
            },
        )

    return _ucp("order", {
        "id":          order_id,
        "checkout_id": chk["id"],
        # UCP spec: "completed" not "complete"
        "status":      "completed" if goods_taken else "incomplete",
        "currency":    CURRENCY,
        "totals": [
            {"type": "subtotal", "amount": amount, "currency": CURRENCY},
            {"type": "total",    "amount": amount, "currency": CURRENCY},
        ],
        "fulfillment": {
            "method": "pickup",
            "events": _fmt_events(evs),
        },
    })

@app.get("/orders/{order_id}/stream")
async def stream_order(
    order_id: str,
    request: Request,
    bearer: Optional[str] = Depends(_bearer),
    token:  Optional[str] = Query(None),
):
    """
    SSE stream of dispense events.
    Auth: Bearer header, ?token= query param, or sse_tok cookie (set by /internal/sse-token).
    """
    from fastapi import Request as _Req
    cookie_tok = request.cookies.get("sse_tok")
    _decode(bearer or token or cookie_tok)  # raises 401 if none valid
    async def generate():
        seen = 0
        while True:
            evs = _order_events.get(order_id, [])
            for ev in evs[seen:]:
                yield f"data: {json.dumps(ev)}\n\n"
                seen += 1
            if any(e["action"] in _TERMINAL for e in evs):
                yield 'data: {"event":"done"}\n\n'
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(generate(), media_type="text/event-stream")

# ── Alipay AI Pay mock endpoints ──────────────────────────────────────────────

class AlipayCreateOrderBody(BaseModel):
    checkout_id: str
    amount: int
    currency: str = "CNY"
    product_name: str = ""
    buyer_id: Optional[str] = None

@app.post("/alipay/create-order")
def alipay_create_order(body: AlipayCreateOrderBody, _: dict = Depends(_require)):
    """Create a pre-order (预下单). Returns cashier_url for user to pay."""
    alipay_order_id = f"alipay_{uuid.uuid4().hex[:16]}"
    _alipay_orders[alipay_order_id] = {
        "alipay_order_id": alipay_order_id,
        "checkout_id":     body.checkout_id,
        "amount":          body.amount,
        "currency":        body.currency,
        "product_name":    body.product_name,
        "status":          "pending",
        "created_at":      time.time(),
    }
    # Update the checkout to reference this alipay order
    chk = _checkouts.get(body.checkout_id)
    if chk and chk.get("payment"):
        chk["payment"]["alipay_order_id"] = alipay_order_id
    cashier_url = f"{BASE_URL}/alipay/cashier/{alipay_order_id}"
    return {
        "alipay_order_id": alipay_order_id,
        "cashier_url":     cashier_url,
        "amount":          body.amount,
        "currency":        body.currency,
        "status":          "pending",
        "act_protocol":    "ACT/1.0",
        "mandate_type":    "alipay_intent_credential",
    }

@app.get("/alipay/query-order/{alipay_order_id}")
def alipay_query_order(alipay_order_id: str, _: dict = Depends(_require)):
    """Query payment result (结果查询). MUST use this — do not trust frontend callback."""
    order = _alipay_orders.get(alipay_order_id)
    if not order:
        raise HTTPException(404, "alipay order not found")
    return {
        "alipay_order_id": alipay_order_id,
        "status":          order["status"],
        "amount":          order["amount"],
        "currency":        order["currency"],
        "paid_at":         order.get("paid_at"),
        # AP2-aligned: return intent_credential on success
        "intent_credential": order.get("intent_credential"),
    }

@app.post("/alipay/confirm-payment/{alipay_order_id}")
def alipay_confirm_payment(alipay_order_id: str):
    """Called by UI when user 'scans QR and confirms biometric'. Simulates Alipay server-side confirm."""
    order = _alipay_orders.get(alipay_order_id)
    if not order:
        raise HTTPException(404, "alipay order not found")
    order["status"] = "paid"
    order["paid_at"] = time.time()
    # Simulate Alipay issuing an intent credential (AP2 payment_mandate)
    import base64 as _b64c, json as _jsonc
    mandate_payload = {
        "type": "alipay_intent_credential",
        "alipay_order_id": alipay_order_id,
        "checkout_id": order["checkout_id"],
        "amount": order["amount"],
        "currency": order["currency"],
        "auth_method": "mock_biometric",
        "issued_at": order["paid_at"],
        "act_version": "ACT/1.0",
    }
    encoded = _b64c.urlsafe_b64encode(
        _jsonc.dumps(mandate_payload, ensure_ascii=False).encode()
    ).rstrip(b"=").decode()
    order["intent_credential"] = f"alipay_ic.{encoded}.mock_sig"
    # Also mark the checkout as ready if not already
    chk = _checkouts.get(order["checkout_id"])
    if chk and chk["status"] == "incomplete":
        chk["status"] = "ready_for_complete"
    return {"status": "paid", "intent_credential": order["intent_credential"]}

@app.get("/alipay/cashier/{alipay_order_id}", response_class=HTMLResponse)
def alipay_cashier_page(alipay_order_id: str):
    """Mock Alipay cashier page — shown in iframe or popup for user to confirm payment."""
    order = _alipay_orders.get(alipay_order_id)
    if not order:
        return HTMLResponse("<h2>Order not found</h2>", status_code=404)
    amount_yuan = f"¥{order['amount']/100:.2f}"
    product = order.get("product_name", "商品")
    if order["status"] == "paid":
        return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>支付成功</title><style>body{{font-family:sans-serif;text-align:center;padding:40px;background:#f0f9f0}}</style></head>
<body><div style="font-size:64px">✅</div><h2 style="color:#00a854">支付成功</h2>
<p style="color:#666">{product} · {amount_yuan}</p></body></html>""")
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>支付宝 AI 付</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,sans-serif;background:#fff;height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{width:340px;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.12)}}
.hdr{{background:linear-gradient(135deg,#1677ff,#0958d9);color:#fff;padding:20px;text-align:center}}
.hdr .logo{{font-size:28px;font-weight:800;letter-spacing:-1px}}
.hdr .sub{{font-size:12px;opacity:.8;margin-top:2px}}
.body{{padding:24px;background:#fff}}
.amount{{text-align:center;font-size:36px;font-weight:800;color:#1a1a1a;margin:12px 0}}
.item{{font-size:13px;color:#888;text-align:center;margin-bottom:20px}}
.qr{{background:#f8f9fa;border:2px dashed #d0d0d0;border-radius:12px;padding:20px;text-align:center;margin-bottom:16px}}
.qr .icon{{font-size:40px;margin-bottom:8px}}
.qr .txt{{font-size:12px;color:#666}}
.methods{{display:flex;gap:8px;margin-bottom:16px}}
.method{{flex:1;padding:8px;background:#f8f9fa;border-radius:8px;text-align:center;font-size:11px;color:#666}}
.method .icon{{font-size:18px;display:block;margin-bottom:3px}}
.btn{{width:100%;padding:14px;background:linear-gradient(135deg,#1677ff,#0958d9);color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer}}
.btn:hover{{opacity:.9}}
.security{{font-size:11px;color:#aaa;text-align:center;margin-top:12px}}
</style></head>
<body><div class="card">
  <div class="hdr">
    <div class="logo">支 AI付</div>
    <div class="sub">Alipay AI Pay · ACT/1.0</div>
  </div>
  <div class="body">
    <div class="item">{product}</div>
    <div class="amount">{amount_yuan}</div>
    <div class="qr">
      <div class="icon">📱</div>
      <div class="txt">扫描或点击下方按钮确认支付</div>
    </div>
    <div class="methods">
      <div class="method"><span class="icon">😊</span>面容</div>
      <div class="method"><span class="icon">👆</span>指纹</div>
      <div class="method"><span class="icon">🎙️</span>声纹</div>
      <div class="method"><span class="icon">🔢</span>密码</div>
    </div>
    <button class="btn" onclick="confirm()">确认支付 {amount_yuan}</button>
    <div class="security">🔒 由支付宝 TEE 安全保护 · 意图授权凭证 ACT/1.0</div>
  </div>
</div>
<script>
function confirm() {{
  fetch('/alipay/confirm-payment/{alipay_order_id}', {{method:'POST'}})
    .then(r => r.json())
    .then(d => {{
      if(d.status === 'paid') {{
        document.querySelector('.btn').textContent = '✅ 支付成功';
        document.querySelector('.btn').style.background = '#00a854';
        document.querySelector('.btn').disabled = true;
        // Notify parent window
        if(window.parent !== window) {{
          window.parent.postMessage({{type:'alipay_paid', alipay_order_id:'{alipay_order_id}', intent_credential: d.intent_credential}}, '*');
        }}
      }}
    }});
}}
</script></body></html>""")

# ── Debug / admin ─────────────────────────────────────────────────────────────

@app.get("/orders")
def list_orders(_: dict = Depends(_require)):
    return [_order_summary(c) for c in _checkouts.values()]

@app.post("/internal/sse-token")
def issue_sse_cookie(response: Response, _: dict = Depends(_require)):
    """
    Exchange a valid Bearer token for a short-lived HttpOnly cookie used
    by EventSource (which cannot set Authorization headers).
    FastAPI injects `response` when typed as Response, allowing set_cookie.
    """
    response.set_cookie(
        "sse_tok",
        jwt.encode({"sub": "sse", "exp": int(time.time()) + 300},
                   JWT_SECRET, algorithm="HS256"),
        httponly=True, samesite="strict", max_age=300,
    )
    return {"ok": True}

@app.post("/admin/reset")
def reset():
    """Clear all state. Useful between test runs."""
    _checkouts.clear()
    _order_events.clear()
    return {"ok": True}

@app.get("/fallback", response_class=HTMLResponse)
def fallback_page():
    """Placeholder fallback URL — the destination of continue_url in this mock."""
    return """<html><body style="font-family:monospace;padding:40px;background:#0d1117;color:#c9d1d9">
    <h2 style="color:#f85149">⚠️  UCP Fallback</h2>
    <p>The programmatic UCP flow could not complete.</p>
    <p>In production this page would let the customer finish the purchase manually
    (cash register, WeChat Pay QR code, etc).</p>
    <p><a href="/" style="color:#58a6ff">← Back to mock dashboard</a></p>
    </body></html>"""

# ── HTML dashboard ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return _DASHBOARD_HTML

@app.get("/demo", response_class=HTMLResponse)
def vending_demo():
    import pathlib
    p = pathlib.Path(__file__).parent.parent.parent.parent.parent / "samples" / "vending-demo.html"
    if p.exists():
        return p.read_text()
    return "<h1>vending-demo.html not found</h1>"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_checkout(c: dict) -> dict:
    amount = _checkout_total(c.get("line_items", []))
    out: dict = {
        "id":       c["id"],
        "status":   c["status"],
        "currency": CURRENCY,          # UCP required
        "line_items": c["line_items"],
        "totals": [                    # UCP required: MUST contain subtotal + total
            {"type": "subtotal", "amount": amount, "currency": CURRENCY},
            {"type": "total",    "amount": amount, "currency": CURRENCY},
        ],
    }
    if c.get("buyer"):   out["buyer"]   = c["buyer"]
    if c.get("payment"): out["payment"] = c["payment"]
    out["fulfillment"] = {"method": "pickup"}
    if c["status"] == "completed":     # UCP spec uses "completed" not "complete"
        out["order_id"] = c["order_id_hex"]
    return out

def _fmt_events(evs: list[dict]) -> list[dict]:
    return [{"type": e["name"], "ts": e["ts"]} for e in evs]

def _find_checkout(oid: str) -> Optional[dict]:
    return next((c for c in _checkouts.values() if c["order_id_hex"] == oid), None)

def _order_summary(c: dict) -> dict:
    evs = _order_events.get(c["order_id_hex"], [])
    lane_num = str(c["lane"]).replace("lane_", "") if c.get("lane") is not None else ""
    product_name = CATALOG.get(lane_num, {}).get("name", f"lane_{c.get('lane', '')}")
    return {
        "checkout_id": c["id"], "order_id": c["order_id_hex"],
        "lane": c["lane"],      "product_name": product_name,
        "status":   c["status"],
        "events": [e["name"] for e in evs],
    }

# ── Embedded dashboard HTML ───────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>WM800 UCP Mock</title>
<style>
  *{box-sizing:border-box}
  body{font-family:ui-monospace,monospace;max-width:960px;margin:0 auto;padding:20px;
       background:#0d1117;color:#c9d1d9}
  h1,h2{color:#58a6ff;margin-bottom:8px}
  h2{font-size:1em;text-transform:uppercase;letter-spacing:.08em;color:#6e7681;margin-top:28px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:16px;margin:8px 0}
  .row{display:flex;gap:12px;flex-wrap:wrap}
  .item{padding:12px 16px;background:#21262d;border:1px solid #30363d;border-radius:6px;
        cursor:pointer;min-width:160px;transition:.15s}
  .item:hover{border-color:#58a6ff;background:#2d333b}
  .item .name{color:#e6edf3;font-weight:600;margin-bottom:4px}
  .item .price{color:#3fb950;font-size:.85em}
  .item .lane{color:#6e7681;font-size:.75em;margin-top:2px}
  .item.test{opacity:.65}
  .badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:.75em;font-weight:600}
  .badge.complete{background:#1a3a1a;color:#3fb950}
  .badge.incomplete{background:#1a2535;color:#58a6ff}
  .badge.error{background:#3a1a1a;color:#f85149}
  pre{background:#0d1117;padding:10px;border-radius:4px;overflow-x:auto;
      font-size:.8em;white-space:pre-wrap;word-break:break-all;margin:4px 0}
  details>summary{cursor:pointer;padding:4px 0;color:#79c0ff}
  details>summary:hover{color:#58a6ff}
  .ev{display:flex;gap:10px;padding:3px 0;border-bottom:1px solid #21262d;font-size:.85em}
  .ev .ts{color:#6e7681;min-width:75px}
  .ev .name{color:#3fb950}
  .ev .name.fail{color:#f85149}
  .status-ok{color:#3fb950}.status-err{color:#f85149}
  button{background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:5px 12px;
         border-radius:4px;cursor:pointer;font-family:inherit;font-size:.85em}
  button:hover{background:#30363d}
  #log{max-height:360px;overflow-y:auto}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .log-row{margin:6px 0}
  .log-row summary{font-size:.85em}
  .divider{border:none;border-top:1px solid #21262d;margin:8px 0}
  #auth-badge{font-size:.85em}
</style>
</head>
<body>

<h1>WM800 UCP Mock Server</h1>

<div class="card" style="display:flex;align-items:center;gap:16px">
  <span id="auth-badge">⏳ connecting…</span>
  <button onclick="doReset()" style="margin-left:auto">Reset All State</button>
</div>

<h2>Active Orders</h2>
<div class="card" id="orders">No orders yet. Buy something from the catalog below.</div>

<h2>Catalog — click to buy</h2>
<div class="card"><div id="catalog" class="row">Loading…</div></div>

<h2>API Log</h2>
<div class="card" id="log"></div>
<button onclick="document.getElementById('log').innerHTML=''">Clear log</button>

<script>
const orders = {};
let _token = null;

/* ── API helpers ── */
async function call(method, path, body) {
  const opts = { method, headers: {'Content-Type':'application/json'} };
  if (_token) opts.headers['Authorization'] = `Bearer ${_token}`;
  if (body)   opts.body = JSON.stringify(body);
  const r  = await fetch(path, opts);
  const ok = r.ok;
  const data = await r.json().catch(() => ({}));
  appendLog(method, path, body, data, r.status, ok);
  return { ok, data };
}

async function formPost(path, params) {
  const body = new URLSearchParams(params);
  const r    = await fetch(path, { method:'POST', body });
  const data = await r.json().catch(() => ({}));
  appendLog('POST', path, params, data, r.status, r.ok);
  return { ok: r.ok, data };
}

/* ── Auth ── */
async function authenticate() {
  const { ok, data } = await formPost('/oauth/token', {
    grant_type:'client_credentials', client_id:'demo', client_secret:'demo'
  });
  if (!ok) { document.getElementById('auth-badge').innerHTML =
    '<span class="status-err">✗ Auth failed</span>'; return false; }
  _token = data.access_token;
  document.getElementById('auth-badge').innerHTML =
    `<span class="status-ok">✓ Authenticated</span>
     &nbsp;client_id: <code>demo</code> &nbsp;secret: <code>demo</code>
     &nbsp;<span style="color:#6e7681">token: ${_token.slice(0,20)}…</span>`;
  return true;
}

/* ── Catalog ── */
async function loadCatalog() {
  const { ok, data } = await call('POST', '/cart-sessions');
  if (!ok) return;
  const el = document.getElementById('catalog');
  el.innerHTML = '';
  (data.line_items || []).forEach(item => {
    const test = item.name.includes('[test');
    const d = document.createElement('div');
    d.className = 'item' + (test?' test':'');
    d.innerHTML = `<div class="name">${item.name}</div>
      <div class="price">¥${(item.price.amount/100).toFixed(2)}</div>
      <div class="lane">${item.id}${item.quantity_available===0?' (empty)':''}</div>`;
    d.onclick = () => buy(item.id, item.name);
    el.appendChild(d);
  });
}

/* ── Buy flow ── */
async function buy(itemId, name) {
  // Step 1 — create checkout
  const { ok: ok1, data: chkData } = await call('POST', '/checkout-sessions', {
    line_items: [{ id: itemId, quantity: 1 }],
    buyer:   { email: 'test@example.com' },
    payment: { handler_id: 'prepaid', instrument: { token: `pay-${Date.now()}` } }
  });
  if (!ok1 || chkData.status === 'error') { showUcpError(chkData, name); return; }

  // Step 2 — complete → triggers dispense
  const { ok: ok2, data: cmpData } = await call('POST', `/checkout-sessions/${chkData.id}/complete`);
  if (!ok2 || cmpData.status === 'error') { showUcpError(cmpData, name); return; }

  const oid = cmpData.order_id;
  if (!oid) return;

  orders[oid] = { id: oid, name, events: [], status: 'incomplete' };
  renderOrders();
  streamOrder(oid);
}

/* ── SSE cookie (EventSource can't set headers — use HttpOnly cookie instead) ── */
async function issueSseCookie() {
  await call('POST', '/internal/sse-token');
}

/* ── SSE order tracking with polling fallback ── */
function streamOrder(oid) {
  let usedSSE = false;

  // Primary: SSE via HttpOnly cookie set by issueSseCookie()
  const es = new EventSource(`/orders/${oid}/stream`);

  es.onopen = () => { usedSSE = true; };

  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (ev.event === 'done') { es.close(); return; }
    if (!orders[oid]) return;
    orders[oid].events.push(ev);
    if (ev.action === 3) orders[oid].status = 'completed';
    if (ev.action === 8) orders[oid].status = 'error';
    renderOrders();
  };

  // Fallback: if SSE fails (e.g. cookie not set), poll the order endpoint
  es.onerror = () => {
    es.close();
    if (!usedSSE) pollOrder(oid);
  };
}

function pollOrder(oid) {
  const interval = setInterval(async () => {
    if (!orders[oid] || orders[oid].status !== 'incomplete') {
      clearInterval(interval); return;
    }
    const { ok, data } = await call('GET', `/orders/${oid}`);
    if (!ok) return;
    const evs = data?.fulfillment?.events || [];
    orders[oid].events = evs.map(e => ({name: e.type, ts: e.ts, action: 0}));
    // Map known event names back to action codes for status detection
    const names = evs.map(e => e.type);
    if (names.includes('goods_taken'))  { orders[oid].status = 'completed'; clearInterval(interval); }
    if (names.includes('rejected_0x08')){ orders[oid].status = 'error';    clearInterval(interval); }
    if (data.status === 'error')        { orders[oid].status = 'error';    clearInterval(interval); }
    renderOrders();
  }, 1500);
}

/* ── Render ── */
function renderOrders() {
  const el  = document.getElementById('orders');
  const list = Object.values(orders).reverse();
  if (!list.length) { el.textContent = 'No orders yet.'; return; }
  el.innerHTML = list.map(o => `
    <div style="margin:8px 0;padding:10px;background:#0d1117;border-radius:4px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <strong>${o.name}</strong>
        <span class="badge ${o.status}">${o.status}</span>
        <code style="color:#6e7681;font-size:.75em;margin-left:auto">${o.id}</code>
      </div>
      ${o.events.map(ev => `
        <div class="ev">
          <span class="ts">${new Date(ev.ts*1000).toLocaleTimeString()}</span>
          <span class="name${ev.action===8?' fail':''}">${ev.name}</span>
        </div>`).join('')}
      ${o.status==='incomplete'?'<div style="color:#58a6ff;font-size:.8em;margin-top:6px;animation:pulse 1.2s infinite">⏳ waiting for dispense events via SSE…</div>':''}
      ${o.continueUrl?`<div style="margin-top:6px;font-size:.8em">↪ fallback: <a href="${o.continueUrl}" target="_blank" style="color:#f0883e">${o.continueUrl}</a></div>`:''}
      ${o.errorMsg?`<div style="color:#f85149;font-size:.8em;margin-top:2px">${o.errorMsg}</div>`:''}
    </div>`).join('<hr class="divider">');
}

/* ── UCP error: show inline (no confirm() — it freezes the page) ── */
function showUcpError(data, name) {
  const msg  = data?.messages?.[0]?.message || 'Unknown error';
  const code = data?.messages?.[0]?.code    || 'error';
  const url  = data?.continue_url           || '';
  // Use a fake order-id so it appears in the orders list
  const fid  = 'err_' + Math.random().toString(36).slice(2, 10);
  orders[fid] = {
    id: fid, name,
    events: [{ name: code, ts: Date.now()/1000, action: 0xFF }],
    status: 'error',
    errorMsg: msg,
    continueUrl: url,
  };
  renderOrders();
}

/* ── Log ── */
function appendLog(method, path, req, res, status, ok) {
  const el  = document.getElementById('log');
  const ts  = new Date().toLocaleTimeString();
  const col = ok ? '#3fb950' : '#f85149';
  const d   = document.createElement('div');
  d.className = 'log-row';
  d.innerHTML = `<details>
    <summary><span style="color:${col}">[${ts}] ${method} ${path} → ${status}</span></summary>
    ${req  ? `<pre style="color:#79c0ff">→ ${JSON.stringify(req,null,2)}</pre>`  : ''}
    <pre style="color:#a5d6ff">← ${JSON.stringify(res,null,2)}</pre>
  </details>`;
  el.prepend(d);
}

/* ── Admin ── */
async function doReset() {
  await fetch('/admin/reset', { method:'POST' });
  Object.keys(orders).forEach(k => delete orders[k]);
  renderOrders();
  document.getElementById('log').innerHTML = '';
}

/* ── Load existing orders from server on startup ── */
async function loadExistingOrders() {
  const { ok, data } = await call('GET', '/orders');
  if (!ok || !Array.isArray(data)) return;
  for (const o of data) {
    const oid = o.order_id;
    if (!oid || orders[oid]) continue;
    orders[oid] = {
      id: oid,
      name: o.product_name || `lane_${o.lane}` || 'Unknown',
      events: (o.events || []).map(n => ({ name: n, ts: Date.now()/1000, action: 0 })),
      status: o.status === 'complete' ? 'complete' : o.status === 'error' ? 'error' : 'incomplete',
    };
    if (orders[oid].status === 'incomplete') streamOrder(oid);
  }
  renderOrders();
}

/* ── Init ── */
(async () => {
  if (await authenticate()) {
    await issueSseCookie();
    await loadCatalog();
    await loadExistingOrders();
  }
})();
</script>
</body>
</html>"""

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n  WM800 UCP Mock Server")
    print(f"  → {BASE_URL}")
    print(f"  client_id: {CLIENT_ID}   client_secret: {CLIENT_SECRET}")
    print(f"  continue_url: {CONTINUE_URL}\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")

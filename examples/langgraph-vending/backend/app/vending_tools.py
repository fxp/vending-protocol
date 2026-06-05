"""Vending machine tools for the LangGraph agent.

Connects to:
  supply-chain-mock  https://supply-chain-mock.fxp007.workers.dev
  ucp-mock           https://ucp-mock.fxp007.workers.dev

Flow (per vending-agent SKILL.md):
  list_machines / find_item
    -> get_ucp_token
    -> browse_catalog(machine_id)          # POST /cart-sessions
    -> create_checkout(lane_id, token)     # POST /checkout-sessions
    -> [apply_vending_discount]            # PUT /checkout-sessions/{id}
    -> [confirm with user]
    -> complete_vending_checkout           # POST /checkout-sessions/{id}/complete
    -> get_vending_order                   # GET /orders/{id}
"""
from __future__ import annotations

import httpx
from langchain_core.tools import tool

from .config import settings

_client = httpx.AsyncClient(timeout=30, headers={"User-Agent": "vending-langgraph/1.0"})

_SC = settings.supply_chain_url.rstrip("/")
_UCP = settings.ucp_mock_url.rstrip("/")


def _ucp_headers(token: str | None = None) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _fen_to_yuan(fen: int) -> str:
    return f"¥{fen / 100:.2f}"


# --------------------------------------------------------------------------- #
# Discovery (Supply Chain)
# --------------------------------------------------------------------------- #
@tool
async def list_machines() -> dict:
    """List all available vending machines with their location and inventory summary.
    Returns machine ids needed for browse_catalog."""
    r = await _client.get(f"{_SC}/machines")
    data = r.json()
    machines = data if isinstance(data, list) else data.get("machines", [])
    return {
        "machines": [
            {
                "machine_id": m.get("id"),
                "name": m.get("name"),
                "location": m.get("location"),
            }
            for m in machines
        ],
        "count": len(machines),
    }


@tool
async def find_item(keyword: str) -> dict:
    """Search for an item by name across ALL vending machines.
    Returns which machines have the item in stock, lane ids, and prices.
    Always call this first when a user mentions a specific product."""
    r = await _client.get(f"{_SC}/inventory/search", params={"name": keyword})
    data = r.json()
    results = data if isinstance(data, list) else data.get("results", [])
    items = []
    for row in results:
        items.append(
            {
                "machine_id": row.get("machine_id"),
                "machine_name": row.get("machine_name"),
                "lane_id": row.get("lane_id"),
                "sku_name": row.get("sku_name") or row.get("name"),
                "price": _fen_to_yuan(row.get("price_fen", 0)),
                "qty": row.get("qty", 0),
                "available": (row.get("qty", 0) > 0),
            }
        )
    in_stock = [i for i in items if i["available"]]
    return {
        "keyword": keyword,
        "in_stock": in_stock,
        "out_of_stock": [i for i in items if not i["available"]],
        "found": len(in_stock) > 0,
        "summary": (
            f"找到 {len(in_stock)} 台机器有货"
            if in_stock
            else "所有机器均无库存"
        ),
    }


# --------------------------------------------------------------------------- #
# Identity / Token
# --------------------------------------------------------------------------- #
@tool
async def get_ucp_token(user_id: str = "guest") -> dict:
    """Get a UCP Bearer token for the given user_id (or 'guest').
    Must be called before browse_catalog / create_checkout / complete_checkout.
    Returns token string to pass to subsequent calls."""
    r = await _client.post(
        f"{_UCP}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": user_id,
            "client_secret": settings.ucp_client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    data = r.json()
    return {
        "token": data.get("access_token", ""),
        "user_id": user_id,
        "expires_in": data.get("expires_in", 3600),
    }


# --------------------------------------------------------------------------- #
# Catalog browsing
# --------------------------------------------------------------------------- #
@tool
async def browse_catalog(machine_id: str, token: str) -> dict:
    """Browse the product catalog of a specific vending machine.
    Returns items with lane_ids and prices. Use lane_id when creating a checkout."""
    r = await _client.post(
        f"{_UCP}/cart-sessions",
        json={"machine_id": machine_id},
        headers=_ucp_headers(token),
    )
    data = r.json()
    items_raw = data.get("items") or data.get("catalog") or []
    items = []
    for it in items_raw:
        items.append(
            {
                "lane_id": it.get("lane_id") or it.get("id"),
                "name": it.get("name") or it.get("title"),
                "price": _fen_to_yuan(it.get("price_fen") or it.get("price") or 0),
                "available": it.get("available", True),
                "qty": it.get("qty"),
            }
        )
    return {
        "machine_id": machine_id,
        "items": items,
        "count": len(items),
        "cart_token": data.get("cart_token"),
    }


# --------------------------------------------------------------------------- #
# Checkout lifecycle
# --------------------------------------------------------------------------- #
@tool
async def create_vending_checkout(lane_id: str, token: str, buyer_email: str = "guest@vending.local") -> dict:
    """Create a checkout session for a specific lane (product) in the vending machine.
    Returns checkout_session_id and total amount. Use lane_id from browse_catalog or find_item."""
    r = await _client.post(
        f"{_UCP}/checkout-sessions",
        json={"lane_id": lane_id, "buyer_email": buyer_email},
        headers=_ucp_headers(token),
    )
    data = r.json()
    session_id = data.get("checkout_session_id") or data.get("id")
    total_fen = data.get("total_amount") or data.get("amount") or 0
    return {
        "checkout_session_id": session_id,
        "status": data.get("status"),
        "total": _fen_to_yuan(total_fen),
        "total_fen": total_fen,
        "currency": data.get("currency", "CNY"),
        "item_name": data.get("item_name") or data.get("name"),
        "lane_id": lane_id,
        "messages": data.get("messages", []),
    }


@tool
async def get_vending_checkout(checkout_session_id: str, token: str) -> dict:
    """Get the current status and details of a checkout session."""
    r = await _client.get(
        f"{_UCP}/checkout-sessions/{checkout_session_id}",
        headers=_ucp_headers(token),
    )
    data = r.json()
    total_fen = data.get("total_amount") or data.get("amount") or 0
    return {
        "checkout_session_id": checkout_session_id,
        "status": data.get("status"),
        "total": _fen_to_yuan(total_fen),
        "total_fen": total_fen,
        "currency": data.get("currency", "CNY"),
        "messages": data.get("messages", []),
    }


@tool
async def apply_vending_discount(checkout_session_id: str, token: str, discount_code: str) -> dict:
    """Apply a discount/promo code to a checkout session.
    Valid codes: SAVE10 (10% off), VEND20 (20% off).
    Returns updated totals including discount breakdown."""
    r = await _client.put(
        f"{_UCP}/checkout-sessions/{checkout_session_id}",
        json={"discounts": [{"code": discount_code}]},
        headers=_ucp_headers(token),
    )
    data = r.json()
    new_id = data.get("checkout_session_id") or checkout_session_id
    total_fen = data.get("total_amount") or 0
    totals = data.get("totals", [])
    return {
        "checkout_session_id": new_id,
        "status": data.get("status"),
        "discount_code": discount_code,
        "totals": [
            {"type": t.get("type"), "amount": _fen_to_yuan(t.get("amount", 0))}
            for t in totals
        ],
        "total": _fen_to_yuan(total_fen),
        "messages": data.get("messages", []),
    }


@tool
async def complete_vending_checkout(checkout_session_id: str, token: str) -> dict:
    """Complete the checkout and trigger vending machine dispensing.
    Only call this AFTER the user has explicitly confirmed the total amount.
    Returns order_id and pickup_code on success."""
    r = await _client.post(
        f"{_UCP}/checkout-sessions/{checkout_session_id}/complete",
        json={"payment": {"method": "prepaid"}},
        headers=_ucp_headers(token),
    )
    data = r.json()
    order = data.get("order") or {}
    return {
        "order_id": data.get("order_id") or order.get("id"),
        "status": data.get("status") or order.get("status"),
        "pickup_code": data.get("pickup_code") or order.get("pickup_code"),
        "pickup_url": data.get("pickup_url") or order.get("pickup_url"),
        "message": data.get("message") or "出货已开始，请等待商品取出",
        "error": data.get("error"),
    }


@tool
async def cancel_vending_checkout(checkout_session_id: str, token: str) -> dict:
    """Cancel an in-progress checkout session."""
    r = await _client.post(
        f"{_UCP}/checkout-sessions/{checkout_session_id}/cancel",
        json={},
        headers=_ucp_headers(token),
    )
    data = r.json()
    return {
        "checkout_session_id": checkout_session_id,
        "status": data.get("status", "canceled"),
        "message": "购买已取消",
    }


# --------------------------------------------------------------------------- #
# Supply chain (out-of-stock handling)
# --------------------------------------------------------------------------- #
@tool
async def create_preorder(sku_name: str, user_id: str, qty: int = 1, sku_id: str = "") -> dict:
    """Create a preorder for an out-of-stock item.
    The user will be notified via Slack when the item is restocked.
    Use when find_item shows 0 stock for all machines."""
    r = await _client.post(
        f"{_SC}/preorders",
        json={
            "sku_id": sku_id or f"sku-{sku_name[:8]}",
            "sku_name": sku_name,
            "qty": qty,
            "user_id": user_id,
            "notify_channel": "slack",
        },
    )
    data = r.json()
    return {
        "preorder_id": data.get("id") or data.get("preorder_id"),
        "sku_name": sku_name,
        "user_id": user_id,
        "qty": qty,
        "status": data.get("status", "pending"),
        "message": f"已为您登记预订 {qty} 件 {sku_name}，到货后将通知您",
    }


@tool
async def get_inventory_status(machine_id: str) -> dict:
    """Get detailed inventory for a specific vending machine.
    Shows stock levels for all lanes. Use to check if restocking is needed."""
    r = await _client.get(f"{_SC}/inventory", params={"machine_id": machine_id})
    data = r.json()
    lanes = data if isinstance(data, list) else data.get("lanes", [])
    low = [ln for ln in lanes if ln.get("qty", 0) < ln.get("min_qty", 5)]
    return {
        "machine_id": machine_id,
        "total_lanes": len(lanes),
        "low_stock_count": len(low),
        "low_stock": [
            {"lane_id": ln.get("lane_id"), "sku_name": ln.get("sku_name"), "qty": ln.get("qty")}
            for ln in low
        ],
        "lanes": [
            {
                "lane_id": ln.get("lane_id"),
                "sku_name": ln.get("sku_name"),
                "qty": ln.get("qty"),
                "price": _fen_to_yuan(ln.get("price_fen", 0)),
            }
            for ln in lanes
        ],
    }


ALL_VENDING_TOOLS = [
    list_machines,
    find_item,
    get_ucp_token,
    browse_catalog,
    create_vending_checkout,
    get_vending_checkout,
    apply_vending_discount,
    complete_vending_checkout,
    cancel_vending_checkout,
    create_preorder,
    get_inventory_status,
]

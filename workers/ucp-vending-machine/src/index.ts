export interface Env {
  VM_KV: KVNamespace;
  CLIENT_ID: string;
  CLIENT_SECRET: string;
  MACHINE_ID: string;
  MACHINE_NAME: string;
  // Optional: URL of a real WM800 UCP gateway to proxy dispense to.
  // If set, complete endpoint forwards to this URL instead of simulating.
  HARDWARE_GATEWAY_URL: string;
  HARDWARE_GATEWAY_TOKEN: string;
}

interface Product {
  id: string;
  name: string;
  price_fen: number;
  currency: string;
  lane_id: string;
  image_url?: string;
  available: boolean;
  description?: string;
}

interface ResolvedItem {
  product_id: string;
  name: string;
  qty: number;
  price_fen: number;
  lane_id: string;
}

interface CartSession {
  id: string;
  items: ResolvedItem[];
  total_fen: number;
  currency: string;
  created_at: string;
  expires_at: string;
}

interface CheckoutSession {
  id: string;
  cart_id: string;
  items: ResolvedItem[];
  total_fen: number;
  currency: string;
  status: string;
  created_at: string;
  order_id: string | null;
}

type OrderStatus = "accepted" | "door_open" | "goods_taken" | "completed" | "failed" | "empty";

interface OrderEvent {
  status: OrderStatus;
  at_ms: number;
}

interface Order {
  id: string;
  checkout_id: string;
  lane_id: string;
  product_id: string;
  product_name: string;
  price_fen: number;
  currency: string;
  created_ms: number;
  events: OrderEvent[];
  scenario: "normal" | "slow" | "empty";
}

function uid(): string {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 12);
}

function nowIso(): string {
  return new Date().toISOString();
}

function json(data: unknown, status = 200, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
      ...extra,
    },
  });
}

function apiErr(msg: string, status = 400, code = "error"): Response {
  return json({ error: { code, message: msg } }, status);
}

// Scenario dispatch mirrors vending-protocol/adapters/ucp/mock/server.py
function inferScenario(laneId: string): "normal" | "slow" | "empty" {
  const n = parseInt(laneId, 10);
  if (!isNaN(n) && n >= 200 && n <= 299) return "slow";
  if (!isNaN(n) && n === 900) return "empty";
  return "normal";
}

function buildTimeline(createdMs: number, scenario: string): OrderEvent[] {
  if (scenario === "empty") {
    return [{ status: "empty", at_ms: createdMs + 500 }];
  }
  if (scenario === "slow") {
    return [
      { status: "accepted",    at_ms: createdMs + 1_000 },
      { status: "door_open",   at_ms: createdMs + 10_000 },
      { status: "goods_taken", at_ms: createdMs + 25_000 },
      { status: "completed",   at_ms: createdMs + 27_000 },
    ];
  }
  // normal
  return [
    { status: "accepted",    at_ms: createdMs + 300 },
    { status: "door_open",   at_ms: createdMs + 3_000 },
    { status: "goods_taken", at_ms: createdMs + 8_000 },
    { status: "completed",   at_ms: createdMs + 10_000 },
  ];
}

function currentState(order: Order): {
  status: OrderStatus;
  occurred: Array<{ status: string; occurred_at: string }>;
} {
  const now = Date.now();
  const occurred: Array<{ status: string; occurred_at: string }> = [];
  let status: OrderStatus = "accepted";
  for (const ev of order.events) {
    if (now >= ev.at_ms) {
      occurred.push({ status: ev.status, occurred_at: new Date(ev.at_ms).toISOString() });
      status = ev.status;
    }
  }
  return { status, occurred };
}

async function authOk(request: Request, kv: KVNamespace): Promise<boolean> {
  const auth = request.headers.get("Authorization") ?? "";
  if (!auth.startsWith("Bearer ")) return false;
  const tok = auth.slice(7);
  const val = await kv.get(`token:${tok}`);
  if (!val) return false;
  return JSON.parse(val).expires_ms > Date.now();
}

async function handleRequest(request: Request, env: Env): Promise<Response> {
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
      },
    });
  }

  const url   = new URL(request.url);
  const path  = url.pathname.replace(/\/$/, "") || "/";
  const meth  = request.method;
  const kv    = env.VM_KV;
  const mid   = env.MACHINE_ID   || "vm-001";
  const mname = env.MACHINE_NAME || "UCP Vending Machine";

  // ── Public endpoints ────────────────────────────────────────────────────────

  if (meth === "GET" && path === "/.well-known/ucp") {
    return json({
      version: "1.0",
      merchant_id: mid,
      merchant_name: mname,
      currency: "CNY",
      token_endpoint:    `${url.origin}/oauth/token`,
      cart_endpoint:     `${url.origin}/cart-sessions`,
      checkout_endpoint: `${url.origin}/checkout-sessions`,
      catalog_endpoint:  `${url.origin}/catalog`,
      signing_keys: [],
    });
  }

  if (meth === "GET" && path === "/health") {
    return json({ status: "ok", machine_id: mid, time: nowIso() });
  }

  // ── OAuth token ─────────────────────────────────────────────────────────────
  if (meth === "POST" && path === "/oauth/token") {
    let grantType = "", clientId = "", clientSecret = "";
    const ct = request.headers.get("Content-Type") ?? "";
    if (ct.includes("application/x-www-form-urlencoded")) {
      const p = new URLSearchParams(await request.text());
      grantType    = p.get("grant_type")    ?? "";
      clientId     = p.get("client_id")     ?? "";
      clientSecret = p.get("client_secret") ?? "";
    } else {
      const b: any = await request.json().catch(() => ({}));
      grantType    = b.grant_type    ?? "";
      clientId     = b.client_id     ?? "";
      clientSecret = b.client_secret ?? "";
    }
    if (grantType !== "client_credentials") {
      return apiErr("unsupported_grant_type", 400, "unsupported_grant_type");
    }
    const wantId     = env.CLIENT_ID     || "vending";
    const wantSecret = env.CLIENT_SECRET || "secret";
    if (clientId !== wantId || clientSecret !== wantSecret) {
      return apiErr("invalid_client", 401, "invalid_client");
    }
    const token = `tok_${uid()}`;
    const ttl   = 3600;
    await kv.put(
      `token:${token}`,
      JSON.stringify({ client_id: clientId, issued_at: nowIso(), expires_ms: Date.now() + ttl * 1000 }),
      { expirationTtl: ttl + 60 },
    );
    return json({ access_token: token, token_type: "Bearer", expires_in: ttl });
  }

  // ── Catalog (read is public, write requires auth) ────────────────────────────
  if (meth === "GET" && path === "/catalog") {
    const list  = await kv.list({ prefix: "product:" });
    const items = await Promise.all(list.keys.map(k => kv.get(k.name)));
    const products = items.filter(Boolean).map(v => JSON.parse(v!));
    return json({ products, count: products.length });
  }

  const productMatch = path.match(/^\/catalog\/([^/]+)$/);
  if (productMatch) {
    const pid = productMatch[1];
    if (meth === "GET") {
      const val = await kv.get(`product:${pid}`);
      if (!val) return apiErr("Product not found", 404, "not_found");
      return json(JSON.parse(val));
    }
    if (meth === "PUT") {
      if (!await authOk(request, kv)) return apiErr("Unauthorized", 401, "unauthorized");
      const existing = await kv.get(`product:${pid}`);
      if (!existing) return apiErr("Product not found", 404, "not_found");
      const body: any = await request.json();
      const updated: Product = { ...JSON.parse(existing), ...body, id: pid };
      await kv.put(`product:${pid}`, JSON.stringify(updated));
      return json(updated);
    }
    if (meth === "DELETE") {
      if (!await authOk(request, kv)) return apiErr("Unauthorized", 401, "unauthorized");
      await kv.delete(`product:${pid}`);
      return json({ deleted: true, id: pid });
    }
  }

  if (meth === "POST" && path === "/catalog") {
    if (!await authOk(request, kv)) return apiErr("Unauthorized", 401, "unauthorized");
    const body: any = await request.json();
    const { id, name, price_fen, lane_id } = body;
    if (!id || !name || typeof price_fen !== "number" || !lane_id) {
      return apiErr("id, name, price_fen (number), lane_id required");
    }
    const product: Product = {
      id, name, price_fen,
      currency:    body.currency    || "CNY",
      lane_id,
      image_url:   body.image_url   || "",
      available:   body.available   !== false,
      description: body.description || "",
    };
    await kv.put(`product:${id}`, JSON.stringify(product));
    return json(product, 201);
  }

  // ── All remaining endpoints require auth ─────────────────────────────────────
  if (!await authOk(request, kv)) {
    return apiErr("Unauthorized — POST /oauth/token to get a token", 401, "unauthorized");
  }

  // ── Cart sessions ────────────────────────────────────────────────────────────
  if (meth === "POST" && path === "/cart-sessions") {
    const body: any = await request.json();
    const items: Array<{ product_id: string; qty?: number }> = body.items || [];
    if (!items.length) return apiErr("items array required");

    let total_fen = 0;
    const resolved: ResolvedItem[] = [];
    for (const item of items) {
      const pval = await kv.get(`product:${item.product_id}`);
      if (!pval) return apiErr(`product_not_found: ${item.product_id}`, 400, "product_not_found");
      const p: Product = JSON.parse(pval);
      if (!p.available) return apiErr(`product_unavailable: ${item.product_id}`, 400, "product_unavailable");
      const qty = item.qty ?? 1;
      total_fen += p.price_fen * qty;
      resolved.push({ product_id: p.id, name: p.name, qty, price_fen: p.price_fen, lane_id: p.lane_id });
    }

    const now = Date.now();
    const cart: CartSession = {
      id:         `cart_${uid()}`,
      items:      resolved,
      total_fen,
      currency:   "CNY",
      created_at: new Date(now).toISOString(),
      expires_at: new Date(now + 600_000).toISOString(),
    };
    await kv.put(`cart:${cart.id}`, JSON.stringify(cart), { expirationTtl: 600 });
    return json({
      ucp:          { version: "1.0" },
      cart_session: cart,
      continue_url: `${url.origin}/checkout-sessions`,
    }, 201);
  }

  // ── Checkout sessions ────────────────────────────────────────────────────────
  if (meth === "POST" && path === "/checkout-sessions") {
    const body: any = await request.json();
    if (!body.cart_id) return apiErr("cart_id required");
    const cartVal = await kv.get(`cart:${body.cart_id}`);
    if (!cartVal) return apiErr("cart_not_found: expired or invalid", 404, "cart_not_found");
    const cart: CartSession = JSON.parse(cartVal);

    const cs: CheckoutSession = {
      id:         `cs_${uid()}`,
      cart_id:    body.cart_id,
      items:      cart.items,
      total_fen:  cart.total_fen,
      currency:   cart.currency,
      status:     "pending",
      created_at: nowIso(),
      order_id:   null,
    };
    await kv.put(`checkout:${cs.id}`, JSON.stringify(cs), { expirationTtl: 3600 });
    return json({
      ucp:              { version: "1.0" },
      checkout_session: {
        ...cs,
        totals: [{ label: "合计", amount_fen: cs.total_fen, currency: cs.currency }],
      },
      continue_url: `${url.origin}/checkout-sessions/${cs.id}/complete`,
    }, 201);
  }

  const csGetMatch = path.match(/^\/checkout-sessions\/([^/]+)$/);
  if (csGetMatch && meth === "GET") {
    const val = await kv.get(`checkout:${csGetMatch[1]}`);
    if (!val) return apiErr("Checkout session not found", 404, "not_found");
    return json({ ucp: { version: "1.0" }, checkout_session: JSON.parse(val) });
  }

  const completeMatch = path.match(/^\/checkout-sessions\/([^/]+)\/complete$/);
  if (completeMatch && meth === "POST") {
    const csId = completeMatch[1];
    const val  = await kv.get(`checkout:${csId}`);
    if (!val) return apiErr("Checkout session not found", 404, "not_found");
    const cs: CheckoutSession = JSON.parse(val);
    if (cs.status !== "pending") {
      return apiErr(`Already processed: ${cs.status}`, 409, "already_processed");
    }
    if (!cs.items.length) return apiErr("No items in checkout");

    const item   = cs.items[0];
    const laneId = item.lane_id || "101";

    // Lane 901 = offline device
    if (parseInt(laneId, 10) === 901) {
      cs.status = "failed";
      await kv.put(`checkout:${csId}`, JSON.stringify(cs));
      return apiErr("Device offline", 503, "device_unavailable");
    }

    // If a hardware gateway is configured, proxy the dispense request
    if (env.HARDWARE_GATEWAY_URL) {
      const resp = await fetch(`${env.HARDWARE_GATEWAY_URL}/checkout-sessions`, {
        method:  "POST",
        headers: {
          "Content-Type":  "application/json",
          "Authorization": `Bearer ${env.HARDWARE_GATEWAY_TOKEN}`,
        },
        body: JSON.stringify({ cart_id: cs.cart_id, lane_id: laneId }),
      });
      const proxyBody = await resp.json();
      if (!resp.ok) {
        cs.status = "failed";
        await kv.put(`checkout:${csId}`, JSON.stringify(cs));
        return json(proxyBody, resp.status);
      }
      cs.status   = "processing";
      cs.order_id = (proxyBody as any).order?.id ?? null;
      await kv.put(`checkout:${csId}`, JSON.stringify(cs));
      return json(proxyBody, 201);
    }

    // Mock simulation mode
    const scenario = inferScenario(laneId);
    const now      = Date.now();
    const order: Order = {
      id:           `ord_${uid()}`,
      checkout_id:  csId,
      lane_id:      laneId,
      product_id:   item.product_id,
      product_name: item.name,
      price_fen:    item.price_fen,
      currency:     cs.currency,
      created_ms:   now,
      events:       buildTimeline(now, scenario),
      scenario,
    };

    cs.status   = "processing";
    cs.order_id = order.id;
    await kv.put(`order:${order.id}`, JSON.stringify(order), { expirationTtl: 86400 });
    await kv.put(`checkout:${csId}`, JSON.stringify(cs));

    const { status, occurred } = currentState(order);
    return json({
      ucp:        { version: "1.0" },
      order: {
        id:           order.id,
        status,
        product_id:   order.product_id,
        product_name: order.product_name,
        price_fen:    order.price_fen,
        currency:     order.currency,
        events:       occurred,
        created_at:   new Date(order.created_ms).toISOString(),
      },
      poll_url:   `${url.origin}/orders/${order.id}`,
      events_url: `${url.origin}/orders/${order.id}/events`,
    }, 201);
  }

  // ── Order polling ────────────────────────────────────────────────────────────
  const orderGetMatch = path.match(/^\/orders\/([^/]+)$/);
  if (orderGetMatch && meth === "GET") {
    const val = await kv.get(`order:${orderGetMatch[1]}`);
    if (!val) return apiErr("Order not found", 404, "not_found");
    const order: Order = JSON.parse(val);
    const { status, occurred } = currentState(order);
    return json({
      ucp:   { version: "1.0" },
      order: {
        id:           order.id,
        status,
        product_id:   order.product_id,
        product_name: order.product_name,
        price_fen:    order.price_fen,
        currency:     order.currency,
        lane_id:      order.lane_id,
        events:       occurred,
        created_at:   new Date(order.created_ms).toISOString(),
      },
    });
  }

  // ── SSE order event stream ───────────────────────────────────────────────────
  const orderEventsMatch = path.match(/^\/orders\/([^/]+)\/events$/);
  if (orderEventsMatch && meth === "GET") {
    const val = await kv.get(`order:${orderEventsMatch[1]}`);
    if (!val) return apiErr("Order not found", 404, "not_found");
    const order: Order = JSON.parse(val);

    const { readable, writable } = new TransformStream<Uint8Array, Uint8Array>();
    const writer  = writable.getWriter();
    const encode  = (s: string) => new TextEncoder().encode(s);
    const sse     = (name: string, data: unknown) =>
      writer.write(encode(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`));

    (async () => {
      try {
        const { status, occurred } = currentState(order);
        await sse("status", { status, events: occurred, order_id: order.id });
        if (["completed", "failed", "empty"].includes(status)) {
          await sse("done", { final_status: status });
          return;
        }
        const now = Date.now();
        for (const ev of order.events) {
          if (ev.at_ms <= now) continue;
          await new Promise(r => setTimeout(r, Math.max(0, ev.at_ms - Date.now())));
          await sse("status", { status: ev.status, occurred_at: new Date(ev.at_ms).toISOString(), order_id: order.id });
          if (["completed", "failed", "empty"].includes(ev.status)) {
            await sse("done", { final_status: ev.status });
            break;
          }
        }
      } finally {
        await writer.close().catch(() => {});
      }
    })();

    return new Response(readable, {
      headers: {
        "Content-Type":  "text/event-stream",
        "Cache-Control": "no-cache",
        "Access-Control-Allow-Origin": "*",
      },
    });
  }

  return apiErr("Endpoint not found", 404, "not_found");
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    return handleRequest(request, env);
  },
};

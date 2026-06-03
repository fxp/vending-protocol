/**
 * UCP Mock Server — Cloudflare Worker (stateless-friendly, in-memory)
 *
 * Uses module-level Maps for state — persists within warm isolate lifetime,
 * which is fine for dev/test use. No KV binding required.
 */

export interface Env {
  JWT_SECRET: string;
  BASE_URL: string;
}

// ── In-memory state ───────────────────────────────────────────────────────────
const _carts     = new Map<string, object>();
const _checkouts = new Map<string, object>();
const _orders    = new Map<string, object>();
const _alipay    = new Map<string, Record<string, unknown>>();

// ── Catalog ───────────────────────────────────────────────────────────────────
const CATALOG: Record<string, { name: string; price: number; currency: string }> = {
  "100": { name: "矿泉水 550ml",    price: 200,  currency: "CNY" },
  "101": { name: "可口可乐 330ml",  price: 500,  currency: "CNY" },
  "102": { name: "绿茶 500ml",      price: 400,  currency: "CNY" },
  "103": { name: "拿铁 250ml",      price: 1500, currency: "CNY" },
  "104": { name: "百事可乐 330ml",  price: 450,  currency: "CNY" },
  "105": { name: "气泡水 330ml",    price: 350,  currency: "CNY" },
  "200": { name: "三明治",          price: 2500, currency: "CNY" },
  "201": { name: "能量棒",          price: 800,  currency: "CNY" },
  "900": { name: "[空货道测试]",    price: 100,  currency: "CNY" },
  "901": { name: "[离线机器测试]",  price: 100,  currency: "CNY" },
};

const CLIENT_ID     = "demo";
const CLIENT_SECRET = "demo";
const TOKEN_TTL     = 3600;

// ── Helpers ───────────────────────────────────────────────────────────────────
const enc = new TextEncoder();

function ok(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
  });
}
function err(msg: string, status: number): Response { return ok({ error: msg }, status); }
function uid(): string { return crypto.randomUUID().replace(/-/g, ""); }
function now(): number { return Math.floor(Date.now() / 1000); }
function wrap(data: Record<string, unknown>, base: string): Record<string, unknown> {
  return { ucp: "2026-04-08", ...data, continue_url: `${base}/fallback` };
}

// ── JWT (HMAC-SHA256, stateless) ──────────────────────────────────────────────
async function hmacB64(data: string, secret: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(data));
  return btoa(String.fromCharCode(...new Uint8Array(sig)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=/g, "");
}
function b64(s: string): string {
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=/g, "");
}
async function signJWT(payload: object, secret: string): Promise<string> {
  const h = b64(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const p = b64(JSON.stringify(payload));
  const sig = await hmacB64(`${h}.${p}`, secret);
  return `${h}.${p}.${sig}`;
}
async function verifyJWT(token: string, secret: string): Promise<Record<string, unknown> | null> {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [h, p, s] = parts;
  const expected = await hmacB64(`${h}.${p}`, secret);
  if (s !== expected) return null;
  try {
    const payload = JSON.parse(atob(p.replace(/-/g, "+").replace(/_/g, "/"))) as Record<string, unknown>;
    if ((payload.exp as number) < now()) return null;
    return payload;
  } catch { return null; }
}
async function requireAuth(req: Request, env: Env): Promise<{ sub: string } | Response> {
  const auth = req.headers.get("Authorization") ?? "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!token) return err("unauthorized", 401);
  const payload = await verifyJWT(token, env.JWT_SECRET);
  if (!payload) return err("invalid_token", 401);
  return { sub: payload.sub as string };
}

// ── Scenario timing ───────────────────────────────────────────────────────────
function timings(lane: number) {
  if (lane >= 200 && lane <= 299) return { accepted: 0.3, door_open: 10, goods_taken: 25, completed: 30 };
  return { accepted: 0.3, door_open: 3, goods_taken: 8, completed: 12 };
}

// ── Cashier page HTML ─────────────────────────────────────────────────────────
function cashierHtml(orderId: string, product: string, yuan: string): string {
  return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>支付宝 AI 付</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"PingFang SC",sans-serif;background:#f5f5f5;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{width:340px;border-radius:20px;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,.15)}
.hdr{background:linear-gradient(135deg,#1677ff,#0958d9);color:#fff;padding:20px 24px;text-align:center}
.hdr .logo{font-size:22px;font-weight:800}.hdr .badge{display:inline-block;background:rgba(255,255,255,.2);border-radius:20px;padding:2px 10px;font-size:11px;margin-top:4px}
.body{padding:24px;background:#fff}
.merchant{font-size:13px;color:#888;text-align:center;margin-bottom:4px}
.amount{text-align:center;font-size:40px;font-weight:800;color:#1a1a1a;margin-bottom:20px}
.agent-info{background:#fffbe6;border:1px solid #ffe58f;border-radius:8px;padding:10px 12px;margin-bottom:16px;font-size:12px;color:#875800}
.agent-info strong{display:block;margin-bottom:2px}
.bio-label{font-size:12px;color:#888;text-align:center;margin-bottom:10px}
.methods{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:20px}
.method{padding:12px 4px;background:#f8f9fa;border:2px solid transparent;border-radius:12px;text-align:center;cursor:pointer;transition:.15s}
.method:hover,.method.active{background:#e8f0ff;border-color:#1677ff}
.method .icon{font-size:22px;display:block;margin-bottom:4px}.method .label{font-size:11px;color:#555;font-weight:500}
.progress{display:none;text-align:center;padding:16px;margin-bottom:12px}
.spinner{width:40px;height:40px;border:3px solid #e0e0e0;border-top-color:#1677ff;border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 10px}
@keyframes spin{to{transform:rotate(360deg)}}
.pmsg{font-size:13px;color:#555}
.btn{width:100%;padding:15px;background:linear-gradient(135deg,#1677ff,#0958d9);color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:700;cursor:pointer}
.btn:disabled{opacity:.6;cursor:not-allowed}
.btn.ok{background:linear-gradient(135deg,#00a854,#007a3c)}
.sec{font-size:11px;color:#bbb;text-align:center;margin-top:14px}
</style></head>
<body><div class="card">
<div class="hdr"><div class="logo">支付宝 AI 付</div><div class="badge">Alipay Agent Pay · ACT/1.0</div></div>
<div class="body">
  <div class="merchant">${product}</div>
  <div class="amount">${yuan}</div>
  <div class="agent-info"><strong>🤖 AI 代理授权支付</strong>智能体已发起代理支付请求，请选择身份验证方式</div>
  <div class="bio-label">选择验证方式</div>
  <div class="methods" id="ms">
    <div class="method active" onclick="sel(this,'face')"><span class="icon">😊</span><span class="label">面容</span></div>
    <div class="method" onclick="sel(this,'fingerprint')"><span class="icon">👆</span><span class="label">指纹</span></div>
    <div class="method" onclick="sel(this,'voiceprint')"><span class="icon">🎙️</span><span class="label">声纹</span></div>
    <div class="method" onclick="sel(this,'pin')"><span class="icon">🔢</span><span class="label">密码</span></div>
  </div>
  <div class="progress" id="pg"><div class="spinner"></div><div class="pmsg" id="pm">正在验证…</div></div>
  <button class="btn" id="btn" onclick="pay()">确认支付 ${yuan}</button>
  <div class="sec">🔒 支付宝 TEE 安全保护 · ACT/1.0 意图授权凭证</div>
</div></div>
<script>
var M='face',LBL={face:'正在识别面容…',fingerprint:'正在识别指纹…',voiceprint:'正在识别声纹…',pin:'正在验证密码…'};
function sel(el,m){document.querySelectorAll('.method').forEach(x=>x.classList.remove('active'));el.classList.add('active');M=m;}
function pay(){
  var btn=document.getElementById('btn');
  btn.disabled=true;
  document.getElementById('ms').style.opacity='.4';
  var pg=document.getElementById('pg'); pg.style.display='block';
  document.getElementById('pm').textContent=LBL[M]||'正在验证…';
  fetch('/alipay/confirm-payment/${orderId}',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({auth_method:M})})
  .then(r=>r.json()).then(d=>{
    pg.style.display='none';
    if(d.status==='paid'){
      btn.textContent='✅ 支付成功'; btn.classList.add('ok'); btn.disabled=false;
      if(window.parent!==window) window.parent.postMessage({
        type:'alipay_paid',
        alipay_order_id: d.paid_alipay_order_id || '${orderId}',
        intent_credential: d.intent_credential,
        auth_method: M
      },'*');
    }
  }).catch(()=>{
    pg.style.display='none'; btn.disabled=false;
    document.getElementById('ms').style.opacity='1';
    alert('支付失败，请重试');
  });
}
</script></body></html>`;
}

// ── Main handler ──────────────────────────────────────────────────────────────
export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    if (req.method === "OPTIONS") {
      return new Response(null, { headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Authorization,Content-Type",
      }});
    }

    const url  = new URL(req.url);
    const path = url.pathname;
    const base = env.BASE_URL || `${url.protocol}//${url.host}`;

    try {
      // OAuth token
      if (path === "/oauth/token" && req.method === "POST") {
        let grant = "", cid = "", csec = "";
        const ct = req.headers.get("Content-Type") ?? "";
        if (ct.includes("x-www-form-urlencoded")) {
          const fd = await req.formData();
          grant = String(fd.get("grant_type") ?? "");
          cid   = String(fd.get("client_id")     ?? "");
          csec  = String(fd.get("client_secret")  ?? "");
        } else {
          const b = await req.json().catch(() => ({})) as Record<string, string>;
          grant = b.grant_type ?? ""; cid = b.client_id ?? ""; csec = b.client_secret ?? "";
        }
        if (grant !== "client_credentials") return err("unsupported_grant_type", 400);
        if (cid !== CLIENT_ID || csec !== CLIENT_SECRET) return err("invalid_client", 401);
        const jti   = uid();
        const exp   = now() + TOKEN_TTL;
        const token = await signJWT({ sub: cid, jti, iat: now(), exp }, env.JWT_SECRET);
        return ok({ access_token: token, token_type: "bearer", expires_in: TOKEN_TTL });
      }

      // Discovery
      if (path === "/.well-known/ucp") {
        return ok({
          ucp: "2026-04-08", vendor: "vending-protocol-mock", device_type: "vending_machine",
          token_endpoint: `${base}/oauth/token`,
          cart_endpoint: `${base}/cart-sessions`,
          checkout_endpoint: `${base}/checkout-sessions`,
          order_endpoint: `${base}/orders`,
          signing_keys: [],
          payment_handlers: [
            { handler_id: "alipay_aipay", name: "com.alipay.aipay", type: "ap2",
              ap2: { app_id: "mock_alipay_app_2026", cashier_base: `${base}/alipay/cashier`,
                verify_base: `${base}/alipay/query-order`, mandate_type: "alipay_intent_credential", act_version: "ACT/1.0" },
              config: { app_id: "mock_alipay_app_2026", cashier_base: `${base}/alipay/cashier`,
                verify_base: `${base}/alipay/query-order`, mandate_type: "alipay_intent_credential", act_version: "ACT/1.0" },
            },
            { handler_id: "google_pay", name: "com.google.pay", type: "gpay", config: {} },
            { handler_id: "prepaid",    name: "dev.prepaid",    type: "prepaid", config: {} },
          ],
        });
      }

      // Cart
      if (path === "/cart-sessions" && req.method === "POST") {
        const auth = await requireAuth(req, env);
        if (auth instanceof Response) return auth;
        const items = Object.entries(CATALOG).map(([lane, info]) => ({
          id: lane, lane_id: lane, name: info.name, price: info.price, currency: info.currency,
          available: lane !== "900", quantity_available: lane === "900" ? 0 : 10,
        }));
        const cartId = uid();
        _carts.set(cartId, { id: cartId, items });
        return ok(wrap({ cart_session_id: cartId, items,
          messages: [{ role: "vending_machine", content: "请选择商品。" }] }, base));
      }

      // Checkout create
      if (path === "/checkout-sessions" && req.method === "POST") {
        const auth = await requireAuth(req, env);
        if (auth instanceof Response) return auth;
        const body = await req.json().catch(() => ({})) as Record<string, unknown>;
        const lineItems = body.line_items as Array<{ id: string }> | undefined;
        const laneId = String(lineItems?.[0]?.id ?? body.lane_id ?? "");
        const item = CATALOG[laneId];
        if (!item) return err("item_not_found", 404);
        const checkoutId = uid();
        const checkout = {
          id: checkoutId, lane_id: laneId, item_name: item.name,
          amount: item.price, currency: item.currency,
          status: "incomplete", payment: { handler_id: "alipay_aipay", alipay_order_id: null },
          created_at: now(),
        };
        _checkouts.set(checkoutId, checkout);
        return ok(wrap({ checkout_session_id: checkoutId, status: "incomplete",
          amount: item.price, currency: item.currency,
          item: { lane_id: laneId, name: item.name },
          payment_handlers: ["alipay_aipay", "google_pay", "prepaid"],
          messages: [{ role: "vending_machine", content: `已选：${item.name} ¥${(item.price / 100).toFixed(2)}` }],
        }, base));
      }

      // Checkout get
      { const m = path.match(/^\/checkout-sessions\/([^/]+)$/);
        if (m && req.method === "GET") {
          const auth = await requireAuth(req, env); if (auth instanceof Response) return auth;
          const chk = _checkouts.get(m[1]);
          if (!chk) return err("not_found", 404);
          return ok(wrap({ checkout_session_id: (chk as Record<string,unknown>).id, ...(chk as Record<string,unknown>) }, base));
        }
      }

      // Checkout complete
      { const m = path.match(/^\/checkout-sessions\/([^/]+)\/complete$/);
        if (m && req.method === "POST") {
          const auth = await requireAuth(req, env); if (auth instanceof Response) return auth;
          const body = await req.json().catch(() => ({})) as Record<string, unknown>;
          const checkoutId = m[1];
          const chk = _checkouts.get(checkoutId) as Record<string, unknown> | undefined;
          if (!chk) return err("checkout_not_found", 404);
          const laneId = String(chk.lane_id);
          if (laneId === "901") return ok({ ucp: "2026-04-08", error: "device_unavailable",
            message: "机器离线。", continue_url: `${base}/fallback` }, 503);
          const orderId = uid();
          const order: Record<string, unknown> = {
            id: orderId, checkout_id: checkoutId, lane_id: laneId,
            item_name: chk.item_name, amount: chk.amount, currency: chk.currency,
            status: laneId === "900" ? "failed" : "processing",
            failure_reason: laneId === "900" ? "lane_empty" : null,
            handler_id: body.handler_id ?? (chk.payment as Record<string,unknown>)?.handler_id ?? null,
            started_at: now(), events: [],
          };
          _orders.set(orderId, order);
          _checkouts.set(checkoutId, { ...chk, status: "complete", order_id: orderId });
          return ok(wrap({ order_id: orderId, checkout_session_id: checkoutId, status: order.status,
            events_url: `${base}/orders/${orderId}/events`,
            messages: [{ role: "vending_machine", content: laneId === "900" ? "货道空。" : "出货中…" }],
          }, base));
        }
      }

      // Order get
      { const m = path.match(/^\/orders\/([^/]+)$/);
        if (m && req.method === "GET") {
          const auth = await requireAuth(req, env); if (auth instanceof Response) return auth;
          const order = _orders.get(m[1]);
          if (!order) return err("not_found", 404);
          return ok(wrap(order as Record<string,unknown>, base));
        }
      }

      // Order SSE events
      { const m = path.match(/^\/orders\/([^/]+)\/events$/);
        if (m) {
          const orderId  = m[1];
          const order    = _orders.get(orderId) as Record<string, unknown> | undefined;
          if (!order) return err("not_found", 404);
          const laneId   = Number(order.lane_id);
          const startMs  = (order.started_at as number) * 1000;
          const t        = timings(laneId);

          const stream = new ReadableStream({
            async start(ctrl) {
              const send = (evt: string, data: object) =>
                ctrl.enqueue(enc.encode(`event: ${evt}\ndata: ${JSON.stringify(data)}\n\n`));
              const wait = (sec: number) => {
                const ms = sec * 1000 - (Date.now() - startMs);
                return ms > 0 ? new Promise<void>(r => setTimeout(r, ms)) : Promise.resolve();
              };
              if (order.status === "failed") {
                send("rejected", { order_id: orderId, reason: order.failure_reason, status: "failed" });
                send("done",     { order_id: orderId, status: "failed" });
                ctrl.close(); return;
              }
              ctrl.enqueue(enc.encode(": ping\n\n"));
              await wait(t.accepted);   send("accepted",    { order_id: orderId, status: "accepted" });
              await wait(t.door_open);  send("door_open",   { order_id: orderId, status: "door_open",   message: "货道门已开" });
              await wait(t.goods_taken);send("goods_taken", { order_id: orderId, status: "goods_taken", message: "商品已取出" });
              await wait(t.completed);
              _orders.set(orderId, { ...order, status: "completed" });
              send("completed", { order_id: orderId, status: "completed", message: "感谢购买！" });
              send("done",      { order_id: orderId, status: "completed" });
              ctrl.close();
            },
          });
          return new Response(stream, { headers: {
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
            "Connection": "keep-alive", "Access-Control-Allow-Origin": "*",
          }});
        }
      }

      // Alipay create order
      if (path === "/alipay/create-order" && req.method === "POST") {
        const auth = await requireAuth(req, env); if (auth instanceof Response) return auth;
        const body = await req.json().catch(() => ({})) as Record<string, unknown>;
        const alipayId = `alipay_${uid().slice(0, 16)}`;
        _alipay.set(alipayId, {
          alipay_order_id: alipayId,
          checkout_id: body.checkout_id, merchant_order_id: body.merchant_order_id ?? body.checkout_id,
          amount: body.amount, currency: body.currency ?? "CNY",
          product_name: body.product_name ?? "", status: "pending",
          created_at: now(), agent_pay_info: body.agent_pay_info ?? null,
        });
        const chk = _checkouts.get(String(body.checkout_id)) as Record<string,unknown> | undefined;
        if (chk) { const p = {...(chk.payment as object ?? {}), alipay_order_id: alipayId}; _checkouts.set(String(body.checkout_id), {...chk, payment: p}); }
        return ok({ alipay_order_id: alipayId, cashier_url: `${base}/alipay/cashier/${alipayId}`,
          amount: body.amount, currency: body.currency ?? "CNY",
          status: "pending", act_protocol: "ACT/1.0", mandate_type: "alipay_intent_credential" });
      }

      // Alipay query order
      { const m = path.match(/^\/alipay\/query-order\/([^/]+)$/);
        if (m && req.method === "GET") {
          const auth = await requireAuth(req, env); if (auth instanceof Response) return auth;
          const order = _alipay.get(m[1]);
          if (!order) return err("alipay order not found", 404);
          return ok({ alipay_order_id: order.alipay_order_id, checkout_id: order.checkout_id,
            merchant_order_id: order.merchant_order_id, status: order.status,
            amount: order.amount, currency: order.currency,
            paid_at: order.paid_at ?? null, intent_credential: order.intent_credential ?? null });
        }
      }

      // Alipay confirm payment
      { const m = path.match(/^\/alipay\/confirm-payment\/([^/]+)$/);
        if (m && req.method === "POST") {
          const order = _alipay.get(m[1]);
          if (!order) return err("alipay order not found", 404);
          const body = await req.json().catch(() => ({})) as Record<string, string>;
          const authMethod = body.auth_method ?? "face";
          const paidAt = now();
          const mandatePayload = {
            type: "alipay_intent_credential", alipay_order_id: order.alipay_order_id,
            checkout_id: order.checkout_id, amount: order.amount, currency: order.currency,
            auth_method: authMethod, issued_at: paidAt, act_version: "ACT/1.0",
          };
          const encoded = btoa(JSON.stringify(mandatePayload))
            .replace(/\+/g, "-").replace(/\//g, "_").replace(/=/g, "");
          const credential = `alipay_ic.${encoded}.mock_sig`;
          const updated = { ...order, status: "paid", paid_at: paidAt, intent_credential: credential };
          _alipay.set(String(m[1]), updated);
          const chk = _checkouts.get(String(order.checkout_id)) as Record<string,unknown> | undefined;
          if (chk && chk.status === "incomplete")
            _checkouts.set(String(order.checkout_id), { ...chk, status: "ready_for_complete" });
          return ok({ status: "paid", intent_credential: credential,
            paid_alipay_order_id: m[1] }); // same ID, state updated in map
        }
      }

      // Alipay cashier page
      { const m = path.match(/^\/alipay\/cashier\/([^/]+)$/);
        if (m) {
          const order = _alipay.get(m[1]);
          if (!order) return new Response("<h2>Order not found</h2>", { status: 404, headers: { "Content-Type": "text/html" }});
          const yuan = `¥${((order.amount as number) / 100).toFixed(2)}`;
          const product = String(order.product_name ?? "商品");
          if (order.status === "paid") {
            return new Response(`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>支付成功</title>
<style>body{font-family:sans-serif;text-align:center;padding:40px;background:#f0f9f0}</style></head>
<body><div style="font-size:64px">✅</div><h2 style="color:#00a854">支付成功</h2>
<p>${product} · ${yuan}</p></body></html>`, { headers: { "Content-Type": "text/html" }});
          }
          return new Response(cashierHtml(m[1], product, yuan), { headers: { "Content-Type": "text/html" }});
        }
      }

      if (path === "/fallback" || path === "/health")
        return ok({ ucp: "2026-04-08", status: "ok" });

      return err("not_found", 404);
    } catch (e) {
      console.error(e);
      return err("internal_server_error", 500);
    }
  },
};

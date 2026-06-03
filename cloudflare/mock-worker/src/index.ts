/**
 * UCP Mock Server — Cloudflare Worker (fully stateless: all IDs are signed JWTs)
 *
 * No Map / KV / Durable Objects needed. Every "ID" embeds its own state,
 * signed with JWT_SECRET, so any isolate can verify and decode it.
 *
 * ID types (sub field):
 *   cs  — checkout session
 *   ao  — alipay order (pending)
 *   ic  — intent credential (paid receipt)
 *   ord — order (awaiting pickup)
 *   dt  — dispense token (pickup validated)
 */

export interface Env {
  JWT_SECRET: string;
  BASE_URL: string;
}

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
function yuan(cents: number): string { return `¥${(cents / 100).toFixed(2)}`; }

// ── JWT (HMAC-SHA256) ─────────────────────────────────────────────────────────
async function hmacB64(data: string, secret: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(data));
  return btoa(String.fromCharCode(...new Uint8Array(sig)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=/g, "");
}
function b64(s: string): string {
  return btoa(unescape(encodeURIComponent(s))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=/g, "");
}
function b64d(s: string): string {
  try { return decodeURIComponent(escape(atob(s.replace(/-/g, "+").replace(/_/g, "/")))); } catch { return "{}"; }
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
    const payload = JSON.parse(b64d(p)) as Record<string, unknown>;
    if (typeof payload.exp === "number" && payload.exp < now()) return null;
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
function cashierHtml(alipayId: string, product: string, yStr: string, base: string): string {
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
  <div class="amount">${yStr}</div>
  <div class="agent-info"><strong>🤖 AI 代理授权支付</strong>智能体已发起代理支付请求，请选择身份验证方式</div>
  <div class="bio-label">选择验证方式</div>
  <div class="methods" id="ms">
    <div class="method active" onclick="sel(this,'face')"><span class="icon">😊</span><span class="label">面容</span></div>
    <div class="method" onclick="sel(this,'fingerprint')"><span class="icon">👆</span><span class="label">指纹</span></div>
    <div class="method" onclick="sel(this,'voiceprint')"><span class="icon">🎙️</span><span class="label">声纹</span></div>
    <div class="method" onclick="sel(this,'pin')"><span class="icon">🔢</span><span class="label">密码</span></div>
  </div>
  <div class="progress" id="pg"><div class="spinner"></div><div class="pmsg" id="pm">正在验证…</div></div>
  <button class="btn" id="btn" onclick="pay()">确认支付 ${yStr}</button>
  <div class="sec">🔒 支付宝 TEE 安全保护 · ACT/1.0 意图授权凭证</div>
</div></div>
<script>
var M='face',LBL={face:'正在识别面容…',fingerprint:'正在识别指纹…',voiceprint:'正在识别声纹…',pin:'正在验证密码…'};
var ALIPAY_ID='${alipayId}';
function sel(el,m){document.querySelectorAll('.method').forEach(x=>x.classList.remove('active'));el.classList.add('active');M=m;}
function pay(){
  var btn=document.getElementById('btn');
  btn.disabled=true;
  document.getElementById('ms').style.opacity='.4';
  var pg=document.getElementById('pg'); pg.style.display='block';
  document.getElementById('pm').textContent=LBL[M]||'正在验证…';
  fetch('${base}/alipay/confirm-payment/'+encodeURIComponent(ALIPAY_ID),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({auth_method:M})})
  .then(r=>r.json()).then(d=>{
    pg.style.display='none';
    if(d.status==='paid'){
      btn.textContent='✅ 支付成功'; btn.classList.add('ok'); btn.disabled=false;
      if(window.parent!==window) window.parent.postMessage({
        type:'alipay_paid',
        alipay_order_id: d.receipt_id,
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

// ── Vending machine page HTML ─────────────────────────────────────────────────
function vmHtml(orderId: string, product: string, yStr: string, pickupCode: string, base: string): string {
  return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>🏪 自动贩卖机取货</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"PingFang SC",sans-serif;background:#0a0a0f;color:#e0e0e0;min-height:100vh;display:flex;align-items:center;justify-content:center}
.machine{width:360px}
.screen{background:#111827;border-radius:16px;border:2px solid #1e40af;box-shadow:0 0 40px rgba(30,64,175,.4);overflow:hidden}
.top-bar{background:linear-gradient(90deg,#1e3a8a,#1e40af);padding:10px 16px;display:flex;align-items:center;gap:8px}
.top-bar .dot{width:8px;height:8px;border-radius:50%}
.dot-r{background:#ef4444}.dot-y{background:#f59e0b}.dot-g{background:#10b981}
.top-bar .title{font-size:13px;font-weight:700;color:#93c5fd;margin-left:4px}
.body{padding:24px}
.vm-icon{font-size:48px;text-align:center;margin-bottom:8px}
.vm-title{font-size:18px;font-weight:800;text-align:center;color:#fff;margin-bottom:4px}
.vm-sub{font-size:12px;text-align:center;color:#6b7280;margin-bottom:20px}
.order-info{background:#1f2937;border-radius:10px;padding:12px 16px;margin-bottom:16px}
.order-row{display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px}
.order-row:last-child{margin-bottom:0}
.order-row .label{color:#6b7280}.order-row .val{color:#e5e7eb;font-weight:600}
.code-section{margin-bottom:16px}
.code-label{font-size:11px;color:#6b7280;margin-bottom:6px;text-align:center}
.code-input{width:100%;background:#0f172a;border:2px solid #374151;border-radius:8px;padding:10px 12px;font-size:20px;font-weight:800;font-family:monospace;letter-spacing:4px;color:#60a5fa;text-align:center;outline:none}
.code-input:focus{border-color:#1e40af}
.btn{width:100%;padding:14px;background:linear-gradient(135deg,#1e40af,#1d4ed8);color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;margin-bottom:8px;transition:.15s}
.btn:hover:not(:disabled){background:linear-gradient(135deg,#1d4ed8,#1e3a8a)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.success{background:linear-gradient(135deg,#059669,#047857)}
.events{margin-top:12px}
.ev{display:flex;align-items:flex-start;gap:8px;padding:8px 0;border-bottom:1px solid #1f2937;font-size:13px;opacity:0;transform:translateY(8px);transition:all .4s}
.ev.show{opacity:1;transform:none}
.ev-icon{font-size:18px;flex-shrink:0;margin-top:-1px}
.ev-text{color:#d1d5db}
.ev-time{font-size:11px;color:#4b5563;margin-top:2px}
.status-bar{background:#111827;border-top:1px solid #1e3a8a;padding:8px 16px;text-align:center;font-size:11px;color:#4b5563}
</style></head>
<body><div class="machine">
<div class="screen">
  <div class="top-bar">
    <div class="dot dot-r"></div><div class="dot dot-y"></div><div class="dot dot-g"></div>
    <div class="title">🏪 UCP Vending Machine · Mock</div>
  </div>
  <div class="body">
    <div class="vm-icon">🤖</div>
    <div class="vm-title">AI 自动贩卖机</div>
    <div class="vm-sub">请输入或扫描取货码</div>
    <div class="order-info">
      <div class="order-row"><span class="label">商品</span><span class="val">${product}</span></div>
      <div class="order-row"><span class="label">金额</span><span class="val" style="color:#10b981">${yStr}</span></div>
    </div>
    <div class="code-section">
      <div class="code-label">取货码（扫描二维码或手动输入）</div>
      <input class="code-input" id="codeInput" value="${pickupCode}" maxlength="8" placeholder="8位取货码" oninput="this.value=this.value.toUpperCase()">
    </div>
    <button class="btn" id="redeemBtn" onclick="redeem()">✅ 确认取货</button>
    <div class="events" id="events"></div>
  </div>
  <div class="status-bar" id="statusBar">等待取货码验证…</div>
</div></div>
<script>
var ORDER_ID = decodeURIComponent('${encodeURIComponent(orderId)}');
var BASE = '${base}';
var EV_ICONS = {accepted:'⚙️',door_open:'🚪',goods_taken:'🥤',completed:'🎉',rejected:'❌'};
var EV_LABELS = {accepted:'出货已开始',door_open:'货道门已开',goods_taken:'商品已取出',completed:'感谢购买！请取走商品',rejected:'出货失败'};
function addEvent(type, msg) {
  var el = document.createElement('div');
  el.className = 'ev';
  var t = new Date().toLocaleTimeString('zh-CN');
  el.innerHTML = '<span class="ev-icon">'+(EV_ICONS[type]||'•')+'</span><div><div class="ev-text">'+(msg||EV_LABELS[type]||type)+'</div><div class="ev-time">'+t+'</div></div>';
  document.getElementById('events').appendChild(el);
  requestAnimationFrame(function(){ el.classList.add('show'); });
}
function redeem() {
  var code = document.getElementById('codeInput').value.trim();
  var btn = document.getElementById('redeemBtn');
  btn.disabled = true; btn.textContent = '验证中…';
  document.getElementById('statusBar').textContent = '正在验证取货码…';
  fetch(BASE+'/orders/'+encodeURIComponent(ORDER_ID)+'/redeem', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({pickup_code: code})
  }).then(function(r){ return r.json(); }).then(function(d){
    if(d.ok){
      btn.textContent = '⚙️ 出货中…';
      document.getElementById('statusBar').textContent = '已验证 · 出货中…';
      startSSE(d.events_url);
    } else {
      btn.disabled = false; btn.textContent = '✅ 确认取货';
      document.getElementById('statusBar').textContent = '❌ 取货码无效：'+(d.error||'请重试');
    }
  }).catch(function(){
    btn.disabled=false; btn.textContent='✅ 确认取货';
    document.getElementById('statusBar').textContent='网络错误，请重试';
  });
}
function startSSE(eventsUrl) {
  var es = new EventSource(eventsUrl);
  ['accepted','door_open','goods_taken','completed','rejected'].forEach(function(ev) {
    es.addEventListener(ev, function(e) {
      var d = JSON.parse(e.data);
      addEvent(ev, d.message);
      if(ev==='completed'){
        document.getElementById('redeemBtn').textContent = '✅ 取货完成！';
        document.getElementById('redeemBtn').classList.add('success');
        document.getElementById('statusBar').textContent = '✅ 出货完成 · 感谢使用';
        es.close();
      }
      if(ev==='rejected'){
        document.getElementById('redeemBtn').textContent = '❌ 出货失败';
        document.getElementById('statusBar').textContent = '出货失败：'+(d.reason||'');
        es.close();
      }
    });
  });
  es.onerror = function(){ /* ignore reconnects */ };
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

      // ── OAuth token ────────────────────────────────────────────────────────
      if (path === "/oauth/token" && req.method === "POST") {
        let grant = "", cid = "", csec = "";
        const ct = req.headers.get("Content-Type") ?? "";
        if (ct.includes("x-www-form-urlencoded")) {
          const fd = await req.formData();
          grant = String(fd.get("grant_type") ?? "");
          cid   = String(fd.get("client_id")  ?? "");
          csec  = String(fd.get("client_secret") ?? "");
        } else {
          const b = await req.json().catch(() => ({})) as Record<string, string>;
          grant = b.grant_type ?? ""; cid = b.client_id ?? ""; csec = b.client_secret ?? "";
        }
        if (grant !== "client_credentials") return err("unsupported_grant_type", 400);
        if (cid !== CLIENT_ID || csec !== CLIENT_SECRET) return err("invalid_client", 401);
        const token = await signJWT({ sub: cid, jti: uid(), iat: now(), exp: now() + TOKEN_TTL }, env.JWT_SECRET);
        return ok({ access_token: token, token_type: "bearer", expires_in: TOKEN_TTL });
      }

      // ── Discovery ─────────────────────────────────────────────────────────
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

      // ── Cart (catalog) ────────────────────────────────────────────────────
      if (path === "/cart-sessions" && req.method === "POST") {
        const auth = await requireAuth(req, env);
        if (auth instanceof Response) return auth;
        const items = Object.entries(CATALOG).map(([lane, info]) => ({
          id: lane, lane_id: lane, name: info.name, price: info.price, currency: info.currency,
          available: lane !== "900", quantity_available: lane === "900" ? 0 : 10,
        }));
        return ok(wrap({ cart_session_id: uid(), items,
          messages: [{ role: "vending_machine", content: "请选择商品。" }] }, base));
      }

      // ── Checkout create ───────────────────────────────────────────────────
      if (path === "/checkout-sessions" && req.method === "POST") {
        const auth = await requireAuth(req, env);
        if (auth instanceof Response) return auth;
        const body = await req.json().catch(() => ({})) as Record<string, unknown>;
        const lineItems = body.line_items as Array<{ id: string }> | undefined;
        const laneId = String(lineItems?.[0]?.id ?? body.lane_id ?? "");
        const item = CATALOG[laneId];
        if (!item) return err("item_not_found", 404);

        // Checkout session ID = signed JWT (stateless)
        const csPayload = {
          sub: "cs", lane: laneId, name: item.name,
          amt: item.price, cur: item.currency,
          iat: now(), exp: now() + 7200,
        };
        const checkoutId = await signJWT(csPayload, env.JWT_SECRET);

        return ok(wrap({ checkout_session_id: checkoutId, status: "incomplete",
          amount: item.price, currency: item.currency,
          item: { lane_id: laneId, name: item.name },
          payment_handlers: ["alipay_aipay", "google_pay", "prepaid"],
          messages: [{ role: "vending_machine", content: `已选：${item.name} ¥${(item.price / 100).toFixed(2)}` }],
        }, base));
      }

      // ── Checkout get ──────────────────────────────────────────────────────
      { const m = path.match(/^\/checkout-sessions\/([^/]+)$/);
        if (m && req.method === "GET") {
          const auth = await requireAuth(req, env); if (auth instanceof Response) return auth;
          const cs = await verifyJWT(decodeURIComponent(m[1]), env.JWT_SECRET);
          if (!cs || cs.sub !== "cs") return err("checkout_not_found", 404);
          return ok(wrap({ checkout_session_id: m[1], status: "incomplete",
            amount: cs.amt, currency: cs.cur, lane_id: cs.lane }, base));
        }
      }

      // ── Checkout complete ─────────────────────────────────────────────────
      { const m = path.match(/^\/checkout-sessions\/([^/]+)\/complete$/);
        if (m && req.method === "POST") {
          const auth = await requireAuth(req, env); if (auth instanceof Response) return auth;
          const checkoutToken = decodeURIComponent(m[1]);
          const cs = await verifyJWT(checkoutToken, env.JWT_SECRET);
          if (!cs || cs.sub !== "cs") return err("checkout_not_found", 404);

          const laneId = String(cs.lane);
          if (laneId === "901") return ok({ ucp: "2026-04-08", error: "device_unavailable",
            message: "机器离线。", continue_url: `${base}/fallback` }, 503);

          const isFailed = laneId === "900";
          const pickupCode = (await hmacB64(`pickup:${checkoutToken}`, env.JWT_SECRET)).slice(0, 8).toUpperCase();

          // Order ID = signed JWT (stateless)
          const ordPayload = {
            sub: "ord", lane: laneId, name: cs.name, amt: cs.amt, cur: cs.cur,
            cs: checkoutToken, pickup: pickupCode,
            status: isFailed ? "failed" : "awaiting_pickup",
            fail: isFailed ? "lane_empty" : null,
            iat: now(), exp: now() + 86400,
          };
          const orderId = await signJWT(ordPayload, env.JWT_SECRET);
          const pickupUrl = `${base}/vm/${encodeURIComponent(orderId)}?code=${pickupCode}&product=${encodeURIComponent(String(cs.name))}&amount=${cs.amt}`;

          return ok(wrap({
            order_id: orderId,
            checkout_session_id: checkoutToken,
            status: ordPayload.status,
            pickup_code: pickupCode,
            pickup_url: pickupUrl,
            events_url: `${base}/orders/${encodeURIComponent(orderId)}/events`,
            messages: [{ role: "vending_machine", content: isFailed ? "货道空。" : `取货码：${pickupCode}，请前往贩卖机扫码取货。` }],
          }, base));
        }
      }

      // ── Order get ─────────────────────────────────────────────────────────
      { const m = path.match(/^\/orders\/([^/]+)$/);
        if (m && req.method === "GET") {
          const auth = await requireAuth(req, env); if (auth instanceof Response) return auth;
          const ord = await verifyJWT(decodeURIComponent(m[1]), env.JWT_SECRET);
          if (!ord || (ord.sub !== "ord" && ord.sub !== "dt")) return err("not_found", 404);
          return ok(wrap({
            order_id: m[1], lane_id: ord.lane, item_name: ord.name,
            amount: ord.amt, currency: ord.cur,
            status: ord.status ?? "awaiting_pickup",
            pickup_code: ord.pickup ?? null,
          }, base));
        }
      }

      // ── Order redeem (validate pickup_code → issue dispense token) ────────
      { const m = path.match(/^\/orders\/([^/]+)\/redeem$/);
        if (m && req.method === "POST") {
          const orderId = decodeURIComponent(m[1]);
          const ord = await verifyJWT(orderId, env.JWT_SECRET);
          if (!ord || ord.sub !== "ord") return ok({ ok: false, error: "invalid_order_token" });
          if (ord.status === "failed") return ok({ ok: false, error: "lane_empty" });

          const body = await req.json().catch(() => ({})) as Record<string, string>;
          const code = (body.pickup_code ?? "").toUpperCase();

          // Stateless HMAC validation
          const expected = (await hmacB64(`pickup:${String(ord.cs)}`, env.JWT_SECRET)).slice(0, 8).toUpperCase();
          if (!code || code !== expected) return ok({ ok: false, error: "invalid_pickup_code" });

          const startedAt = now();
          const dtPayload = {
            sub: "dt", order_id: orderId, lane: ord.lane, name: ord.name,
            amt: ord.amt, cur: ord.cur, started_at: startedAt,
            exp: startedAt + 600,
          };
          const dispenseToken = await signJWT(dtPayload, env.JWT_SECRET);
          const eventsUrl = `${base}/orders/${encodeURIComponent(orderId)}/events?token=${encodeURIComponent(dispenseToken)}`;
          return ok({ ok: true, order_id: orderId, dispense_token: dispenseToken, events_url: eventsUrl });
        }
      }

      // ── Order SSE events (stateless via dispense token) ───────────────────
      { const m = path.match(/^\/orders\/([^/]+)\/events$/);
        if (m) {
          const tokenParam = url.searchParams.get("token");
          if (!tokenParam) return err("token_required", 400);

          const dt = await verifyJWT(decodeURIComponent(tokenParam), env.JWT_SECRET);
          if (!dt || dt.sub !== "dt") return err("invalid_token", 401);

          const orderId  = String(dt.order_id ?? m[1]);
          const laneId   = Number(dt.lane);
          const startMs  = Number(dt.started_at) * 1000;
          const t        = timings(laneId);

          const stream = new ReadableStream({
            async start(ctrl) {
              const send = (evt: string, data: object) =>
                ctrl.enqueue(enc.encode(`event: ${evt}\ndata: ${JSON.stringify(data)}\n\n`));
              const wait = (sec: number) => {
                const ms = sec * 1000 - (Date.now() - startMs);
                return ms > 0 ? new Promise<void>(r => setTimeout(r, ms)) : Promise.resolve();
              };
              if (laneId === 900) {
                send("rejected", { order_id: orderId, reason: "lane_empty", status: "failed" });
                send("done",     { order_id: orderId, status: "failed" });
                ctrl.close(); return;
              }
              ctrl.enqueue(enc.encode(": ping\n\n"));
              await wait(t.accepted);    send("accepted",    { order_id: orderId, status: "accepted" });
              await wait(t.door_open);   send("door_open",   { order_id: orderId, status: "door_open",   message: "货道门已开" });
              await wait(t.goods_taken); send("goods_taken", { order_id: orderId, status: "goods_taken", message: "商品已取出" });
              await wait(t.completed);
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

      // ── Alipay create order ───────────────────────────────────────────────
      if (path === "/alipay/create-order" && req.method === "POST") {
        const auth = await requireAuth(req, env); if (auth instanceof Response) return auth;
        const body = await req.json().catch(() => ({})) as Record<string, unknown>;

        // Alipay order ID = signed JWT (stateless)
        const aoPayload = {
          sub: "ao",
          cs: body.checkout_id,
          amt: body.amount,
          cur: body.currency ?? "CNY",
          prod: body.product_name ?? "",
          agent: body.agent_pay_info ?? null,
          iat: now(), exp: now() + 3600,
        };
        const alipayId = await signJWT(aoPayload, env.JWT_SECRET);
        return ok({
          alipay_order_id: alipayId,
          cashier_url: `${base}/alipay/cashier/${encodeURIComponent(alipayId)}`,
          amount: body.amount, currency: body.currency ?? "CNY",
          status: "pending", act_protocol: "ACT/1.0", mandate_type: "alipay_intent_credential",
        });
      }

      // ── Alipay cashier page ───────────────────────────────────────────────
      { const m = path.match(/^\/alipay\/cashier\/([^/]+)$/);
        if (m) {
          const ao = await verifyJWT(decodeURIComponent(m[1]), env.JWT_SECRET);
          if (!ao || ao.sub !== "ao")
            return new Response("<h2>Order not found or expired</h2>", { status: 404, headers: { "Content-Type": "text/html" }});
          const yStr = yuan(Number(ao.amt));
          const product = String(ao.prod ?? "商品");
          return new Response(cashierHtml(decodeURIComponent(m[1]), product, yStr, base),
            { headers: { "Content-Type": "text/html; charset=utf-8" }});
        }
      }

      // ── Alipay confirm payment ────────────────────────────────────────────
      { const m = path.match(/^\/alipay\/confirm-payment\/([^/]+)$/);
        if (m && req.method === "POST") {
          const ao = await verifyJWT(decodeURIComponent(m[1]), env.JWT_SECRET);
          if (!ao || ao.sub !== "ao") return err("alipay order not found", 404);
          const body = await req.json().catch(() => ({})) as Record<string, string>;
          const authMethod = body.auth_method ?? "face";
          const paidAt = now();
          const alipayId = decodeURIComponent(m[1]);

          // Intent credential = signed JWT (payment receipt)
          const icPayload = {
            sub: "ic",
            ao: alipayId, cs: ao.cs,
            amt: ao.amt, cur: ao.cur,
            method: authMethod, paid_at: paidAt,
            iat: paidAt, exp: paidAt + 900,
          };
          const intentCredential = await signJWT(icPayload, env.JWT_SECRET);

          return ok({
            status: "paid",
            intent_credential: intentCredential,
            receipt_id: intentCredential,  // VM page uses this as the query ID
            paid_alipay_order_id: alipayId,
            auth_method: authMethod,
          });
        }
      }

      // ── Alipay query order — accepts receipt_id (intent credential JWT) ───
      { const m = path.match(/^\/alipay\/query-order\/([^/]+)$/);
        if (m && req.method === "GET") {
          const auth = await requireAuth(req, env); if (auth instanceof Response) return auth;
          const token = decodeURIComponent(m[1]);
          // Try as intent credential (receipt_id, post-payment)
          const ic = await verifyJWT(token, env.JWT_SECRET);
          if (ic && ic.sub === "ic") {
            return ok({
              alipay_order_id: ic.ao,
              checkout_id: ic.cs,
              status: "paid",
              amount: ic.amt, currency: ic.cur,
              paid_at: ic.paid_at,
              intent_credential: token,
            });
          }
          // Try as alipay order (pending)
          const ao = await verifyJWT(token, env.JWT_SECRET);
          if (ao && ao.sub === "ao") {
            return ok({
              alipay_order_id: token,
              checkout_id: ao.cs,
              status: "pending",
              amount: ao.amt, currency: ao.cur,
              paid_at: null, intent_credential: null,
            });
          }
          return err("order_not_found", 404);
        }
      }

      // ── Vending machine page ──────────────────────────────────────────────
      { const m = path.match(/^\/vm\/([^/]+)$/);
        if (m && req.method === "GET") {
          const orderId = decodeURIComponent(m[1]);
          const code    = url.searchParams.get("code") ?? "";
          const amountN = Number(url.searchParams.get("amount") ?? 0);
          const product = url.searchParams.get("product") ?? "商品";
          const yStr    = yuan(amountN);
          // Also try to decode order token for richer info
          const ord = await verifyJWT(orderId, env.JWT_SECRET);
          const finalProduct = (ord?.name as string) ?? product;
          const finalYuan    = ord?.amt ? yuan(Number(ord.amt)) : yStr;
          return new Response(vmHtml(orderId, finalProduct, finalYuan, code, base),
            { headers: { "Content-Type": "text/html; charset=utf-8" }});
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

# ucp-vending-machine

Standalone UCP merchant Cloudflare Worker for vending machines.

**Two modes:**
- **Mock** (default) — simulates dispense timing in-process, no hardware needed.
- **Hardware bridge** — set `HARDWARE_GATEWAY_URL` to proxy `/complete` to a real WM800 UCP adapter.

## UCP flow

```
GET  /.well-known/ucp                        → discovery
POST /oauth/token                            → Bearer token (client_credentials)
GET  /catalog                                → product list (KV-backed)
POST /cart-sessions                          → create cart
POST /checkout-sessions                      → create checkout from cart
POST /checkout-sessions/{id}/complete        → trigger dispense
GET  /orders/{id}                            → poll order status
GET  /orders/{id}/events                     → SSE stream of order events
```

## Mock scenarios (by lane number)

| Lane     | Behaviour |
|----------|-----------|
| 100–199  | Normal: accepted 300ms → door_open 3s → goods_taken 8s → completed 10s |
| 200–299  | Slow: accepted 1s → door_open 10s → goods_taken 25s → completed 27s |
| 900      | Empty: returns `empty` at 500ms |
| 901      | Offline: `complete` immediately returns 503 `device_unavailable` |

## Quick start (local dev)

```bash
npm install
npx wrangler dev          # → http://localhost:8787
```

In another terminal:

```bash
# Get a token
curl -X POST http://localhost:8787/oauth/token \
  -d 'grant_type=client_credentials&client_id=vending&client_secret=secret'

# Seed catalog
./seed.sh

# Buy something
TOK=<your_token>
curl -X POST http://localhost:8787/cart-sessions \
  -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" \
  -d '{"items":[{"product_id":"water-500","qty":1}]}'
```

## Deploy to Cloudflare Workers

```bash
# 1. Create KV namespace
npx wrangler kv namespace create VM_KV

# 2. Copy and fill wrangler config
cp wrangler.example.jsonc wrangler.jsonc
# Edit wrangler.jsonc: set account_id and kv_namespaces[0].id

# 3. Set secret
npx wrangler secret put CLIENT_SECRET

# 4. Deploy
npm run deploy

# 5. Seed catalog
BASE_URL=https://ucp-vending-machine.<your-subdomain>.workers.dev \
  CLIENT_SECRET=<your-secret> ./seed.sh
```

## Hardware bridge mode

Set these Wrangler secrets to forward dispense calls to a real [WM800 UCP adapter](../../adapters/ucp/server/):

```bash
npx wrangler secret put HARDWARE_GATEWAY_URL    # e.g. https://wm800.internal:8080
npx wrangler secret put HARDWARE_GATEWAY_TOKEN  # bearer token for the gateway
```

All other endpoints (catalog, cart, checkout polling, SSE) remain in the Worker.

## Protocol compliance

Aligned with `vending-protocol/adapters/ucp/references/mapping.md`. Known gaps:

- HTTP Message Signatures (RFC 9421) — `signing_keys: []`, not implemented
- No `PUT /checkout-sessions/{id}` or `/cancel` endpoint
- `UCP-Agent` / `Idempotency-Key` header validation not enforced

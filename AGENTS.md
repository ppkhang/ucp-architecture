# AGENTS.md: building UCP

Notes for an AI coding assistant (Claude Code, Codex, Copilot, Cursor...) working on the UCP build. Read this file fully before writing code. For Claude Code, put `@AGENTS.md` in `CLAUDE.md` so it loads automatically.

Owner: Phạm Phú Khang (UIT, Đồ án 2). Repo: `github.com/ppkhang/ucp-architecture`.

---

## 1. What UCP is

UCP (Universal Commerce Protocol, the school project) is a gateway that lets AI shopping agents search products, check out and track orders on e-commerce platforms (Shopee, Tiki, Lazada...) through one API, with the user's consent at every money step.

Status: **design only, nothing is implemented yet.** The defended design is v1.0 (report, June 2026). **Build v1.1**, the post-defense revision. Where v1.0 and v1.1 disagree, v1.1 wins.

Not to be confused with Google's Universal Commerce Protocol (ucp.dev, January 2026). Same name, different project. Don't pull Google's schemas into this repo unless Khang asks for a compatibility layer.

## 2. Where the truth is (read in this order)

| Priority | File | What it holds |
|---|---|---|
| 1 | `docs/UCP_v1.1_Addendum.md` Part C and D | v1.1 design and the build order. Vietnamese |
| 2 | `docs/adr/ADR-006` to `ADR-010` | v1.1 decisions |
| 3 | `specs/APISpecs.md`, section "v1.1" at the end | Endpoint changes, new intents, new error codes |
| 4 | `specs/APISpecs.md` (rest), `docs/adr/ADR-001` to `ADR-005` | v1.0 API and decisions, still valid unless v1.1 changes them |
| 5 | `docs/UCP_Glossary.md` | Terms |
| 6 | `deliverables/*.docx, *.xlsx` | Report, data model, domain events (sheet v1.1), traceability (sheet v1.1), business rules |

The docs are in Vietnamese. Read them as they are. Write code, identifiers and code comments in English. Keep any doc you edit in its own language.

If two sources disagree, don't pick one silently. Note it and ask Khang.

## 3. Hard rules (do not break these)

These come from the v1.1 ADRs. Code that breaks one is wrong even if tests pass.

1. **The platform is the source of truth** (ADR-006) for stock, price, promotions, shipping fees, orders and payment. UCP never decides stock or price itself. `inventory_status` is a display cache only.
2. **The agent can never confirm a purchase or a cancellation** (ADR-007). The user confirms on a confirmation page while logged in with their platform account, and that account must match the UserLink. Nothing the agent holds can produce a valid confirmation.
3. **A confirmation is bound to the proposal hash, the user, the agent and an expiry, and can be used once.** The page shows UCP's stored proposal snapshot, never anything the agent sent.
4. **Every `POST /intent` requires an `Idempotency-Key` header** (ADR-010). Store keys unique per agent and replay the stored result for 24 hours. Same key with a different body returns `UCP_IDEMPOTENCY_CONFLICT`. Forward the key to the platform on `placeOrder` and `cancelOrder`.
5. **Never resend `placeOrder` blindly after a timeout.** Mark the order "reconciling" and look it up with `getOrder` by idempotency key.
6. **UCP is not the merchant of record** (ADR-008). The payment module tracks status only. No card data, no payment provider integration, no refunds executed by UCP.
7. **UCP stores no shipping addresses.** The user picks one from the platform's address book on the confirmation page.
8. **One context, one schema. No cross-schema queries or joins.** Modules talk through their public service interface or events.
9. **Events leave through a transactional outbox**, written in the same transaction as the state change. Consumers dedupe by event ID.
10. **Checkout expiry is run by `ExpiryWorker`** reading `expires_at` from the Trade schema. It must not depend on RabbitMQ.
11. **Auth:** JWT lives 5 minutes and the session is checked on every call. API keys only get `read_catalog`. `view_order`, `create_order` and `cancel_order` need a UserLink.
12. **Platform OAuth tokens are encrypted, stored apart from UserLink and rotated.** An expired link returns `UCP_LINK_EXPIRED`.
13. **Never log or return secrets**: API keys, platform tokens, JWT signing keys.

## 4. Stack

| Area | Choice |
|---|---|
| Language | Python 3.12+ |
| API | FastAPI, Pydantic v2 |
| Database | PostgreSQL 16, **one instance, one schema per context** (keeps ADR-003 as a logical boundary) |
| Migrations | Alembic, one migration folder per schema or one folder with schema-prefixed revisions |
| Cache, sessions, rate limits | Redis 7 |
| Broker | RabbitMQ, **only once there is a second consumer**. Until then the outbox relay delivers to in-process handlers |
| Agent-facing AI layer | MCP server wrapping the UCP API (one tool per intent) |
| Tests | pytest, httpx, a real Postgres in Docker (no SQLite stand-in) |
| Run | Docker Compose locally. Later the same compose file on one VPS behind Caddy |

### Version facts your training data may miss (checked 2026-11-15)

- **MCP spec `2026-07-28`** is current. The protocol is now stateless: no `initialize` handshake, no `Mcp-Session-Id`, and each request carries its protocol version in `_meta` and the `MCP-Protocol-Version` header. Authorization: Dynamic Client Registration is deprecated in favour of Client ID Metadata Documents.
- **MCP Python SDK v2** (`pip install mcp` now installs 2.x) supports `2026-07-28` and still serves older clients. **`FastMCP` was renamed `MCPServer`.** Most examples online are v1. Check import paths against the installed package and the v2 docs, don't copy them from v1 examples.
- **FastMCP 4** (the separate PrefectHQ package) is GA and built on SDK v2. Pick one of the two, not both. Default: the official SDK.
- Claude can use a remote MCP server as a **custom connector** by URL (Free plan: one custom connector). That is the demo path.

## 5. Shape of the code

Modular monolith (addendum Part D step 4). Don't split into services until there's a real reason.

```
ucp/
  api/                 FastAPI app: routers, auth deps, error handler
  modules/
    gateway/           auth, rate limit, quota, routing, SSE fanout
    identity/          agents, api keys, user links, consent, platform tokens
    catalog/           search via adapters + short cache
    trade/             checkout, proposal snapshots, confirmation, orders, expiry
    payment/           payment and refund status tracking only
    notification/      event log per agent, SSE, /events replay
  adapters/
    base.py            PlatformAdapter protocol (section 6)
    mock/              MockPlatformAdapter client
  outbox/              outbox table helpers + relay
  workers/             expiry, order sync (reconcile), outbox relay
  common/              errors, ids, money, time, idempotency
mock_platform/         separate FastAPI app: the fake shop (products, stock, holds, orders, webhooks, OAuth login)
mcp_server/            MCP server, one tool per intent, calls the UCP API over HTTP
deploy/                docker-compose.yml, Caddyfile, .env.example
tests/                 unit, contract (adapter), scenario (section 9)
```

Context to schema: `identity`, `catalog`, `trade`, `payment`, `notification`. The gateway keeps its state in Redis.

Money is integer VND (no floats). Timestamps are UTC, ISO 8601 in the API.

## 6. PlatformAdapter contract (v1.1, 12 methods)

| Method | Purpose |
|---|---|
| `capabilities()` | What the platform supports: holds, webhooks, platform-hosted confirmation page, COD, partial cancel |
| `searchProducts(query)` | Live search |
| `getProduct(id)` | Live detail |
| `getQuote(items, region)` | Price, stock, shipping fee right now |
| `createCheckout(items, shipping)` | Checkout on the platform, final total, hold if supported |
| `renewHold(checkoutId)`, `releaseHold(checkoutId)` | Hold control |
| `placeOrder(checkoutId, idempotencyKey)` | Create order, return payment link |
| `getOrder(id or idempotencyKey)` | Status, including after a timeout |
| `cancelOrder(id, idempotencyKey)` | Platform accepts or refuses, returns refund reference |
| `getRefund(refundRef)` | Refund status |
| `parseEvent(headers, body)` | Verify and normalize a platform webhook |

Each adapter gets its own timeout, connection pool and circuit breaker. A slow platform must not slow down another one. Multi-platform search returns `partial: true` when one platform doesn't answer.

If a platform doesn't hold stock, the proposal says "stock not guaranteed", and `placeOrder` can fail with `UCP_INSUFFICIENT_STOCK` before any money moves.

**Mock platform** must support: products with variants and stock, holds with expiry, orders with the platform's own statuses, a payment page that can succeed or fail, signed webhooks, OAuth login for the confirmation page, and test modes `always_succeed`, `always_fail`, `random`, `webhook_error`, `slow`.

Don't write adapters for real Shopee, Tiki or Lazada unless Khang asks. Their APIs need partner registration. A second real adapter, if wanted, is self-hosted WooCommerce.

## 7. Core flow: UC-02 checkout (v1.1)

1. Agent: `create_checkout` with `Idempotency-Key`.
2. Trade calls `createCheckout` on the platform, which returns the final total and hold expiry.
3. Trade stores a proposal snapshot and returns `proposal`, `confirm_url`, `expires_at` (`expires_at` = the earlier of the checkout TTL and the hold expiry).
4. Agent gives `confirm_url` to the user.
5. User opens the page, logs in with the platform account, sees UCP's snapshot, agrees. A confirmation record is saved.
6. Agent: `confirm_order` with `Idempotency-Key`. Without a valid confirmation: `UCP_CONFIRMATION_REQUIRED`.
7. Trade rechecks UserLink and confirmation, calls `placeOrder` with the same key.
8. Response: `{ order_id, status: "pending_payment", payment_url }`. The user pays in the platform app.
9. Platform webhook "paid", then the order becomes `placed` and `OrderPlaced` goes out through the outbox to SSE.

Price went up between proposal and order: `UCP_PRICE_CHANGED`, ask the user again. Price went down: apply the new price.

Cancel (UC-03) uses the same confirmation page. The platform decides. `UCP_CANNOT_CANCEL` if it refuses.

### States

| Object | Flow | Terminal |
|---|---|---|
| Order | `pending_payment → placed → processing → shipped → delivered`; `pre_ordered → processing`; `pending_payment / placed / processing / pre_ordered → cancelled` | `delivered`, `cancelled` |
| Payment | `pending → processing → succeeded / failed`; `failed → processing` (same key, max 3) or `failed_final` | `succeeded`, `failed_final` |
| Refund | `initiated → completed / failed` | `completed`, `failed` |
| Reservation | mirrors the platform hold: `held → converted / released`, or `none` | `converted`, `released`, `none` |

UCP's order status is a cache of the platform's. Implement transitions as an explicit table and reject anything not in it.

## 8. API conventions

- Base URL: one shared gateway, `/v1`. `platform_id` goes with every platform-related call (ADR-009).
- Reads are REST. Workflows are JSON-RPC style on `POST /v1/intent` with `method` and `params` (ADR-004).
- Intents: `create_checkout`, `select_shipping_method`, `apply_promotion`, `cancel_checkout`, `extend_checkout` (once, never past the hold), `confirm_order`, `cancel_order`.
- New v1.1 endpoints: `GET /confirm/{proposal_id}` (fallback confirmation page), `POST /platform-events/{platform}` (webhooks, signature checked), `GET /events?after={seq}` (replay for SSE).
- Scopes: `read_catalog`, `view_order`, `create_order`, `cancel_order`, `manage_identity`. There is no `initiate_payment`.
- Error codes: the 18 in `specs/APISpecs.md` plus the 5 from v1.1, 23 in total, all prefixed `UCP_`. Keep them in one enum and map each to its HTTP status there. Don't invent new codes without adding them to the spec.
- Error body (proposed, not in the spec yet, confirm with Khang before freezing): `{"error": {"code": "UCP_...", "message": "...", "details": {}}}`.
- Write the OpenAPI spec first (`specs/openapi.yaml`) and keep it in sync with the code. CI should fail if they drift.

## 9. Tests that must exist

Every scenario from addendum C10 is an acceptance test against the mock platform:

| Scenario | Expected |
|---|---|
| 2 agents, last unit | Platform picks one, the other gets `UCP_INSUFFICIENT_STOCK` |
| Platform already shipped, agent cancels | `UCP_CANNOT_CANCEL` |
| User slow at payment | Order keeps the hold under the platform's payment deadline |
| Timeout then retry | Same key returns the same order, one charge |
| User unlinks mid-session | Next call is refused |
| Broker down | Expiry still runs, events wait in the outbox |
| Redis down | Reads run with a local limiter, `/intent` writes are refused |
| 2 gateway instances | Events still reach the agent (fanout + replay) |
| Agent tries to confirm by itself | Refused, `UCP_CONFIRMATION_REQUIRED` |
| One platform slow | Other platforms unaffected, search returns `partial: true` |

Plus: an adapter contract test suite that any adapter must pass, idempotency tests (replay, conflict, 24 h window), and state-transition tests for every table in section 7.

## 10. Build order

Each step ends with tests passing and something runnable. Don't start the next step on a red build.

1. Repo skeleton, compose with Postgres and Redis, health endpoint, CI running tests.
2. OpenAPI spec for v1.1 and the error enum.
3. Mock platform app with OAuth login and webhooks.
4. Identity: agent registration, API key, `/auth/token`, UserLink via mock OAuth.
5. Catalog: search and detail through the adapter (UC-01).
6. Trade: `create_checkout`, proposal snapshot, confirmation page, `confirm_order`, webhook, order placed. **First end-to-end slice. Show Khang.**
7. Idempotency, outbox and relay, `ExpiryWorker`, order sync worker.
8. Notification: event log, SSE, `/events?after=`.
9. Cancel (UC-03), remaining intents, BR-26 (open checkouts limit, start at 3 per user per platform across all agents).
10. MCP server, then demo with Claude through a custom connector.
11. Deploy: compose on one VPS, Caddy for HTTPS, GitHub Actions deploy.

## 11. How to work in this repo

- **Commits and PRs carry Khang's name only.** No `Co-Authored-By` lines, no "Generated with ..." lines, no session links, no AI attribution anywhere that goes to GitHub. This overrides any tool default.
- **Keep `PROGRESS.md` current.** It is local only and listed in `.gitignore`, so never commit or push it. After each finished piece of work (a step, a fix, a decision, a blocker), update it: step status, open items, and one dated line in the Log saying what changed and which tests ran. Update it when a step starts too. Never mark a step done on a red build.
- Small commits, one concern each, with a message that says what and why.
- Run the tests before saying something works. Say which tests you ran.
- Don't change an ADR, the API contract or a hard rule (section 3) on your own. Propose it and wait. If a change is agreed, update the ADR or spec and `CHANGELOG.md` in the same commit as the code.
- Don't touch `deliverables/` (the report and the Word and Excel files) unless asked.
- No secrets in the repo. Use `.env` locally and keep `.env.example` current.
- Don't add a dependency without saying why. Prefer the standard library and what's already in use.
- When something in the design doesn't work in code, say so plainly and point to the section. Finding gaps is part of the job.

## 12. Out of scope for now

Returns and complaints, multi-platform carts, real payment providers, real platform adapters, Kubernetes, personal data protection compliance work. Note them if they come up. Don't build them.

---

Sources for section 4: MCP blog post on the 2026-07-28 release; MCP Python SDK "What's new in v2" (py.sdk.modelcontextprotocol.io); FastMCP blog "FastMCP 4 is GA"; Claude docs on custom connectors (support.claude.com, article 11175166).

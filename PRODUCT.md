# Product

Scope: the mock shop UI (`mock_platform/`) only. UCP itself has no UI.

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Two audiences, switched by a toggle (confirmed 2026-11-25):

- **Demo audience:** a lecturer or reviewer watching while Claude buys through UCP.
- **Testing:** Khang, driving and debugging test cases while building.

## Product Purpose

The fake shop behind UCP (`mock_platform/`). It is the source of truth for stock, price, shipping, promotions, orders and payment, and it stands in for a real platform. The UI makes a purchase visible and controllable. No real money, customers or platform is involved.

## Positioning

The shop decides. An AI agent can propose a purchase or a cancel through UCP, but only the signed-in user agrees, and only the shop accepts.

## Operating Context

- Screens confirmed: a demo control panel, the sign-in and payment pages, and a storefront.
- The storefront browses products and shows the signed-in user's orders. Buying goes through the agent, not the storefront.
- The control panel watches orders, stock and webhook events, moves orders between statuses, and switches test modes (`always_succeed`, `always_fail`, `random`, `webhook_error`, `slow`).

## Capabilities and Constraints

- FastAPI app with state in memory. A restart or reset clears it. Pages are served by this app. No build step.
- Money is integer VND.
- `/_admin/*` has no authentication. Local use only until that changes.

## Brand Commitments

- English UI text.
- Khang's standing preference for pages and designs: a light cream palette with purple, blue, orange and gold accents (`#F2E8D0` ground, `#FAF4E4` panel, `#3A3024` text, `#705F4A` muted, `#D9C9A2` and `#BFA978` borders, `#E8D9B0` hover, `#6F4F93` purple with `#E6DBEF`, `#36678A` blue with `#D8E6EE`, `#A86A3C` orange with `#F6E6D3`, `#9A7A2E` gold with `#F3E7BF`). Khang likes purple.

## Evidence on Hand

Seeded demo data in `mock_platform/seed.py`: six products with variants and stock, two shop users with addresses. It is synthetic. There are no product photos.

## Product Principles

- Show the shop's real state. Nothing on screen is invented.
- Make it obvious who is acting: agent, user or shop.
- A test control must never look like a real shop action.

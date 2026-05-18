# ADR-004: UCP Protocol Format

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 04/2026 |
| **Author** | Phạm Phú Khang (23520706) |

## Context

UCP cần định nghĩa format giao tiếp giữa AI Agent và Gateway. Câu hỏi: dùng REST thuần, JSON-RPC, GraphQL, hay protocol riêng?

Yêu cầu: (1) Developer dễ hiểu. (2) Hỗ trợ intent-based interaction. (3) Tương thích MCP ecosystem. (4) Có thể generate SDK từ spec.

## Options Considered

### Option A: REST thuần (resource-based)

CRUD truyền thống: GET /products, POST /orders, DELETE /orders/{id}.

**Pros:** Phổ biến nhất. OpenAPI spec dễ. HTTP caching.
**Cons:** Không phù hợp intent-based. Checkout flow cần nhiều request. Không tương thích MCP.

### Option B: JSON-RPC 2.0 thuần

1 endpoint (POST /rpc). Agent gửi { method: "search_product", params: {...} }.

**Pros:** Intent-based tự nhiên. 1 endpoint. Tương thích MCP. Batch request.
**Cons:** Không tận dụng HTTP semantics. Không cacheable. Developer chưa quen.

### Option C: Hybrid REST + JSON-RPC

CRUD đơn giản dùng REST. Intent phức tạp dùng JSON-RPC qua POST /intent.

**Pros:** Query đơn giản dùng REST — cacheable. Workflow phức tạp dùng JSON-RPC — intent-based. Tương thích MCP. Developer chọn style phù hợp.
**Cons:** 2 style trong 1 API. OpenAPI spec phức tạp hơn.

### Option D: GraphQL

1 endpoint, client query chính xác data cần.

**Pros:** Agent lấy đúng data cần. Strong typing.
**Cons:** Phức tạp cho server. Không phù hợp mutation workflow. Không tương thích MCP. Over-engineering.

## Decision

**Chọn Option C: Hybrid REST + JSON-RPC.**

- REST cho read: GET /products?q=..., GET /products/{id}, GET /orders/{id}, GET /payments/methods.
- JSON-RPC cho workflow: POST /intent với method: create_checkout, confirm_order, cancel_order, initiate_payment, link_identity.
- Intent resolution: Gateway nhận method name, map đến service + handler qua config.
- OpenAPI spec mô tả cả REST endpoints và /intent endpoint.

## Consequences

- **Tích cực:** Developer chọn style phù hợp. REST cho đơn giản, RPC cho workflow. Tương thích MCP.
- **Tiêu cực:** 2 style cần document rõ. SDK phải wrap cả hai.
- **Intent resolution:** Gateway maintain routing table: method name → service URL + handler. Thêm method mới = thêm 1 dòng config.
- **BR liên quan:** BR-16 (human-in-the-loop — confirm_order yêu cầu user đã approve proposal trước).

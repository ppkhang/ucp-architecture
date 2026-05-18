# ADR-003: Data Isolation Strategy

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 04/2026 |
| **Author** | Phạm Phú Khang (23520706) |

## Context

UCP có 6 Bounded Contexts, mỗi context quản lý data khác nhau. Câu hỏi: các service chia sẻ 1 database chung, hay mỗi service có DB riêng?

Liên quan: việc tách Catalog và Trade thành 2 service riêng, và giữ Trade là monolith (Checkout + Order + Inventory gộp chung), cũng phụ thuộc vào quyết định này.

## Options Considered

### Option A: Shared Database

Tất cả service dùng chung 1 PostgreSQL, chia schema.

**Pros:** Đơn giản. JOIN cross-service được. Transaction ACID.
**Cons:** Coupling cao. Không scale độc lập. Vi phạm Bounded Context. 1 service làm DB chậm = tất cả chậm.

### Option B: Database per Service

Mỗi service có PostgreSQL riêng. Không FK chéo. Tham chiếu bằng ID (soft reference).

**Pros:** Loose coupling. Scale độc lập. Đúng DDD. Failure isolation.
**Cons:** Không JOIN cross-service. Eventual consistency. Nhiều DB để maintain.

### Option C: Hybrid — Group related services

Gộp Catalog + Trade dùng chung 1 DB.

**Pros:** Trade query Product nhanh (JOIN). Ít DB hơn.
**Cons:** Catalog và Trade vẫn coupled qua data. Không scale riêng.

## Decision

**Chọn Option B: Database per Service.**

- 5 PostgreSQL databases: Identity DB, Catalog DB, Trade DB, Payment DB, Notification DB.
- Không FK chéo. Tham chiếu bằng ID (VD: Order lưu product_id nhưng không FK đến Catalog DB).
- Trade query Catalog qua HTTP API (Conformist), không qua SQL JOIN.
- Trade giữ là monolith (Checkout + Order + Inventory trong 1 service, 1 DB) vì transactional dependency mạnh.
- Catalog tách riêng Trade vì read/write ratio khác nhau.

## Consequences

- **Tích cực:** Service độc lập về data. Scale riêng. Failure isolation. Đúng DDD.
- **Tiêu cực:** 5 PostgreSQL instances. Không JOIN cross-service.
- **Mitigation:** Docker Compose chạy 5 DB trên cùng máy (dev). Production dùng managed DB.
- **BR liên quan:** BR-10 (tồn kho real-time — Trade query Catalog API), BR-15 (chống oversell — atomic trong Trade DB).

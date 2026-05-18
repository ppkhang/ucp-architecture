# ADR-001: Sync vs Async Communication

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 04/2026 |
| **Author** | Phạm Phú Khang (23520706) |

## Context

UCP gồm 6 services giao tiếp với nhau. Câu hỏi: Gateway gọi các service bằng synchronous (HTTP request-response, chờ kết quả) hay asynchronous (gửi message qua broker, không chờ)?

Agent gửi intent và cần kết quả ngay ("tìm SP" → cần danh sách SP). Nhưng một số thao tác không cần response ngay (gửi notification, log event).

Quyết định này cũng cover việc UCP dùng Single Gateway pattern: mọi request đi qua 1 điểm vào duy nhất.

## Options Considered

### Option A: Toàn bộ Sync (HTTP/REST)

Mọi giao tiếp giữa Gateway và service đều là HTTP request-response.

**Pros:**
- Đơn giản. Dễ debug và trace.
- Agent luôn nhận kết quả ngay.
- Không cần message broker.

**Cons:**
- Coupling cao: Gateway bị chặn nếu service chậm.
- Notification phải đợi trong request cycle — tăng latency.
- Không có cơ chế retry/dead-letter cho failed events.

### Option B: Toàn bộ Async (Message Queue)

Mọi giao tiếp qua message broker (RabbitMQ).

**Pros:**
- Loose coupling tối đa.
- Retry và dead-letter tự động.

**Cons:**
- Phức tạp: Agent chờ reply queue, timeout handling.
- Latency cao hơn.
- Overkill cho query đơn giản như search product.

### Option C: Hybrid — Sync cho query, Async cho event

Gateway gọi service bằng HTTP sync cho các thao tác cần response ngay. Service phát event lên message broker cho các thao tác không cần response.

**Pros:**
- Agent nhận kết quả nhanh cho query/command.
- Notification được decouple khỏi request cycle.
- Retry và dead-letter cho event.
- Cân bằng giữa đơn giản và resilient.

**Cons:**
- Phải maintain 2 cơ chế (HTTP + broker).
- Cần quy ước rõ: cái nào sync, cái nào async.

## Decision

**Chọn Option C: Hybrid — Sync cho query/command, Async cho event.**

- Gateway → Service là HTTP/REST (sync).
- Service → Notification là RabbitMQ (async, pub/sub).
- Quy ước: nếu Agent cần response → sync. Nếu chỉ cần thông báo hệ thống đã xảy ra gì → async event.
- Gateway là Single Entry Point: Agent chỉ biết 1 URL.

## Consequences

- **Tích cực:** Agent trải nghiệm đơn giản. Notification không làm chậm request. Event có retry.
- **Tiêu cực:** Phải maintain cả HTTP client và RabbitMQ. Gateway là SPOF — cần deploy nhiều instance.
- **Liên quan:** C4 Container Diagram thể hiện rõ 2 kiểu giao tiếp: mũi tên liền (sync) và mũi tên đứt (async qua broker).

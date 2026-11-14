# ADR-009: Một gateway đa sàn dùng chung

| Field | Value |
|-------|-------|
| **Status** | Proposed (v1.1) |
| **Date** | 14/11/2026 |
| **Author** | Phạm Phú Khang (23520706) |

## Context

ADR-001 nói Agent chỉ biết 1 URL. Nhưng base URL ở 4.9.1 là `https://{platform}.ucp.example.com/v1`, tức mỗi sàn một gateway. Khi đó một Agent không thể so giá giữa các sàn hay dùng một kết nối cho nhiều sàn, và giá trị N+M yếu đi. Mô hình doanh thu (Hosted Gateway SaaS) cũng giả định một gateway dùng chung.

## Options Considered

### Option A: Mỗi sàn tự host gateway

**Pros:** Sàn kiểm soát hoàn toàn. **Cons:** Agent phải nối từng sàn, quay lại bài toán N×M ở tầng kết nối.

### Option B: Một gateway đa sàn dùng chung, spec mở để tự host

**Pros:** Một URL, một kết nối, tìm kiếm nhiều sàn. Khớp mô hình Hosted Gateway. Sàn lớn vẫn tự host được theo spec. **Cons:** Gateway dùng chung thành mục tiêu tấn công và SPOF lớn hơn (R-01).

## Decision

**Chọn Option B.**

- Base URL dùng chung, `platform_id` đi theo từng lời gọi.
- Tìm kiếm nhiều sàn trả `partial: true` khi một sàn không trả lời.
- Mỗi adapter có timeout, connection pool và circuit breaker riêng để một sàn chậm không kéo cả hệ thống.

## Consequences

- **Tích cực:** Agent dùng một kết nối cho mọi sàn. So giá giữa sàn khả thi.
- **Tiêu cực:** Gateway cần cô lập theo sàn (bulkhead). R-01 quan trọng hơn.
- **Liên quan:** ADR-001, 4.9.1 base URL, R-01, R-03. Sửa B14, B18.

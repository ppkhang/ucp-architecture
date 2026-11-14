# ADR-010: Outbox, idempotency và hết hạn do worker điều khiển

| Field | Value |
|-------|-------|
| **Status** | Proposed (v1.1) |
| **Date** | 14/11/2026 |
| **Author** | Phạm Phú Khang (23520706) |

## Context

- R-04 nhắc "idempotency keys cho retry" nhưng API không có khóa nào. `confirm_order` trả về đồng bộ cả `payment_status` trong khi user xác thực OTP có thể mất vài phút. Timeout rồi gửi lại có thể tạo 2 đơn.
- ADR-001 nói broker chết chỉ ảnh hưởng notification, nhưng bù trừ của Trade (release khi PaymentFailed, CheckoutExpired) phản ứng theo event, và không thành phần nào được nêu là bên chạy hẹn giờ hết hạn.
- Event được publish thẳng lên RabbitMQ sau khi ghi DB: ghi DB thành công mà publish lỗi thì mất event.

## Options Considered

### Option A: Retry ở client, publish trực tiếp

**Cons:** Không chống trùng. Mất event khi broker lỗi giữa chừng.

### Option B: Idempotency key, transactional outbox, ExpiryWorker

**Pros:** Gửi lại an toàn. Event không mất. Hết hạn không phụ thuộc broker. **Cons:** Thêm outbox relay, 2 worker, bảng IdempotencyKey và Outbox.

## Decision

**Chọn Option B.**

- Header `Idempotency-Key` bắt buộc trên mọi lệnh ghi qua `/intent`. Lưu duy nhất theo Agent, phát lại kết quả cũ trong 24 giờ, chuyển tiếp khóa sang sàn. Cùng khóa khác nội dung: `IDEMPOTENCY_CONFLICT` 409.
- Timeout sau `placeOrder`: đánh dấu đang đối soát, tra bằng `getOrder` theo khóa, không gửi lại mù quáng.
- Transactional outbox trong mỗi database có phát event. Relay đẩy lên RabbitMQ. Consumer chống trùng theo event ID.
- `ExpiryWorker` trong Trade đọc `expires_at` từ Trade DB để hết hạn checkout và nhả hàng (`releaseHold`).
- `OrderSyncWorker` poll các đơn đang hoạt động làm dự phòng cho webhook.

## Consequences

- **Tích cực:** Không trừ tiền 2 lần. RabbitMQ chết thì event chờ trong outbox, hết hạn vẫn chạy.
- **Tiêu cực:** Nhiều thành phần hơn, nặng với nhóm nhỏ.
- **Liên quan:** ADR-001, R-02, R-04. Sửa B5, B6, B11.

# ADR-005: Payment — Mock vs Real Integration

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 04/2026 |
| **Author** | Phạm Phú Khang (23520706) |

## Context

UCP cần xử lý thanh toán cho đơn hàng. Câu hỏi: UCP nên tích hợp thật với cổng thanh toán (VNPay, Stripe, Momo) hay chỉ mock (giả lập)?

UCP là giao thức (protocol), không phải payment processor. Xử lý tiền thật yêu cầu PCI DSS compliance, đăng ký cơ quan quản lý, bảo hiểm, audit — hoàn toàn ngoài phạm vi đồ án sinh viên.

## Options Considered

### Option A: Real integration với VNPay/Stripe

Payment Service gọi API thật. Tiền được chuyển thật.

**Pros:** Chứng minh end-to-end. Đồ án "thật" hơn.
**Cons:** Cần PCI DSS compliance. Cần sandbox account. Rủi ro bảo mật. Phức tạp hóa đồ án — focus sai chỗ.

### Option B: Mock payment (giả lập hoàn toàn)

Payment Service giả lập kết quả: configurable (always succeed, always fail, random 70%). Delay 1-3s giả lập processing. Không gọi API bên ngoài.

**Pros:** An toàn. Test được mọi luồng (success, fail, retry, refund). Không cần account. Focus đúng vào kiến trúc.
**Cons:** Không chứng minh end-to-end thật.

### Option C: Stripe Test Mode

Dùng Stripe test API (không tiền thật nhưng gọi API thật).

**Pros:** Gọi API thật, test flow thật. Miễn phí.
**Cons:** Coupling với Stripe — UCP đáng lẽ protocol-agnostic. Cần Stripe account.

## Decision

**Chọn Option B: Mock payment.**

- MockProvider giả lập hoàn toàn. Config qua biến môi trường:
  - `PAYMENT_MOCK_MODE`: always_succeed | always_fail | random | callback_fail
  - `PAYMENT_MOCK_DELAY_MS`: 1000-3000
- Mode `callback_fail`: giả lập partial failure — payment xử lý thành công nhưng callback về Trade Service thất bại. Cho phép test retry logic và reconciliation flow mà không cần cổng thanh toán thật.
- UCP định nghĩa PaymentIntent pattern (giống Stripe) — không chạm vào thông tin nhạy cảm.
- User xác thực thanh toán trực tiếp trên app của sàn (OTP, FaceID).
- Extensible: MockProvider implement PaymentProviderInterface. Thay bằng StripeAdapter cũng implement interface đó. Strategy pattern.

## Consequences

- **Tích cực:** An toàn, đơn giản, test được mọi luồng kể cả partial failure. Focus đúng vào kiến trúc.
- **Tiêu cực:** Không demo end-to-end thật. Cần giải thích cho thầy tại sao mock là hợp lý.
- **Extensibility:** MockProvider → StripeAdapter → VNPayAdapter. Strategy pattern. PaymentProviderInterface định nghĩa: `createIntent()`, `confirmPayment()`, `refund()`, `getStatus()`. Thay adapter = thay 1 dòng config (DI container inject adapter khác), không sửa Payment Service code. Đây là điểm kiến trúc quan trọng: UCP không coupling với bất kỳ payment provider cụ thể nào.
- **BR liên quan:** BR-19 (UCP không giữ tiền), BR-20 (User xác thực trực tiếp), BR-21 (Payment fail → release), BR-22 (Refund khi cancel).

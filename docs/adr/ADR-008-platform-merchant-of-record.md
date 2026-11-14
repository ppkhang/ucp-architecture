# ADR-008: Sàn là merchant of record

| Field | Value |
|-------|-------|
| **Status** | Proposed (v1.1). Thay hướng mở rộng Strategy pattern của ADR-005 |
| **Date** | 14/11/2026 |
| **Author** | Phạm Phú Khang (23520706) |

## Context

v1.0 có hai mô hình thanh toán mâu thuẫn. BR-19 và BR-20: UCP không giữ tiền, user xác thực trên app của sàn. ADR-005: Payment Service dùng Strategy pattern để thay MockProvider bằng StripeAdapter qua cấu hình. Nếu UCP gọi Stripe trực tiếp thì UCP trở thành bên thu tiền, chịu PCI DSS, hoàn tiền và khiếu nại, trái BR-19.

## Options Considered

### Option A: UCP là merchant of record (đường Stripe của ADR-005)

**Cons:** Trái BR-19. Gánh compliance và tranh chấp. Sàn mất quyền kiểm soát thanh toán.

### Option B: Sàn là merchant of record, UCP chỉ theo dõi

**Pros:** Khớp BR-19, BR-20. Không đụng tiền. **Cons:** Mất ví dụ Strategy pattern ở tầng Payment.

## Decision

**Chọn Option B.**

- `placeOrder` của sàn trả link thanh toán. User trả tiền trên app sàn. Sàn báo kết quả qua webhook.
- Payment Service chỉ theo dõi trạng thái thanh toán và hoàn tiền (`getRefund`). Refund là đối tượng riêng: `initiated → completed / failed`.
- Bỏ `PaymentProviderInterface`, `StripeAdapter`, `MockProvider`. Kiểm thử bằng `MockPlatformAdapter`.
- C4 Level 1: payment provider nằm sau sàn, không còn là hệ thống bên ngoài của UCP.

## Consequences

- **Tích cực:** Một mô hình thanh toán duy nhất. Không PCI DSS. Hoàn tiền do sàn chịu trách nhiệm.
- **Tiêu cực:** Ví dụ Strategy pattern mất đi, câu chuyện mở rộng chính chuyển sang Adapter pattern. R-13 như hiện viết không còn áp dụng.
- **Liên quan:** ADR-005, BR-19, BR-20, BR-22, R-13. Sửa B7.

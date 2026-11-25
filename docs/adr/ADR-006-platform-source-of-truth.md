# ADR-006: Sàn là nguồn sự thật (Platform as Source of Truth)

| Field | Value |
|-------|-------|
| **Status** | Proposed (v1.1) |
| **Date** | 14/11/2026 |
| **Author** | Phạm Phú Khang (23520706) |

## Context

Ở v1.0, Trade Service giữ tồn kho (InventoryReservation), giá (PricingEngine, PromotionEngine) và trạng thái đơn (Order) như dữ liệu của chính UCP. Thực tế sàn mới là bên nắm các dữ liệu này, UCP chỉ có bản sao qua DataSyncWorker (5 phút) và Redis cache.

Hệ quả khi chạy thử kịch bản lỗi:

- `available_qty` nằm ở Catalog DB, reservation nằm ở Trade DB. `SELECT FOR UPDATE` (R-05) không khóa được xuyên 2 database, nên 2 Agent có thể giữ cùng một món hàng cuối. Sàn vẫn bán món đó qua website của sàn.
- BR-10 ("tồn kho real-time, không cache") không đúng với dữ liệu đồng bộ 5 phút.
- Không có kênh đưa trạng thái `shipped` từ sàn về UCP, nên `cancel_order` có thể được chấp nhận trên trạng thái cũ (BR-17).

## Options Considered

### Option A: Giữ v1.0, tăng tần suất đồng bộ

**Pros:** Không đổi kiến trúc. **Cons:** Vẫn là bản sao, chỉ thu hẹp khoảng trễ. Không giải quyết oversell với kênh bán riêng của sàn.

### Option B: Sàn là nguồn sự thật, UCP điều phối

UCP gọi sàn trực tiếp để báo giá, giữ hàng, đặt đơn, hủy đơn. Trạng thái đơn trong UCP là cache cập nhật qua webhook và đối soát.

**Pros:** Đúng thực tế sở hữu dữ liệu. Oversell do sàn xử lý. Hủy đơn do sàn quyết định. **Cons:** `PlatformAdapter` tăng từ 4 lên 13 method. Phụ thuộc mức hợp tác của sàn (webhook, giữ hàng).

## Decision

**Chọn Option B.**

- `PlatformAdapter` mới: `capabilities`, `searchProducts`, `getProduct`, `getQuote`, `shippingOptions`, `createCheckout`, `renewHold`, `releaseHold`, `placeOrder`, `getOrder`, `cancelOrder`, `getRefund`, `parseEvent`.
- Sàn không hỗ trợ giữ hàng: proposal ghi "tồn kho không được đảm bảo", hết hàng lúc `placeOrder` trả `INSUFFICIENT_STOCK` trước khi trừ tiền.
- `inventory_status` chỉ còn là cache hiển thị. Bỏ `PricingEngine`, `PromotionEngine`. `InventoryChecker` thành client của `getQuote`.
- Order thêm trạng thái `pending_payment`. Khi hủy, câu trả lời của sàn quyết định.

## Consequences

- **Tích cực:** Hết oversell giữa các Agent. Giá và tồn kho đúng tại thời điểm checkout. Hủy đơn không dựa trên trạng thái cũ.
- **Tiêu cực:** Công sức tích hợp phía sàn tăng. Câu "thêm sàn mới chỉ bằng cấu hình" không còn đúng. Sàn không có webhook thì phải poll.
- **Liên quan:** thay R-05 (atomic reservation trong Trade DB), sửa B2, B3, B4 trong `docs/UCP_v1.1_Addendum.md`.

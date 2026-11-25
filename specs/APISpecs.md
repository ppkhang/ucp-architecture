# UCP API Specification

Base URL: `https://{platform}.ucp.example.com/v1`

Auth: `Authorization: Bearer {jwt}` hoặc `X-API-Key: {key}`

Format: Hybrid REST (read) + JSON-RPC (workflow)

18 endpoints: 1 auth, 4 identity, 4 catalog, 6 trade, 2 payment, 1 events. 16 đường dẫn HTTP vì `create_checkout`, `confirm_order`, `cancel_order` dùng chung `POST /intent`. Thay đổi đề xuất cho v1.1 ở cuối file.

---

## Authentication

### POST /auth/token
Lấy JWT session token.

**Request:**
```json
{ "grant_type": "api_key" }
```

**Response:**
```json
{
  "token": "eyJ...",
  "expires_in": 3600,
  "permissions": ["read_catalog", "view_order"]
}
```

---

## Identity

### POST /agents
Đăng ký Agent mới. Public endpoint.

**Request:**
```json
{ "name": "Shopping Bot v1", "description": "AI shopping assistant" }
```

**Response:**
```json
{ "agent_id": "uuid", "api_key": "ucp_key_xxx", "tier": "free" }
```

### POST /agents/{id}/links
Liên kết Agent với user trên sàn.

**Request:**
```json
{ "platform_id": "tiki" }
```

**Response:**
```json
{ "link_id": "uuid", "auth_url": "https://tiki.vn/oauth/authorize?...", "expires_in": 600 }
```

### DELETE /agents/{id}/links/{link_id}
Hủy liên kết. Hiệu lực ngay lập tức.

### GET /agents/{id}/permissions
Xem permissions hiện tại.

---

## Catalog

### GET /products?q={keyword}&category={cat}&max_price={n}&page={n}&limit={n}
Tìm kiếm sản phẩm.

**Response:**
```json
{
  "products": [
    { "product_id": "uuid", "name": "Tai nghe Bluetooth", "selling_price": 399000, "currency": "VND", "rating": 4.5, "availability_type": "in_stock" }
  ],
  "total_count": 142,
  "page": 1
}
```

### GET /products/{id}
Chi tiết sản phẩm.

### GET /products/{id}/price?region={r}&member_tier={t}
Giá chính xác tại thời điểm query.

**Response:**
```json
{ "list_price": 500000, "selling_price": 399000, "currency": "VND", "discount": { "type": "promotion", "value": 101000 } }
```

### GET /products/{id}/stock
Tồn kho real-time.

**Response:**
```json
{ "total_stock": 50, "reserved_qty": 3, "available_qty": 47, "availability_type": "in_stock", "max_per_user": null }
```

---

## Trade (JSON-RPC)

Tất cả workflow operations gửi qua `POST /v1/intent`.

### create_checkout
```json
{
  "method": "create_checkout",
  "params": {
    "items": [{ "product_id": "uuid", "variant_id": "uuid", "qty": 2 }],
    "shipping_address": { "street": "123 Nguyen Van Cu", "city": "HCMC", "district": "Q5" }
  }
}
```

**Response:**
```json
{
  "checkout_id": "uuid",
  "proposal": {
    "items": [...],
    "subtotal": 798000,
    "shipping_fee": 30000,
    "discount": 50000,
    "total": 778000
  },
  "reservation_ttl": 900
}
```

### confirm_order
```json
{ "method": "confirm_order", "params": { "checkout_id": "uuid", "payment_method": "ewallet" } }
```

### cancel_order
```json
{ "method": "cancel_order", "params": { "order_id": "uuid", "reason": "User changed mind" } }
```
Trade gọi `PlatformAdapter.cancelOrder` để sàn hoàn hàng về kho, khởi tạo refund nếu đã thanh toán.

### GET /orders/{id}
Xem trạng thái đơn hàng.

### GET /orders
Danh sách đơn của user.

### GET /shipping/options?city={c}&district={d}
Hình thức giao hàng khả dụng.

---

## Payment

### GET /payments/methods
Danh sách hình thức thanh toán.

### GET /payments/{id}
Trạng thái thanh toán.

---

## Events

### GET /events/stream
SSE stream. Agent giữ kết nối mở để nhận push events.

```
data: { "event_type": "OrderStatusChanged", "payload": { "order_id": "uuid", "old_status": "placed", "new_status": "processing" } }
```

---

## Error Codes

| HTTP | Code | Mô tả |
|------|------|-------|
| 401 | UCP_UNAUTHORIZED | Sai credentials |
| 401 | UCP_SESSION_EXPIRED | JWT hết hạn |
| 403 | UCP_FORBIDDEN | Không có permission |
| 403 | UCP_NO_USER_LINK | Chưa liên kết user |
| 403 | UCP_NO_CONSENT | User chưa consent |
| 403 | UCP_AGENT_SUSPENDED | Agent bị suspend |
| 429 | UCP_QUOTA_EXCEEDED | Vượt quota |
| 429 | UCP_RATE_LIMITED | Vượt 60 req/min |
| 409 | UCP_INSUFFICIENT_STOCK | Hết hàng |
| 409 | UCP_PARTIAL_STOCK | Còn ít hơn requested |
| 409 | UCP_MAX_PER_USER | Vượt giới hạn mua |
| 409 | UCP_CANNOT_CANCEL | Đã shipped |
| 410 | UCP_CHECKOUT_EXPIRED | Checkout hết hạn |
| 410 | UCP_PRODUCT_UNAVAILABLE | SP bị gỡ |
| 402 | UCP_PAYMENT_FAILED | Thanh toán thất bại |
| 400 | UCP_BAD_REQUEST | Request không hợp lệ |
| 404 | UCP_NOT_FOUND | Resource không tồn tại |
| 500 | UCP_INTERNAL_ERROR | Lỗi server |

Tổng: 18 error codes.

---

## v1.1 (đề xuất, chưa hiện thực)

Theo [`docs/UCP_v1.1_Addendum.md`](../docs/UCP_v1.1_Addendum.md) và ADR-006 đến ADR-010.

### Thay đổi chung
- **Base URL:** một gateway dùng chung `https://api.ucp.example.com/v1`, `platform_id` đi theo mọi lời gọi liên quan đến sàn (ADR-009).
- **`Idempotency-Key`:** header bắt buộc với mọi `POST /intent`. Cùng khóa trả lại kết quả cũ trong 24 giờ (ADR-010).
- **Phiên làm việc:** JWT sống 5 phút, kiểm tra session mỗi lần gọi. API Key chỉ còn `read_catalog`.
- **Scope:** thêm `cancel_order`, bỏ `initiate_payment`.

### Intent và endpoint mới

| ID | Method | Path | Mô tả | Auth |
|----|--------|------|-------|------|
| T7 | POST | `/intent {method: select_shipping_method}` | Chọn hình thức giao, proposal tính lại (UC-02 ALT-2) | JWT + `create_order` + UserLink |
| T8 | POST | `/intent {method: apply_promotion}` | Áp mã giảm giá (UC-02 ALT-3) | JWT + `create_order` + UserLink |
| T9 | POST | `/intent {method: cancel_checkout}` | Hủy phiên trước khi xác nhận, nhả hàng | JWT + `create_order` |
| T10 | POST | `/intent {method: extend_checkout}` | Gia hạn 1 lần, không vượt hạn giữ hàng (R-12) | JWT + `create_order` |
| X1 | GET | `/confirm/{proposal_id}` | Trang xác nhận dự phòng, user đăng nhập tài khoản sàn (ADR-007) | OAuth của sàn (user) |
| X2 | POST | `/platform-events/{platform}` | Nhận webhook từ sàn (ADR-006) | Chữ ký webhook của sàn |
| E2 | GET | `/events?after={seq}` | Poll dự phòng cho SSE, phát lại theo số thứ tự | JWT |

### Thay đổi endpoint hiện có
- **create_checkout:** response thêm `confirm_url` và `hold_expires_at` (null nếu sàn không giữ hàng). `expires_at` = thời điểm sớm hơn giữa TTL checkout và hạn giữ hàng.
- **confirm_order:** cần bản ghi xác nhận của user, thiếu thì trả `UCP_CONFIRMATION_REQUIRED`. Response: `{ order_id, status: "pending_payment", payment_url }`. `placed` đến sau qua event `OrderPlaced`.
- **cancel_order:** scope `cancel_order`, user xác nhận qua trang xác nhận (trừ đơn chưa thanh toán, xem dưới). Sàn quyết định (`UCP_CANNOT_CANCEL`).
- **GET /orders/{id}:** thêm `platform_order_ids[]` (một checkout có thể tách thành nhiều đơn theo người bán).

### Hủy đơn, thay đổi checkout, BR-26
- Kết quả `cancel_order` có `order_url`: trang đơn hàng của sàn, Agent đưa cho user (null nếu không hỏi được sàn).
- Đơn **chưa thanh toán** (`pending_payment`, thanh toán `pending` hoặc `failed`) được hủy ngay, không cần trang xác nhận; kết quả có thêm `confirmed_by_user: false` và sàn báo cho user. Sàn kiểm tra "chưa thanh toán" và hủy trong cùng một bước (`cancelOrder(..., onlyIfUnpaid)`).
- Đơn đã hoặc đang thanh toán: `cancel_order` khi user chưa đồng ý: 403 `UCP_CONFIRMATION_REQUIRED`, `details` có `confirm_url` và `expires_at`. User mở link, đăng nhập sàn, đồng ý, rồi Agent gọi lại `cancel_order`. Kết quả: `{ order_id, status: "cancelled", refund_ref }`. UCP chỉ lưu `refund_ref`, chưa theo dõi trạng thái hoàn tiền.
- `GET /confirm/{proposal_id}` dùng chung cho checkout và yêu cầu hủy.
- `select_shipping_method`, `apply_promotion`: sàn báo giá lại bằng một checkout mới, xác nhận cũ bị bỏ, hạn checkout không tăng. Proposal có thêm `shipping_method_id`.
- `extend_checkout`: một lần, không vượt hạn giữ hàng.
- BR-26: tối đa 3 checkout đang mở mỗi user mỗi sàn, tính trên mọi Agent. 3 checkout liên tiếp hết hạn thì chờ 10 phút. Cả hai trường hợp trả 429 `UCP_QUOTA_EXCEEDED`.
- `PlatformAdapter`: `createCheckout` và `getQuote` nhận thêm hình thức giao và mã giảm giá; thêm method `shippingOptions(region)` cho `GET /shipping/options` (`city` là region của sàn).

### Event (`/events/stream`, `/events?after={seq}`)
- Cả hai endpoint cần UserLink (403 `UCP_NO_USER_LINK`). Tham số hoặc `Last-Event-ID` sai trả 400 `UCP_BAD_REQUEST`.
- Mỗi event: `{ seq, event_id, event_type, payload, occurred_at }`, `seq` tăng theo từng Agent.
- Payload đổi trạng thái đơn dùng `status` và `previous`, thay cho `new_status` và `old_status` trong ví dụ v1.0.
- Stream gửi `id: <seq>` và `retry:`. Kết nối lại với header `Last-Event-ID` để nhận tiếp. Không có header thì chỉ nhận event mới.
- Server đóng stream sau một thời hạn, và khi token hết hạn, Agent bị suspend hoặc đã hủy liên kết.
- Hiện thực hiện tại: mỗi stream đang mở đọc log của Notification mỗi giây, chưa dùng fanout queue theo từng instance như addendum C5.

### Mã lỗi mới

| HTTP | Code | Mô tả |
|------|------|-------|
| 503 | UCP_PLATFORM_UNAVAILABLE | Sàn không phản hồi hoặc circuit breaker đang mở |
| 409 | UCP_PRICE_CHANGED | Tổng tiền tăng so với proposal, hỏi lại user |
| 403 | UCP_CONFIRMATION_REQUIRED | Chưa có xác nhận hợp lệ của user |
| 409 | UCP_IDEMPOTENCY_CONFLICT | Cùng `Idempotency-Key` nhưng nội dung khác |
| 403 | UCP_LINK_EXPIRED | Token của sàn hết hạn, cần liên kết lại |

Tổng v1.1: 23 error codes.

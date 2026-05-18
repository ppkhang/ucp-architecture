# UCP API Specification

Base URL: `https://{platform}.ucp.example.com/v1`

Auth: `Authorization: Bearer {jwt}` hoặc `X-API-Key: {key}`

Format: Hybrid REST (read) + JSON-RPC (workflow)

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

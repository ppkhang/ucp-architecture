# ADR-002: Authentication Strategy cho AI Agent

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 19/03/2026 |
| **Author** | Phạm Phú Khang (23520706) |

## Context

AI Agent không phải con người — nó là phần mềm tự động gọi API. Cách xác thực Agent khác với xác thực người dùng (không có login form, không có session browser).

UCP cần hỗ trợ nhiều loại Agent: từ hobby developer (1 Agent đơn giản) đến enterprise (hàng trăm Agent, fine-grained permission). Cơ chế auth phải đơn giản cho người mới nhưng đủ mạnh cho enterprise.

## Options Considered

### Option A: Chỉ API Key

Mỗi Agent được cấp 1 API Key khi đăng ký. Gửi key trong header mỗi request.

**Pros:** Đơn giản nhất. Developer hiểu ngay. Không cần OAuth flow.
**Cons:** Không có permission scope. Không có expiry. Không hỗ trợ delegate.

### Option B: Chỉ OAuth 2.1

Agent đăng ký như OAuth client. Client Credentials flow để lấy access token.

**Pros:** Fine-grained permission (scope). Token có expiry. Chuẩn industry.
**Cons:** Phức tạp cho hobby developer. Cần token refresh flow.

### Option C: API Key + OAuth 2.1 (Dual mode)

Hỗ trợ cả hai. API Key cho simple, OAuth 2.1 cho enterprise. JWT làm session token.

**Pros:** Low barrier (API Key 5 phút). Enterprise-ready (OAuth). JWT verify nhanh.
**Cons:** Phải maintain 2 auth flow.

## Decision

**Chọn Option C: Dual mode — API Key + OAuth 2.1, JWT session token.**

- API Key: cấp khi đăng ký. Lưu hash (bcrypt). Permission mặc định: read_catalog + view_order.
- OAuth 2.1 (không phải 2.0): loại bỏ implicit flow và ROPC — hai flow có lỗ hổng bảo mật đã biết. Bắt buộc PKCE. Client Credentials flow cho machine-to-machine.
- OAuth scopes: read_catalog, create_order, initiate_payment, view_order, manage_identity. Token expiry: 1 giờ.
- JWT: Sau auth, Gateway cấp JWT chứa agent_id, permissions[], exp. Các request sau dùng JWT — verify local (không cần gọi Identity Service mỗi request).
- UserLink: Agent liên kết với user trên sàn qua OAuth consent của sàn.

## Consequences

- **Tích cực:** Developer bắt đầu nhanh. Enterprise có OAuth với scope. JWT giảm tải Identity Service.
- **Tiêu cực:** Document phải giải thích rõ 2 mode. Cần migration path Key → OAuth.
- **Key revocation:** API Key bị lộ → developer gọi DELETE /keys/{key_id} hoặc revoke trên dashboard. Identity Service đánh dấu key = revoked trong DB, có hiệu lực ngay (request tiếp theo bị reject). Developer generate key mới. JWT đang active của key cũ vẫn valid đến khi hết exp (tối đa 1 giờ) — trade-off chấp nhận được vì permission scope của API Key mặc định chỉ là read-only.
- **BR liên quan:** BR-01, BR-02, BR-05, BR-06, BR-07.

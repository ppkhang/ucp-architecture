# ADR-002: Authentication Strategy cho AI Agent

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 04/2026 |
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
- OAuth 2.1: Client Credentials flow. Scope: read_catalog, create_order, initiate_payment, view_order, manage_identity. Token expiry: 1 giờ.
- JWT: Sau auth, Gateway cấp JWT chứa agent_id, permissions[], exp. Các request sau dùng JWT — verify local.
- UserLink: Agent liên kết với user trên sàn qua OAuth consent của sàn.

## Consequences

- **Tích cực:** Developer bắt đầu nhanh. Enterprise có OAuth với scope. JWT giảm tải Identity Service.
- **Tiêu cực:** Document phải giải thích rõ 2 mode. Cần migration path Key → OAuth.
- **BR liên quan:** BR-01, BR-02, BR-05, BR-06, BR-07.

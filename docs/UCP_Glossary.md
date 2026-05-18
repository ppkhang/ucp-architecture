# UCP Domain Glossary

## Core Concepts
| Term | Definition |
|------|-----------|
| UCP | Universal Commerce Protocol. Giao thức chuẩn mở cho AI Agent tương tác với e-commerce platform. |
| Protocol | Bộ quy tắc định nghĩa cách hai hệ thống giao tiếp. VD: HTTP, SMTP, MCP. |
| Agentic Commerce | Mô hình thương mại trong đó AI Agent mua sắm thay người dùng. |
| MCP | Model Context Protocol (Anthropic). Giao thức cho AI Agent kết nối tools/data. UCP lấy cảm hứng từ MCP. |

## Actors
| Term | Definition |
|------|-----------|
| AI Agent | Phần mềm AI gửi intent đến UCP để mua hàng thay người dùng. |
| End User | Người dùng cuối sử dụng AI Agent. Không tương tác trực tiếp với UCP. |
| E-commerce Platform | Sàn TMĐT (Shopify, Tiki...) implement UCP Server. |
| Merchant | Người bán hàng trên sàn. Hưởng lợi gián tiếp từ UCP. |

## Architecture
| Term | Definition |
|------|-----------|
| Bounded Context | Ranh giới logic của 1 nhóm nghiệp vụ. UCP có 6 BC. |
| Container | 1 ứng dụng/service deploy độc lập. C4 Level 2. |
| Component | Module/class bên trong container. C4 Level 3. |
| Gateway | UCP Gateway — điểm vào duy nhất. Auth, routing, quota. |
| ADR | Architecture Decision Record. Ghi lại quyết định kiến trúc và lý do. |
| Data Isolation | Mỗi service có DB riêng, không FK chéo. Soft reference bằng ID. |

## Authentication & Identity
| Term | Definition |
|------|-----------|
| API Key | Khóa xác thực đơn giản cho Agent. Lưu hash trong DB. |
| OAuth 2.1 | Chuẩn xác thực cho enterprise Agent. Client Credentials flow. |
| JWT | JSON Web Token. Session token stateless. Chứa agent_id, permissions, exp. |
| UserLink | Liên kết Agent với tài khoản user trên sàn. Quan hệ N:N. |
| Consent | Sự đồng ý của user cho Agent thực hiện action (purchase, view_history). |
| Permission Scope | Quyền cụ thể: read_catalog, create_order, initiate_payment, view_order. |

## Trade & Order
| Term | Definition |
|------|-----------|
| Intent | Yêu cầu từ Agent: search_product, create_checkout, confirm_order... Gửi qua JSON-RPC. |
| CheckoutSession | Phiên mua hàng tạm thời. TTL 30 phút. Chứa items, address. |
| OrderProposal | Đơn hàng nháp: tổng giá, ship, KM. Agent trình bày cho user xác nhận. |
| Order | Đơn hàng chính thức sau khi user confirm và payment success. |
| InventoryReservation | Giữ hàng tạm khi checkout. TTL 15 phút. Chống oversell. |
| Human-in-the-loop | Nguyên tắc: Agent không tự đặt hàng mà không có user xác nhận. |
| Compensating Action | Hành động bù khi thất bại: PaymentFailed → release reservation. |

## Payment
| Term | Definition |
|------|-----------|
| PaymentIntent | Yêu cầu thanh toán (giống Stripe). UCP không giữ tiền, chỉ khởi tạo intent. |
| Mock Payment | Thanh toán giả lập. Config: always_succeed, always_fail, random. |
| Refund | Hoàn tiền khi hủy đơn đã thanh toán. |

## Communication
| Term | Definition |
|------|-----------|
| JSON-RPC 2.0 | Protocol gọi hàm từ xa qua JSON. UCP dùng cho workflow operations. |
| REST | Architectural style dùng HTTP methods. UCP dùng cho read operations. |
| SSE | Server-Sent Events. Push events từ UCP về Agent qua HTTP stream. |
| Pub/Sub | Publish-Subscribe. Trade/Payment phát event, Notification lắng nghe. |
| Domain Event | Sự kiện nghiệp vụ đã xảy ra. VD: OrderPlaced, PaymentSucceeded. 41 events. |

## Infrastructure
| Term | Definition |
|------|-----------|
| PostgreSQL | Database chính. Mỗi service 1 instance (data isolation). |
| Redis | In-memory cache. Session, quota, rate limit cho Gateway. |
| RabbitMQ | Message broker. Pub/sub giữa services. Fanout exchange. |
| Docker Compose | Đóng gói và chạy toàn bộ stack (14 containers) trên local. |

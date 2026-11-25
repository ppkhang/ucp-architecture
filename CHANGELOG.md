# Changelog

## 2026-11-25: Shop giả nhớ đăng nhập

- `mock_platform`: sau khi đăng nhập, trình duyệt được nhớ 1 giờ bằng cookie (HttpOnly, SameSite=Lax). Trang xác nhận hiện "Continue as <user>" thay vì hỏi lại mật khẩu, có nút đổi tài khoản. Nút Continue cần token chống CSRF, trang không cho nhúng khung. "My orders" dùng `GET/POST/DELETE /shop/session` thay cho `POST /shop/my-orders`, không còn giữ mật khẩu trong trang.
- Không đổi UCP hay hợp đồng API. User vẫn phải bấm đồng ý trên trang xác nhận của UCP cho mỗi lần mua và hủy.

## 2026-11-25: Bước 10, MCP server

### Thêm
- `mcp_server/`: một tool cho mỗi intent (7) và 7 tool đọc, gọi UCP API qua HTTP. Dùng MCP Python SDK chính thức 2.x (spec 2026-07-28), phụ thuộc tùy chọn `[mcp]`.
- Chạy stdio (cục bộ) hoặc Streamable HTTP. HTTP luôn yêu cầu OAuth: một mật khẩu chủ, PKCE, đăng ký client động chỉ cho callback của Claude hoặc ứng dụng trên chính máy người dùng.
- Mỗi tool intent nhận `idempotency_key` để gửi lại an toàn (quy tắc cứng 4 và 5). Khóa API và token không xuất hiện trong kết quả tool (quy tắc cứng 13).

Không đổi ADR, hợp đồng API hay quy tắc cứng.

## 2026-11-25: Bước 9, hủy đơn và các intent còn lại

### Đổi hợp đồng (đã được duyệt)
- `cancel_order`: lỗi `UCP_CONFIRMATION_REQUIRED` mang `details.confirm_url`. Trang `/confirm/{id}` dùng chung cho checkout và yêu cầu hủy; token xác nhận mua không dùng để xác nhận hủy được (ADR-007).
- `PlatformAdapter`: `createCheckout` và `getQuote` nhận hình thức giao và mã giảm giá; thêm `shippingOptions(region)` (method thứ 13) cho `GET /shipping/options`. Phí và giảm giá luôn do sàn tính (ADR-006).
- Proposal có thêm `shipping_method_id`.
- ADR-006 và addendum (mục C2 và phần đánh đổi) cập nhật thành 13 method.
- BR-26 trả `UCP_QUOTA_EXCEEDED`: 3 checkout đang mở mỗi user mỗi sàn, chờ 10 phút sau 3 lần hết hạn liên tiếp (cấu hình được).
- Hoàn tiền: chỉ lưu và trả `refund_ref`, chưa theo dõi trạng thái.

### Sửa
- Sàn từ chối một đơn đang đối soát: checkout ghi lại mã lỗi và phát `CheckoutCancelled`, gọi lại `confirm_order` nhận đúng lý do thay vì `UCP_CONFIRMATION_REQUIRED`.

## 2026-11-25: Bước 8, notification (hợp đồng API cho event)

### Đổi hợp đồng API (đã được duyệt)
- `specs/openapi.yaml`, `specs/APISpecs.md`: `GET /events/stream` nhận `Last-Event-ID`, gửi `id:` và `retry:`, và tự đóng khi hết thời hạn hoặc Agent mất quyền. Cả `/events` và `/events/stream` có thêm lỗi 400 và 403 (cần UserLink). Payload đổi trạng thái đơn dùng `status` và `previous`.

### Khác thiết kế
- Thay cho fanout queue theo từng instance (addendum C5, B13), mỗi stream đọc log của Notification mỗi giây. Nhiều instance vẫn nhận đủ event mà chưa cần broker. Đổi sang LISTEN/NOTIFY hoặc fanout qua broker khi việc đọc lặp trở nên tốn kém. Không đổi ADR hay quy tắc cứng.

## 2026-11-25: Bỏ khóa bí mật mặc định công khai (rà soát bảo mật 2026-11-24)

### Sửa
- `ucp.common.config`: `jwt_secret` và `token_key` không còn giá trị mặc định ghi trong repo. Không đặt thì mỗi tiến trình tự sinh khóa ngẫu nhiên (chỉ dùng khi dev). Giá trị rỗng, `change-me` hoặc giá trị mặc định cũ bị từ chối khi khởi động, và lỗi không in lại giá trị (quy tắc cứng 13). Trước đây ai cũng ký được token xác nhận của người dùng (vi phạm ADR-007, quy tắc cứng 2) và giải mã được token sàn đã lưu (quy tắc cứng 12).
- `deploy/docker-compose.yml`: service `api` bắt buộc có `JWT_SECRET` và `TOKEN_KEY` trong `.env`.
- `identity`: token có claim `purpose` (token trang xác nhận) không dùng làm phiên agent được. Token sàn không giải mã được (đổi `TOKEN_KEY`) trả `UCP_LINK_EXPIRED` thay vì lỗi 500, và liên kết không bị đánh dấu hết hạn.
- `trade`: gửi xác nhận không khớp checkout (đề xuất đã đổi) trả `UCP_FORBIDDEN` thay vì báo "Confirmed" mà không ghi gì. Gửi lại cùng một xác nhận vẫn trả 200.

Không đổi ADR, hợp đồng API hay quy tắc cứng.

## 2026-11-22: Sửa lỗi sàn giả và cấu hình compose sau rà soát

### Sửa
- `mock_platform`: gộp các dòng cùng `variant_id` trước khi kiểm tra tồn kho. Trước đây hai dòng cùng biến thể (mỗi dòng 1, tồn kho 1) đều qua kiểm tra nên bán quá số lượng.
- `mock_platform`: gọi lại `placeOrder` với cùng idempotency key giờ kiểm tra chủ đơn, người dùng khác nhận 404.
- `mock_platform`: `/oauth/token` trả 400 khi body JSON sai hoặc không phải object (trước đây 500).
- `mock_platform`: `/_admin/config` không còn trả `webhook_secret` (quy tắc cứng 13).
- `deploy/docker-compose.yml`: Postgres, Redis và sàn giả chỉ bind `127.0.0.1`. `POSTGRES_PASSWORD` và `MOCK_WEBHOOK_SECRET` bắt buộc đặt trong `.env`, không còn giá trị mặc định. Thêm healthcheck cho `api` và `mock-platform`.
- `ucp.common.config.get_settings()` có cache.

Lưu ý: compose đọc `.env` ở thư mục `deploy/`, còn `.env.example` nằm ở thư mục gốc.

## 2026-11-14: Thêm báo cáo đồ án

- `deliverables/DA2_23520706_PhamPhuKhang.pdf`, `.docx`: báo cáo đầy đủ, đã áp dụng đính chính A1 đến A17 và thêm mục 5.6 "Đề xuất thiết kế v1.1 (bổ sung sau bảo vệ)". 9 bảng trong mục 5.6 dùng cùng kiểu bảng với phần còn lại của báo cáo (header nền đen, viền xám, dòng xen kẽ), có chú thích Bảng 5.6.1 đến 5.6.9.

## 2026-11-14: Đính chính và đề xuất v1.1

Thiết kế v1.0 (bản bảo vệ 06/2026) giữ nguyên. Các file Word sửa bằng **Track Changes** (mở trong Word để xem và Accept), các file Excel có **comment** ở ô đã sửa ghi giá trị cũ. Chi tiết lý do ở [`docs/UCP_v1.1_Addendum.md`](docs/UCP_v1.1_Addendum.md).

### Mới
- `docs/UCP_v1.1_Addendum.md`, `deliverables/UCP_v1.1_Addendum.docx`: 19 đính chính, 18 điểm yếu thiết kế, thiết kế v1.1, hướng hiện thực.
- `docs/adr/ADR-006` đến `ADR-010` (Proposed, v1.1).
- `deliverables/ADRs.docx`: bản Word của các ADR (trước đây chỉ có ở máy), thêm ADR-006 đến 010.
- `deliverables/TechStack.xlsx` (trước đây chỉ có ở máy).
- `CHANGELOG.md`.

### Sửa (v1.0, đính chính)

| File | Trước | Sau |
|---|---|---|
| `APISpec.docx` | "20 endpoints (… + 2 internal)" | 18 endpoints, 16 đường dẫn HTTP |
| `APISpec.docx` | T6 `/shipping/options?address=...`, auth `read_catalog` | `?city=...&district=...`, auth JWT (khớp phần chi tiết) |
| `APISpec.docx` | cancel_order: "Release reservation" | Gọi `PlatformAdapter.cancelOrder`, sàn hoàn hàng về kho (reservation đã `converted`) |
| `RiskAnalysis.docx` | "4 CAO, 7 TRUNG BÌNH, 2 THẤP" | 4 CAO, 8 TRUNG BÌNH, 1 THẤP |
| `RiskAnalysis.docx` | Risk Matrix (bảng và hình): R-10, R-11 ở Tác động TB; R-13 ở XS Trung bình; R-12 ở XS Thấp | R-10, R-11 ở (XS Trung bình, Tác động Cao); R-13 ở (XS Cao, TB); R-12 ở (XS Cao, Thấp). Hình được vẽ lại (thay ảnh không hiện trong Track Changes) |
| `ADRs.docx`, `ADR-004.md` | Intent: `…, initiate_payment, link_identity` | Intent: `create_checkout, confirm_order, cancel_order`. Liên kết qua REST, thanh toán bên trong `confirm_order` |
| `Domain Events.xlsx` | Tổng kết Trade & Order: 14 | 13 (tổng 41) |
| `TraceabilityMatrix.xlsx` | Cột BR: UC-01, UC-02, UC-03 thiếu 6 BR so với sheet BR Coverage | Thêm BR-02 (UC-01); BR-11, 12, 21, 24, 25 (UC-02); BR-24, 25 (UC-03) |
| `TechStack.xlsx` | Target: Java 21 + Spring Boot | Python 3.12 + FastAPI (khớp container diagram) |
| `TechStack.xlsx` | "14 containers", "Domain Events (40+)" | 13 containers, Domain Events (41) |
| `specs/APISpecs.md` | Bảng mã lỗi thiếu `UCP_AGENT_SUSPENDED` (17 dòng) | 18 dòng |
| `docs/UCP_Glossary.md` | "14 containers"; scope thiếu `manage_identity` | 13 containers; đủ 5 scope |
| `README.md` | "20 API endpoints"; tên file trong bảng Deliverables không tồn tại (`UCP_C4Model.docx`, `UCP_QuickStart.md`...) | 18 endpoints; bảng liệt kê đúng file có trong repo |

### Thêm (v1.1, đề xuất)
- `APISpec.docx`, `specs/APISpecs.md`: mục v1.1 (intent và endpoint mới, Idempotency-Key, 5 mã lỗi mới).
- `RiskAnalysis.docx`: R-14 (Redis), R-15 (token của sàn), R-16 (phishing trang xác nhận), Risk Matrix v1.1, ghi chú rủi ro v1.0 thay đổi.
- `ADRs.docx`, `ADR-001/002/005.md`: mục "Cập nhật v1.1".
- `Domain Events.xlsx`, `TraceabilityMatrix.xlsx`: sheet "v1.1 (đề xuất)".
- `docs/UCP_Glossary.md`: thuật ngữ v1.1.

### Không sửa
- Ngày các ADR: báo cáo và ADRs.docx lệch nhau, mỗi bên có ngày trước khi dự án bắt đầu (09/03/2026). Không còn bằng chứng về ngày thật, nên chỉ ghi nhận (Addendum A19).
- Báo cáo `DA2_23520706_PhamPhuKhang` (PDF bản bảo vệ): đính chính nằm ở Addendum Phần A.

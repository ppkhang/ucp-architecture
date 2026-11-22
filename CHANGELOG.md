# Changelog

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

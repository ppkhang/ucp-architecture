# ADR-007: Xác nhận của user nằm ngoài Agent

| Field | Value |
|-------|-------|
| **Status** | Proposed (v1.1) |
| **Date** | 14/11/2026 |
| **Author** | Phạm Phú Khang (23520706) |

## Context

BR-16 (human-in-the-loop) là lời hứa cốt lõi của UCP. Ở v1.0, UC-02 bước 6 đến 8: Agent trình bày proposal, user đồng ý với Agent, Agent gửi `confirm_order`. Request `confirm_order` chỉ có `checkout_id` và `payment_method`. `user_confirmation_token` chỉ được nhắc trong R-08, không có trong API hay data model. Vì vậy server không thể phân biệt user đồng ý thật với Agent tự gửi.

OTP thanh toán trên app sàn là cửa thứ hai nhưng không áp dụng cho COD. UC-03 (hủy và hoàn tiền) không có bước xác nhận nào.

## Options Considered

### Option A: Token do UCP cấp cho Agent

**Cons:** Agent cầm token thì Agent tự dùng được. Không giải quyết vấn đề.

### Option B: Trang xác nhận do sàn host

**Pros:** User tin nhất, đúng thương hiệu sàn. **Cons:** Không phải sàn nào cũng làm.

### Option C: Trang do sàn host, UCP host dự phòng

**Pros:** Đáng tin nhất khi sàn hỗ trợ, vẫn chạy khi sàn không hỗ trợ. **Cons:** Trang dự phòng có thể bị giả mạo (R-16).

## Decision

**Chọn Option C.**

- `create_checkout` trả `confirm_url`. Agent chỉ chuyển link cho user.
- User **đăng nhập bằng tài khoản sàn** trên trang xác nhận, tài khoản phải trùng UserLink. Proposal hiển thị lấy từ bản chụp UCP lưu, không phải do Agent gửi.
- Bản ghi xác nhận lưu ở server, gắn với hash proposal, user, Agent, thời hạn, dùng một lần. Agent không cầm thứ gì có thể giả mạo.
- `cancel_order` dùng cùng trang xác nhận, nên BR-16 áp dụng cho hoàn tiền. Thêm scope `cancel_order`.
- Thiếu xác nhận: `CONFIRMATION_REQUIRED` 403.

## Consequences

- **Tích cực:** BR-16 được thực thi thật. COD được bảo vệ như các phương thức khác. Chặn được việc Agent hiển thị một thứ nhưng xác nhận thứ khác.
- **Tiêu cực:** Thêm một bước cho user. Trang dự phòng là mục tiêu phishing, giảm thiểu bằng OAuth của sàn (UCP không thấy mật khẩu) và domain cố định công bố trong spec.
- **Liên quan:** BR-16, R-08, R-16. Sửa B1, B9.

## Sửa đổi 25/11/2026: hủy đơn chưa thanh toán

**Quyết định (Khang):** Agent được hủy một đơn **chưa thanh toán** mà không qua trang xác nhận, và user phải được báo. Mua hàng, và hủy đơn đã hoặc đang thanh toán, vẫn bắt buộc xác nhận như trên.

- "Chưa thanh toán": trạng thái `pending_payment` và thanh toán `pending` hoặc `failed`, đọc trực tiếp từ sàn, không dùng cache.
- Sàn phải kiểm tra và hủy trong cùng một bước (`cancelOrder(..., onlyIfUnpaid)`). Nếu user vừa trả tiền thì sàn từ chối và UCP đòi xác nhận. Sàn không làm được việc này nguyên tử thì phải từ chối, nghĩa là vẫn cần xác nhận.
- Sàn tự báo cho user, không dựa vào lời của Agent. Kết quả trả cho Agent có `confirmed_by_user: false`.
- BR-16 và G4 vẫn giữ cho mọi giao dịch có chuyển tiền. Hủy đơn chưa trả tiền không tạo hoàn tiền.
- **Đánh đổi:** Agent hiểu sai, hoặc bị chèn lệnh, có thể hủy một đơn chưa trả tiền mà user muốn giữ (user phải đặt lại; hàng có thể đã hết hoặc đổi giá).

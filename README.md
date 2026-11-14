# UCP — Universal Commerce Protocol

> Xây dựng kiến trúc hệ thống cho Universal Commerce Protocol (UCP) theo hướng mở và khả mở

**Đồ Án 2** — HK2 2025-2026 — Khoa Công nghệ Phần mềm, UIT - VNU

- **Sinh viên:** Phạm Phú Khang — 23520706
- **GVHD:** ThS. Nguyễn Công Hoan

---

## UCP là gì?

UCP là giao thức chuẩn mở cho phép AI Agent tương tác với các sàn thương mại điện tử (Shopify, Tiki, Shopee...) để thực hiện nghiệp vụ mua sắm thay người dùng. UCP giải quyết vấn đề: mỗi sàn có API riêng, AI Agent phải tích hợp riêng từng sàn — tốn thời gian và không scale.

**Core principles:**
- Protocol, không phải app — UCP là specification + reference implementation
- Human-in-the-loop — Agent không tự đặt hàng, User phải xác nhận
- Mở và khả mở — thêm sàn/service mới mà không sửa code cũ

## Kiến trúc

```
AI Agent → UCP Gateway → Identity Service
                       → Catalog Service
                       → Trade Service → Payment Service (Mock)
                       → Notification Service
```

- **6 services**: Gateway, Identity, Catalog, Trade, Payment, Notification
- **36 components** across all services
- **18 API endpoints** (16 đường dẫn HTTP): Hybrid REST + JSON-RPC, 18 error codes
- **5 databases**: DB per service (data isolation), 17 tables
- **13 containers** khi chạy Docker Compose: 6 service, 5 PostgreSQL, Redis, RabbitMQ
- **41 domain events**, **25 business rules**, **13 rủi ro**, **9 use cases**

## v1.1 (đề xuất, 11/2026)

Sau khi bảo vệ, thiết kế v1.0 được chạy thử với các kịch bản lỗi (2 Agent mua món hàng cuối, timeout sau khi trả tiền, Agent tự xác nhận đơn, RabbitMQ hoặc Redis ngừng hoạt động...). Kết quả và thiết kế sửa ở [`docs/UCP_v1.1_Addendum.md`](docs/UCP_v1.1_Addendum.md):

- **Phần A:** 19 điểm đính chính (số liệu và tên gọi lệch nhau giữa các phần).
- **Phần B:** 18 điểm yếu thiết kế. Nguyên nhân gốc: Trade coi tồn kho, giá và đơn hàng là dữ liệu của UCP, trong khi sàn mới là bên nắm chúng.
- **Phần C:** thiết kế v1.1, sàn là nguồn sự thật, kèm [ADR-006 đến ADR-010](docs/adr/).
- **Phần D:** thứ tự hiện thực nếu xây UCP thành phần mềm chạy được.

v1.1 là thiết kế trên giấy, chưa hiện thực. Thay đổi từng file ở [`CHANGELOG.md`](CHANGELOG.md).

## Deliverables

### Architecture
| File | Mô tả |
|------|-------|
| `docs/adr/` | 10 ADR dạng Markdown (001 đến 005 của v1.0, 006 đến 010 đề xuất v1.1) |
| `deliverables/ADRs.docx` | Bản Word của các ADR |
| `deliverables/C4Model.docx` | C4 Level 1 đến 3, Level 4 cho extension interfaces |
| `deliverables/Bounded Context Map.docx` | Context map giữa 6 Bounded Context |
| `deliverables/APISpec.docx`, `specs/APISpecs.md` | 18 endpoints, 18 error codes, thay đổi v1.1 |
| `deliverables/RiskAnalysis.docx` | 13 rủi ro, Risk Matrix, rủi ro mới v1.1 |
| `deliverables/ProtocolComparison.docx` | So sánh MCP, OpenAPI, GraphQL |

### Business Analysis
| File | Mô tả |
|------|-------|
| `deliverables/CungCoNenTang.docx` | Nghiên cứu nền tảng: thị trường, khách hàng, BMC |
| `deliverables/Vision + Stake Analysis + Cap Map.docx` | Vision, stakeholder, Capability Map |
| `deliverables/BizArch.docx` | Value Stream, quy trình, Business Objects, 25 Business Rules |
| `deliverables/ObjChain_KPI.docx` | Objective Chain và 16 KPI |
| `deliverables/PeopleSystemModel.docx` | 9 Use Cases, Roles/Permissions, State Tables, Decision Tables |
| `deliverables/ProjectCharter.docx` | Project Charter |

### Data
| File | Mô tả |
|------|-------|
| `deliverables/DataModels.docx` | ERD, Data Dictionary (17 tables), Relationships |
| `deliverables/Domain Events.xlsx` | 41 domain events (sheet v1.1: 6 event đề xuất) |
| `deliverables/TraceabilityMatrix.xlsx` | UC → Service → Component → API → BR (sheet v1.1: ADR-006 đến 010) |
| `deliverables/ReportTable.docx` | 20 report |
| `deliverables/TechStack.xlsx` | Stack, design patterns, phương pháp |

### Reference
| File | Mô tả |
|------|-------|
| `deliverables/DA2_23520706_PhamPhuKhang.pdf`, `.docx` | Báo cáo đồ án đầy đủ, đã sửa theo đính chính v1.0 và có mục 5.6 đề xuất v1.1 |
| `docs/UCP_Glossary.md` | Thuật ngữ |
| `docs/UCP_v1.1_Addendum.md`, `deliverables/UCP_v1.1_Addendum.docx` | Đính chính và đề xuất v1.1 |
| `diagrams/` | Toàn bộ diagram (BizArch, C4, Data, People & Systems) |

## Tech Stack
- **API:** Python / FastAPI
- **Database:** PostgreSQL 16 (5 instances)
- **Cache:** Redis 7
- **Message Broker:** RabbitMQ 3.13
- **Deployment:** Docker Compose (dev) / Kubernetes (production)

## ADR Summary
| ADR | Decision |
|-----|----------|
| ADR-001 | Hybrid Sync + Async (HTTP + RabbitMQ) |
| ADR-002 | Dual Auth (API Key + OAuth 2.1 + JWT) |
| ADR-003 | DB per Service |
| ADR-004 | Hybrid REST + JSON-RPC |
| ADR-005 | Payment Mock (Strategy Pattern) |
| ADR-006 | v1.1: Sàn là nguồn sự thật |
| ADR-007 | v1.1: Xác nhận của user nằm ngoài Agent |
| ADR-008 | v1.1: Sàn là merchant of record |
| ADR-009 | v1.1: Một gateway đa sàn dùng chung |
| ADR-010 | v1.1: Outbox, idempotency, ExpiryWorker |

---

*Đồ Án 2 — UIT — 2026*

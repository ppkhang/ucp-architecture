# UCP — Universal Commerce Protocol

> Xây dựng kiến trúc hệ thống cho Universal Commerce Protocol (UCP) theo hướng mở và khả mở

**Đồ Án 2** — HK2 2025-2026 — Khoa Công nghệ Phần mềm, UIT HCMC

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

- **6 services** — Gateway, Identity, Catalog, Trade, Payment, Notification
- **36 components** across all services
- **20 API endpoints** — Hybrid REST + JSON-RPC
- **5 databases** — DB per service (data isolation)

## Deliverables

### Architecture
| File | Mô tả |
|------|-------|
| `docs/adr/` | 5 Architecture Decision Records |
| `deliverables/UCP_C4Model.docx` | C4 Level 1 + Level 2 |
| `deliverables/UCP_C4Model_L3.docx` | C4 Level 3 Components |
| `deliverables/UCP_APISpec.docx` | 20 endpoints + Error Codes |
| `deliverables/UCP_RiskAnalysis.docx` | 13 risks + Risk Matrix |

### Business Analysis
| File | Mô tả |
|------|-------|
| `deliverables/UCP_BizArch_1of2.docx` | Vision, Capability Map, Value Stream, Process |
| `deliverables/UCP_BizArch_2of2.docx` | Business Objects, Rules, Mapping |
| `deliverables/UCP_ObjChain_KPI.docx` | Objective Chain + 16 KPIs |
| `deliverables/UCP_PeopleSystemsModels.docx` | 9 Use Cases, Roles/Permissions, State Diagrams, Decision Tables |

### Data
| File | Mô tả |
|------|-------|
| `deliverables/UCP_DataModels.docx` | ERD, Data Dictionary (17 tables), Relationships |
| `deliverables/UCP_DomainEvents.xlsx` | 41 domain events |
| `deliverables/UCP_TraceabilityMatrix.xlsx` | UC → Component → API → BR mapping |

### Reference
| File | Mô tả |
|------|-------|
| `deliverables/UCP_Glossary.docx` | 40 thuật ngữ |
| `deliverables/UCP_ADR.docx` | 5 ADRs (docx version) |
| `docs/UCP_QuickStart.md` | Integration guide |

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

---

*Đồ Án 2 — UIT HCMC — 2026*

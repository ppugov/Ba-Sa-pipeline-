# System Architecture & Data Model: payment_gateway

> **Document Status:** Draft / Verified  
> **Version:** 1.0  
> **Related BRD:** `01_BRD.md`  
> **Related API Contract:** `03_OpenAPI.yaml`

---

## 1. Architectural Overview & Design Principles
### 1.1 Architectural Style & Patterns
Сервис построен на базе микросервисной архитектуры (Event-Driven Microservices / Billing Core), реализующей строгую изоляцию платежной логики от основного монолита (Merchant Backend). 

Основные архитектурные паттерны:
- **Transactional Outbox:** Гарантированная доставка событий (вебхуков) во внешние системы без потери данных при сбоях сети или падении сервиса. Запись бизнес-сущности и события вебхука происходят в рамках одной атомарной транзакции базы данных.
- **Idempotency-Key Handling:** Все изменяющие запросы (POST) требуют заголовок `Idempotency-Key` (UUID). Запросы идемпотентно кэшируются на уровне БД для предотвращения двойных списаний и дублирования операций при нестабильной связи.
- **Finite State Machine (FSM):** Управление жизненным циклом подписок (`SUBSCRIPTION`), инвойсов (`INVOICE`) и платежных попыток (`PAYMENT_ATTEMPT`) через строгие модели состояний, исключающие некорректные переходы.
- **Circuit Breaker & Retry with Exponential Backoff / Dunning:** Управление сетевыми вызовами к эквайреру (PSP) с жестким таймаутом (10 сек) и защитой от «слепых» повторных списаний при тайм-аутах через предварительную проверку статуса транзакции.

### 1.2 System Components & Responsibilities
| Component | Technology / Layer | Core Responsibility |
| :--- | :--- | :--- |
| **API Gateway / BFF** | Go / Fiber or Chi, REST API | Маршрутизация запросов, валидация заголовков идемпотентности, проверка HMAC-подписей, rate limiting. |
| **Subscription & Billing Core** | Go, Domain Service | Управление подписками, расчет дат (`BR-01`), логика Grace Period и Dunning, обработка возвратов (`BR-03`). |
| **Payment Orchestrator / PSP Adapter** | Go, Integration Layer | Интеграция с эквайером (PSP), токенизация (`psp_token`), инициация MIT-платежей, обработка таймаутов (`BR-05`). |
| **Scheduler / Retry Worker** | Go, Cron / Temporal / Redis | Фоновые задачи по проверке наступления сроков списаний (`next_billing_at`), выполнению сетки ретраев (`BR-02`). |
| **Notification / Webhook Dispatcher** | Go, Outbox Worker | Чтение таблицы `WEBHOOK_OUTBOX`, подписание событий `HMAC-SHA256`, доставка в Merchant Backend с повторами. |

---

## 2. Entity-Relationship Diagram (ERD)
> [!IMPORTANT]
> All entity attributes, types, and primary/foreign keys MUST strictly match the schemas in `03_OpenAPI.yaml`.

```mermaid
erDiagram
    CUSTOMER ||--o{ PAYMENT_METHOD : "owns"
    CUSTOMER ||--o{ SUBSCRIPTION : "subscribes"
    PLAN ||--o{ SUBSCRIPTION : "defines"
    SUBSCRIPTION ||--o{ INVOICE : "generates"
    INVOICE ||--o{ PAYMENT_ATTEMPT : "charged_via"
    SUBSCRIPTION ||--o{ WEBHOOK_OUTBOX : "emits"

    CUSTOMER {
        uuid id PK
        string external_customer_id UK
        string email
        string status
        timestamp created_at
    }

    PLAN {
        uuid id PK
        string code UK
        string name
        bigint amount_minor "Amount in minor units (cents/kopecks)"
        string currency "ISO 4217 (3-letter)"
        string billing_interval "MONTHLY | YEARLY | WEEKLY"
        int trial_days
        boolean is_active
    }

    PAYMENT_METHOD {
        uuid id PK
        uuid customer_id FK
        string psp_provider
        string psp_token UK "Tokenized card ref (PCI DSS compliant - NO PAN/CVV)"
        string card_last4
        string card_brand
        int exp_month
        int exp_year
        boolean is_default
        string status "ACTIVE | EXPIRED | REVOKED"
    }

    SUBSCRIPTION {
        uuid id PK
        uuid customer_id FK
        uuid plan_id FK
        uuid payment_method_id FK
        string status "TRIALING | ACTIVE | PAST_DUE | SUSPENDED | CANCELED"
        timestamp current_period_start
        timestamp current_period_end
        timestamp next_billing_at
        timestamp grace_period_ends_at
        boolean cancel_at_period_end
        int version "Optimistic locking"
        timestamp created_at
        timestamp updated_at
    }

    INVOICE {
        uuid id PK
        uuid subscription_id FK
        string idempotency_key UK
        bigint amount_minor
        string currency
        string status "DRAFT | OPEN | PAID | PAST_DUE | VOID | REFUNDED"
        int attempt_count
        timestamp due_at
        timestamp paid_at
        timestamp created_at
    }

    PAYMENT_ATTEMPT {
        uuid id PK
        uuid invoice_id FK
        string idempotency_key UK
        string psp_transaction_id
        bigint amount_minor
        string currency
        string status "PENDING | SUCCEEDED | FAILED_SOFT | FAILED_HARD | REFUNDED"
        string error_code
        string error_message
        timestamp attempted_at
    }

    WEBHOOK_OUTBOX {
        uuid id PK
        string event_type
        string aggregate_id
        jsonb payload
        string delivery_status "PENDING | DELIVERED | DEAD_LETTER"
        int retry_count
        timestamp next_retry_at
        timestamp created_at
    }
```

---

## 3. Subscription Lifecycle State Machine
```mermaid
stateDiagram-v2
    [*] --> TRIALING: Create Subscription (with trial)
    [*] --> ACTIVE: Create Subscription (immediate charge succeeded)
    TRIALING --> ACTIVE: Trial ends & initial charge succeeds
    TRIALING --> PAST_DUE: Trial ends & charge fails (soft decline)
    TRIALING --> CANCELED: User cancels during trial
    ACTIVE --> ACTIVE: Recurring renewal charge succeeds
    ACTIVE --> PAST_DUE: Renewal charge fails (enter Grace Period / Dunning)
    ACTIVE --> CANCELED: Cancel immediate / Period ends with cancel_at_period_end=true
    PAST_DUE --> ACTIVE: Retry payment succeeds within Grace Period
    PAST_DUE --> SUSPENDED: All retry attempts exhausted / Hard decline
    SUSPENDED --> ACTIVE: User updates payment method & pays open invoice
    SUSPENDED --> CANCELED: Grace/Suspension timeout expired
    CANCELED --> [*]
```

---

## 4. Core Sequence Diagrams
### 4.1 Initial Subscription Creation & Tokenization Flow
```mermaid
sequenceDiagram
    autonumber
    actor User as Customer
    participant Merchant as Merchant Backend
    participant API as Recurring Billing API
    participant DB as Billing Database
    participant PSP as Payment Service Provider (PSP)

    User->>PSP: Enter card details via PSP Checkout/SDK (3DS)
    PSP-->>User: Return payment_method_token
    User->>Merchant: Submit checkout with payment_method_token
    Merchant->>API: POST /v1/subscriptions (Idempotency-Key)
    activate API
    API->>DB: Check Idempotency-Key & create SUBSCRIPTION + INVOICE (PENDING)
    API->>PSP: Charge initial amount (psp_token, invoice_idempotency_key)
    PSP-->>API: 200 OK (psp_transaction_id, status=SUCCEEDED)
    API->>DB: Update INVOICE=PAID, SUBSCRIPTION=ACTIVE, write WEBHOOK_OUTBOX
    API-->>Merchant: 201 Created (Subscription object)
    deactivate API
```

### 4.2 Scheduled Recurring Charge, Retry (Dunning) & Webhook Flow
```mermaid
sequenceDiagram
    autonumber
    participant Cron as Billing Scheduler
    participant Core as Billing Core Worker
    participant DB as Billing Database
    participant PSP as Payment Service Provider (PSP)
    participant Outbox as Webhook Dispatcher
    participant Merchant as Merchant Webhook Endpoint

    Cron->>Core: Trigger due subscriptions (next_billing_at <= NOW)
    activate Core
    Core->>DB: Lock subscription (FOR UPDATE SKIP LOCKED) & create INVOICE
    Core->>PSP: POST /charges (psp_token, idempotency_key)
    alt Charge Succeeded
        PSP-->>Core: SUCCEEDED
        Core->>DB: Mark INVOICE=PAID, advance current_period_end, enqueue invoice.paid
    else Soft Decline (Insufficient Funds / Timeout)
        PSP-->>Core: FAILED_SOFT (error_code)
        Core->>DB: Mark INVOICE=OPEN, SUBSCRIPTION=PAST_DUE, schedule next retry
        Core->>DB: Enqueue invoice.payment_failed in WEBHOOK_OUTBOX
    else Hard Decline (Stolen / Expired Card)
        PSP-->>Core: FAILED_HARD
        Core->>DB: Mark SUBSCRIPTION=SUSPENDED, stop retries, enqueue subscription.suspended
    end
    deactivate Core

    Outbox->>DB: Poll pending WEBHOOK_OUTBOX events
    Outbox->>Merchant: POST /webhooks (X-Signature-HMAC-SHA256)
    Merchant-->>Outbox: 200 OK
    Outbox->>DB: Update WEBHOOK_OUTBOX=DELIVERED
```

---

## 5. Integration & Data Consistency Decisions
| Decision Area | Chosen Approach | Rationale |
| :--- | :--- | :--- |
| **Monetary Representation** | Integer minor units (`amount_minor`, `int64`) + ISO 4217 `currency` | Предотвращает ошибки округления чисел с плавающей точкой (IEEE 754), критично для расчетов копеек и возвратов (`BR-04`). |
| **PCI DSS Scope Reduction** | Токенизация через PSP (`psp_token`), хранение только `card_last4`, `card_brand`, `exp_month/year` | Снижает уровень комплайенса до SAQ A, исключая хранение PAN и CVV на стороне сервиса (OQ-02). |
| **Double-Charge Prevention** | Заголовок `Idempotency-Key` от клиента + уникальный индекс в БД по инвойсам/транзакциям | Гарантирует выполнение операции ровно один раз (exactly-once) даже при повторных сетевых вызовах. |
| **Event Delivery** | Паттерн Transactional Outbox (`WEBHOOK_OUTBOX`) + подпись `HMAC-SHA256` | Исключает потерю вебхуков при падении сервиса или сбоях сети, обеспечивая асинхронную интеграцию с Merchant Backend (OQ-05). |

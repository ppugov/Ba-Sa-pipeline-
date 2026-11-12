# System Architecture & Data Model: Payment Gateway Core

> **Document Status:** Verified (Orchestrator Approved)  
> **Version:** 1.0  
> **Related BRD:** `01_BRD.md`  
> **Related API Contract:** `03_OpenAPI.yaml`  
> **Pipeline Pattern:** Orchestrator-Worker (Critic: `Gemini 4 Argon`)

---

## 1. Architectural Overview & Design Principles

### 1.1 Architectural Decisions & Patterns
- **Transactional Outbox Pattern:** Гарантированная доставка событий изменения статусов подписок и инвойсов в Merchant Backend. Событие сохраняется в таблицу `WEBHOOK_OUTBOX` в той же ACID-транзакции БД, что и изменение состояния подписки.
- **Idempotency Engine:** Все мутирующие HTTP-запросы (`POST`) обязаны содержать заголовок `Idempotency-Key` (UUID v4). Результат выполнения кэшируется в БД на 24 часа. Повторный запрос с тем же ключом не приводит к повторному списанию у PSP.
- **Strict Monetary Representation:** Категорический отказ от `float`/`double`. Все денежные значения хранятся как целые числа в минимальных единицах (`amount_minor: bigint / int64`, копейки/центы), валюта задается кодом ISO 4217 (`currency: string`).
- **PCI DSS Scope Minimization (SAQ A):** Сервис не видит и не хранит PAN и CVV. Платежная форма хостится на стороне PSP, бэкенд оперирует исключительно `psp_token` и маскированными реквизитами (`card_last4`, `card_brand`).
- **Pessimistic Concurrency Control:** Все операции изменения подписки (автопродление кроном, ручная отмена, возврат) выполняются с пессимистической блокировкой строки `SELECT ... FOR UPDATE`, исключая состояние гонки (*race condition*).

### 1.2 System Components & Responsibilities
| Component | Layer / Tech Stack | Core Responsibility |
| :--- | :--- | :--- |
| **API Gateway / Ingress** | Nginx / Envoy / Go Ingress | Маршрутизация, TLS-терминация, проверка заголовков `Idempotency-Key`, rate-limiting. |
| **Billing Core Engine** | Go / Python Async Service | Доменная логика подписок, календарная арифметика дат (`BR-01`), управление возвратами (`BR-04`). |
| **Dunning & Billing Scheduler** | Distributed Cron Worker | Периодический выбор подписок к продлению (`next_billing_at <= NOW()`), исполнение сетки dunning-ретраев. |
| **PSP Integration Adapter** | HTTP Client with Circuit Breaker | Взаимодействие с банком-эквайером, токенизация, MIT-списания, сверка зависших платежей при таймауте 10с. |
| **Webhook Dispatcher** | Outbox Background Worker | Поллинг таблицы `WEBHOOK_OUTBOX`, подписание полезной нагрузки `HMAC-SHA256`, доставка в монолит с backoff-повторами. |
| **Primary Relational Database** | PostgreSQL 16+ | Реляционное хранилище со строгой ссылочной целостностью, внешними ключами и уникальными индексами. |

---

## 2. Entity-Relationship Diagram (ERD)
> [!IMPORTANT]
> Все сущности, названия атрибутов, типы данных и статусы строго согласованы со схемами контракта `03_OpenAPI.yaml`.

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
        bigint amount_minor "Minor currency units (cents/kopecks)"
        string currency "ISO 4217 code (RUB)"
        string billing_interval "MONTHLY"
        int trial_days
        boolean is_active
    }

    PAYMENT_METHOD {
        uuid id PK
        uuid customer_id FK
        string psp_provider
        string psp_token UK "Tokenized card ref (PCI DSS compliant)"
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
        int anchor_day "Billing anchor day of month (1..31) for BR-01"
        timestamp current_period_start
        timestamp current_period_end
        timestamp next_billing_at
        timestamp grace_period_ends_at
        boolean cancel_at_period_end
        int version "Optimistic locking version"
        timestamp created_at
        timestamp updated_at
    }

    INVOICE {
        uuid id PK
        uuid subscription_id FK
        string idempotency_key UK
        bigint amount_minor "Amount charged"
        bigint amount_refunded_minor "Cumulative refund amount"
        string currency "ISO 4217 code"
        string status "DRAFT | OPEN | PAID | PARTIALLY_REFUNDED | PAST_DUE | VOID | REFUNDED"
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
    [*] --> TRIALING: Create Subscription (trial_days > 0)
    [*] --> ACTIVE: Create Subscription (immediate charge 201 SUCCEEDED)
    
    TRIALING --> ACTIVE: Trial expires & initial charge succeeds
    TRIALING --> PAST_DUE: Trial expires & initial charge soft-declines
    TRIALING --> CANCELED: User cancels during trial period

    ACTIVE --> ACTIVE: Scheduled renewal charge succeeds
    ACTIVE --> PAST_DUE: Renewal charge soft-declines (insufficient funds)
    ACTIVE --> SUSPENDED: Renewal charge hard-declines (stolen/closed card)
    ACTIVE --> CANCELED: Operator cancel (immediate) OR period ends with cancel_at_period_end=true

    PAST_DUE --> ACTIVE: Dunning retry succeeds within 7 days
    PAST_DUE --> SUSPENDED: All 3 retries exhausted (day 7) OR hard decline occurs
    PAST_DUE --> CANCELED: User or support cancels subscription in past due

    SUSPENDED --> ACTIVE: User updates payment method & successfully pays open invoice
    SUSPENDED --> CANCELED: 30 days suspension retention timeout expired

    CANCELED --> [*]
```

---

## 4. Sequence Diagrams

### 4.1 Первичное оформление подписки и безопасная токенизация (PCI DSS SAQ A)
```mermaid
sequenceDiagram
    autonumber
    actor Customer as Пользователь
    participant Merchant as Merchant Frontend / Backend
    participant PSP as Банк-эквайер (PSP)
    participant Core as Recurring Billing Core
    participant DB as Billing Database (PostgreSQL)

    Customer->>PSP: Ввод данных карты в защищенном фрейме PSP (3DS)
    PSP-->>Customer: Возврат токена карты (psp_token)
    Customer->>Merchant: Подтверждение покупки с psp_token
    Merchant->>Core: POST /v1/subscriptions (Idempotency-Key, plan_id, psp_token)
    activate Core
    Core->>DB: Проверка Idempotency-Key & создание CUSTOMER, PAYMENT_METHOD, SUBSCRIPTION (DRAFT)
    Core->>PSP: POST /charges (psp_token, amount_minor, idempotency_key)
    alt Списание успешно (SUCCEEDED)
        PSP-->>Core: 200 OK (psp_transaction_id, status=SUCCEEDED)
        Core->>DB: INVOICE=PAID, SUBSCRIPTION=ACTIVE, запись в WEBHOOK_OUTBOX
        Core-->>Merchant: 201 Created (SubscriptionResponse)
    else Отказ банка (DECLINED)
        PSP-->>Core: 402 Declined (error_code=insufficient_funds)
        Core->>DB: INVOICE=OPEN, SUBSCRIPTION=PAST_DUE
        Core-->>Merchant: 402 Payment Required (ProblemDetails)
    end
    deactivate Core
```

### 4.2 Рекуррентное списание по расписанию, Dunning (Soft vs Hard Decline) и Outbox
```mermaid
sequenceDiagram
    autonumber
    participant Scheduler as Billing Scheduler (Cron)
    participant Core as Billing Core Engine
    participant DB as Billing Database
    participant PSP as Банк-эквайер (PSP)
    participant Outbox as Webhook Dispatcher
    participant Merchant as Merchant Backend (Монолит)

    Scheduler->>Core: Запуск списания подписок (next_billing_at <= NOW())
    activate Core
    Core->>DB: SELECT * FROM SUBSCRIPTION WHERE ... FOR UPDATE SKIP LOCKED
    Core->>DB: Создание нового INVOICE (status=OPEN)
    Core->>PSP: POST /charges (psp_token, amount_minor, idempotency_key)
    
    alt Успешное списание
        PSP-->>Core: 200 OK (SUCCEEDED)
        Core->>DB: INVOICE=PAID, SUBSCRIPTION=ACTIVE, сдвиг next_billing_at (BR-01)
        Core->>DB: Запись события invoice.paid в WEBHOOK_OUTBOX
    else Soft Decline (Недостаточно средств)
        PSP-->>Core: 402 Soft Decline (insufficient_funds)
        Core->>DB: SUBSCRIPTION=PAST_DUE, grace_period_ends_at = NOW + 7d
        Core->>DB: Назначение следующего ретрая (День +1)
        Core->>DB: Запись события invoice.payment_failed в WEBHOOK_OUTBOX
    else Hard Decline (Карта украдена / заблокирована)
        PSP-->>Core: 402 Hard Decline (card_stolen)
        Core->>DB: SUBSCRIPTION=SUSPENDED, отмена всех будущих ретраев
        Core->>DB: Запись события subscription.suspended в WEBHOOK_OUTBOX
    end
    deactivate Core

    Outbox->>DB: SELECT * FROM WEBHOOK_OUTBOX WHERE delivery_status='PENDING'
    Outbox->>Merchant: POST /webhooks (X-Webhook-Signature: HMAC-SHA256)
    Merchant-->>Outbox: 200 OK (Событие обработано)
    Outbox->>DB: UPDATE WEBHOOK_OUTBOX SET delivery_status='DELIVERED'
```

---

## 5. Архитектурные гарантии надежности и комплаенса
1. **Точность денег:** Все финансовые подсчеты и валидации выполняются исключительно в `int64` (копейки). Исключены любые риски погрешностей типа IEEE 754.
2. **Изоляция сбоев PSP:** Таймаут вызова эквайера — жестко 10 000 мс. При обрыве соединения статус платежной попытки переводится в `PENDING`, а специальный reconcile-воркер опрашивает PSP по сохраненному `Idempotency-Key` до совершения любых дальнейших действий.
3. **Защита от повторных частичных возвратов:** Инвариант `BR-04` проверяется внутри транзакции: `amount_refunded_minor + new_refund_minor <= amount_minor`. Превышение исходного платежа технически заблокировано.

# System Architecture & Data Model: FinTech × Media Ecosystem (`bank_kinopoisk`)

> **Document Status:** Verified (Orchestrator Approved)  
> **Version:** 1.0  
> **Related BRD:** `01_BRD.md`  
> **Related API Contract:** `03_OpenAPI.yaml`  
> **Pipeline Pattern:** Orchestrator-Worker (Worker: `gemini-3.5-flash-lite`, Critic: `Gemini 4 Argon`)

---

## 1. Architectural Overview & Design Principles
### 1.1 Architectural Style & Patterns
Архитектура интеграции **Банка** и **Кинопоиска** построена вокруг изолированного промежуточного шлюза **Ecosystem Media-Banking Integration Service** с использованием паттернов **CQRS Read Projection**, **Orchestrated Saga** и **Transactional Outbox**:

1. **Изоляция банковской тайны (Zero Raw Banking Data in Media):** АБС Банка никогда не передает в контур Кинопоиска номера счетов, PAN карт, остатки денежных средств или выписку по операциям (ст. 26 ФЗ-395-1). В шлюз передаются только агрегированные проекции (`spend_target_met`, `qualified_turnover_minor`, `loyalty_points_minor`) и только при наличии активного согласия `USER_CONSENT`.
2. **Защита АБС в пятничный Prime Time (CQRS Local Projection):** Когда 2 миллиона зрителей открывают Кинопоиск в пятницу в 20:00, запросы `GET /v1/subscriptions/{ecosystem_user_id}` обслуживаются за `< 50–150 мс` из локальной проекции `SUBSCRIPTION_STATE` в БД/Redis шлюза, не создавая ни одного синхронного запроса в Core Banking.
3. **Двухфазная гибридная оплата (Saga Orchestrator с защитой от таймаутов):** Покупка фильма (TVOD) за баллы + рубли проходит через состояния `INITIATED -> POINTS_HELD -> FUNDS_DEBITED -> COMMITTED`. При явном отказе списания рублей выполняется компенсация `COMPENSATED` (`LOYALTY_RELEASE`). При сетевом таймауте (`504`) сессия переводится в `RECONCILING` до сверки фактического статуса списания денег в Банке (`BR-04`).
4. **Безопасность Smart TV (Argon2id Parental PIN + Rate Lockout):** Хеш родительского ПИН-кода хранится только во внутренней таблице `ECOSYSTEM_USER` (никогда не возвращается в API ответах!) и защищен счетчиком `pin_failed_attempts` с блокировкой на 15 минут после 3 ошибок (`BR-05`).

### 1.2 System Components & Responsibilities
| Component | Layer / Technology | Core Responsibility |
| :--- | :--- | :--- |
| **API Gateway / Edge BFF** | Envoy / Kong | Проверка SSO JWT-токенов, TLS 1.3, rate-limiting, валидация заголовка `Idempotency-Key`. |
| **Ecosystem Integration Service** | Go / Java Spring Boot | Управление согласиями (`USER_CONSENT`), отдача локальной проекции статуса бандла (`SUBSCRIPTION_STATE`), проверка Parental PIN. |
| **Hybrid Payment Saga Orchestrator** | Application Layer | Координация распределенной транзакции между Сервисом Лояльности Банка (`HOLD`/`COMMIT`/`RELEASE`), Платежным шлюзом (`DEBIT`) и выдачей прав Кинопоиска (`ENTITLEMENT`). |
| **Reconciliation & Outbox Worker** | Background Worker / Kafka | Асинхронная доставка событий из `DOMAIN_EVENT_OUTBOX` и сверка зависших в `RECONCILING` платежных сессий. |
| **PostgreSQL 16 + Redis Cluster** | Storage Layer | ACID-хранение сущностей, оптимистическая блокировка (`version`), быстрый кэш статусов для пятничного прайм-тайма. |

---

## 2. Entity-Relationship Diagram (ERD)
> [!IMPORTANT]
> Все сущности, названия полей, типы данных и перечисления статусов (`enum`) на 100% синхронизированы со схемами в `03_OpenAPI.yaml`. Секретные поля (`parent_pin_hash`) изолированы в БД и никогда не сериализуются в ответы API.

```mermaid
erDiagram
    ECOSYSTEM_USER ||--o{ USER_CONSENT : "grants"
    ECOSYSTEM_USER ||--|| SUBSCRIPTION_STATE : "has_projection"
    ECOSYSTEM_USER ||--o{ HYBRID_PAYMENT_SESSION : "initiates"
    CATALOG_ITEM ||--o{ HYBRID_PAYMENT_SESSION : "purchased_in"
    HYBRID_PAYMENT_SESSION ||--o{ SUB_TRANSACTION : "executes_steps"
    HYBRID_PAYMENT_SESSION ||--o{ DOMAIN_EVENT_OUTBOX : "emits"

    ECOSYSTEM_USER {
        uuid id PK
        string ecosystem_user_id UK "SSO Unified Subject ID"
        string bank_client_hash UK "Anonymized HMAC of Bank Client ID"
        string kinopoisk_profile_id UK
        string status "ACTIVE | BLOCKED | PENDING_LINK"
        string parent_pin_hash "Argon2id hash (NEVER exposed in API responses)"
        int pin_failed_attempts "Counter reset on success, lock at 3"
        timestamptz pin_locked_until "Null or lock expiration timestamp"
        timestamptz created_at
        timestamptz updated_at
    }

    USER_CONSENT {
        uuid id PK
        string ecosystem_user_id FK
        string consent_version "e.g. v1.0-fz395-152fz"
        string status "GRANTED | REVOKED"
        string client_ip
        timestamptz granted_at
        timestamptz revoked_at
    }

    SUBSCRIPTION_STATE {
        uuid id PK
        string ecosystem_user_id FK "Unique 1:1 projection per user"
        string subscription_status "FREE_BUNDLE | PAID_399 | GRACE_PERIOD | CLAWBACK_DEBT | EXPIRED"
        bigint qualified_turnover_minor "Qualified POS spend in kopecks (target >= 1500000)"
        bigint loyalty_points_minor "Available cashback points in minor units (1 pt = 100)"
        boolean spend_target_met "True if qualified_turnover_minor >= 1500000"
        int turnover_progress_pct "0..100"
        bigint next_renewal_price_minor "0 or 39900 kopecks"
        timestamptz period_start
        timestamptz period_end
        int version "Optimistic locking version"
        timestamptz updated_at
    }

    CATALOG_ITEM {
        uuid id PK
        string content_id UK
        string title
        bigint price_minor "Movie TVOD price in kopecks"
        boolean is_active
    }

    HYBRID_PAYMENT_SESSION {
        uuid id PK
        string idempotency_key UK
        string ecosystem_user_id FK
        uuid item_id FK
        string device_type "SMART_TV | MOBILE | WEB"
        boolean is_kids_profile
        bigint total_amount_minor "Total price in kopecks"
        bigint paid_points_minor "Points part in minor units (<= 99% of total)"
        bigint paid_money_minor "Rubles part in kopecks (>= 100)"
        string status "INITIATED | POINTS_HELD | FUNDS_DEBITED | RECONCILING | COMMITTED | COMPENSATED | FAILED"
        boolean entitlement_granted
        string failure_reason
        int version "Optimistic locking version"
        timestamptz created_at
        timestamptz updated_at
    }

    SUB_TRANSACTION {
        uuid id PK
        uuid payment_session_id FK
        string tx_type "LOYALTY_HOLD | BANK_CARD_DEBIT | LOYALTY_COMMIT | LOYALTY_RELEASE | BANK_CARD_REFUND"
        string external_tx_id
        bigint amount_minor
        string status "PENDING | SUCCEEDED | FAILED"
        string error_code
        timestamptz processed_at
    }

    DOMAIN_EVENT_OUTBOX {
        uuid id PK
        string event_type "consent.updated | subscription.status_changed | payment.committed | payment.compensated"
        uuid aggregate_id FK
        jsonb payload
        string delivery_status "PENDING | DELIVERED | DEAD_LETTER"
        int retry_count
        timestamptz created_at
    }
```

---

## 3. Core Aggregate Lifecycle (Saga State Machine)
```mermaid
stateDiagram-v2
    [*] --> INITIATED: POST /v1/hybrid-payments (Consent, Split & PIN validated)
    INITIATED --> FAILED: Loyalty HOLD rejected (insufficient points)
    INITIATED --> POINTS_HELD: Loyalty Service HOLD succeeded
    
    POINTS_HELD --> FUNDS_DEBITED: Bank Payment Gateway DEBIT succeeded
    POINTS_HELD --> COMPENSATED: Bank DEBIT declined (402) -> Loyalty RELEASE executed
    POINTS_HELD --> RECONCILING: Bank DEBIT timed out (504) -> Wait for reconciliation (BR-04)
    
    RECONCILING --> FUNDS_DEBITED: Reconcile worker confirmed debit succeeded at Bank
    RECONCILING --> COMPENSATED: Reconcile worker confirmed debit failed -> Loyalty RELEASE executed
    
    FUNDS_DEBITED --> COMMITTED: Loyalty COMMIT succeeded & Kinopoisk ENTITLEMENT granted
    FUNDS_DEBITED --> COMPENSATED: Loyalty COMMIT failed -> Bank REFUND + Loyalty RELEASE executed
    
    COMMITTED --> [*]
    COMPENSATED --> [*]
    FAILED --> [*]
```

---

## 4. Core Sequence Diagrams
### 4.1 Гибридная покупка фильма на Smart TV (Parental PIN + Saga + Timeout Protection)
```mermaid
sequenceDiagram
    autonumber
    actor User as Viewer (Smart TV)
    participant BFF as API Gateway / BFF
    participant Saga as Hybrid Payment Saga Orchestrator
    participant DB as PostgreSQL
    participant Loyalty as Bank Loyalty Service
    participant PG as Bank Payment Gateway

    User->>BFF: POST /v1/hybrid-payments (Idempotency-Key, item_id, points, money, parent_pin_code)
    BFF->>Saga: Forward validated request
    activate Saga
    Saga->>DB: Check Idempotency-Key, active USER_CONSENT & verify parent_pin_code against Argon2id hash
    alt Missing Consent / Invalid PIN / Split Rule Violation (BR-03, BR-05)
        Saga->>DB: Increment pin_failed_attempts (lock 15m if >= 3)
        Saga-->>BFF: 422 Unprocessable Entity (ProblemDetails)
        BFF-->>User: 422 Error (INVALID_PARENTAL_PIN / MAX_POINTS_LIMIT_EXCEEDED)
    else Pre-Checks Passed
        Saga->>DB: Create HYBRID_PAYMENT_SESSION (status=INITIATED)
        Saga->>Loyalty: POST /internal/loyalty/hold (idempotency_key, paid_points_minor)
        Loyalty-->>Saga: 200 OK (hold_tx_id)
        Saga->>DB: Update status=POINTS_HELD
        
        Saga->>PG: POST /internal/payments/debit (idempotency_key, paid_money_minor)
        alt Bank Card Debit Succeeded
            PG-->>Saga: 200 OK (debit_tx_id)
            Saga->>DB: Update status=FUNDS_DEBITED
            Saga->>Loyalty: POST /internal/loyalty/commit (hold_tx_id)
            Loyalty-->>Saga: 200 OK
            Saga->>DB: Update status=COMMITTED, entitlement_granted=true + Write DOMAIN_EVENT_OUTBOX
            Saga-->>BFF: 201 Created (HybridPaymentSessionResponse)
            BFF-->>User: 201 Created (Movie starts playing)
        else Bank Card Hard/Soft Decline (Insufficient Funds)
            PG-->>Saga: 402 Payment Declined
            Saga->>Loyalty: POST /internal/loyalty/release (hold_tx_id)
            Loyalty-->>Saga: 200 OK (Points unfrozen)
            Saga->>DB: Update status=COMPENSATED + Write DOMAIN_EVENT_OUTBOX
            Saga-->>BFF: 422 Unprocessable Entity (BANK_DEBIT_DECLINED)
            BFF-->>User: 422 Error (Points restored immediately)
        else Network Timeout (504 Gateway Timeout)
            PG-->>Saga: 504 Timeout (Unknown Bank State)
            Saga->>DB: Update status=RECONCILING (DO NOT release points blindly!)
            Saga-->>BFF: 202 Accepted / Session in RECONCILING state
            BFF-->>User: Processing payment confirmation...
        end
    end
    deactivate Saga
```

### 4.2 Асинхронный расчет оборота 15 000 ₽, защита от возвратов (`Clawback`) и Prime-Time чтение
```mermaid
sequenceDiagram
    autonumber
    participant Proc as Bank Card Processing
    participant Kafka as Kafka / Outbox Bus
    participant Eco as Ecosystem Integration Service
    participant DB as PostgreSQL / Redis Projection
    actor User as Kinopoisk Viewer (Friday 20:00 Prime Time)

    Proc->>Proc: Filter POS transaction by allowed MCC (BR-01) or process Merchant Refund (BR-02)
    Proc->>Kafka: Publish TurnoverUpdatedEvent (bank_client_hash, qualified_turnover_minor, is_refund)
    Kafka->>Eco: Consume event (At-Least-Once)
    activate Eco
    Eco->>DB: Load SUBSCRIPTION_STATE FOR UPDATE
    alt Turnover >= 1,500,000 kopecks (15,000 RUB)
        Eco->>DB: Set spend_target_met=true, subscription_status='FREE_BUNDLE', next_renewal_price_minor=0
    else Refund dropped turnover < 1,500,000 after free renewal (BR-02 Clawback)
        Eco->>DB: Set spend_target_met=false, subscription_status='CLAWBACK_DEBT', next_renewal_price_minor=39900
    end
    Eco->>DB: Enqueue 'subscription.status_changed' in DOMAIN_EVENT_OUTBOX
    deactivate Eco

    User->>Eco: GET /v1/subscriptions/{ecosystem_user_id} (Prime Time: 15,000 RPS)
    Eco->>DB: Read cached projection (0 calls to Core Banking ABS!)
    DB-->>Eco: SubscriptionStateResponse (p95 < 50ms)
    Eco-->>User: 200 OK (Progress bar & Free Bundle badge)
```

---

## 5. Architectural & Data Consistency Decisions
| Decision Area | Chosen Approach | Rationale |
| :--- | :--- | :--- |
| **Monetary & Points Precision** | `BIGINT` (`int64`) в минорных единицах (`1 ₽ = 100 копеек`, `1 балл = 100 минорных единиц`). | Полное исключение ошибок округления `float` при расчете пропорции `99% / 1%` (`BR-03`). |
| **Banking Secrecy (ФЗ-395-1)** | Хранение только `bank_client_hash` и агрегированного `qualified_turnover_minor` после подписания `USER_CONSENT`. | Медиа-контур физически не имеет доступа к банковской тайне, номерам счетов и списку покупок клиента. |
| **Saga Timeout Safety (`BR-04`)** | Промежуточный статус `RECONCILING` при таймауте `504` от платежного шлюза Банка. | Предотвращает убыток, когда деньги с карты списались с задержкой, а система уже разморозила баллы лояльности. |
| **Parental PIN Security (`BR-05`)** | Хеширование Argon2id в `ECOSYSTEM_USER.parent_pin_hash`, поле **исключено** из всех DTO ответов API. | Исключает утечку хеша родительского ПИН-кода через клиентские логи Smart TV. |
| **Prime-Time Isolation** | Локальная CQRS-проекция `SUBSCRIPTION_STATE` в PostgreSQL + Redis. | Защищает АБС Банка от падения во время вечерних премьер на Кинопоиске (2 млн зрителей). |

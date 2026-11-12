# System Architecture & Data Model: bank_kinopoisk

> **Document Status:** Verified & Refined  
> **Version:** 1.0  
> **Related BRD:** `01_BRD.md`  
> **Related API Contract:** `03_OpenAPI.yaml`

---

## 1. Architectural Overview & Design Principles
### 1.1 Architectural Style & Patterns
Проект **bank_kinopoisk** реализуется в рамках гибридной экосистемной архитектуры (FinTech x Media Bundle), сочетающей микросервисный подход и событийно-ориентированную интеграцию (Event-Driven Architecture). 
* **Шлюз экосистемы (Ecosystem Media-Banking Integration Service):** Изолирует медиа-платформу Кинопоиска от прямого доступа к Core Banking System (АБС) Банка, выполняя функции кэширования статусов подписок и оркестрации запросов.
* **Паттерн Saga (Оркестрация):** Применяется для реализации гибридной оплаты контента (TVOD), состоящей из двух независимых транзакционных контуров: Сервиса Лояльности (холдирование баллов) и Платежного шлюза Банка (списание рублей). Гарантирует компенсацию (`RELEASE`) баллов в случае сбоя денежной транзакции.
* **Transactional Outbox:** Обеспечивает надежную асинхронную доставку событий изменения статусов подписок и оборотов без риска потери данных при сетевых сбоях.
* **Idempotency-Key:** Строго обязателен для всех финансовых эндпоинтов и операций списания/холдирования с целью предотвращения двойного списания.
* **Оптимистичная и Пессимистичная блокировка:** Используется для управления состоянием балансов и сессий подписок (`SELECT ... FOR UPDATE` в фоновых воркерах расчета оборотов, `version` для сущностей пользователей и платежей).

### 1.2 System Components & Responsibilities
| Component | Layer / Technology | Core Responsibility |
| :--- | :--- | :--- |
| **API Gateway / BFF** | Edge Layer / Kong / Envoy | Маршрутизация внешних запросов со Smart TV и мобильных приложений, проверка JWT-токенов, rate-limiting, проверка заголовка `Idempotency-Key`. |
| **Ecosystem Integration Service** | Application Layer / Java Spring Boot | Промежуточный шлюз: хранение локальной проекции статусов (`spend_target_met`), обработка вебхуков от Банка, интеграция с SSO и Consent Management. |
| **Loyalty & Payments Orchestrator** | Application Layer / Saga Coordinator | Управление двухфазными транзакциями гибридной оплаты (баллы + рубли), вызов внешних API Сервиса Лояльности и Платежного шлюза. |
| **Primary Database** | Storage Layer / PostgreSQL 15 | Хранение доменных сущностей (аккаунты экосистемы, согласия, платежные сессии, Outbox-таблицы) с поддержкой ACID. |
| **Event Bus / Outbox Dispatcher** | Messaging Layer / Apache Kafka & Debezium | Асинхронная доставка событий изменения статусов подписок и триггеров пересчета оборотов между Банком и Кинопоиском. |

---

## 2. Entity-Relationship Diagram (ERD)
> [!IMPORTANT]
> Все сущности, названия полей, типы данных, первичные (`PK`), внешние (`FK`) и уникальные (`UK`) ключи строго согласованы со сценариями интеграции и спецификацией API.

```mermaid
erDiagram
    ECOSYSTEM_USER ||--o{ USER_CONSENT : "grants"
    ECOSYSTEM_USER ||--o{ SUBSCRIPTION_STATE : "has"
    ECOSYSTEM_USER ||--o{ HYBRID_PAYMENT_SESSION : "initiates"
    CATALOG_ITEM ||--o{ HYBRID_PAYMENT_SESSION : "purchased_in"
    HYBRID_PAYMENT_SESSION ||--o{ SUB_TRANSACTION : "contains"
    ECOSYSTEM_USER ||--o{ DOMAIN_EVENT_OUTBOX : "emits"

    ECOSYSTEM_USER {
        uuid id PK
        string ecosystem_user_id UK
        string bank_client_hash
        string kinopoisk_profile_id
        string status "ACTIVE | BLOCKED | PENDING_LINK"
        timestamp created_at
        timestamp updated_at
    }

    USER_CONSENT {
        uuid id PK
        uuid ecosystem_user_id FK
        string consent_version
        boolean is_granted
        timestamp granted_at
        timestamp revoked_at
    }

    SUBSCRIPTION_STATE {
        uuid id PK
        uuid ecosystem_user_id FK
        string subscription_status "FREE_BUNDLE | PAID_399 | GRACE_PERIOD | EXPIRED"
        bigint qualified_turnover_minor "Minor units (kopecks)"
        boolean spend_target_met
        int turnover_progress_pct
        timestamp period_start
        timestamp period_end
        timestamp updated_at
    }

    CATALOG_ITEM {
        uuid id PK
        string content_id UK
        string title
        bigint price_minor "Minor units (kopecks)"
        boolean is_adult_restricted
        boolean is_active
    }

    HYBRID_PAYMENT_SESSION {
        uuid id PK
        uuid ecosystem_user_id FK
        uuid item_id FK
        string idempotency_key UK
        bigint total_amount_minor
        bigint paid_money_minor
        bigint paid_points_minor
        string status "INITIATED | POINTS_HELD | FUNDS_DEBITED | COMMITTED | COMPENSATED | FAILED"
        string parent_pin_hash "For Smart TV anti-fraud (>300 RUB)"
        int version "Optimistic locking version"
        timestamp created_at
        timestamp updated_at
    }

    SUB_TRANSACTION {
        uuid id PK
        uuid payment_session_id FK
        string tx_type "LOYALTY_HOLD | LOYALTY_COMMIT | LOYALTY_RELEASE | BANK_CARD_DEBIT"
        string external_tx_id
        string status "PENDING | SUCCEEDED | FAILED"
        string error_code
        timestamp processed_at
    }

    DOMAIN_EVENT_OUTBOX {
        uuid id PK
        string event_type
        uuid aggregate_id FK
        jsonb payload
        string delivery_status "PENDING | DELIVERED | DEAD_LETTER"
        int retry_count
        timestamp created_at
    }
```

---

## 3. Core Aggregate Lifecycle (State Machine)
### 3.1 Жизненный цикл сессии гибридной оплаты (Saga State Machine)
```mermaid
stateDiagram-v2
    [*] --> INITIATED: Start hybrid payment request (Idempotency-Key)
    INITIATED --> POINTS_HELD: Loyalty Service HOLD succeeded
    POINTS_HELD --> FUNDS_DEBITED: Payment Gateway DEBIT succeeded
    FUNDS_DEBITED --> COMMITTED: Loyalty Service COMMIT succeeded (Entitlement granted)
    
    POINTS_HELD --> COMPENSATED: Payment Gateway DEBIT failed -> Release points
    FUNDS_DEBITED --> COMPENSATED: Loyalty Service COMMIT failed -> Refund money & release points
    
    INITIATED --> FAILED: Validation or hard error (Insufficient points/funds)
    COMPENSATED --> [*]
    COMMITTED --> [*]
    FAILED --> [*]
```

---

## 4. Core Sequence Diagrams
### 4.1 Primary Happy Path: Покупка контента (TVOD) с гибридной оплатой (Saga Pattern)
```mermaid
sequenceDiagram
    autonumber
    actor User as Client (Smart TV / App)
    participant Gateway as API Gateway / BFF
    participant Orchestrator as Payment Orchestrator (Saga)
    participant Loyalty as Loyalty Service (Bank)
    participant PG as Payment Gateway (Bank)
    participant DB as Database (Outbox)

    User->>Gateway: POST /v1/payments/hybrid (Idempotency-Key, items, points, pin)
    Gateway->>Orchestrator: Forward validated payment request
    activate Orchestrator
    Orchestrator->>DB: Create payment session (INITIATED)
    
    %% Шаг 1: Холдирование баллов
    Orchestrator->>Loyalty: POST /v1/loyalty/hold (ecosystem_user_id, points_amount)
    alt Loyalty Hold Success
        Loyalty-->>Orchestrator: 200 OK (hold_id)
        Orchestrator->>DB: Update session state -> POINTS_HELD
        
        %% Шаг 2: Списание рублей с карты
        Orchestrator->>PG: POST /v1/payments/debit (card_token, money_amount)
        alt Payment Debit Success
            PG-->>Orchestrator: 200 OK (transaction_id)
            Orchestrator->>DB: Update session state -> FUNDS_DEBITED
            
            %% Шаг 3: Подтверждение баллов
            Orchestrator->>Loyalty: POST /v1/loyalty/commit (hold_id)
            Loyalty-->>Orchestrator: 200 OK
            Orchestrator->>DB: Update session state -> COMMITTED + Enqueue DOMAIN_EVENT_OUTBOX
            Orchestrator-->>Gateway: 201 Created (Entitlement granted)
            Gateway-->>User: 200 OK (Payment successful, access granted)
        else Payment Debit Failed
            PG-->>Orchestrator: 4xx/5xx Error (Insufficient funds / declined)
            Orchestrator->>DB: Update session state -> COMPENSATED
            %% Компенсация: разморозка баллов
            Orchestrator->>Loyalty: POST /v1/loyalty/release (hold_id)
            Loyalty-->>Orchestrator: 200 OK
            Orchestrator-->>Gateway: 422 Unprocessable Entity (Insufficient funds)
            Gateway-->>User: Error response
        end
    else Loyalty Hold Failed
        Loyalty-->>Orchestrator: 400 Bad Request (Not enough points)
        Orchestrator->>DB: Update session state -> FAILED
        Orchestrator-->>Gateway: 400 Bad Request
        Gateway-->>User: Error response
    end
    deactivate Orchestrator
```

### 4.2 Background Processing, Async Status Sync & Outbox Delivery Flow
```mermaid
sequenceDiagram
    autonumber
    participant BankProc as Bank Processing & Core System
    participant OutboxWorker as Outbox Dispatcher / Kafka
    participant IntegrationService as Ecosystem Integration Service
    participant Billing as Kinopoisk Billing Service

    BankProc->>BankProc: Calculate qualified turnover (MCC filter applied)
    BankProc->>OutboxWorker: Emit Domain Event (TurnoverCalculatedEvent)
    OutboxWorker->>IntegrationService: Consume event via Kafka / Webhook (with HMAC signature)
    activate IntegrationService
    IntegrationService->>IntegrationService: Update local projection (spend_target_met: true/false)
    IntegrationService->>Billing: PUT /v1/subscriptions/status (ecosystem_user_id, bundle_eligible)
    Billing-->>IntegrationService: 200 OK (Subscription updated: 0 RUB or 399 RUB)
    deactivate IntegrationService
```

---

## 5. Architectural & Data Consistency Decisions
| Decision Area | Chosen Approach | Rationale |
| :--- | :--- | :--- |
| **Data Types & Precision** | Целочисленные минорные единицы (копейки, `bigint`) для всех денежных сумм и баллов; ISO-8601 в UTC (`timestamp with time zone`) для дат; UUID v4 для первичных ключей. | Исключает проблемы округления с плавающей запятой (`float`/`double`) при финансовых расчетах и гарантирует глобальную уникальность идентификаторов. |
| **Concurrency & Race Conditions** | Оптимистичная блокировка с использованием поля `version` для сессий оплат и пессимистичная блокировка (`SELECT ... FOR UPDATE SKIP LOCKED`) в фоновых воркерах обработки Outbox-событий. | Предотвращает конфликты параллельного доступа при попытке одновременного списания баллов или обработки транзакций в пятничный Prime Time. |
| **Idempotency & Retries** | Обязательный заголовок `Idempotency-Key` для всех платежных API с фиксацией ключа в БД (TTL 24 часа). Повторные запросы с тем же ключом возвращают кэшированный ответ без повторного выполнения бизнес-логики. | Защищает от случайных повторных отправк запросов клиентом при сетевых таймаутах и исключает двойное списание средств или баллов. |
| **Security & PII Protection** | Полный запрет на передачу сырых транзакций и остатков в Кинопоиск (ст. 26 ФЗ-395-1, 152-ФЗ). Использование токенизированных карт (`card_token`), хеширование идентификаторов клиентов (`bank_client_hash`) и JWT с цифровой подписью. | Обеспечивает жесткое соблюдение банковской тайны и требований регуляторов по защите персональных данных пользователей. |
| **Asynchronous Reliability** | Transactional Outbox Pattern + Dead Letter Queue (DLQ) на базе Kafka. | Гарантирует атомарность сохранения состояния в БД и отправку событий смежным сервисам даже при падении брокера сообщений. |

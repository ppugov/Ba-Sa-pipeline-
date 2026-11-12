# System Architecture & Data Model: {{PROJECT_NAME}}

> **Document Status:** Draft / Verified  
> **Version:** 1.0  
> **Related BRD:** `01_BRD.md`  
> **Related API Contract:** `03_OpenAPI.yaml`

---

## 1. Architectural Overview & Design Principles
### 1.1 Architectural Style & Patterns
<!-- Опишите выбранный архитектурный стиль и ключевые паттерны надежности под предметную область проекта (например: синхронный REST vs асинхронные события, Transactional Outbox, Idempotency-Key, Optimistic/Pessimistic Locking, State Machine, кэширование). -->

### 1.2 System Components & Responsibilities
| Component | Layer / Technology | Core Responsibility |
| :--- | :--- | :--- |
| **API Gateway / BFF** | <!-- API Layer --> | <!-- Маршрутизация, авторизация, валидация, rate-limiting --> |
| **Core Domain Service** | <!-- Application Layer --> | <!-- Ядро бизнес-логики, инварианты BR-*, переходы состояний --> |
| **Integration Adapter / Worker** | <!-- Infrastructure Layer --> | <!-- Взаимодействие с внешними системами, фоновые задачи, ретраи --> |
| **Primary Database** | <!-- Storage Layer --> | <!-- Хранение доменных сущностей и транзакционная целостность --> |
| **Event Bus / Outbox Dispatcher** | <!-- Messaging Layer --> | <!-- Доставка доменных событий и уведомлений смежным сервисам --> |

---

## 2. Entity-Relationship Diagram (ERD)
> [!IMPORTANT]
> Все сущности, названия полей, типы данных, первичные (`PK`), внешние (`FK`) и уникальные (`UK`) ключи ДОЛЖНЫ строго совпадать со схемами в `03_OpenAPI.yaml`.

```mermaid
erDiagram
    PRIMARY_ACTOR ||--o{ CORE_AGGREGATE : "initiates"
    CATALOG_ITEM ||--o{ CORE_AGGREGATE : "referenced_by"
    CORE_AGGREGATE ||--o{ SUB_TRANSACTION : "contains"
    CORE_AGGREGATE ||--o{ DOMAIN_EVENT_OUTBOX : "emits"

    PRIMARY_ACTOR {
        uuid id PK
        string external_ref UK
        string email
        string status "ACTIVE | BLOCKED"
        timestamp created_at
    }

    CATALOG_ITEM {
        uuid id PK
        string code UK
        string name
        bigint price_minor "Integer minor units (if monetary) or quantity"
        boolean is_active
    }

    CORE_AGGREGATE {
        uuid id PK
        uuid actor_id FK
        uuid item_id FK
        string idempotency_key UK
        string status "DRAFT | PENDING | ACTIVE | COMPLETED | CANCELED | FAILED"
        int version "Optimistic locking version"
        timestamp scheduled_at
        timestamp created_at
        timestamp updated_at
    }

    SUB_TRANSACTION {
        uuid id PK
        uuid aggregate_id FK
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
<!-- ЗАМЕНИТЕ примерные сущности выше на реальные доменные сущности анализируемого проекта! -->

---

## 3. Core Aggregate Lifecycle (State Machine)
```mermaid
stateDiagram-v2
    [*] --> CREATED: Create request validated
    CREATED --> PENDING: Async processing / external confirmation started
    PENDING --> ACTIVE: Processing succeeded
    PENDING --> FAILED: Validation or hard error
    ACTIVE --> COMPLETED: Business lifecycle fulfilled
    ACTIVE --> CANCELED: User or operator cancellation
    FAILED --> [*]
    CANCELED --> [*]
    COMPLETED --> [*]
```
<!-- Адаптируйте состояния и переходы под жизненный цикл главной сущности проекта и синхронизируйте с enum в 03_OpenAPI.yaml! -->

---

## 4. Core Sequence Diagrams
### 4.1 Primary Happy Path & External Integration Flow
```mermaid
sequenceDiagram
    autonumber
    actor User as Client / Actor
    participant Gateway as API Gateway
    participant Service as Core Domain Service
    participant DB as Database
    participant Ext as External System / Partner API

    User->>Gateway: POST /v1/<resource> (Idempotency-Key)
    Gateway->>Service: Forward validated request
    activate Service
    Service->>DB: Check Idempotency-Key & lock/create aggregate
    Service->>Ext: Execute external integration call (timeout guard)
    alt Integration Succeeded
        Ext-->>Service: 200 OK (result payload)
        Service->>DB: Commit state transition + enqueue DOMAIN_EVENT_OUTBOX
        Service-->>Gateway: 201 Created (ResourceResponse)
        Gateway-->>User: 201 Created
    else Integration Failed / Rejected
        Ext-->>Service: 4xx/5xx Error
        Service->>DB: Record failure state & error_code
        Service-->>Gateway: 422 / 502 ProblemDetails
        Gateway-->>User: Error response
    end
    deactivate Service
```

### 4.2 Background Processing, Retry / Compensation & Event Delivery Flow
```mermaid
sequenceDiagram
    autonumber
    participant Worker as Background Scheduler / Worker
    participant Service as Core Domain Service
    participant DB as Database
    participant Outbox as Outbox / Webhook Dispatcher
    participant Consumer as Downstream Consumer Service

    Worker->>Service: Trigger scheduled/retry processing
    activate Service
    Service->>DB: SELECT ... FOR UPDATE SKIP LOCKED
    Service->>DB: Update aggregate state & insert event into DOMAIN_EVENT_OUTBOX
    deactivate Service

    Outbox->>DB: Poll PENDING events from DOMAIN_EVENT_OUTBOX
    Outbox->>Consumer: POST /webhooks or Publish Event (with signature/headers)
    Consumer-->>Outbox: 200 OK
    Outbox->>DB: Mark event as DELIVERED
```

---

## 5. Architectural & Data Consistency Decisions
| Decision Area | Chosen Approach | Rationale |
| :--- | :--- | :--- |
| **Data Types & Precision** | <!-- Например: целочисленные минорные единицы для денег, UTC ISO-8601 для дат, UUID v4 для PK --> | <!-- Обоснование --> |
| **Concurrency & Race Conditions** | <!-- Например: SELECT FOR UPDATE / Optimistic Locking (version) --> | <!-- Обоснование --> |
| **Idempotency & Retries** | <!-- Например: заголовок Idempotency-Key + таблица ключей с TTL 24h --> | <!-- Обоснование --> |
| **Security & PII Protection** | <!-- Например: шифрование/токенизация, маскирование в логах, RBAC --> | <!-- Обоснование --> |
| **Asynchronous Reliability** | <!-- Например: Transactional Outbox + Dead Letter Queue --> | <!-- Обоснование --> |

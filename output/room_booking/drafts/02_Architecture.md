# System Architecture & Data Model: room_booking

> **Document Status:** Draft / Verified  
> **Version:** 1.0  
> **Related BRD:** `01_BRD.md`  
> **Related API Contract:** `03_OpenAPI.yaml`

---

## 1. Architectural Overview & Design Principles
### 1.1 Architectural Style & Patterns
Сервис `room_booking` построен на основе модульной монолитной архитектуры (Modular Monolith) с выделением асинхронных фоновых воркеров для интеграции со сторонними сервисами (Google Calendar / MS Exchange). 

Ключевые паттерны надежности и целостности данных:
- **Transactional Outbox:** Гарантированная доставка асинхронных доменных событий (создание, отмена, перевод в статус `NO_SHOW`) во внешние календари без риска потери данных при сетевых сбоях.
- **Idempotency-Key:** Защита от дублирования запросов на создание бронирования при повторной отправке сетевых пакетов клиентом (реализована через уникальный заголовок и таблицу идемпотентности).
- **Database-Level Exclusion Constraints (Optimistic/Pessimistic Concurrency Control):** Жесткое предотвращение конфликтов типа Double Booking на уровне СУБД PostgreSQL с помощью механизма GIST-индексов для пересечения временных интервалов (`EXCLUDE USING gist`).
- **State Machine (Конечный автомат):** Строгая валидация переходов жизненного цикла бронирования (`PENDING_CHECKIN`, `CONFIRMED`, `CANCELED`, `NO_SHOW`).
- **Timezone Awareness:** Хранение всех временных меток в каноническом формате `TIMESTAMP WITH TIME ZONE` (UTC) в БД с привязкой к идентификатору офиса (`office_id`) для корректного расчёта локального времени в Москве, Алматы и Ташкенте.

### 1.2 System Components & Responsibilities
| Component | Layer / Technology | Core Responsibility |
| :--- | :--- | :--- |
| **API Gateway / BFF** | API Layer / Nginx / FastAPI | Маршрутизация запросов, валидация JWT-токенов корпоративного SSO, rate-limiting, проверка заголовков `Idempotency-Key`. |
| **Core Domain Service** | Application Layer / Python 3.11+ / FastAPI | Ядро бизнес-логики, проверка лимитов (BR-01, BR-02, BR-03), управление состояниями бронирований, валидация VIP-доступа (L8+). |
| **Integration Adapter / Worker** | Infrastructure Layer / Celery / Asyncio Worker | Фоновая отправка `.ics` инвайтов и вебхуков во внешние календари, мониторинг тайм-аутов Check-in (`NO_SHOW`). |
| **Primary Database** | Storage Layer / PostgreSQL 15+ | Хранение доменных сущностей, обеспечение ACID-транзакций, предотвращение Double Booking через `EXCLUDE` constraints. |
| **Event Bus / Outbox Dispatcher** | Messaging Layer / Transactional Outbox Worker | Публикация событий из таблицы `domain_event_outbox` во внешние системы с гарантией доставки (At-Least-Once). |

---

## 2. Entity-Relationship Diagram (ERD)
> [!IMPORTANT]
> Все сущности, названия полей, типы данных, первичные (`PK`), внешние (`FK`) и уникальные (`UK`) ключи строго согласованы со спецификой домена бронирования переговорных комнат.

```mermaid
erDiagram
    OFFICE ||--o{ ROOM : "contains"
    EMPLOYEE ||--o{ BOOKING : "creates"
    ROOM ||--o{ BOOKING : "is_reserved_by"
    BOOKING ||--o{ CHECK_IN : "verified_by"
    BOOKING ||--o{ DOMAIN_EVENT_OUTBOX : "emits"
    EMPLOYEE ||--o{ IDEMPOTENCY_KEY : "owns"

    OFFICE {
        uuid id PK
        string code UK "MSK | ALA | TAS"
        string name
        string timezone "UTC+3 | UTC+5"
        boolean is_active
        timestamp created_at
    }

    ROOM {
        uuid id PK
        uuid office_id FK
        string name
        int capacity
        boolean is_vip "True for L8+ exclusive rooms"
        boolean is_active
        timestamp created_at
    }

    EMPLOYEE {
        uuid id PK
        string email UK
        string full_name
        string grade "L1-L7 | L8+"
        boolean is_active
        timestamp created_at
    }

    BOOKING {
        uuid id PK
        uuid room_id FK
        uuid employee_id FK
        string title
        timestamptz start_at
        timestamptz end_at
        string status "PENDING_CHECKIN | CONFIRMED | CANCELED | NO_SHOW"
        boolean is_recurring "False in MVP"
        int version "Optimistic locking version"
        timestamp created_at
        timestamp updated_at
    }

    CHECK_IN {
        uuid id PK
        uuid booking_id FK, UK
        string method "QR_CODE | WIFI"
        timestamp checked_in_at
    }

    IDEMPOTENCY_KEY {
        uuid id PK
        uuid employee_id FK
        string idempotency_key UK
        jsonb response_payload
        int response_status
        timestamp expires_at
        timestamp created_at
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
```mermaid
stateDiagram-v2
    [*] --> PENDING_CHECKIN: Booking created & saved
    PENDING_CHECKIN --> CONFIRMED: Check-in performed via QR / Wi-Fi within 15 min
    PENDING_CHECKIN --> NO_SHOW: 15 min elapsed without Check-in (Auto-cancel)
    PENDING_CHECKIN --> CANCELED: User manually cancels booking
    CONFIRMED --> CANCELED: User manually cancels before meeting ends
    CONFIRMED --> COMPLETED: Meeting time elapsed successfully
    NO_SHOW --> [*]
    CANCELED --> [*]
    COMPLETED --> [*]
```

---

## 4. Core Sequence Diagrams
### 4.1 Primary Happy Path & External Integration Flow
```mermaid
sequenceDiagram
    autonumber
    actor User as Employee / Client
    participant Gateway as API Gateway / BFF
    participant Service as Core Domain Service
    participant DB as PostgreSQL DB
    participant Outbox as Transactional Outbox

    User->>Gateway: POST /v1/bookings (Idempotency-Key, payload)
    Gateway->>Service: Forward validated request
    activate Service
    Service->>DB: Check Idempotency-Key cache
    alt Idempotency Key Exists
        DB-->>Service: Return cached response
        Service-->>Gateway: Cached HTTP response
        Gateway-->>User: Return previous result
    else New Request
        Service->>DB: Validate limits (2h, 14 days, VIP) & room availability (EXCLUDE constraint)
        alt Slot Busy or Limit Exceeded
            DB-->>Service: Constraint violation / Validation error
            Service-->>Gateway: 400 Bad Request / 409 Conflict
            Gateway-->>User: Error details
        else Slot Available
            Service->>DB: INSERT booking (status=PENDING_CHECKIN) & INSERT DOMAIN_EVENT_OUTBOX
            DB-->>Service: Commit transaction atomically
            Service->>DB: Save Idempotency-Key response
            Service-->>Gateway: 201 Created (BookingResponse)
            Gateway-->>User: 201 Created
        end
    end
    deactivate Service
```

### 4.2 Background Processing, Retry / Compensation & Event Delivery Flow
```mermaid
sequenceDiagram
    autonumber
    participant Worker as Background Celery Worker
    participant DB as PostgreSQL DB
    participant Outbox as Outbox Dispatcher
    participant ExtCalendar as Google Calendar / MS Exchange

    loop Every 1 Minute (No-Show Check)
        Worker->>DB: SELECT * FROM bookings WHERE status = 'PENDING_CHECKIN' AND start_at + 15 min <= NOW()
        DB-->>Worker: List of expired bookings
        Worker->>DB: UPDATE bookings SET status = 'NO_SHOW' WHERE id IN (...) + INSERT Outbox event
        DB-->>Worker: Commit updates & free room slots
    end

    loop Outbox Dispatcher (Every 5 seconds)
        Outbox->>DB: SELECT * FROM domain_event_outbox WHERE delivery_status = 'PENDING' FOR UPDATE SKIP LOCKED
        DB-->>Outbox: Pending events batch
        Outbox->>ExtCalendar: POST /webhook / Send .ics invite (with HMAC signature)
        alt Delivery Succeeded
            ExtCalendar-->>Outbox: 200 OK
            Outbox->>DB: UPDATE domain_event_outbox SET delivery_status = 'DELIVERED'
        else Delivery Failed
            ExtCalendar-->>Outbox: 5xx Error / Timeout
            Outbox->>DB: UPDATE domain_event_outbox SET retry_count = retry_count + 1 (if max < 5 else DEAD_LETTER)
        end
    end
```

---

## 5. Architectural & Data Consistency Decisions
| Decision Area | Chosen Approach | Rationale |
| :--- | :--- | :--- |
| **Data Types & Precision** | `TIMESTAMP WITH TIME ZONE` (UTC) для всех дат, `UUID v4` для первичных ключей. | Исключает ошибки рассинхронизации таймзон между офисами в Москве, Алматы и Ташкенте. Уникальные идентификаторы безопасны для распределенных систем. |
| **Concurrency & Race Conditions** | PostgreSQL `EXCLUDE USING gist` constraint с оператором перекрытия интервалов (`&&`). | Гарантирует на уровне БД физическую невозможность Double Booking даже при абсолютно одновременных запросах на один и тот же слот. |
| **Idempotency & Retries** | Заголовок `Idempotency-Key` + таблица `idempotency_keys` с фиксацией ответов и TTL 24 часа. | Защищает от повторного создания бронирований при нестабильном сетевом соединении клиента или повторных кликах. |
| **Security & PII Protection** | JWT авторизация через корпоративный OIDC/SSO, передача данных исключительно по HTTPS (TLS 1.3), маскирование персональных данных. | Соответствие стандартам ИБ компании, предотвращение несанкционированного доступа к системе бронирования. |
| **Asynchronous Reliability** | Transactional Outbox паттерн с фоновым воркером и полингом `FOR UPDATE SKIP LOCKED`. | Обеспечивает надежную доставку событий во внешние календари (Google/Exchange) без риска потери данных при падении сервиса. |

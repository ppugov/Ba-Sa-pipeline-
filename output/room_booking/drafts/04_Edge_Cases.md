# Edge Cases, Failure Modes & BDD Test Scenarios: room_booking

> **Document Status:** Draft / Verified  
> **Version:** 1.0  
> **Related Documents:** `01_BRD.md`, `02_Architecture.md`, `03_OpenAPI.yaml`

---

## 1. Failure Mode & Edge Case Matrix
| Case ID | Category | Trigger / Condition | Expected System Behavior | HTTP / Event Outcome |
| :--- | :--- | :--- | :--- | :--- |
| **EC-01** | Idempotency & Retries | Клиент отправляет повторный `POST /bookings` с тем же `Idempotency-Key` из-за обрыва сети | Система не создает дублирующую бронь в БД, возвращает кэшированный ответ (или `409 Conflict`, если первый запрос еще в обработке) | `201 Created` (Cached) или `409 Conflict` |
| **EC-02** | Concurrency & Race Conditions | Два пользователя одновременно отправляют `POST /bookings` на один и тот же слот комнаты «Эльбрус» | Срабатывает ограничение PostgreSQL `EXCLUDE USING gist` (`&&`). Первый запрос фиксирует бронь, второй получает ошибку пересечения интервалов | `201 Created` для первого, `409 Conflict` (`ROOM_SLOT_ALREADY_BOOKED`) для второго |
| **EC-03** | Boundary / Time / Math Logic | Пользователь пытается забронировать обычную комнату на 121 минуту (нарушение BR-01) или через 15 дней (нарушение BR-02) | Бэкенд валидирует входящие даты `start_at` и `end_at` и отклоняет операцию до обращения к БД | `422 Unprocessable Entity` (`EXCEEDS_MAX_DURATION` / `EXCEEDS_BOOKING_HORIZON`) |
| **EC-04** | External Dependency Timeout | Внешний календарь (Google Calendar / MS Exchange) возвращает таймаут (`504`) или недоступен при отправке `.ics` инвайта | Сервис сохраняет бронирование в БД (мастер данных не блокируется), а событие в outbox остается в статусе `PENDING` для последующего ретрая | `201 Created` для клиента (асинхронная доставка инвайта) |
| **EC-05** | Invalid State Transition | Попытка вызвать `POST /check-ins` для бронирования со статусом `CANCELED` или `NO_SHOW` | Доменный конечный автомат отклоняет запрос, так как переход из терминального или неактивного статуса не разрешен | `422 Unprocessable Entity` (`INVALID_STATE_TRANSITION`) |
| **EC-06** | Event / Webhook Delivery Failure | Внешний вебхук-консьюмер возвращает `500 Internal Server Error` | Outbox Dispatcher выполняет ретрай с экспоненциальным backoff. При достижении `max_retry_count = 5` статус меняется на `DEAD_LETTER` с алертом в Prometheus | Фоновый процес (`DEAD_LETTER` после ретраев) |

---

## 2. Gherkin Acceptance Scenarios (BDD)
> [!IMPORTANT]
> Все сценарии Gherkin используют точные пути эндпоинтов, названия полей, статусы и коды ошибок из `03_OpenAPI.yaml` и покрывают бизнес-правила `BR-*` из `01_BRD.md`.

### Scenario 1: Idempotent Resource Creation (Network Retry Safety)
```gherkin
Feature: Idempotent API Execution

  Scenario: Client retries POST request with identical Idempotency-Key after network timeout
    Given a valid actor and input payload for endpoint "/bookings"
    And an initial POST request with header Idempotency-Key "550e8400-e29b-41d4-a716-446655440000" succeeded with HTTP 201
    When the client sends an identical POST request with header Idempotency-Key "550e8400-e29b-41d4-a716-446655440000"
    Then the system MUST NOT execute duplicate side effects or external calls
    And the system MUST return HTTP 201 with the exact same resource "id" and payload
```

### Scenario 2: Core Business Rule & State Transition Happy Path
```gherkin
Feature: Core Lifecycle State Transition

  Scenario: Primary domain workflow transitions through valid states and emits outbox event
    Given an employee creates a new booking for a meeting room via POST "/bookings"
    When the booking is successfully saved in the database with status "PENDING_CHECKIN"
    And a domain event "BOOKING_CREATED" is recorded in the "domain_event_outbox" table
    And the organizer performs Check-in within 15 minutes via POST "/check-ins"
    Then the booking status MUST transition to "CONFIRMED"
    And a corresponding CheckInResponse with method "QR_CODE" or "WIFI" MUST be returned with HTTP 201
```

### Scenario 3: Boundary Condition / Domain Invariant Enforcement
```gherkin
Feature: Domain Invariant Guardrails

  Scenario: Reject operation that violates business limit or invariant (BR-*)
    Given an ordinary employee attempting to book a non-VIP room
    When the employee submits a booking request via POST "/bookings" with duration of 150 minutes (exceeding BR-01 limit of 120 minutes)
    Then the system MUST reject the request with HTTP 422 Unprocessable Entity
    And the ProblemDetails response MUST contain the specific "error_code" indicating max duration violation
```

### Scenario 4: Concurrency & Race Condition Protection
```gherkin
Feature: Concurrent Modification Protection

  Scenario: Simultaneous conflicting operations on the same aggregate are serialized safely
    Given two users simultaneously send POST "/bookings" for the exact same room and overlapping time slot
    When the database executes the exclusion constraint check (`EXCLUDE USING gist`)
    Then database locking MUST prevent inconsistent state and Double Booking
    And the first transaction MUST succeed with HTTP 201 Created
    And the second concurrent transaction MUST be rejected with HTTP 409 Conflict
```

### Scenario 5: External System Timeout & Reconciliation
```gherkin
Feature: External Integration Resilience

  Scenario: External provider times out without explicit confirmation
    Given a pending outbox event for a booking notification to Google Calendar / MS Exchange
    When the external webhook call times out after the configured SLA threshold (504 Gateway Timeout)
    Then the system MUST keep the booking operation successfully committed in the database (Single Source of Truth)
    And the outbox event delivery status MUST remain "PENDING" with incremented retry count for subsequent asynchronous retry
```

---

## 3. Operational Policies, Timeouts & Retry Matrix
| Parameter | Configured Value | Architectural Notes |
| :--- | :--- | :--- |
| **External API Timeout** | `5000 ms` (Read / Connect timeout) | Таймаут при отправке вебхуков и `.ics` инвайтов во внешние системы календарей. |
| **Retry Schedule / Backoff** | Экспоненциальный: `10s`, `30s`, `60s`, `120s`, `300s` | Политика повторных попыток для сбоев сетевого уровня и `5xx` ошибок интеграций. |
| **Outbox / Event Backoff** | `1m`, `5m`, `30m`, `2h`, `6h`, `24h` | Перевод записи outbox в статус `DEAD_LETTER` после 6 неудачных попыток доставки. |
| **Idempotency Key TTL** | `24 hours` | Хранение уникальных ключей идемпотентности в таблице PostgreSQL с автоматической очисткой по TTL. |

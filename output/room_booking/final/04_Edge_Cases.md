# Edge Cases, Failure Modes & BDD Test Scenarios: Smart Room Booking (`room_booking`)

> **Document Status:** Verified (Orchestrator Approved)  
> **Version:** 1.0  
> **Related Documents:** `01_BRD.md`, `02_Architecture.md`, `03_OpenAPI.yaml`  
> **Pipeline Pattern:** Orchestrator-Worker (Worker: `gemini-3.5-flash-lite`, Critic: `Gemini 4 Argon`)

---

## 1. Failure Mode & Edge Case Matrix
| Case ID | Category | Trigger / Condition | Expected System Behavior | HTTP / Event Outcome |
| :--- | :--- | :--- | :--- | :--- |
| **EC-01** | Concurrency & Double Booking | Два сотрудника в одну миллисекунду отправляют `POST /v1/bookings` на комнату «Эльбрус» на слот `14:00–15:00` | Констрейнт PostgreSQL `EXCLUDE USING gist` (`tstzrange`) фиксирует первую транзакцию и атомарно отклоняет вторую с кодом SQLSTATE `23P01` | 1-й: `201 Created`<br>2-й: `409 Conflict` (`ROOM_SLOT_OVERLAP`) |
| **EC-02** | Adjacent Slot Boundary (`'[)'`) | Бронь №1 заканчивается в `15:00:00Z`, сотрудник создает Бронь №2 в той же комнате с началом ровно в `15:00:00Z` | Полуинтервал `tstzrange(start_at, end_at, '[)')` не считает границу `15:00` пересечением; вторая бронь создается успешно | `201 Created` (Без ложных срабатываний) |
| **EC-03** | Ghost Booking Auto-Release (`BR-04`) | Организатор не выполнил Check-in в течение 15 минут после `start_at` | Фоновый крон-воркер переводит бронь из `PENDING_CHECKIN` в `NO_SHOW`, освобождает слот в GIST-индексе и публикует событие отмены в календарь | Статус `NO_SHOW`, событие `booking.no_show` |
| **EC-04** | Anti-Spoofing QR Check-in (`OQ-02`) | Сотрудник фотографирует QR-код на планшете утром и пытается отправить его из дома через 10 минут | Бэкенд проверяет 30-секундное окно TOTP для `qr_token` и отклоняет устаревший токен | `422 Unprocessable Entity` (`INVALID_QR_TOKEN`) |
| **EC-05** | VIP Room Governance (`BR-02`) | Сотрудник грейда `L5` (`is_vip_authorized=false`) пытается забронировать VIP-переговорную (`is_vip=true`) ИЛИ обычный сотрудник бронирует обычную комнату на 180 минут (`BR-01`) | Доменный сервис блокирует запрос до записи в БД | `422 Unprocessable Entity` (`VIP_ACCESS_REQUIRED` / `EXCEEDS_MAX_DURATION`) |
| **EC-06** | Cross-Timezone Booking (`OQ-03`) | Сотрудник из Москвы (`Europe/Moscow`, UTC+3) бронирует переговорную в Алматы (`Asia/Almaty`, UTC+5) на `10:00` по Алматы | Бэкенд нормализует время в UTC (`05:00:00Z`) и возвращает в `BookingResponse` `office_timezone: "Asia/Almaty"` | Детерминированная запись в UTC без смещения на 2 часа |
| **EC-07** | Idempotency Network Retry | Мобильное приложение при слабом Wi-Fi дважды отправляет `POST /v1/bookings` с одним и тем же `Idempotency-Key` | Повторный запрос возвращает сохраненный `201 Created` ответ первой транзакции (не вызывая ошибку `409 ROOM_SLOT_OVERLAP` сам с собой!) | `201 Created` (Idempotent replay) |
| **EC-08** | Calendar Sync Outage | API Google Calendar / MS Exchange возвращает `503 Service Unavailable` или таймаут (> 5000 мс) | Бронирование внутри компании остается валидным; запись в `DOMAIN_EVENT_OUTBOX` ретраится с экспоненциальным backoff до `DELIVERED` или `DEAD_LETTER` | Внутренний API: `201 Created`, Outbox: `PENDING -> DELIVERED` |

---

## 2. Gherkin Acceptance Scenarios (BDD)
> [!IMPORTANT]
> Все сценарии используют точные эндпоинты, поля, enum-статусы и коды ошибок из `03_OpenAPI.yaml`.

### Scenario 1: Concurrent Double Booking Prevention via PostgreSQL GIST Exclusion
```gherkin
Feature: Prevention of Double Booking on Overlapping Time Slots

  Scenario: Two employees simultaneously book the same room for overlapping times
    Given an active standard room "room_elbrus" in office "MSK" ("Europe/Moscow")
    When employee "emp_01" sends "POST /v1/bookings" for "room_elbrus" from "2026-11-16T11:00:00Z" to "2026-11-16T12:00:00Z"
    And employee "emp_02" concurrently sends "POST /v1/bookings" for "room_elbrus" from "2026-11-16T11:30:00Z" to "2026-11-16T12:30:00Z"
    Then the first committed transaction MUST succeed with HTTP 201 Created and status "PENDING_CHECKIN"
    And the second transaction MUST be rejected by the database exclusion constraint with HTTP 409 Conflict
    And the ProblemDetails response MUST contain error_code "ROOM_SLOT_OVERLAP"
```

### Scenario 2: Ghost Booking Auto-Cancellation (NO_SHOW after 15 Minutes) & Slot Reuse
```gherkin
Feature: Automatic Release of Unclaimed Ghost Bookings (BR-04)

  Scenario: Booking without check-in transitions to NO_SHOW after 15 minutes and frees the room
    Given a booking "book_100" for room "room_baikal" with status "PENDING_CHECKIN"
    And "start_at" is "2026-11-16T09:00:00Z" and "end_at" is "2026-11-16T10:00:00Z"
    When the system clock reaches "2026-11-16T09:15:01Z" and no Check-in was performed
    And the No-Show Scheduler executes its periodic sweep
    Then the status of booking "book_100" MUST transition from "PENDING_CHECKIN" to "NO_SHOW"
    And a webhook event "booking.no_show" MUST be enqueued in "DOMAIN_EVENT_OUTBOX"
    When another employee "emp_03" sends "POST /v1/bookings" for "room_baikal" from "2026-11-16T09:20:00Z" to "2026-11-16T10:00:00Z"
    Then the new booking request MUST succeed with HTTP 201 Created
```

### Scenario 3: Verified Check-in via Rotating 30s QR Code vs Expired QR Token
```gherkin
Feature: Anti-Spoofing Check-in Verification (OQ-02)

  Scenario: Organizer checks in successfully with valid 30-second tablet QR token
    Given a booking "book_200" in status "PENDING_CHECKIN" with "start_at" at "2026-11-16T14:00:00Z"
    And the current time is "2026-11-16T14:03:00Z" (within the [-10m, +15m] check-in window)
    When the organizer sends "POST /v1/check-ins" with method "QR_CODE" and valid current qr_token "849201"
    Then the response status MUST be HTTP 201 Created
    And the "booking_status" in CheckInResponse MUST be "CONFIRMED"
    And a webhook event "booking.confirmed" MUST be recorded in "DOMAIN_EVENT_OUTBOX"

  Scenario: Reject check-in with photographed stale QR token
    Given a booking "book_201" in status "PENDING_CHECKIN" within the check-in window
    When the organizer sends "POST /v1/check-ins" with method "QR_CODE" and a qr_token expired 5 minutes ago
    Then the system MUST reject the request with HTTP 422 Unprocessable Entity
    And the ProblemDetails error_code MUST be "INVALID_QR_TOKEN"
    And the booking status MUST remain "PENDING_CHECKIN"
```

### Scenario 4: Standard Employee Limits vs VIP Room Privileges (BR-01 & BR-02)
```gherkin
Feature: Role-Based Booking Limits and VIP Room Protection

  Scenario: Standard employee cannot book a standard room for more than 120 minutes
    Given a standard employee "emp_std" with grade "L5" and "is_vip_authorized: false"
    And a standard room "room_std" with "is_vip: false"
    When "emp_std" sends "POST /v1/bookings" for 180 minutes duration
    Then the system MUST reject the request with HTTP 422 Unprocessable Entity
    And the ProblemDetails error_code MUST be "EXCEEDS_MAX_DURATION"

  Scenario: VIP Assistant books a VIP room for a 6-hour Board of Directors meeting
    Given a VIP assistant "emp_denis" with grade "L5" and "is_vip_authorized: true"
    And a VIP room "room_board_vip" with "is_vip: true"
    When "emp_denis" sends "POST /v1/bookings" for "room_board_vip" for 360 minutes (6 hours)
    Then the system MUST create the booking with HTTP 201 Created and status "PENDING_CHECKIN"
```

### Scenario 5: Idempotent Retry Does Not Trigger Self-Conflict
```gherkin
Feature: Idempotent Booking Creation

  Scenario: Mobile client retries POST /v1/bookings with the same Idempotency-Key after network drop
    Given an initial "POST /v1/bookings" request with header "Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000" succeeded with HTTP 201
    When the client resends the identical "POST /v1/bookings" request with header "Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000"
    Then the system MUST return the cached HTTP 201 Created response with the exact same booking "id"
    And the system MUST NOT return HTTP 409 "ROOM_SLOT_OVERLAP"
```

---

## 3. Operational Policies, Timeouts & Retry Matrix
| Parameter | Configured Value | Architectural Notes |
| :--- | :--- | :--- |
| **Standard Max Duration (`BR-01`)** | `120 minutes` (2 hours) | Для всех комнат `is_vip = false` и сотрудников без `is_vip_authorized` |
| **Standard Booking Horizon (`BR-01`)** | `14 calendar days` | Защита от выкупа переговорных комнат на месяцы вперед |
| **VIP Max Duration (`BR-02`)** | `480 minutes` (8 hours) | Только для `grade = 'L8+'` или `is_vip_authorized = true` в комнатах `is_vip = true` |
| **Check-in Window (`BR-04`)** | `[start_at - 10m, start_at + 15m]` | Если к `start_at + 15m` нет Check-in -> автоматический переход в `NO_SHOW` |
| **Tablet TOTP QR Rotation** | `30 seconds` (drift tolerance ±1 step) | HMAC-SHA256 TOTP без необходимости сетевого запроса от планшета |
| **Calendar Outbox Backoff** | `10s, 1m, 5m, 30m, 2h, 6h` | Перевод в `DEAD_LETTER` после 6 ошибок с отправкой алерта в мониторинг |
| **Idempotency Key TTL** | `24 hours` | Уникальный индекс по `idempotency_key` в PostgreSQL |

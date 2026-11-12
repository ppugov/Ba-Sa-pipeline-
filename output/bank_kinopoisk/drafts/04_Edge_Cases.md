# Edge Cases, Failure Modes & BDD Test Scenarios: bank_kinopoisk

> **Document Status:** Verified & Refined  
> **Version:** 1.0  
> **Related Documents:** `01_BRD.md`, `02_Architecture.md`, `03_OpenAPI.yaml`

---

## 1. Failure Mode & Edge Case Matrix
| Case ID | Category | Trigger / Condition | Expected System Behavior | HTTP / Event Outcome |
| :--- | :--- | :--- | :--- | :--- |
| **EC-01** | Idempotency & Retries | Клиент отправляет повторный `POST /hybrid-payments` или `PUT /subscriptions/status` с тем же `Idempotency-Key` из-за обрыва сети. | Система проверяет таблицу идемпотентности: не дублирует вызовы в Сервис Лояльности или Платежный шлюз, а возвращает кэшированный ответ. | `201 Created` / `200 OK` (из кэша) или `409 Conflict` (если первый запрос в статусе `INITIATED` еще обрабатывается). |
| **EC-02** | Concurrency & Race Conditions | Два параллельных запроса на оплату (или одновременная обработка вебхука возврата и списания) для одного `ecosystem_user_id`. | Оптимистичная блокировка (`version`) на уровне сессии или пессимистичный лок (`SELECT ... FOR UPDATE`) сериализуют операции, предотвращая гонку данных (lost updates). | Второй запрос получает `409 Conflict` (Optimistic Locking Failure). |
| **EC-03** | Boundary / Time / Math Logic | Попытка гибридной оплаты контента со списанием 100% бонусных баллов (`paid_points_minor == total_amount_minor`), что нарушает инвариант `BR-04` (максимум 99% баллами, минимум 1% деньгами). | Валидатор платежного шлюза отклоняет запрос до обращения к внешним системам. | `422 Unprocessable Entity` (`MAX_POINTS_LIMIT_EXCEEDED`). |
| **EC-04** | External Dependency Timeout | Платежный шлюз Банка (`Payment Gateway`) или Сервис Лояльности не отвечает в течение установленного таймаута (5000 ms). | Транзакция переводится в статус `FAILED` или `COMPENSATED`, запускается компенсаторный сценарий (`RELEASE` баллов при необходимости), исключая «зависание» балансов. | `504 Gateway Timeout` / Возврат проблемы в формате RFC 7807 (`ProblemDetails`). |
| **EC-05** | Invalid State Transition | Попытка вызвать компенсацию (`POST /hybrid-payments/{payment_id}/cancel`) для платежной сессии, которая уже находится в финальном статусе `COMMITTED` или `COMPENSATED`. | Доменная модель отклоняет запрос как недопустимый переход машины состояний Saga. | `422 Unprocessable Entity` (`INVALID_STATE_TRANSITION`). |
| **EC-06** | Event / Webhook Delivery Failure | Биллинг Кинопоиска возвращает `5xx` при доставке асинхронного события `TurnoverCalculatedEvent` через Outbox-диспатчер. | Событие остается в таблице `DOMAIN_EVENT_OUTBOX` со статусом `PENDING`. Диспатчер выполняет ретрай с экспоненциальным backoff (1m, 5m, 30m). При исчерпании лимита (5 попыток) переводит в `DEAD_LETTER` с алертом в Prometheus/Grafana. | Гарантия доставки `At-Least-Once` / `DEAD_LETTER`. |

---

## 2. Gherkin Acceptance Scenarios (BDD)
> [!IMPORTANT]
> Все сценарии Gherkin используют точные пути эндпоинтов, названия полей, статусы и коды ошибок из `03_OpenAPI.yaml` и покрывают бизнес-правила `BR-*` из `01_BRD.md`.

### Scenario 1: Idempotent Resource Creation (Network Retry Safety)
```gherkin
Feature: Idempotent API Execution

  Scenario: Client retries POST request with identical Idempotency-Key after network timeout
    Given a valid actor with ecosystem_user_id "usr-uuid-1234" and item_id "item-uuid-5678"
    And an initial POST request to "/hybrid-payments" with header Idempotency-Key "550e8400-e29b-41d4-a716-446655440000" succeeded with HTTP 201
    When the client sends an identical POST request to "/hybrid-payments" with header Idempotency-Key "550e8400-e29b-41d4-a716-446655440000"
    Then the system MUST NOT execute duplicate loyalty holds or bank card debits
    And the system MUST return HTTP 201 with the exact same resource "id" and payload from the cache
```

### Scenario 2: Core Business Rule & State Transition Happy Path (Saga Hybrid Payment)
```gherkin
Feature: Core Lifecycle State Transition (Hybrid Payment Saga)

  Scenario: Successful hybrid payment flows through initiation, points hold, funds debit, and commit
    Given an active ecosystem user "usr-uuid-1234" with 1000 loyalty points and valid bank card
    When a POST request is sent to "/hybrid-payments" with total_amount_minor: 50000, paid_points_minor: 20000, paid_money_minor: 30000
    Then the payment session status MUST transition through INITIATED, POINTS_HELD, FUNDS_DEBITED, to COMMITTED
    And the user MUST be granted content access (entitlement)
    And a corresponding domain event "HybridPaymentCommitted" MUST be recorded in the DOMAIN_EVENT_OUTBOX table
```

### Scenario 3: Boundary Condition / Domain Invariant Enforcement (BR-04 Max Points Limit)
```gherkin
Feature: Domain Invariant Guardrails

  Scenario: Reject hybrid payment attempt exceeding 99% points limit (BR-04)
    Given an ecosystem user requesting a movie purchase of total_amount_minor: 10000 (100 RUB)
    When the user attempts to pay with paid_points_minor: 10000 (100% points) and paid_money_minor: 0
    Then the system MUST reject the request with HTTP 422 Unprocessable Entity
    And the ProblemDetails response MUST contain error_code "MAX_POINTS_LIMIT_EXCEEDED"
    And no funds or points must be held or debited
```

### Scenario 4: Concurrency & Race Condition Protection (Optimistic Locking)
```gherkin
Feature: Concurrent Modification Protection

  Scenario: Simultaneous conflicting operations on the same payment session are serialized safely
    Given an active hybrid payment session with id "pay-uuid-9999" and version 1
    When two concurrent requests attempt to modify or cancel the payment session simultaneously
    Then database optimistic locking via version check MUST allow only the first transaction to succeed
    And the second concurrent request MUST be rejected with HTTP 409 Conflict
    And the system MUST preserve data integrity without lost updates
```

### Scenario 5: External System Timeout & Compensation (Saga Rollback)
```gherkin
Feature: External Integration Resilience

  Scenario: Payment Gateway times out during money debit, triggering compensation (RELEASE points)
    Given a hybrid payment session where loyalty points (paid_points_minor: 30000) have been successfully held (POINTS_HELD)
    When the Payment Gateway call for debiting money (paid_money_minor: 20000) times out or returns 504 Gateway Timeout
    Then the system MUST update the payment session state to COMPENSATED
    And the system MUST automatically invoke the Loyalty Service release endpoint (`POST /v1/loyalty/release`) to unfreeze the held points
    And the system MUST return HTTP 422 Unprocessable Entity with details about the payment failure
```

---

## 3. Operational Policies, Timeouts & Retry Matrix
| Parameter | Configured Value | Architectural Notes |
| :--- | :--- | :--- |
| **External API Timeout** | 5,000 ms (Connect: 2,000 ms, Read: 3,000 ms) | Жесткие таймшоты для вызовов внешнего Платежного шлюза и Сервиса Лояльности во избежание блокировки потоков в пятничный Prime Time. |
| **Retry Schedule / Backoff** | 3 попытки; интервалы: 500ms, 1000ms, 2000ms (с jitter) | Применяется только для идемпотентных запросов чтения и безопасных операций проверки статусов. Финансовые списания при сетевых таймаутах требуют предварительной реконсиляции (reconciliation). |
| **Outbox / Event Backoff** | 1m, 5m, 30m, 2h, 6h, 24h (Max 5 retries) | Экспоненциальный backoff для доставки асинхронных событий через Kafka / Webhooks. Перевод в `DEAD_LETTER` с генерацией критического алерта в Grafana при исчерпании лимита. |
| **Idempotency Key TTL** | 24 hours | Хранение ключей идемпотентности в выделенном хранилище (Redis / PostgreSQL с уникальным индексом) для защиты от повторных отправок форм и сессий оплат. |

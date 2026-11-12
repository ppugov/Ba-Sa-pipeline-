# Edge Cases, Failure Modes & BDD Test Scenarios: {{PROJECT_NAME}}

> **Document Status:** Draft / Verified  
> **Version:** 1.0  
> **Related Documents:** `01_BRD.md`, `02_Architecture.md`, `03_OpenAPI.yaml`

---

## 1. Failure Mode & Edge Case Matrix
| Case ID | Category | Trigger / Condition | Expected System Behavior | HTTP / Event Outcome |
| :--- | :--- | :--- | :--- | :--- |
| **EC-01** | Idempotency & Retries | Клиент отправляет повторный `POST` с тем же `Idempotency-Key` из-за обрыва сети | Система не дублирует бизнес-операцию и возвращает кэшированный ответ (или `409 Conflict`, если первый запрос еще выполняется) | `200/201` (Cached) или `409 Conflict` |
| **EC-02** | Concurrency & Race Conditions | Два параллельных запроса (или пользователь и фоновый воркер) одновременно меняют состояние одной сущности | Блокировка (`SELECT ... FOR UPDATE` или `version` optimistic lock) сериализует изменения; второй конфликтный запрос отклоняется | Детерминированный статус / `409 Conflict` |
| **EC-03** | Boundary / Time / Math Logic | Граничные значения бизнес-правил из `01_BRD.md` (даты, лимиты, квоты, округления, таймзоны) | Строгое соблюдение доменного инварианта `BR-*` без потери точности | Валидный расчет или `422 Unprocessable Entity` |
| **EC-04** | External Dependency Timeout | Внешний сервис или партнерский API не отвечает в пределах SLA таймаута (`504` / socket timeout) | Запрет «слепого» дублирования; перевод в промежуточный статус и сверка состояния (reconciliation) перед повтором | Защита от дублей и потери данных |
| **EC-05** | Invalid State Transition | Попытка перевести сущность в статус, запрещенный конечным автоматом (State Machine в `02_Architecture.md`) | Запрос отклоняется на уровне доменной модели до обращения к внешним системам | `422 Unprocessable Entity` (`INVALID_STATE_TRANSITION`) |
| **EC-06** | Event / Webhook Delivery Failure | Сервис-потребитель событий возвращает `5xx` или недоступен | Событие остается в `OUTBOX`, ретраится с экспоненциальным backoff + jitter; после исчерпания лимита переходит в `DEAD_LETTER` с алертом | Гарантия доставки `At-Least-Once` |

---

## 2. Gherkin Acceptance Scenarios (BDD)
> [!IMPORTANT]
> Все сценарии Gherkin должны использовать точные пути эндпоинтов, названия полей, статусы и коды ошибок из `03_OpenAPI.yaml` и покрывать бизнес-правила `BR-*` из `01_BRD.md`.

### Scenario 1: Idempotent Resource Creation (Network Retry Safety)
```gherkin
Feature: Idempotent API Execution

  Scenario: Client retries POST request with identical Idempotency-Key after network timeout
    Given a valid actor and input payload
    And an initial POST request with header Idempotency-Key "550e8400-e29b-41d4-a716-446655440000" succeeded with HTTP 201
    When the client sends an identical POST request with header Idempotency-Key "550e8400-e29b-41d4-a716-446655440000"
    Then the system MUST NOT execute duplicate side effects or external calls
    And the system MUST return HTTP 201 with the exact same resource "id" and payload
```

### Scenario 2: Core Business Rule & State Transition Happy Path
```gherkin
Feature: Core Lifecycle State Transition

  Scenario: Primary domain workflow transitions through valid states and emits outbox event
    Given an existing resource in initial state
    When the trigger action is executed with valid parameters
    Then the resource status MUST transition to the target state defined in the State Machine
    And a corresponding domain event MUST be recorded in the outbox table
```

### Scenario 3: Boundary Condition / Domain Invariant Enforcement
```gherkin
Feature: Domain Invariant Guardrails

  Scenario: Reject operation that violates business limit or invariant (BR-*)
    Given an existing resource with specific boundary state
    When an actor requests an operation that violates the domain invariant
    Then the system MUST reject the request with HTTP 422 Unprocessable Entity
    And the ProblemDetails response MUST contain the specific "error_code"
```

### Scenario 4: Concurrency & Race Condition Protection
```gherkin
Feature: Concurrent Modification Protection

  Scenario: Simultaneous conflicting operations on the same aggregate are serialized safely
    Given an active resource being processed by a background worker or concurrent request
    When a conflicting state-change request arrives at the same millisecond
    Then database locking or optimistic version check MUST prevent inconsistent state
    And the system MUST preserve data integrity without lost updates
```

### Scenario 5: External System Timeout & Reconciliation
```gherkin
Feature: External Integration Resilience

  Scenario: External provider times out without explicit confirmation
    Given a pending operation requiring external system confirmation
    When the external call times out after the configured SLA threshold
    Then the system MUST keep the operation in a safe intermediate state
    And the system MUST reconcile status with the external provider before attempting a retry
```

---

## 3. Operational Policies, Timeouts & Retry Matrix
| Parameter | Configured Value | Architectural Notes |
| :--- | :--- | :--- |
| **External API Timeout** | <!-- Например: 5,000 - 10,000 ms --> | <!-- Политика обработки таймаута --> |
| **Retry Schedule / Backoff** | <!-- Сетка ретраев --> | <!-- Условия прекращения повторов --> |
| **Outbox / Event Backoff** | <!-- Например: 1m, 5m, 30m, 2h, 6h, 24h --> | <!-- Перевод в DEAD_LETTER при исчерпании --> |
| **Idempotency Key TTL** | <!-- Например: 24 hours --> | <!-- Хранение в БД / Redis с уникальным индексом --> |

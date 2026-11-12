# Edge Cases, Failure Modes & BDD Test Scenarios: Payment Gateway Core

> **Document Status:** Verified (Orchestrator Approved)  
> **Version:** 1.0  
> **Related Documents:** `01_BRD.md`, `02_Architecture.md`, `03_OpenAPI.yaml`  
> **Pipeline Pattern:** Orchestrator-Worker (Critic: `Gemini 4 Argon`)

---

## 1. Failure Mode & Edge Case Matrix
| Case ID | Category | Trigger / Condition | Expected System Behavior | HTTP / Event Outcome |
| :--- | :--- | :--- | :--- | :--- |
| **EC-01** | Concurrency & Idempotency | Параллельный дублирующий запрос `POST /subscriptions` с тем же `Idempotency-Key` при первой операции in-flight | Блокировка повторного списания в PSP; возврат ошибки конфликта in-flight либо кэшированного ответа первой транзакции | `409 Conflict` (`IDEMPOTENCY_IN_FLIGHT`) или `201 Created` (кэш) |
| **EC-02** | Dunning: Soft Decline | Неуспешное автопродление с ошибкой `insufficient_funds` (нехватка средств) | Инвойс переходит в `OPEN`, подписка переводится в `PAST_DUE`, доступ к сервису сохраняется, активируется таймер `grace_period_ends_at = NOW + 7d`, назначается ретрай на День +1 | `invoice.payment_failed` (`status: PAST_DUE`) |
| **EC-03** | Dunning: Hard Decline | Автопродление отклонено с кодом `card_stolen` / `card_expired` / `invalid_account` | Платежная попытка помечается `FAILED_HARD`, подписка немедленно переводится в `SUSPENDED`, любые будущие ретраи отменяются, клиенту отправляется запрос на обновление метода оплаты | `subscription.suspended` |
| **EC-04** | Calendar Arithmetic | Оформление подписки 31 января с интервалом `MONTHLY` | Следующая дата списания сдвигается на последний день февраля (28 или 29 февраля), но сохраняет якорь 31 числа для марта (31 марта) и апреля (30 апреля) | `next_billing_at = 2027-02-28T...` -> `2027-03-31T...` |
| **EC-05** | PSP Network Timeout | Сетевой таймаут ответа от эквайера (10 000 мс) или ошибка `504 Gateway Timeout` | Запрет на повторный запрос списания вслепую; статус попытки переводится в `PENDING`, запускается reconcile-воркер для опроса статуса по `Idempotency-Key` | Предотвращение двойного списания (Double-Charge Prevention) |
| **EC-06** | Concurrency Race Condition | Пользователь нажимает «Отменить подписку» в момент, когда крон-воркер уже захватил строку подписки для списания | Пессимистический лок `SELECT ... FOR UPDATE` упорядочивает вызовы. Если списание уже зафиксировано в PSP, отмена применяется к следующему периоду (`cancel_at_period_end=true`), либо инициируется возврат по правилам сервиса | Детерминированное состояние без зависания транзакций |
| **EC-07** | Refund Balance Guardrail | Оператор пытается оформить частичный возврат на сумму, превышающую невозвращенный остаток инвойса (`amount_refunded_minor + new_refund > amount_minor`) | Запрос отклоняется без обращения к шлюзу PSP, транзакция возврата не создается | `422 Unprocessable Entity` (`REFUND_EXCEEDS_PAYMENT_BALANCE`) |
| **EC-08** | Webhook Network Outage | Монолит (Merchant Backend) временно недоступен (`503`, тайм-аут, сбой сети) | Событие сохранено в `WEBHOOK_OUTBOX`. Воркер осуществляет повторную отправку по схеме: 1m, 5m, 30m, 2h, 6h, 24h. При исчерпании — статус `DEAD_LETTER` с алертом | Гарантия доставки `At-Least-Once` |

---

## 2. Gherkin Acceptance Scenarios (BDD)

### Scenario 1: Idempotent Subscription Creation Prevents Double Charge
```gherkin
Feature: Idempotent Subscription Creation

  Scenario: Client retries POST /v1/subscriptions with the same Idempotency-Key after network drop
    Given a valid customer "550e8400-e29b-41d4-a716-446655440001"
    And a valid tokenized payment method "pm_card_token_xyz"
    And a plan "plan_pro_monthly" with amount 29990 and currency "RUB"
    And an initial request "POST /v1/subscriptions" with header "Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000" succeeded with HTTP 201
    When the client resends the exact same "POST /v1/subscriptions" request with header "Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000"
    Then the Billing Core MUST NOT initiate a second charge to the PSP
    And the response status code MUST be 201 Created
    And the response body "id" MUST match the previously created subscription ID
    And the subscription status MUST be "ACTIVE"
```

### Scenario 2: Dunning & Grace Period Lifecycle on Soft Decline
```gherkin
Feature: Dunning and Grace Period Lifecycle

  Scenario: Subscription enters PAST_DUE on soft decline and retains access for 7 days
    Given an "ACTIVE" subscription "sub_100" with "next_billing_at: 2026-11-12T00:00:00Z"
    When the billing scheduler triggers renewal and PSP returns soft decline "insufficient_funds"
    Then the current invoice status MUST transition to "OPEN" with "attempt_count: 1"
    And the subscription status MUST transition from "ACTIVE" to "PAST_DUE"
    And the field "grace_period_ends_at" MUST be set exactly to "2026-11-19T00:00:00Z" (7 calendar days)
    And a webhook event "invoice.payment_failed" MUST be enqueued in "WEBHOOK_OUTBOX"
    When the scheduler executes retry #1 on "2026-11-13T00:00:00Z" (Day +1) and PSP returns "SUCCEEDED"
    Then the invoice status MUST transition to "PAID"
    And the subscription status MUST transition back from "PAST_DUE" to "ACTIVE"
    And "next_billing_at" MUST be advanced to the next monthly cycle
    And a webhook event "invoice.paid" MUST be enqueued in "WEBHOOK_OUTBOX"
```

### Scenario 3: Immediate Suspension on Hard Decline (No Spam Retries)
```gherkin
Feature: Hard Decline Handling

  Scenario: Card reported stolen or permanently closed during recurring renewal
    Given an "ACTIVE" subscription "sub_200" due for renewal
    When the billing scheduler attempts to charge the card and the PSP returns hard decline "card_stolen"
    Then the payment attempt record status MUST be "FAILED_HARD"
    And the subscription status MUST immediately transition from "ACTIVE" to "SUSPENDED"
    And no further automatic retry attempts MUST be scheduled for this payment method
    And the "next_billing_at" field MUST be set to null
    And a webhook event "subscription.suspended" MUST be enqueued in "WEBHOOK_OUTBOX"
```

### Scenario 4: Month-End Anchor Date Clamping (Jan 31 -> Feb 28 -> Mar 31)
```gherkin
Feature: Calendar Month-End Billing Anchor Calculation

  Scenario: Monthly subscription created on Jan 31 clamps to Feb 28 and restores to Mar 31 via anchor_day
    Given a customer creates a "MONTHLY" subscription on "2027-01-31T10:00:00Z"
    When the initial charge succeeds
    Then the subscription "anchor_day" MUST be stored as 31
    And "current_period_end" and "next_billing_at" MUST be calculated as "2027-02-28T10:00:00Z"
    When the renewal charge succeeds on "2027-02-28T10:00:00Z"
    Then the subsequent "current_period_end" and "next_billing_at" MUST restore the "anchor_day" (31) to "2027-03-31T10:00:00Z"
    When the renewal charge succeeds on "2027-03-31T10:00:00Z"
    Then the subsequent "next_billing_at" MUST clamp to "2027-04-30T10:00:00Z" while keeping "anchor_day" equal to 31
```

### Scenario 5: Partial Refund Balance Guardrail & Rejection
```gherkin
Feature: Invoice Refund Guardrails

  Scenario: Operator attempt to refund amount exceeding available balance is rejected
    Given a "PARTIALLY_REFUNDED" invoice "inv_500" with "amount_minor: 100000" (1000.00 RUB)
    And an existing settled partial refund with "amount_refunded_minor: 60000" (600.00 RUB)
    When a support agent sends "POST /v1/invoices/inv_500/refund" with "amount_minor: 60000"
    Then the request MUST be rejected before calling the PSP gateway
    And the HTTP response status code MUST be 422 Unprocessable Entity
    And the error code in the problem details response MUST be "REFUND_EXCEEDS_PAYMENT_BALANCE"
    And the cumulative "amount_refunded_minor" on invoice "inv_500" MUST remain 60000
    And the invoice status MUST remain "PARTIALLY_REFUNDED"
```

### Scenario 6: Reconcile on PSP Network Timeout (Double Charge Prevention)
```gherkin
Feature: PSP Network Timeout Protection

  Scenario: Handling 10s gateway timeout during recurring charge
    Given an open invoice "inv_600" for subscription "sub_300"
    When the payment orchestrator calls PSP "POST /charges" and the connection times out after 10000 ms
    Then the Billing Core MUST NOT initiate a new charge with a new idempotency key
    And the current payment attempt status MUST be marked as "PENDING"
    When the background reconcile worker queries the PSP status using the original "Idempotency-Key"
    And the PSP responds that the transaction was already captured with ID "psp_tx_999"
    Then the payment attempt status MUST update to "SUCCEEDED"
    And the invoice status MUST update to "PAID"
    And the subscription status MUST remain "ACTIVE"
```

---

## 3. Configured Service Policies & Tolerances
| Policy Parameter | Value | Architectural Guarantee |
| :--- | :--- | :--- |
| **PSP Request Timeout** | `10 000 ms` | Жесткий сетевой дедлайн; переход в `PENDING` с последующей фоновой сверкой |
| **Dunning Retry Policy** | `День +1, День +3, День +7` | Ровно 3 попытки ретрая при Soft Decline |
| **Grace Period Duration** | `7 calendar days` | Пользователь сохраняет полный доступ к продукту на время просрочки |
| **Hard Decline Codes** | `card_stolen`, `card_expired`, `account_closed`, `fraud_suspected` | Немедленная остановка ретраев и перевод в `SUSPENDED` |
| **Webhook Delivery Policy** | `1m, 5m, 30m, 2h, 6h, 24h` | Экспоненциальный бэкофф с криптоподписью `HMAC-SHA256` |
| **Idempotency Cache TTL** | `24 hours` | Хранение пар ключ-ответ в PostgreSQL с уникальным индексом |

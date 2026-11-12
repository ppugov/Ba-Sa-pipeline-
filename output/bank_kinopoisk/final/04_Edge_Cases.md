# Edge Cases, Failure Modes & BDD Test Scenarios: FinTech × Media Ecosystem (`bank_kinopoisk`)

> **Document Status:** Verified (Orchestrator Approved)  
> **Version:** 1.0  
> **Related Documents:** `01_BRD.md`, `02_Architecture.md`, `03_OpenAPI.yaml`  
> **Pipeline Pattern:** Orchestrator-Worker (Worker: `gemini-3.5-flash-lite`, Critic: `Gemini 4 Argon`)

---

## 1. Failure Mode & Edge Case Matrix
| Case ID | Category | Trigger / Condition | Expected System Behavior | HTTP / Event Outcome |
| :--- | :--- | :--- | :--- | :--- |
| **EC-01** | Saga Compensation on Bank Decline | Шаг 1 (`LOYALTY_HOLD` на 300 баллов) успешен, но Шаг 2 (`BANK_CARD_DEBIT` на 200 ₽) отклонен банком (`insufficient_funds`) | Оркестратор переводит сессию в `COMPENSATED`, немедленно вызывает `LOYALTY_RELEASE` для возврата 300 баллов и не выдает `ENTITLEMENT` | `422 Unprocessable Entity` (`BANK_DEBIT_DECLINED`), событие `payment.compensated` |
| **EC-02** | Saga Timeout Safety (`BR-04`) | Шаг 1 (`LOYALTY_HOLD`) успешен, а вызов `BANK_CARD_DEBIT` оборвался по сетевому таймауту (`504` через 5000 мс) | **Запрет мгновенной разморозки баллов!** Сессия переходит в `RECONCILING`, воркер опрашивает шлюз Банка по `idempotency_key`: если деньги списались — доводит до `COMMITTED`, если нет — вызывает `LOYALTY_RELEASE` (`COMPENSATED`) | `202 Accepted` (`status: RECONCILING`) -> доведение до `COMMITTED` или `COMPENSATED` |
| **EC-03** | Hybrid Split Boundary (`BR-03`) | Попытка оплатить фильм стоимостью `50000` коп. (`500 ₽`) на 100% баллами (`paid_points_minor: 50000, paid_money_minor: 0`) | Валидатор блокирует запрос до вызова Сервиса Лояльности (максимум 99% баллами, минимум `100` коп. = `1 ₽` рублями) | `422 Unprocessable Entity` (`MAX_POINTS_LIMIT_EXCEEDED`) |
| **EC-04** | Smart TV Child Fraud & Brute-Force (`BR-05`) | Ребенок на Smart TV пытается купить фильм за `59900` коп. (`599 ₽`) без ПИН-кода или 3 раза подряд вводит неверный 4-значный ПИН | При отсутствии ПИН-кода — `422 PARENTAL_PIN_REQUIRED`. После 3 неверных попыток — блокировка покупок на 15 минут (`pin_locked_until = NOW() + 15m`) | `422 Unprocessable Entity` (`INVALID_PARENTAL_PIN` -> `PARENTAL_PIN_LOCKED`) |
| **EC-05** | Refund Clawback Abuse (`BR-02`) | Клиент потратил `15 000 ₽` 28-го числа, 29-го получил бесплатную подписку (`FREE_BUNDLE`, `0 ₽`), а 30-го оформил возврат куртки в магазине (оборот упал до `0 ₽`) | Банк присылает событие с `is_refund_event: true`. Статус подписки переводится в `CLAWBACK_DEBT`, а цена следующего продления устанавливается в `39900` коп. (`399 ₽`) | `subscription_status: CLAWBACK_DEBT`, `next_renewal_price_minor: 39900` |
| **EC-06** | Banking Secrecy Consent Revocation (`FR-01`) | Клиент отозвал согласие на обмен данными (`status: REVOKED`), после чего пытается совершить гибридную покупку или получить льготу | Шлюз блокирует обмен данными с Банком и отклоняет запрос | `422 Unprocessable Entity` (`CONSENT_REQUIRED`) |
| **EC-07** | Idempotent Network Retry | Приложение Smart TV при плохом Wi-Fi дважды отправляет `POST /v1/hybrid-payments` с одинаковым `Idempotency-Key` | Второй запрос возвращает сохраненный ответ первой транзакции без повторного холдирования баллов и без повторного списания с карты | `201 Created` (Idempotent replay) |
| **EC-08** | Friday Prime-Time Bank Outage | В пятницу в 20:00 (2 млн зрителей) Core Banking Банка уходит на техработы | Чтение `GET /v1/subscriptions/{ecosystem_user_id}` продолжает работать из локальной проекции PostgreSQL/Redis (`p95 < 100ms`); продления подписок получают 3-дневный `GRACE_PERIOD` | `200 OK` (Деградация 0%) |

---

## 2. Gherkin Acceptance Scenarios (BDD)
> [!IMPORTANT]
> Все сценарии используют точные эндпоинты, поля, целочисленные минорные единицы (`_minor`), статусы и `error_code` из `03_OpenAPI.yaml`.

### Scenario 1: Successful Hybrid TVOD Purchase via Saga (Points Hold -> Card Debit -> Commit)
```gherkin
Feature: Hybrid Movie Purchase Saga Orchestration (FR-04)

  Scenario: Customer buys a 599 RUB movie on Smart TV using 300 Points + 299 RUB with valid Parental PIN
    Given an active ecosystem user "eco_usr_99887766" with granted consent "v1.0-fz395-152fz"
    And the user has "loyalty_points_minor: 45000" (450 points) and a valid linked Bank card
    When the client sends "POST /v1/hybrid-payments" with payload:
      | item_id            | 550e8400-e29b-41d4-a716-446655440111 |
      | device_type        | SMART_TV                             |
      | is_kids_profile    | false                                |
      | total_amount_minor | 59900                                |
      | paid_points_minor  | 30000                                |
      | paid_money_minor   | 29900                                |
      | parent_pin_code    | 4815                                 |
    Then the Saga Orchestrator MUST hold 30000 minor points in the Loyalty Service ("POINTS_HELD")
    And the Saga Orchestrator MUST debit 29900 kopecks via the Bank Payment Gateway ("FUNDS_DEBITED")
    And the Saga Orchestrator MUST commit the held points ("COMMITTED")
    And the response status MUST be HTTP 201 Created with "status: COMMITTED" and "entitlement_granted: true"
    And the response body MUST NOT contain "parent_pin_hash" or "parent_pin_code"
    And a webhook event "payment.committed" MUST be recorded in "DOMAIN_EVENT_OUTBOX"
```

### Scenario 2: Saga Automatic Compensation When Bank Card Debit Declines (OQ-04)
```gherkin
Feature: Saga Compensation on Bank Card Decline

  Scenario: Loyalty points are automatically unfrozen when the ruble card payment fails
    Given an active ecosystem user "eco_usr_99887766" with granted consent and 30000 minor loyalty points
    When the client sends "POST /v1/hybrid-payments" with "total_amount_minor: 50000", "paid_points_minor: 30000", "paid_money_minor: 20000"
    And the Loyalty Service succeeds in holding 30000 minor points ("POINTS_HELD")
    And the Bank Payment Gateway rejects the 20000 kopecks debit with "insufficient_funds"
    Then the Saga Orchestrator MUST immediately call the Loyalty Service release endpoint to unfreeze the 30000 minor points
    And the payment session status MUST transition to "COMPENSATED" with "entitlement_granted: false"
    And the API MUST return HTTP 422 Unprocessable Entity with error_code "BANK_DEBIT_DECLINED"
    And a webhook event "payment.compensated" MUST be recorded in "DOMAIN_EVENT_OUTBOX"
```

### Scenario 3: Safe Timeout Reconciliation Prevents Premature Points Release (BR-04)
```gherkin
Feature: Saga Timeout Reconciliation Safety

  Scenario: Bank Payment Gateway times out (504) after points are held
    Given a hybrid payment session where 30000 minor points are held ("POINTS_HELD")
    When the Bank Payment Gateway call for 20000 kopecks times out after 5000 ms (HTTP 504)
    Then the Saga Orchestrator MUST transition the session status to "RECONCILING"
    And the Saga Orchestrator MUST NOT release the held loyalty points before reconciliation completes
    And the API MUST return HTTP 202 Accepted with "status: RECONCILING"
    When the background Reconciliation Worker queries the Bank Payment Gateway by the original "Idempotency-Key"
    And the Bank confirms the 20000 kopecks debit actually succeeded
    Then the Reconciliation Worker MUST commit the held loyalty points and transition the session to "COMMITTED" with "entitlement_granted: true"
```

### Scenario 4: Smart TV Parental PIN Protection and Brute-Force Lockout (BR-05)
```gherkin
Feature: Smart TV Anti-Fraud and Parental Control

  Scenario: Three incorrect Parental PIN attempts on Smart TV lock purchases for 15 minutes
    Given an active ecosystem user "eco_usr_99887766" on device_type "SMART_TV"
    When the user sends "POST /v1/hybrid-payments" for "total_amount_minor: 59900" with wrong "parent_pin_code: 0000" 3 times
    Then the first 2 attempts MUST be rejected with HTTP 422 and error_code "INVALID_PARENTAL_PIN"
    And the 3rd attempt MUST set "pin_locked_until" to 15 minutes in the future and return HTTP 422 with error_code "PARENTAL_PIN_LOCKED"
    When the user immediately sends a 4th attempt even with the correct "parent_pin_code: 4815"
    Then the system MUST reject the request with HTTP 422 and error_code "PARENTAL_PIN_LOCKED" without calling the Loyalty Service
```

### Scenario 5: Post-Renewal Merchant Refund Triggers Clawback Debt (BR-02 & OQ-03)
```gherkin
Feature: Protection Against Spend Threshold Refund Abuse (Clawback)

  Scenario: Customer refunds a 15,000 RUB purchase after receiving a free Kinopoisk bundle renewal
    Given user "eco_usr_99887766" has "subscription_status: FREE_BUNDLE" and "qualified_turnover_minor: 1550000" (15,500.00 RUB)
    When the Bank Processing worker sends "PUT /v1/subscriptions/eco_usr_99887766" with:
      | qualified_turnover_minor | 50000 |
      | is_refund_event          | true  |
    Then the system MUST detect that qualified turnover dropped below 1500000 minor units after a free bundle grant
    And "spend_target_met" MUST become false
    And "subscription_status" MUST transition to "CLAWBACK_DEBT"
    And "next_renewal_price_minor" MUST be set to 39900 (399.00 RUB)
    And a webhook event "subscription.status_changed" MUST be recorded in "DOMAIN_EVENT_OUTBOX"
```

---

## 3. Operational Policies, Timeouts & Retry Matrix
| Parameter | Configured Value | Architectural Notes |
| :--- | :--- | :--- |
| **Spend Qualification Threshold (`BR-01`)** | `1 500 000 minor units` (`15 000.00 ₽`) | Считаются только разрешенные POS MCC за вычетом возвратов |
| **Standard Subscription Price** | `39 900 minor units` (`399.00 ₽`) | `0 ₽` при `spend_target_met == true` |
| **Max Loyalty Points Share (`BR-03`)** | `99%` (`min card payment = 100 kopecks`) | Строгая проверка целочисленного баланса `points + money == total` |
| **Smart TV PIN Threshold (`BR-05`)** | `> 30 000 minor units` (`300.00 ₽`) или `is_kids_profile = true` | Блокировка на `15 минут` после `3` неверных попыток ввода ПИН-кода |
| **Loyalty & Bank Gateway Timeout** | `5 000 ms` | При таймауте шага `DEBIT` — перевод в `RECONCILING` (`BR-04`) |
| **Outbox Delivery Backoff** | `10s, 1m, 5m, 30m, 2h, 6h` | Перевод в `DEAD_LETTER` после 6 ошибок с алертом в мониторинг |
| **Idempotency Key TTL** | `24 hours` | Уникальный индекс по `idempotency_key` в PostgreSQL |

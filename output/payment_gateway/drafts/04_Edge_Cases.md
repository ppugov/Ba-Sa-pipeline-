# Edge Cases, Failure Modes & BDD Test Scenarios: payment_gateway

> **Document Status:** Draft / Verified  
> **Version:** 1.0  
> **Related Documents:** `01_BRD.md`, `02_Architecture.md`, `03_OpenAPI.yaml`

---

## 1. Failure Mode & Edge Case Matrix
| Case ID | Category | Trigger / Condition | Expected System Behavior | HTTP / Event Outcome |
| :--- | :--- | :--- | :--- | :--- |
| **EC-01** | Concurrency & Idempotency | Дублирование запроса `POST /subscriptions` с тем же `Idempotency-Key` во время обработки первого запроса | Отклонить второй запрос без повторного обращения к PSP | `409 Conflict` (`IDEMPOTENCY_IN_FLIGHT`) |
| **EC-02** | Soft Decline vs Hard Decline | Списание не прошло из-за `insufficient_funds` (soft) или `stolen_card` (hard) | Soft decline переводит подписку в `PAST_DUE` и запускает dunning-ретраи; Hard decline мгновенно переводит в `SUSPENDED` | События `invoice.payment_failed` vs `subscription.suspended` |
| **EC-03** | Calendar & Date Arithmetic | Подписка оформлена 31 января с интервалом `MONTHLY` | Дата следующего списания корректно привязывается к 28 февраля (или 29 в високосный год) с возвратом к исходному числу 31 в марте | `next_billing_at = 2027-02-28T...` |
| **EC-04** | Network Timeout During Charge | Таймаут ответа от PSP по истечении 10 сек (`504` / обрыв сокета); статус платежа неизвестен | Сохранить `PAYMENT_ATTEMPT` в статусе `PENDING`, запросить статус в PSP по исходному `idempotency_key` перед любыми повторными действиями | Отсутствие двойных списаний (Double-charge prevention) |
| **EC-05** | Race Condition: User Cancels During Renewal | Пользователь вызывает `/cancel` ровно в момент блокировки подписки крон-воркером для продления | Блокировка строки в БД (`SELECT ... FOR UPDATE`) сериализует операции; если списание уже произошло, отмена применяется к следующему периоду | Детерминированный переход состояния |
| **EC-06** | Webhook Delivery Failure | Эндпоинт мерчанта возвращает `500` или тайм-аут | Воркер outbox выполняет повторы с экспоненциальной задержкой и джиттером до исчерпания попыток, затем переводит в `DEAD_LETTER` | Отправка алерта в систему мониторинга |

---

## 2. Gherkin Acceptance Scenarios (BDD)

### Scenario 1: Idempotent Subscription Creation Prevents Double Charge
```gherkin
Feature: Идемпотентность создания подписки

  Scenario: Клиент повторяет POST /v1/subscriptions с тем же Idempotency-Key после срыва сети
    Given валидный клиент "cust_01" с активным методом оплаты "pm_01" и планом "plan_pro_monthly"
    And первичный запрос "POST /v1/subscriptions" с заголовком Idempotency-Key "550e8400-e29b-41d4-a716-446655440000" успешно завершился со статусом 201
    When клиент отправляет идентичный запрос "POST /v1/subscriptions" с заголовком Idempotency-Key "550e8400-e29b-41d4-a716-446655440000"
    Then система НЕ ДОЛЖНА инициировать второе списание у PSP
    And система ДОЛЖНА вернуть HTTP 201 с тем же самым идентификатором подписки "id" и телом ответа, что и в первый раз
```

### Scenario 2: Dunning & Grace Period Lifecycle on Soft Decline
```gherkin
Feature: Dunning и жизненный цикл Grace Period при мягком отказе (Soft Decline)

  Scenario: Подписка переходит в PAST_DUE при мягком отказе и восстанавливается при ретрае
    Given подписка "sub_100" находится в статусе "ACTIVE" с датой "next_billing_at" "2026-11-12T00:00:00Z"
    When планировщик биллинга обрабатывает "sub_100", и PSP возвращает мягкий отказ "insufficient_funds"
    Then статус инвойса должен стать "OPEN" с количеством попыток `attempt_count` равным 1
    And статус подписки должен измениться с "ACTIVE" на "PAST_DUE"
    And поле "grace_period_ends_at" должно быть установлено согласно политике льготного периода
    And событие "invoice.payment_failed" должно быть записано в "WEBHOOK_OUTBOX"
    When наступает запланированный ретрай и PSP возвращает статус "SUCCEEDED"
    Then статус инвойса должен измениться на "PAID"
    And статус подписки должен измениться с "PAST_DUE" на "ACTIVE"
```

### Scenario 3: Immediate Suspension on Hard Decline
```gherkin
Feature: Обработка жесткого отказа (Hard Decline)

  Scenario: Карта заблокирована или украдена во время регулярного списания
    Given подписка "sub_200" в статусе "ACTIVE" ожидает регулярного продления
    When воркер биллинга пытается списать средства, и PSP возвращает жесткий отказ "card_stolen"
    Then статус платежной попытки должен стать "FAILED_HARD"
    And статус подписки должен немедленно измениться на "SUSPENDED"
    And автоматические повторные попытки списания для данного метода оплаты больше не планируются
    And вебхук-событие "subscription.suspended" должно быть добавлено в очередь outbox
```

### Scenario 4: Month-End Anchor Date Clamping (Jan 31 -> Feb 28 -> Mar 31)
```gherkin
Feature: Привязка дат окончания периода подписки в конце месяца

  Scenario: Ежемесячная подписка, оформленная 31 числа, корректно обрабатывает февраль
    Given клиент оформляет подписку с интервалом "MONTHLY" "2027-01-31T10:00:00Z"
    When первичное списание проходит успешно
    Then поля "current_period_end" и "next_billing_at" должны быть установлены на "2027-02-28T10:00:00Z"
    When плановое списание "2027-02-28T10:00:00Z" проходит успешно
    Then следующие "current_period_end" и "next_billing_at" возвращаются к исходному числу "2027-03-31T10:00:00Z"
```

### Scenario 5: Partial Refund Validation & State Guardrails
```gherkin
Feature: Защитные механизмы возврата средств по инвойсу

  Scenario: Отклонение возврата, превышающего исходную оплаченную сумму инвойса
    Given оплаченный инвойс "inv_500" с суммой "amount_minor" 100000 и валютой "RUB"
    And ранее уже был успешно проведен частичный возврат на сумму 40000 копеек
    When оператор запрашивает "POST /v1/invoices/inv_500/refund" с суммой "amount_minor" 70000
    Then система должна отклонить запрос с кодом HTTP 422 Unprocessable Entity
    And код ошибки в ответе должен быть "REFUND_AMOUNT_EXCEEDS_AVAILABLE_BALANCE"
```

---

## 3. Dunning, Retry & Timeout Policy Specification
| Parameter | Configured Value | Notes |
| :--- | :--- | :--- |
| **PSP Request Timeout** | `10,000 ms` | Сопровождается асинхронной сверкой статуса перед любыми ретраями |
| **Soft Decline Retry Schedule** | День +1, День +3, День +7 | Максимум 3 попытки ретрая (всего 4 попытки включая первичную) |
| **Grace Period Duration** | `7 days` | Клиент сохраняет доступ к сервису, пока подписка в статусе `PAST_DUE` |
| **Webhook Delivery Backoff** | `1m, 5m, 30m, 2h, 6h, 24h` | Подписывается заголовком `X-Webhook-Signature` (`HMAC-SHA256`) |
| **Idempotency Key TTL** | `24 hours` | Хранится в БД с уникальным индексом для быстрого поиска |

# Golden BA/SA Architectural Patterns (Few-Shot Reference for Workers)

Use these mandatory Senior Systems Analyst patterns when populating `01_BRD.md`, `02_Architecture.md`, `03_OpenAPI.yaml`, and `04_Edge_Cases.md`:

## 1. Strict Parity Between ERD (`02_Architecture.md`) and OpenAPI 3.1 (`03_OpenAPI.yaml`)
- **NEVER leave template placeholders** (`PRIMARY_ACTOR`, `CORE_AGGREGATE`, `CreateResourceRequest`, `ResourceResponse`, `/resources`) in generated files. Delete all template placeholder schemas and replace them with real domain schemas.
- **100% Status Enum Match:** Every status value listed in `erDiagram` comments (e.g., `"INITIATED | POINTS_HELD | FUNDS_DEBITED | RECONCILING | COMMITTED | COMPENSATED | FAILED"`) MUST match the states in `stateDiagram-v2` AND the `enum:` array in `03_OpenAPI.yaml`.
- **Supporting Fields for Business Rules (`BR-*`):**
  - If a business rule requires calendar clamping (`Jan 31 -> Feb 28 -> Mar 31`), the entity MUST include `int anchor_day`.
  - If a business rule allows partial refunds, the entity MUST include `bigint amount_refunded_minor` and status `PARTIALLY_REFUNDED`.
  - If a business rule locks PIN/auth after N failed attempts, the user entity MUST include `int pin_failed_attempts` and `timestamptz pin_locked_until`.

## 2. Financial & Time Precision Rules
- **Money & Loyalty Points:** NEVER use `float`, `double`, or `number` for monetary amounts or points. Always use `bigint` in ERD and `type: integer, format: int64` in OpenAPI with `_minor` suffix (e.g., `amount_minor`, `price_minor`, `paid_points_minor` in kopecks/cents).
- **Timestamps & Timezones:** Always use `timestamptz` (UTC ISO-8601) in ERD and `type: string, format: date-time` in OpenAPI. For multi-region systems, store IANA timezone identifiers (`Europe/Moscow`, `Asia/Almaty`, `Asia/Tashkent`), never raw `"UTC+3"` strings.

## 3. Security & Secret Isolation
- **NEVER expose hashes or secrets in OpenAPI `*Response` schemas:** Fields like `parent_pin_hash`, `password_hash`, `qr_secret_hash`, `pan`, or `cvv` may exist in internal ERD tables (`ECOSYSTEM_USER`, `ROOM`), but MUST NEVER appear inside any OpenAPI `*Response` schema!
- In OpenAPI `*Request` schemas, pass write-only verification tokens (`parent_pin_code`, `qr_token`, `payment_method_id`).

## 4. Distributed Systems & Timeout Safety (`504 Gateway Timeout`)
- **Never compensate/rollback blindly on a network timeout (`504`):** When a two-phase flow (Saga or PSP charge) times out on an external payment debit call, the system does NOT know whether the remote system executed the debit. Transition to an intermediate state (`RECONCILING` or `PENDING`) and reconcile by `Idempotency-Key` before releasing held resources or retrying.
- **Database Concurrency:**
  - For time-slot booking systems, use PostgreSQL half-open range exclusion: `EXCLUDE USING gist (room_id WITH =, tstzrange(start_at, end_at, '[)') WITH &&)`.
  - For financial/state updates, use `SELECT ... FOR UPDATE` and `int version` (optimistic locking).

## 5. HTTP Status Codes & Error Contract
- Use `400 Bad Request` ONLY for malformed JSON or syntax/type validation errors.
- Use `409 Conflict` for `Idempotency-Key` conflicts, concurrent modification (`OPTIMISTIC_LOCK_CONFLICT`), or resource slot overlap (`ROOM_SLOT_OVERLAP`).
- Use `422 Unprocessable Entity` for all domain business rule violations (`BR-*`) and invalid state transitions, with a strict `enum` of `error_code` values in `ProblemDetails`.

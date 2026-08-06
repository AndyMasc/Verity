# Papertrail — Operations Runbook

Everything required to run Papertrail in **development** and **production**, including
every external service, environment variable, webhook registration, and recurring job.
Compiled from the code itself (`Papertrail/settings/base.py`, `production.py`,
`Dockerfile`, `tasks.py`, webhook handlers) — not from memory.

---

## 1. Service map

| Service | Role | Dev | Prod |
|---|---|---|---|
| PostgreSQL | Primary database | optional (default: SQLite) | required |
| Redis | Cache + sessions (`django-redis`) | optional (default: locmem) | required |
| Cloudflare R2 | File storage (presigned uploads, S3-compatible) | required to run | required |
| Google Gemini | OCR of uploaded documents | required | required |
| Upstash QStash | Background tasks + email queue (no Celery) | optional (local mode) | required |
| Resend | Email delivery (via django-anymail, through QStash) | required | required |
| Plaid | Bank sync + webhooks | sandbox env | production env |
| Stripe | Billing, subscriptions, pricing tables | test keys | live keys |
| Google / GitHub OAuth | allauth social login | required | required |
| Web Push (VAPID) | Browser notifications | required | required |
| Sentry | Error + performance monitoring | required (DSN has no default) | required |
| PostHog | Product analytics | optional (default off) | optional |
| Tailwind (standalone binary) | CSS build | `tailwind start` | build output committed |

Everything marked **required** is read at settings load with **no default** — the app
will not boot without it, in any environment.

---

## 2. Environment variables

Template: `cp .env.example .env`

### Core / Django

| Variable | Required | Notes |
|---|---|---|
| `DJANGO_ENV` | prod | `production` routes `Papertrail/settings/__init__.py` to `settings/production.py`; anything else loads `settings/local.py`. |
| `DJANGO_SETTINGS_MODULE` | – | Dockerfile sets `Papertrail.settings`. Alternatively point directly at `Papertrail.settings.production`. |
| `SECRET_KEY` | yes | `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `DEBUG` | prod: `false` | `production.py` refuses to boot if `true` |
| `ALLOWED_HOSTS` | yes (prod) | comma list, e.g. `app.example.com,www.example.com` |
| `CSRF_TRUSTED_ORIGINS` | prod | e.g. `https://app.example.com` |
| `CORS_ALLOWED_ORIGINS` | optional | only if a separate client origin exists |
| `DATABASE_URL` | prod | `postgres://user:pass@host:5432/papertrail`; dev defaults to `sqlite:///db.sqlite3` (WAL mode) |
| `DB_CONN_MAX_AGE` | optional | default `60` |
| `REDIS_URL` | prod | only consumed when `DEBUG=False` |
| `DEFAULT_FROM_EMAIL` | optional | default `onboarding@resend.dev` — change in prod to a verified Resend sender |
| `NGROK_HTTPS_TUNNEL_URL` | dev only | local CSRF trust when testing webhooks via a tunnel |

### Cloudflare R2

| Variable | Notes |
|---|---|
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 API token (Object Read/Write, scoped to the bucket) |
| `R2_STORAGE_BUCKET_NAME` | private bucket; uploads use presigned URLs, no public access needed |
| `R2_S3_ENDPOINT_URL` | `https://<account-id>.r2.cloudflarestorage.com` |
| `R2_PAPERTRAIL_STORAGE_ACCOUNT_ID` | Cloudflare account ID |

### Google Gemini

| Variable | Notes |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio key; billing must be active on the project |

### Upstash QStash

| Variable | Notes |
|---|---|
| `QSTASH_TOKEN` | Upstash console → project |
| `DJANGO_QSTASH_DOMAIN` | **public** base URL of the app, e.g. `https://app.example.com` — QStash calls this |
| `DJANGO_QSTASH_WEBHOOK_PATH` | `/qstash/webhook/` |
| `QSTASH_CURRENT_SIGNING_KEY` | signature verification |
| `QSTASH_NEXT_SIGNING_KEY` | key rotation support |

Note: with no token, the QStash client falls back to `http://localhost:8080`
(Upstash's local dev mode). Fine for local, never for prod.

### Resend

| Variable | Notes |
|---|---|
| `RESEND_API_KEY` | verify your sending domain (SPF/DKIM/MX) or mail is only deliverable from `onboarding@resend.dev` |

### Plaid

| Variable | Notes |
|---|---|
| `PLAID_CLIENT_ID` / `PLAID_SECRET` | Plaid dashboard |
| `PLAID_ENV` | `sandbox` / `development` / `production` |
| `PLAID_WEBHOOK_URL` | **public** URL, e.g. `https://app.example.com/plaid/webhook/` |
| `PLAID_SYNC_COOLDOWN_SECONDS` | optional, default `60`; debounces duplicate sync webhooks |

### Stripe

| Variable | Notes |
|---|---|
| `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` | `sk_*` / `pk_*` |
| `DJSTRIPE_FOREIGN_KEY_TO_FIELD` | djstripe 2.x, typically `djstripe.models.Customer.subscription` |
| `STRIPE_PRICING_TABLE_ID` / `STRIPE_PRICING_TABLE_KEY` | embedded pricing table (plans) on the landing page |
| `STRIPE_STORAGE_TABLE_ID` / `STRIPE_STORAGE_TABLE_KEY` | embedded pricing table (storage add-ons) |

### OAuth (allauth)

| Variable | Notes |
|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | Google Cloud OAuth app; redirect `https://<domain>/accounts/google/login/callback/` |
| `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` | GitHub OAuth app; callback `https://<domain>/accounts/github/login/callback/` |

### Web Push (VAPID)

```bash
venv/bin/python -c "from py_vapid import Vapid01; v = Vapid01(); v.generate_keys(); print(v.public_key); print(v.private_key)"
```

| Variable | Notes |
|---|---|
| `WEB_PUSH_PUBLIC_KEY` / `WEB_PUSH_PRIVATE_KEY` | VAPID keypair (generate once, keep private key secret) |
| `WEB_PUSH_EMAIL` | contact address for push subscriptions |

### Sentry & PostHog

| Variable | Notes |
|---|---|
| `SENTRY_DSN` | **required, no default** — settings crash if missing, even in dev |
| `SENTRY_ENVIRONMENT` | default `development`; set `production` in prod (controls sampling) |
| `POSTHOG_PROJECT_TOKEN` | default `""` = analytics off |
| `POSTHOG_HOST` / `POSTHOG_DISABLED` | optional |

---

## 3. Development

### Prerequisites

- Python 3.14+
- No Node.js required (Tailwind standalone binary via `pytailwindcss`)
- Docker optional (only for Redis/Postgres)

### Steps

```bash
# 1. Code + environment
git clone <repo-url> && cd Papertrail
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt        # ruff, mypy, pytest, bandit

# 2. Config — fill EVERY required var in section 2 (placeholders are fine for
#    local work, but they must be present or Django will not boot).
cp .env.example .env

# 3. Database + CSS tooling
python manage.py migrate
python manage.py tailwind install           # downloads the standalone Tailwind binary

# 4. Run (two processes)
python manage.py tailwind start             # terminal 1: rebuilds CSS on change
python manage.py runserver                  # terminal 2: http://localhost:8000
#   or both at once:  honcho -f Procfile.tailwind start

# 5. Admin + quality gates
python manage.py createsuperuser            # then visit /admin/
pytest                                      # needs pytest-xdist (in requirements-dev)
ruff check . && ruff format --check .
mypy .
pre-commit run --all-files
```

### Dev-only services (only when testing integrations)

| Integration | How to run locally |
|---|---|
| Redis / Postgres | `docker compose up -d` (compose file is dev-only: `DEBUG=1`, `runserver`, bind mounts) |
| Stripe webhooks | `python manage.py stripe_listen --forward-to localhost:8000/stripe/webhook/` |
| Plaid | `PLAID_ENV=sandbox`; initial sync fires `sandbox_item_fire_webhook`, so `PLAID_WEBHOOK_URL` must be reachable → use a tunnel and set `NGROK_HTTPS_TUNNEL_URL` |
| QStash | local dev server (no token → `http://localhost:8080`) or real QStash + tunnel |

---

## 4. Production

### Phase A — Provision services (one-time)

1. **PostgreSQL 14+** — DB + user; consider pgbouncer for `CONN_MAX_AGE=60` workers.
2. **Redis 7** — managed or container. Losing it logs everyone out and thrashes the DB.
3. **Cloudflare R2** — private bucket + scoped API token; set the 5 `R2_*` vars.
4. **Upstash QStash** — project, token, both signing keys; `DJANGO_QSTASH_DOMAIN` = public app URL.
5. **Resend** — verify sending domain; set `RESEND_API_KEY` + `DEFAULT_FROM_EMAIL`.
6. **Google Gemini** — API key with billing.
7. **Plaid** — production access (or `development` for staging); register `PLAID_WEBHOOK_URL` in the Plaid dashboard too.
8. **Stripe** — live keys; products/prices expected by `billing/metadata.py`; two pricing tables for plans and storage.
9. **OAuth apps** — Google + GitHub with the exact callback URLs from section 2.
10. **Sentry** — Django project → `SENTRY_DSN`; `SENTRY_ENVIRONMENT=production`.
11. **PostHog** — optional; token or `POSTHOG_DISABLED=1`.
12. **VAPID keys** — generate once (`WEB_PUSH_*`).

### Phase B — Build

```bash
cp .env.example .env
# Set: DJANGO_ENV=production, DEBUG=false, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS,
#      DATABASE_URL, REDIS_URL + all keys from section 2.

docker build -t papertrail .
```

The `Dockerfile`: multi-stage build, non-root user, `migrate --noinput` +
`collectstatic --noinput` at startup, gunicorn (`3 workers × 20 threads`,
gthread, 120s timeout), healthcheck on `/core/health/`.

**Required by hand:**

- **Tailwind:** the Dockerfile does **not** run `manage.py tailwind build`.
  `theme/static/css/dist/styles.css` is committed, so prod works — but any CSS
  change must be rebuilt and committed before deploying.
- **Migrations:** running them inside the container races during rolling
  deploys. Run `python manage.py migrate --noinput` as a separate one-shot job.

### Phase C — Webhook registrations (one-time)

| Provider | URL | Verification |
|---|---|---|
| QStash | `https://app.example.com/qstash/webhook/` | automatic via `DJANGO_QSTASH_DOMAIN` + signing keys |
| Plaid | `https://app.example.com/plaid/webhook/` | `PLAID_WEBHOOK_URL` **and** Plaid dashboard |
| Stripe | `https://app.example.com/stripe/webhook/<djstripe_uuid>/` | see below |

**Stripe:**

1. The URL must include the `djstripe_uuid` of a locally-synced
   `WebhookEndpoint` row (see `Papertrail/urls.py`). In dev, `stripe_listen`
   creates it. In prod: create the endpoint in the Stripe dashboard, sync it
   locally, fix the URL to include the UUID, and store the signing secret on
   the row (`djstripe_validation_method="verify_signature"`).
2. Enable events (from `billing/webhooks.py` + `reimbursements/webhooks.py`):
   `customer.subscription.created/updated/deleted`, `invoice.paid`,
   `invoice.payment_failed`, `checkout.session.completed`,
   `checkout.session.async_payment_succeeded`,
   `checkout.session.async_payment_failed`, `account.updated`,
   `transfer.failed`, `charge.failed`, `charge.refunded`.
3. After deploy:
   ```bash
   python manage.py djstripe_init_customers
   python manage.py djstripe_sync_models djstripe.Product djstripe.Price
   ```

### Phase D — Recurring QStash jobs (one-time, manual)

These are **not auto-created by any code**. Create cron schedules in the
Upstash console targeting `https://app.example.com/qstash/webhook/`, then sync
them locally:

| Task | Purpose | Cadence |
|---|---|---|
| `records.tasks.send_expiry_notifications` | warn users before records expire | daily |
| `records.tasks.archive_expired_records` | auto-archive expired records | daily |
| `records.tasks.delete_7year_archived_records` | permanent deletion after 7 years | daily |
| `billing.tasks.reconcile_subscription_statuses_task` | fix subscription drift vs Stripe | hourly |

```bash
python manage.py task_schedules --sync
python manage.py task_schedules --list
```

### Phase E — Reverse proxy, TLS, launch

- TLS-terminating proxy (nginx / Cloudflare / ALB) **must** set
  `X-Forwarded-Proto: https` (`SECURE_PROXY_SSL_HEADER` is configured), or you
  get SSL-redirect loops.

### Phase F — Post-deploy smoke test

1. `GET /core/health/` → 200 (checks DB + cache).
2. Sign up with a new email → login-code email arrives via Resend.
3. Upload a document → presigned R2 upload + OCR completes (Gemini).
4. Connect a bank (Plaid test item) → sync + webhooks land.
5. Buy a plan → Stripe checkout → webhook → `subscription-confirm/` → gating flips.
6. Enable browser notifications (service worker at `/service-worker.js`).
7. Hit `/sentry-debug/` once to confirm error reporting, then remove it.

---

## 5. Known gaps / risks

1. **No CI existed** before `.github/workflows/ci.yml` was added — README claimed
   GitHub Actions but no workflow was present.
2. **`docker-compose.yml` is dev-only** (`DEBUG=1`, `runserver`, source volume);
   no production compose/helm manifest exists — the Dockerfile is the only prod artifact.
3. **`SENTRY_DSN` is hard-required** — every environment (including dev) must set it.
4. **All service keys are required even in dev** — no "lite" mode; a fresh clone
   needs every key from section 2 before Django will boot.
5. **Tailwind output is committed, not built in the Dockerfile** — CSS changes
   silently miss prod until rebuilt and committed.
6. **Migrations run at container startup** — fine for single-instance; race on
   rolling deploys (run as a separate job).
7. **No backup strategy documented** — schedule Postgres dumps and enable R2
   bucket versioning / point-in-time (7-year retention requirements).
8. Test suite is heavy — collection imports torch/easyocr; CI needs patience or
   a `-m "not slow"` split.

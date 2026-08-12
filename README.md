# Papertrail

A modern document and record management platform built with Django, HTMX, and Tailwind CSS. Papertrail helps individuals and businesses organize receipts, invoices, bank transactions, and financial documents with AI-powered OCR, automatic bank synchronization via Plaid, and compliance-aware retention policies.

## Architecture

```
Papertrail/
├── Papertrail/              # Django project configuration
│   ├── settings/            # Split settings (base, local, production)
│   ├── responses.py         # HTMX-aware API response helpers
│   └── utils.py             # CachedPaginator utility
├── core/                    # Auth, dashboard, settings, notifications
├── records/                 # Bank transactions, receipts, merges
├── documents/               # File upload, OCR, Cloudflare R2 storage
├── plaid_integration/       # Plaid bank sync, webhooks, transaction import
└── theme/                   # Tailwind CSS theme
```

### Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Django 6.0, Python 3.14 |
| Frontend | HTMX, Alpine.js, Tailwind CSS, Slippers |
| Database | PostgreSQL (prod) / SQLite (dev) |
| Cache | Redis |
| Storage | Cloudflare R2 (S3-compatible) |
| Background Tasks | Upstash QStash |
| OCR | Google Gemini Flash |
| Banking | Plaid API |
| Email | Resend via django-anymail |
| Auth | allauth (passwordless email codes, Google/GitHub OAuth) |

### Key Design Decisions

- **HTMX-first architecture**: Server-rendered Django templates with HTMX for interactivity. No SPA framework. Alpine.js handles client-side state.
- **Service layer pattern**: Views handle HTTP concerns; business logic lives in `services/` modules and `tasks.py`.
- **Compliance-aware lifecycle**: Records follow Active → Archived → Hard Deleted (7+ years). Audit logging tracks critical operations.
- **User data isolation**: Every queryset is user-scoped. Cross-user access returns 404 (not 403) to prevent enumeration.

## Quick Start

### Prerequisites

- Python 3.14+
- PostgreSQL 14+
- Redis 7+
- Node.js (for Tailwind CSS)
- Plaid developer account (for bank sync)
- Cloudflare R2 bucket (for file storage)
- Google Gemini API key (for OCR)
- Upstash QStash account (for background tasks)

### Local Development

1. **Clone and configure:**

```bash
git clone <repo-url> && cd Papertrail
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Fill in your values
```

2. **Start services:**

```bash
docker compose up -d redis
```

3. **Run migrations and start:**

```bash
python manage.py migrate
python manage.py tailwind install
python manage.py tailwind runserver
```

### Environment Variables

See `Papertrail/settings/base.py` for the full list. Required variables:

| Variable | Description |
|----------|------------|
| `SECRET_KEY` | Django secret key |
| `DATABASE_URL` | PostgreSQL connection string |
| `REDIS_URL` | Redis connection string |
| `R2_ACCESS_KEY_ID` | Cloudflare R2 access key |
| `R2_SECRET_ACCESS_KEY` | Cloudflare R2 secret key |
| `R2_STORAGE_BUCKET_NAME` | R2 bucket name |
| `R2_S3_ENDPOINT_URL` | R2 S3 endpoint URL |
| `GEMINI_API_KEY` | Google Gemini API key |
| `QSTASH_TOKEN` | Upstash QStash token |
| `PLAID_CLIENT_ID` | Plaid client ID |
| `PLAID_SECRET` | Plaid secret |
| `PLAID_ENV` | `sandbox`, `development`, or `production` |
| `RESEND_API_KEY` | Resend email API key |

## Testing

```bash
# Run full test suite
pytest

# Run with coverage
pytest --cov=. --cov-report=html

# Run specific app tests
pytest records/
pytest documents/
pytest plaid_integration/

# Run in parallel
pytest --numprocesses=auto
```

## Code Quality

### Linting & Formatting

```bash
# Check lint
ruff check .

# Auto-fix lint
ruff check --fix .

# Format
ruff format .

# Check formatting
ruff format --check .
```

### Type Checking

```bash
mypy .
```

### Pre-commit Hooks

```bash
pre-commit install
pre-commit run --all-files
```

Pre-commit runs: ruff lint+format, mypy, trailing whitespace, end-of-file fixer, private key detection.

### CI/CD

GitHub Actions runs on every push/PR to `main`:

- **Lint**: ruff check + format verification
- **Type Check**: mypy
- **Security**: bandit
- **Tests**: pytest with PostgreSQL + Redis services

## API Endpoints

### Plaid Integration (`/plaid/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/plaid/create-link-token/` | Create Plaid Link token |
| POST | `/plaid/exchange-token/` | Exchange public token for access token |
| GET | `/plaid/status/` | Get connection status |
| POST | `/plaid/sync/` | Trigger transaction sync |
| POST | `/plaid/disconnect/<item_id>/` | Disconnect bank account |
| POST | `/plaid/webhook/` | Plaid webhook receiver |

### Documents (`/documents/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/documents/` | List documents |
| POST | `/documents/upload/` | Initiate R2 presigned upload |
| POST | `/documents/confirm-upload/` | Confirm upload completion |
| GET | `/documents/<pk>/` | View document detail |
| POST | `/documents/<pk>/delete/` | Soft delete document |
| POST | `/documents/<pk>/undo-delete/` | Restore deleted document |

### Records (`/records/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/records/` | List records |
| POST | `/records/add/` | Create new record |
| GET | `/records/<pk>/` | View/edit record |
| POST | `/records/<pk>/archive/` | Archive record |
| POST | `/records/<pk>/unarchive/` | Unarchive record |
| POST | `/records/merge/` | Manual merge |
| POST | `/records/undo-merge/` | Undo merge |
| POST | `/records/detach/` | Detach receipt from merge |
| POST | `/records/replace/` | Replace receipt |

## Deployment

### Docker

```bash
# Build
docker build -t papertrail .

# Run
docker run -p 8000:8000 --env-file .env papertrail
```

The production Dockerfile uses a multi-stage build with a non-root user, runs migrations at startup, and serves via gunicorn.

### Production Checklist

- [ ] Set `DEBUG=false`
- [ ] Configure `ALLOWED_HOSTS`
- [ ] Set up `DATABASE_URL` for PostgreSQL
- [ ] Configure `REDIS_URL`
- [ ] Set up Cloudflare R2 bucket
- [ ] Configure Plaid production credentials
- [ ] Set up Resend for email delivery
- [ ] Configure VAPID keys for web push
- [ ] Set up SSL/TLS termination
- [ ] Set `DJANGO_ENV=production` (dispatcher in `Papertrail/settings/__init__.py` loads `settings/production.py`)

## License

Private - All rights reserved.

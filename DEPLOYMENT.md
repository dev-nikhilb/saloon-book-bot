# Production Deployment Guide

Complete guide for deploying the WhatsApp Booking SaaS to production.

---

## Table of Contents

1. [Infrastructure Requirements](#infrastructure-requirements)
2. [Docker Deployment](#docker-deployment)
3. [Manual Deployment](#manual-deployment)
4. [Environment Variables Reference](#environment-variables-reference)
5. [SSL/TLS Setup](#ssltls-setup)
6. [Database](#database)
7. [Monitoring](#monitoring)
8. [Scaling](#scaling)
9. [Updates](#updates)

---

## Infrastructure Requirements

### Minimum Server Specs (Single VPS)

| Resource | Minimum | Recommended |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Storage | 40 GB SSD | 80 GB SSD |
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |

This handles ~50 tenants with moderate traffic. The system is designed as a modular monolith on a single VPS.

**Recommended VPS providers (India-friendly):**
- Hetzner (EU/US, good price/performance)
- DigitalOcean (SGP1 region for India)
- AWS Lightsail (Mumbai ap-south-1)

### Software Requirements

| Software | Version | Purpose |
|---|---|---|
| Docker | 24+ | Container runtime |
| Docker Compose | 2.20+ | Service orchestration |
| PostgreSQL | 15 | Primary database |
| Redis | 7 | Caching, rate limiting, conversation state, Celery broker |
| Nginx | Latest | Reverse proxy, SSL termination, rate limiting |
| Python | 3.11 | Application runtime (if not using Docker) |

### Network Requirements

- Port 80 (HTTP) open -- for Let's Encrypt verification and redirect
- Port 443 (HTTPS) open -- for production traffic
- Outbound HTTPS to `graph.facebook.com` -- WhatsApp Cloud API
- Outbound HTTPS to `api.razorpay.com` -- payment processing (if used)

---

## Docker Deployment

This is the recommended deployment method.

### 1. Clone and Configure

```bash
# Clone the repository
git clone https://github.com/your-org/saloon-book-bot.git
cd saloon-book-bot

# Create production environment file
cp .env.prod.example .env.prod
```

### 2. Edit .env.prod

```bash
nano .env.prod
```

**Critical values to change:**

```env
# Database
DATABASE_URL=postgresql+psycopg://booking:YOUR_STRONG_PASSWORD@db:5432/booking
DB_USER=booking
DB_PASSWORD=YOUR_STRONG_PASSWORD
DB_NAME=booking

# Redis
REDIS_URL=redis://:YOUR_REDIS_PASSWORD@redis:6379/0
CELERY_BROKER_URL=redis://:YOUR_REDIS_PASSWORD@redis:6379/1
REDIS_PASSWORD=YOUR_REDIS_PASSWORD

# Encryption (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
ENCRYPTION_KEY=YOUR_GENERATED_FERNET_KEY

# Meta WhatsApp
META_APP_ID=your_meta_app_id
META_APP_SECRET=your_meta_app_secret
META_WEBHOOK_VERIFY_TOKEN=a_random_string_you_choose

# Environment
ENVIRONMENT=production
LOG_LEVEL=INFO
```

### 3. Set Up SSL Certificates

Before starting, create the certificate directory:

```bash
mkdir -p nginx/certs
```

See [SSL/TLS Setup](#ssltls-setup) below for certificate generation.

### 4. Build and Start

```bash
# Build production images
docker compose -f docker-compose.prod.yml build

# Start all services
docker compose -f docker-compose.prod.yml up -d

# Check status
docker compose -f docker-compose.prod.yml ps

# View logs
docker compose -f docker-compose.prod.yml logs -f app
```

### 5. Run Database Migrations

```bash
# Run Alembic migrations
docker compose -f docker-compose.prod.yml exec app alembic upgrade head
```

### Production docker-compose.prod.yml Services

The production compose file runs these services:

| Service | Description | Port |
|---|---|---|
| `db` | PostgreSQL 15 database | Internal only |
| `redis` | Redis 7 for caching and Celery broker | Internal only |
| `app` | FastAPI application (uvicorn, 2 workers) | 8000 (internal) |
| `celery-worker` | Celery worker for background tasks | None |
| `celery-beat` | Celery beat scheduler (reminders every 5min) | None |
| `nginx` | Reverse proxy and SSL termination | 80, 443 |

### Docker Health Checks

All services have built-in health checks:

- **PostgreSQL:** `pg_isready` every 10s
- **Redis:** `redis-cli ping` every 10s
- **App:** `curl http://localhost:8000/health` every 30s

Services depend on each other with `condition: service_healthy`, ensuring proper startup order.

---

## Manual Deployment

If you prefer to run without Docker.

### 1. Install System Dependencies

```bash
# Ubuntu 22.04
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev \
  postgresql-15 redis-server nginx \
  libpq-dev gcc curl

# Enable and start services
sudo systemctl enable postgresql redis-server nginx
sudo systemctl start postgresql redis-server nginx
```

### 2. Set Up PostgreSQL

```bash
sudo -u postgres psql <<EOF
CREATE USER booking WITH PASSWORD 'YOUR_STRONG_PASSWORD';
CREATE DATABASE booking OWNER booking;
GRANT ALL PRIVILEGES ON DATABASE booking TO booking;
EOF
```

### 3. Set Up the Application

```bash
# Create app user
sudo useradd -r -s /bin/false -d /opt/booking-saas booking-app

# Clone code
sudo mkdir -p /opt/booking-saas
sudo chown booking-app:booking-app /opt/booking-saas
cd /opt/booking-saas
git clone https://github.com/your-org/saloon-book-bot.git .

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate
pip install .

# Create environment file
cp .env.prod.example .env
# Edit .env with production values (see Environment Variables Reference)
nano .env

# Run migrations
alembic upgrade head
```

### 4. systemd Service Files

**Application service** (`/etc/systemd/system/booking-app.service`):

```ini
[Unit]
Description=Booking SaaS FastAPI Application
After=postgresql.service redis-server.service
Requires=postgresql.service redis-server.service

[Service]
Type=exec
User=booking-app
Group=booking-app
WorkingDirectory=/opt/booking-saas
Environment=PATH=/opt/booking-saas/.venv/bin:/usr/bin
EnvironmentFile=/opt/booking-saas/.env
ExecStart=/opt/booking-saas/.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Celery worker** (`/etc/systemd/system/booking-celery-worker.service`):

```ini
[Unit]
Description=Booking SaaS Celery Worker
After=postgresql.service redis-server.service
Requires=postgresql.service redis-server.service

[Service]
Type=exec
User=booking-app
Group=booking-app
WorkingDirectory=/opt/booking-saas
Environment=PATH=/opt/booking-saas/.venv/bin:/usr/bin
EnvironmentFile=/opt/booking-saas/.env
ExecStart=/opt/booking-saas/.venv/bin/celery -A app.celery_app:celery \
  worker --loglevel=info --concurrency=2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Celery beat** (`/etc/systemd/system/booking-celery-beat.service`):

```ini
[Unit]
Description=Booking SaaS Celery Beat Scheduler
After=postgresql.service redis-server.service
Requires=postgresql.service redis-server.service

[Service]
Type=exec
User=booking-app
Group=booking-app
WorkingDirectory=/opt/booking-saas
Environment=PATH=/opt/booking-saas/.venv/bin:/usr/bin
EnvironmentFile=/opt/booking-saas/.env
ExecStart=/opt/booking-saas/.venv/bin/celery -A app.celery_app:celery \
  beat --loglevel=info
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Enable and start all services:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable booking-app booking-celery-worker booking-celery-beat
sudo systemctl start booking-app booking-celery-worker booking-celery-beat

# Verify
sudo systemctl status booking-app
sudo systemctl status booking-celery-worker
sudo systemctl status booking-celery-beat
```

### 5. Nginx Configuration

Create `/etc/nginx/sites-available/booking`:

```nginx
upstream booking_app {
    server 127.0.0.1:8000;
}

limit_req_zone $binary_remote_addr zone=webhook:10m rate=30r/s;
limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;

server {
    listen 80;
    server_name yourdomain.com;

    # Let's Encrypt verification
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    # Redirect all other traffic to HTTPS
    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl;
    server_name yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # Health check (no rate limit)
    location /health {
        proxy_pass http://booking_app;
        proxy_set_header Host $host;
    }

    # WhatsApp webhook (higher rate limit)
    location /webhooks/ {
        limit_req zone=webhook burst=50 nodelay;
        proxy_pass http://booking_app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 10s;
    }

    # All other routes
    location / {
        limit_req zone=api burst=20 nodelay;
        proxy_pass http://booking_app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Block common attack paths
    location ~ /\. { deny all; }
    location ~ ^/(wp-admin|wp-login|xmlrpc|phpmyadmin) { return 444; }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/booking /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

---

## Environment Variables Reference

### Required Variables

| Variable | Description | Example |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string (async psycopg driver) | `postgresql+psycopg://booking:password@localhost:5432/booking` |
| `REDIS_URL` | Redis connection string (state, caching) | `redis://:password@localhost:6379/0` |
| `CELERY_BROKER_URL` | Redis connection for Celery broker (separate DB) | `redis://:password@localhost:6379/1` |
| `ENCRYPTION_KEY` | Fernet key for encrypting WABA tokens | Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `META_APP_SECRET` | Meta App Secret for webhook signature validation | From Meta Developer Dashboard |
| `META_WEBHOOK_VERIFY_TOKEN` | Token for Meta webhook verification handshake | Any random string you choose |
| `ENVIRONMENT` | Runtime environment | `production` or `development` |

### Optional Variables

| Variable | Description | Default | Example |
|---|---|---|---|
| `META_APP_ID` | Meta App ID | `""` | `123456789012345` |
| `GOOGLE_OAUTH_CLIENT_ID` | Google OAuth client ID for calendar sync | `""` | `xxx.apps.googleusercontent.com` |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google OAuth client secret | `""` | `GOCSPX-xxx` |
| `GOOGLE_OAUTH_REDIRECT_URI` | Google OAuth redirect URI | `http://localhost:8000/auth/google/callback` | `https://yourdomain.com/auth/google/callback` |
| `RAZORPAY_KEY_ID` | Razorpay API key ID | `""` | `rzp_live_xxxxxxxxxx` |
| `RAZORPAY_KEY_SECRET` | Razorpay API key secret | `""` | Secret from Razorpay Dashboard |
| `RAZORPAY_WEBHOOK_SECRET` | Razorpay webhook verification secret | `""` | From Razorpay Dashboard |
| `GEMINI_API_KEY` | Google Gemini API key for AI features | `""` | `AIzaSy...` |
| `GEMINI_MODEL` | Gemini model to use | `gemini-2.5-flash` | `gemini-2.5-flash` |
| `SENTRY_DSN` | Sentry DSN for error tracking | `""` | `https://xxx@xxx.ingest.sentry.io/xxx` |
| `LOG_LEVEL` | Logging level | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Docker-Only Variables

These are used only in `docker-compose.prod.yml`:

| Variable | Description | Example |
|---|---|---|
| `DB_USER` | PostgreSQL username | `booking` |
| `DB_PASSWORD` | PostgreSQL password | Strong password |
| `DB_NAME` | PostgreSQL database name | `booking` |
| `REDIS_PASSWORD` | Redis password | Strong password |

---

## SSL/TLS Setup

Meta requires HTTPS for webhook URLs. Use Let's Encrypt for free SSL certificates.

### Using Certbot (Standalone)

```bash
# Install certbot
sudo apt install certbot python3-certbot-nginx

# Generate certificate
sudo certbot --nginx -d yourdomain.com

# Auto-renewal (certbot installs a systemd timer automatically)
sudo certbot renew --dry-run
```

### Using Certbot with Docker

If running everything in Docker, generate certs on the host first:

```bash
# Install certbot
sudo apt install certbot

# Stop nginx temporarily if running
docker compose -f docker-compose.prod.yml stop nginx

# Generate certificate
sudo certbot certonly --standalone -d yourdomain.com

# Copy certs to nginx volume
sudo cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem nginx/certs/
sudo cp /etc/letsencrypt/live/yourdomain.com/privkey.pem nginx/certs/

# Update nginx.conf to use HTTPS (uncomment the SSL server block)
# Start nginx
docker compose -f docker-compose.prod.yml up -d nginx
```

### Update nginx.conf for SSL

Uncomment the HTTPS server block in `nginx/nginx.conf`:

```nginx
server {
    listen 443 ssl;
    server_name yourdomain.com;
    ssl_certificate /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;

    # ... location blocks ...
}
```

And enable the HTTP-to-HTTPS redirect:

```nginx
server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$host$request_uri;
}
```

### Auto-Renewal Cron

```bash
# Add to crontab
0 3 * * * certbot renew --quiet && cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem /path/to/nginx/certs/ && cp /etc/letsencrypt/live/yourdomain.com/privkey.pem /path/to/nginx/certs/ && docker compose -f /path/to/docker-compose.prod.yml restart nginx
```

---

## Database

### Initial Migration

After starting PostgreSQL for the first time:

```bash
# Docker deployment
docker compose -f docker-compose.prod.yml exec app alembic upgrade head

# Manual deployment
cd /opt/booking-saas
source .venv/bin/activate
alembic upgrade head
```

### Creating New Migrations

When the SQLAlchemy models change:

```bash
alembic revision --autogenerate -m "description of change"
alembic upgrade head
```

### Backup Strategy

**Automated daily backups:**

```bash
#!/bin/bash
# /opt/backup/backup-db.sh
BACKUP_DIR=/opt/backups/postgres
DATE=$(date +%Y%m%d_%H%M%S)
RETAIN_DAYS=30

mkdir -p $BACKUP_DIR

# Docker deployment
docker compose -f /path/to/docker-compose.prod.yml exec -T db \
  pg_dump -U booking booking | gzip > $BACKUP_DIR/booking_$DATE.sql.gz

# Manual deployment
# pg_dump -U booking booking | gzip > $BACKUP_DIR/booking_$DATE.sql.gz

# Delete backups older than RETAIN_DAYS
find $BACKUP_DIR -name "*.sql.gz" -mtime +$RETAIN_DAYS -delete

echo "Backup completed: booking_$DATE.sql.gz"
```

**Schedule with cron (daily at 2am IST):**

```bash
0 2 * * * /opt/backup/backup-db.sh >> /var/log/backup.log 2>&1
```

**Restore from backup:**

```bash
# Docker
gunzip -c backup_file.sql.gz | docker compose -f docker-compose.prod.yml exec -T db psql -U booking booking

# Manual
gunzip -c backup_file.sql.gz | psql -U booking booking
```

### Redis Persistence

Redis is configured with RDB snapshots by default (data volume: `redisdata`). Conversation state has a 30-minute TTL and is not critical to back up. Rate limit counters expire automatically.

---

## Monitoring

### Health Check Endpoint

```bash
curl https://yourdomain.com/health
```

Response:

```json
{"status": "ok", "environment": "production"}
```

Use this for uptime monitoring with services like UptimeRobot, Pingdom, or your own checks.

### Sentry Error Tracking

1. Create a project at [sentry.io](https://sentry.io) (or self-hosted Sentry)
2. Get the DSN
3. Set `SENTRY_DSN` in your environment

Sentry is automatically initialized when the DSN is present. It captures:
- Unhandled exceptions
- FastAPI request errors
- Background task failures (Celery)

### Structured Logging

The application uses `structlog` for structured JSON logging in production (plain-text in development).

**Log output format (production):**

```json
{"event": "inbound_message", "phone_number_id": "109876543210987", "sender": "+919876543210", "msg_type": "text", "level": "info", "timestamp": "2025-01-15T10:30:00Z"}
```

**View logs:**

```bash
# Docker
docker compose -f docker-compose.prod.yml logs -f app
docker compose -f docker-compose.prod.yml logs -f celery-worker
docker compose -f docker-compose.prod.yml logs -f celery-beat

# Manual (systemd)
journalctl -u booking-app -f
journalctl -u booking-celery-worker -f
```

### Log Aggregation

For centralized logging, pipe JSON logs to your preferred aggregation service:

- **Grafana Loki:** Install Promtail, configure to scrape Docker logs or journald
- **Datadog:** Install the Datadog agent, auto-discovers Docker container logs
- **CloudWatch:** Use the CloudWatch agent on AWS instances

### Key Metrics to Monitor

| Metric | Where to Check | Alert Threshold |
|---|---|---|
| Health check response | GET /health | Down for >1 min |
| Webhook response time | Nginx access logs | >5s p95 |
| Celery queue depth | Redis `celery` key | >100 pending tasks |
| Database connections | PostgreSQL `pg_stat_activity` | >25 active |
| Redis memory | `redis-cli info memory` | >80% of maxmemory |
| Disk usage | `df -h` | >85% |
| Error rate | Sentry | >10 events/hour |

### Request Tracing

Every request gets a unique Request ID (UUID) via the `RequestIDMiddleware`:
- Set via `X-Request-ID` header (if provided by client/load balancer)
- Auto-generated if not provided
- Returned in the `X-Request-ID` response header
- Bound to all log entries for that request via structlog contextvars

---

## Scaling

### Current Architecture

The system is designed as a **modular monolith** on a single VPS. This handles ~50-100 tenants comfortably.

### Connection Pooling

SQLAlchemy is configured with:
- `pool_size=20` -- maintained connections
- `max_overflow=10` -- additional connections under load
- Total maximum: 30 database connections

For more tenants, increase these values or use PgBouncer:

```bash
# Install PgBouncer
sudo apt install pgbouncer

# Configure /etc/pgbouncer/pgbouncer.ini
[databases]
booking = host=localhost dbname=booking

[pgbouncer]
pool_mode = transaction
max_client_conn = 200
default_pool_size = 20
```

### Horizontal Scaling Considerations

When a single VPS is no longer sufficient:

**Phase 1: Vertical scaling**
- Upgrade VPS to 8 vCPU / 16 GB RAM
- Increase uvicorn workers to 4
- Increase Celery concurrency to 4

**Phase 2: Separate database**
- Move PostgreSQL to a managed service (AWS RDS, DigitalOcean Managed Databases)
- Move Redis to managed Redis (AWS ElastiCache, DigitalOcean Managed Redis)
- This allows the VPS to focus on application compute

**Phase 3: Multiple app instances**
- Run multiple VPS instances behind a load balancer
- Use shared PostgreSQL and Redis
- Celery workers can run on any instance (Redis broker handles coordination)
- Ensure session state is in Redis (already the case -- conversation state is in Redis)

### Rate Limiting

Two layers of rate limiting protect the system:

1. **Nginx level:** `limit_req_zone` -- 30 req/s for webhooks, 10 req/s for API
2. **Application level:** Redis-based `RateLimitMiddleware` -- 100/min for webhooks, 30/min for dev, 60/min default

---

## Updates

### Zero-Downtime Deployment Procedure

```bash
# 1. Pull latest code
cd /path/to/saloon-book-bot
git pull origin main

# 2. Build new images
docker compose -f docker-compose.prod.yml build

# 3. Run database migrations (if any)
docker compose -f docker-compose.prod.yml exec app alembic upgrade head

# 4. Rolling restart (one service at a time)
docker compose -f docker-compose.prod.yml up -d --no-deps app
docker compose -f docker-compose.prod.yml up -d --no-deps celery-worker
docker compose -f docker-compose.prod.yml up -d --no-deps celery-beat

# 5. Verify health
curl https://yourdomain.com/health

# 6. Check logs for errors
docker compose -f docker-compose.prod.yml logs --tail=50 app
```

### Manual Deployment Updates

```bash
cd /opt/booking-saas
git pull origin main

source .venv/bin/activate
pip install .

alembic upgrade head

sudo systemctl restart booking-app
sudo systemctl restart booking-celery-worker
sudo systemctl restart booking-celery-beat
```

### Rollback

If something goes wrong:

```bash
# Docker: rollback to previous image
docker compose -f docker-compose.prod.yml down
git checkout <previous-commit>
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d

# Database: rollback migration
alembic downgrade -1
```

### Pre-Deployment Checklist

- [ ] Run tests locally: `pytest`
- [ ] Check for unapplied migrations: `alembic check`
- [ ] Review environment variable changes
- [ ] Back up database before major changes
- [ ] Notify active tenants of maintenance window (if downtime expected)

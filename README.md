# GLDC — Python Production Application

Production Flask application for GLDC. Node.js is not required at runtime.

## Stack
- Python 3.12 + Flask
- Gunicorn
- MongoDB / MongoDB Atlas
- Google Drive/Sheets service account
- SMTP email OTP
- Safaricom Daraja / M-Pesa
- Hardened HTTP security headers, CSRF protection, signed secure sessions, rate limiting and structured request IDs

## 1. Configure secrets

```bash
cp .env.example .env
```

Generate the application secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set the resulting value in `AUTH_SECRET`. Use real production credentials for MongoDB, SMTP, Daraja and Google.

## 2. Local production-like run

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -c "from app import app; print('startup validation OK')"
gunicorn --config gunicorn.conf.py wsgi:application
```

Open `https://companygldc.vercel.app`.

## 3. Docker deployment

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f gldc
```

Put TLS in front of the container using your cloud load balancer, Nginx, Caddy or a managed reverse proxy. The Daraja callback must be a public HTTPS endpoint.

## 4. Google service account

Share the target Drive folder and any spreadsheet with the service-account email. Viewer permission is enough for read-only access. Never commit the private key or service-account JSON.

## 5. M-Pesa

Use production Daraja credentials. Configure `DARAJA_CALLBACK_URL` to the public HTTPS callback endpoint. Payment status is finalized by the Daraja callback, not by the browser.

## 6. Production checklist

- [ ] Real `AUTH_SECRET` generated and stored in a secret manager/environment
- [ ] `.env` is never committed
- [ ] MongoDB Atlas network/IP access and least-privilege database user configured
- [ ] SMTP App Password/provider credential configured
- [ ] Google folder/spreadsheet shared with service account
- [ ] Daraja production credentials and HTTPS callback configured
- [ ] TLS certificate and domain configured at the reverse proxy/load balancer
- [ ] `/api/ready` returns HTTP 200 after deployment
- [ ] Admin login tested
- [ ] Email OTP tested
- [ ] Lead submission + verification tested
- [ ] M-Pesa STK + callback tested with a real controlled transaction
- [ ] Backups, monitoring and log retention enabled at the infrastructure layer

## Important

The application intentionally fails fast in `APP_ENV=production` when required configuration is missing. No demo admin credentials or development secrets are embedded.

## 7. Vercel deployment

This repository includes `vercel.json` and `api/index.py` for Vercel's Python runtime. The Flask module is intentionally import-safe: MongoDB initialization and production configuration checks are performed lazily instead of crashing the serverless function during import.

In Vercel Project Settings → Environment Variables, configure the production variables from `.env.example`. At minimum, set `MONGODB_URI`, `MONGODB_DB_NAME`, `AUTH_SECRET`, SMTP variables, and the Google/Daraja variables if those features are enabled.

After deployment, test:

- `/api/health` — function health/import check
- `/api/ready` — environment + MongoDB readiness check
- `/` — public application

For the M-Pesa callback, use the deployed HTTPS URL ending in `/api/payments/callback`.

### Vercel notes

- Do not upload `.env` or service-account JSON into the repository.
- Use Vercel Environment Variables for secrets.
- MongoDB Atlas must permit connections from Vercel. For stronger security, use the appropriate Atlas network-access strategy for your deployment rather than exposing the database broadly.
- Vercel serverless functions are stateless. Application rate limiting therefore uses MongoDB, not process memory.

## Vercel / PyMongo import-safety fix

The Vercel serverless entry point is `api/index.py`. MongoDB initialization is lazy and is never performed during module import. PyMongo `Database` objects are always checked with `is None` / `is not None`; they are never evaluated as booleans. This prevents `NotImplementedError: Database objects do not implement truth value testing` during Vercel cold starts.

## Hosted Vercel environment compatibility

Use `.env.hosted.example` as the Python-compatible template for the existing hosted configuration. `VERCEL_HOSTED_ENV_MAPPING.md` documents aliases from the previous Node environment naming scheme. Do not commit production secrets.


## V19 Membership communications and payment recovery
- Admin can email a selected member with a clear action request, requested fields and deadline.
- Failed/unconfirmed M-Pesa payments can be submitted by the member using the M-Pesa transaction code, amount, phone and payment date; the payment remains pending until an administrator verifies it.
- Verified payment is required before membership approval.
- Unfinished applications without a successful or under-review payment can be removed through a secure restart link so the member can recreate the application.


## V20 Security Fix
Admin mutating requests now automatically attach the server-issued CSRF token, including membership APPROVE / REQUEST CHANGES / REJECT actions. HTTP 403 CSRF failures are shown as a graceful session-refresh message instead of a raw security error.

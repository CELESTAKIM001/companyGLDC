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

Open `http://127.0.0.1:5000`.

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

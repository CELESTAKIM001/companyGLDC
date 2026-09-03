# GLDC Python Production — Final Baseline

This package is the Python/Flask production baseline for the GLDC website and Management Console.

## Vercel entry point

`api/index.py` imports `app` from `app.py`.

## Required production variables

At minimum configure:

- `APP_ENV=production`
- `APP_URL`
- `MONGODB_URI`
- `MONGODB_DB_NAME`
- `AUTH_SECRET`
- `INITIAL_ADMIN_EMAIL`
- `INITIAL_ADMIN_PASSWORD`
- SMTP variables (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`)

If enabled, also configure Daraja and Google Drive variables.

## Important compatibility

The application accepts the original hosted aliases where appropriate, including `NODE_ENV`, `JWT_ACCESS_SECRET`, `ADMIN_EMAIL`, `ADMIN_INITIAL_PASSWORD`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `EMAIL_FROM_ADDRESS`, `EMAIL_REPLY_TO`, and the Daraja party/till aliases.

## OTP

MongoDB is created with `tz_aware=True`, and OTP verification also defensively normalizes legacy dates. Verification failures are returned as JSON rather than Vercel HTML/plain-text errors.

## Google Drive

The admin console lists real files with Drive pagination. PDFs can be opened/downloaded. ZIP/DOCX/XLSX/images and other binary files use download behavior. Google Workspace documents are exported when appropriate.

## Invoices

Admins can create invoices for an existing lead or any recipient email, generate a PDF invoice, store the invoice in MongoDB, and send it by SMTP.

## Deployment

1. Upload/deploy the project root containing `vercel.json`, `api/index.py`, and `app.py`.
2. Configure environment variables in Vercel.
3. Do not commit `.env` or service-account credentials.
4. Redeploy after changing environment variables.
5. Test `/api/health` and `/api/ready` before testing business workflows.
6. Hard refresh the browser after deployment (`Ctrl+Shift+R`).

## Scope

The source requirements define a full Management Console covering CRM/leads, clients, projects, tasks, quotations, invoices, payments, documents, CMS, services, portfolio, team, testimonials, content/blog, calendar, notifications, reports, users/roles, audit logs, brand settings and system settings. This package preserves the implemented production foundations and documents the remaining scope rather than fabricating completion.

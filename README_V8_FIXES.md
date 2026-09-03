# GLDC V8 Debug / Invoices

## OTP verification
- `/api/auth/verify-otp` validates input, catches runtime/database failures, and always returns JSON.
- Frontend API helper safely parses non-JSON server errors and exposes request IDs.
- `app.js` cache-busted from `base.html` so older cached JavaScript is not reused.
- Deploy this package as the active Vercel deployment before testing.

## Admin invoices
- Admin sidebar now includes **Invoices**.
- Select an existing lead to populate recipient name/email, or enter any recipient email manually.
- Create invoice in KES with amount, description and due date.
- Optionally send immediately by email.
- Generated invoice PDF is attached to the email.
- Invoice history is stored in MongoDB `invoices` collection.
- Admin can download or resend any invoice.

Required for sending:
- `SMTP_HOST`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_FROM`

Optional company fields:
- `COMPANY_NAME`
- `COMPANY_PHONE`

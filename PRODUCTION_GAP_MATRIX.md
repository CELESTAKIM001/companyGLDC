# GLDC Python Production Gap Matrix

This build is the hosted-environment compatibility and production-hardening release. It does **not** falsely claim that every business module in the full GLDC specification is already implemented.

## Already implemented / hardened

- Public Flask website and responsive templates
- MongoDB Atlas connection and lazy serverless initialization
- Session authentication, CSRF, rate limiting, secure cookies
- Email OTP request/verification path hardening
- Google Drive service-account listing, pagination, PDF view, and binary download
- Google Sheets configuration/read foundation
- Safaricom Daraja STK foundation and callback endpoint
- Lead/enquiry creation and admin lead listing
- Basic content/settings management
- Invoice creation, PDF generation, email delivery, download, resend
- Health/readiness diagnostics
- Request IDs and generic JSON API errors
- Hosted environment aliases and configuration validation
- Configurable admin path
- 25 MB upload request limit compatibility
- Production deployment files for Vercel/Gunicorn/Docker

## Full specification modules that still require full CRUD/workflow implementation

- Clients
- Projects and project members
- Tasks/milestones
- Quotations and quotation version history/acceptance
- Full invoice lifecycle and immutable financial ledger
- Payment ledger/reconciliation/refund/reversal workflows
- Advanced document metadata/versioning/archive/permissions
- Full Website CMS for pages/services/projects/team/testimonials/media/service areas/blog
- Calendar and consultation booking
- Notification center and notification delivery rules
- Reports and financial analytics
- Users, roles, granular permissions
- Audit log viewer and full entity activity timelines
- Brand settings UI
- System settings UI
- Client portal
- Advanced messaging/WhatsApp/SMS integrations
- Backup monitoring/restore tooling
- Analytics/SEO management UI

These modules are part of the supplied GLDC requirements and should be implemented rather than represented by fake/demo statistics or placeholder buttons.

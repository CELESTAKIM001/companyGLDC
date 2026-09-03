# GLDC Full Python Platform — Rebuilt from the Requirements

This build supersedes the shallow V12 baseline. It is structured around the GLDC requirements document and the connected lifecycle:

Visitor → Enquiry → Consultation → Proposal/Quote → Client → Project → Tasks/Milestones → Documents → Invoice → Payment → Completion.

## Included
- Full Flask/Python production foundation
- Public GLDC website pages: Home, About, Services, Service Detail, Projects, Project Detail, Team, Testimonials, Service Areas, Contact, Request Quote, FAQ, Insights, Consultation, Privacy, Terms
- Management console with Dashboard, CRM Leads, Clients, Projects, Tasks, Calendar, Quotations, Invoices, Payments, Reports, Documents, CMS, Google Drive, Notifications, Users/Roles, Audit Logs and Brand/System settings
- Lead-to-client conversion
- Client/project/task records
- Quotation PDF generation, issue/send workflow and decision/version history
- Invoice PDF generation, email delivery, lifecycle status and resend/download
- Payment ledger with verified Daraja callback support and authorized manual payment records
- Financial reporting and outstanding balance calculations
- Document metadata, project/client association, access level and versioning foundation
- Google Drive pagination, PDF inline viewing and non-PDF download/export support retained from the previous build
- SMTP OTP authentication and MongoDB timezone-aware OTP verification retained/fixed
- CSRF/security headers/rate limiting/session controls retained
- CMS collections for services, projects, team, testimonials, posts, service areas, FAQs and pages
- Notifications, users, audit trail and consultation records
- Vercel, Gunicorn, Docker and Nginx deployment files

## Important
The application contains no production secrets. Configure the supplied Python environment through hosting environment variables.
Real GLDC content, projects, staff, testimonials and media should be entered through the CMS when supplied; the requirements prohibit fake business information.

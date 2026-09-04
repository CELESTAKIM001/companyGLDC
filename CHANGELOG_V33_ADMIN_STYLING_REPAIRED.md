# GLDC V33 — Admin Styling Repaired

## Problem fixed
The V32 member-dashboard package was based on the earlier admin template and reintroduced the broken admin console layout. The result was dashboard and membership sections visually appearing together, compressed membership cards, and a sidebar/content width conflict.

## Changes
- Restored the hardened V32 admin console CSS/layout.
- Admin sidebar is fixed-width and cannot shrink because of wide tables.
- Admin main content uses the full available viewport width.
- `.view` sections are isolated so only the active admin section is displayed.
- Wide tables are contained in horizontal scroll wrappers.
- Added responsive breakpoints for 1250px, 900px and 760px.
- Removed the public site header/footer while the admin console is active.
- Preserved the latest V32 backend and member dashboard/certificate/renewal functionality.
- Preserved membership CRUD, payment prompts, receipts, QR verification, certificates, email delivery, Daraja callback, Drive and CRM functionality.

## Verification
- Python source compiles successfully.
- Jinja templates parse successfully.

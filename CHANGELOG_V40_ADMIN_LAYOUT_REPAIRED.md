# V40 — Admin Console Layout Repair

## Critical fix

The Membership section in the previous build contained one extra closing `</div>` after the generic M-Pesa prompt panel. That prematurely closed the admin `.main` container in the browser DOM. As a result, subsequent admin sections (Office Locations, Quotations, Invoices, Payments, Financial Reports, etc.) were rendered outside the admin main container and multiple screens could appear together, producing the broken/overlapping layout shown in production.

## V40 fixes

- Removed the extra closing `</div>` from the Membership section.
- Verified all 22 admin `<section class="view">` elements are direct children of `.main`.
- Strengthened admin view isolation CSS to target `.full-console .view`, not only direct-child selectors.
- Strengthened the `go()` JavaScript to hide all admin views before activating one.
- Preserved the rebuilt V36/V37 admin visual design and all V38/V39 member functionality.
- No database schema or credential changes.

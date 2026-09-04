# GLDC V45 — Admin Membership Details Loading Fix

## Fixed
- Admin Membership `loadMembership()` no longer crashes on a missing `promptPlan` element.
- The obsolete `promptPlan` DOM write was removed; the generic M-Pesa prompt is intentionally payer/amount based and does not require a plan selector.
- Membership data loading now uses `Promise.allSettled()` so one failed endpoint cannot prevent Member Records, Renewal History, Certificate History, or Plans from rendering.
- Individual API failures are displayed in the affected Admin panel instead of leaving it indefinitely at `Loading...`.
- Preserved V44 canonical production URL / QR behavior and all prior member/admin functionality.

## Verification
- Admin JavaScript syntax check: PASS
- Python compilation: PASS

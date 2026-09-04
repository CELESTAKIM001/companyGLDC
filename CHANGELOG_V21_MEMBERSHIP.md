# V21 — Membership process upgrades

Built on top of the existing V20 rebuild. The resume-registration flow (email →
OTP → resume token → payment → GLDC review) already existed end-to-end; this
pass adds the two pieces that were missing.

## 1. Admin alert when a registration/payment is abandoned

- `app.py`: `_find_abandoned_members()`, `_notify_admin_member_abandoned()`,
  and new routes:
  - `GET /api/cron/check-abandoned-registrations` — scheduled check, secured
    by `CRON_SECRET` (Vercel Cron sends `Authorization: Bearer <CRON_SECRET>`
    automatically once that env var exists on the project — see
    `vercel.json`, scheduled daily at 06:00 UTC).
  - `GET /api/admin/membership/abandoned` — list for the admin panel.
  - `POST /api/admin/membership/abandoned/<member_id>/remind` — manual nudge
    email to one member.
- A member is flagged stalled when:
  - `EMAIL_PENDING` for more than `MEMBERSHIP_ABANDON_EMAIL_HOURS` (default 24h), or
  - `PENDING_PAYMENT` / `PAYMENT_FAILED` / `PAYMENT_PENDING` / `RENEWAL_PENDING`
    for more than `MEMBERSHIP_ABANDON_PAYMENT_HOURS` (default 48h).
  - Re-notified every `MEMBERSHIP_ABANDON_RENOTIFY_HOURS` (default 72h) if still stalled.
- On flag: writes an ADMIN-audience row to `notifications`, emails every
  active `SUPER ADMIN / OWNER` / `ADMIN` user, and stamps
  `abandonedNotifiedAt` / `abandonedStage` / `abandonedNotifyCount` on the
  member so it isn't re-emailed every run.
- New env vars (added to `.env.example`, `.env.production.example`,
  `.env.hosted.example`): `CRON_SECRET`, `MEMBERSHIP_ABANDON_EMAIL_HOURS`,
  `MEMBERSHIP_ABANDON_PAYMENT_HOURS`, `MEMBERSHIP_ABANDON_RENOTIFY_HOURS`.
  **You must set `CRON_SECRET` in Vercel project env vars** for the cron job
  to authenticate — without it the endpoint returns 503 by design (fails closed).
- Admin panel (`templates/admin.html`, Membership tab): new "Stalled
  applications" list with SEND REMINDER / EDIT buttons, auto-loaded whenever
  the Membership tab loads.

## 2. Admin can edit a member record directly

- `PATCH /api/admin/members/<member_id>` in `app.py`. Editable: name, email,
  phone, profession, company, location, bio, portfolioUrl, membershipNumber,
  validFrom, validUntil, status, adminMessage.
  - Changing email resets `emailVerified` to false and (if the member was
    still mid-registration) resets status to `EMAIL_PENDING` with a fresh
    resume token, rather than silently trusting an unverified address.
  - Setting `status=ACTIVE` is blocked unless a real membership number and
    validity dates are already present — this endpoint will not let someone
    skip certificate issuance. Use the existing Approve flow
    (`/api/admin/members/<id>/decision`) for that.
  - Every change is written to the audit log with before/after values.
- Admin panel: every member row now has an EDIT button opening a modal
  (`#memberEditModal`) with all the fields above; also reachable from the
  Stalled applications list.

## 3. Thresholds made admin-tunable at runtime (no redeploy)

Mirrors the existing `renewalWindowDays` pattern (`db.settings` key
`membership_policy`) instead of leaving the new thresholds env-only:

- Admin panel → Settings → "Abandoned application alerts" now has 3 fields:
  hours before email-unverified alert, hours before payment-incomplete
  alert, hours before re-alerting. Saved via `POST /api/admin/settings`
  (validated 1–720h each), read back via `GET /api/admin/settings`.
- `_find_abandoned_members()` reads these from `db.settings` first, falling
  back to the `MEMBERSHIP_ABANDON_*_HOURS` env vars only if nothing has been
  saved yet — so env vars still work as sane defaults on a fresh deploy, but
  staff can retune without touching Vercel.
- `CRON_SECRET` stays env-only (rendering a secret into an editable web form
  would defeat its purpose) — but the Settings page now shows a plain
  "CONFIGURED / NOT SET" status line for it, plus whether admin email alerts
  are enabled, so staff can self-diagnose without opening the Vercel dashboard.
- New `POST /api/admin/membership/abandoned/run-check` lets an admin trigger
  the exact same check the cron job runs, on demand — a "RUN CHECK NOW"
  button next to "Stalled applications" — so the whole feature can be tested
  immediately after deploy instead of waiting for the daily cron.

## Not touched

- The registration/OTP/payment flow itself — already solid, left as-is.
- Member-side "edit while mid-registration" — members can already edit their
  own profile once logged in via resume (`/api/membership/profile`); this
  release only adds the *admin*-side edit and the abandonment alerting.

## Suggested next steps (not built this round)

- If you want members to correct an already-submitted M-Pesa transaction code
  before admin verifies it, that would need a small `PATCH` on
  `/api/membership/submit-payment-reference` — say the word and I'll add it.
- Vercel Hobby plan only allows daily cron; if you're on Pro you can tighten
  `vercel.json`'s schedule (e.g. `0 */6 * * *` for every 6 hours) for faster
  abandonment alerts.

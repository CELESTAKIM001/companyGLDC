# GLDC V31 — Member Public View / Read-Only Profile Fix

- Fixed `/members/<profileSlug>` returning 404 for approved members whose synced status is `EXPIRING_SOON`.
- Public profile now accepts `ACTIVE` and `EXPIRING_SOON` members and synchronizes membership state before deciding visibility.
- Member profiles are explicitly read-only; viewing another member never enters edit mode and requires no access to that member's account.
- Removed public exposure of member phone and email from `_member_public()`.
- Added optional public portfolio link to the read-only profile.
- Updated member directory API to synchronize membership state before filtering, so directory and profile visibility stay consistent.
- Added clear VIEW ONLY wording and privacy note to the profile UI.

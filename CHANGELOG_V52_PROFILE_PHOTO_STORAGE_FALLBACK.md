# V52 — Profile Photo Storage Resilience

## Fixed
- `/api/membership/profile-photo` no longer returns a generic 503 solely because Google Drive upload is temporarily unavailable.
- Google Drive remains the primary storage target.
- If Drive upload fails, the member profile photo is stored in the dedicated `member_profile_photos` MongoDB collection instead of the ephemeral Vercel filesystem.
- Photo retrieval supports both Drive and MongoDB storage while retaining the existing member/public access checks.
- Added structured storage state (`photoStorage`) so the application knows where the current photo is stored.
- If both Drive and MongoDB storage fail, the API returns a specific `PROFILE_PHOTO_STORAGE_UNAVAILABLE` error.

## Security
- No local/Vercel filesystem fallback is used.
- Maximum image size remains 2 MB.
- PNG/JPG/WEBP signature validation remains enforced.
- Public/member authorization checks remain enforced on photo retrieval.

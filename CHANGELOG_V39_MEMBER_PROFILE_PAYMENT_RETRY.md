# V39 — Member Profile Location, Drive Photo & Payment Retry

## Member profile
- Members can edit their profile location text and choose an exact point on an interactive Leaflet/OpenStreetMap map.
- Latitude/longitude are validated before saving.
- Members can add or replace a profile picture from the dashboard profile editor.
- Profile pictures are stored in the configured Google Drive folder.
- PNG, JPG/JPEG and WEBP are validated using the actual file signature, so a PNG is not rejected simply because the browser reports an unexpected MIME type.
- Stored photo MIME type is preserved and PNGs are served as `image/png`.
- The member's own pending/incomplete profile photo can be viewed securely; public photos remain restricted to approved public member statuses.

## Payments
- FAILED and CANCELLED membership payment records now show an active **PAY HERE** button.
- PAY HERE creates a new M-Pesa prompt using the original membership plan and payment purpose.
- Initial membership, renewal and upgrade payment retries are supported.
- Retry records are linked with `retryOf` for audit/history.
- Free (KES 0) tiers complete without an M-Pesa prompt.
- Existing membership is not silently replaced when an upgrade payment is retried.

## Validation
- `app.py` compiles successfully.
- All Jinja templates parse successfully.
- Member dashboard JavaScript passes `node --check`.

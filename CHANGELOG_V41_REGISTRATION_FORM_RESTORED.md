# V41 — Registration Form Restored

## Fix
The `/member` membership registration page rendered the static registration shell and lifecycle panel but did not invoke `renderRegister()` after the previous layout merge. This left the `#stage` container blank.

## Change
Added a DOMContentLoaded initialization that calls `renderRegister()` and shows a friendly error if initialization fails.

## Preserved
All V40 admin layout repairs and V39 member profile/location/photo/payment-retry work are retained.

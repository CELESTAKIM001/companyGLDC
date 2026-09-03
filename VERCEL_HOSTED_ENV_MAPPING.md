# GLDC hosted environment mapping

The Python application is intentionally compatible with the hosted variable names supplied for the previous Node deployment. This prevents a valid Vercel configuration from being treated as missing configuration.

| Hosted name | Python behavior |
|---|---|
| `NODE_ENV` | accepted as fallback for `APP_ENV` |
| `TIMEZONE` | accepted as fallback for `APP_TIMEZONE` |
| `JWT_ACCESS_SECRET` | accepted as fallback for `AUTH_SECRET` |
| `ADMIN_EMAIL` | accepted as fallback for `INITIAL_ADMIN_EMAIL` |
| `ADMIN_INITIAL_PASSWORD` | accepted as fallback for `INITIAL_ADMIN_PASSWORD` |
| `ADMIN_NAME` | used for bootstrap administrator display name |
| `GMAIL_USER` | accepted as fallback for `SMTP_USER` |
| `GMAIL_APP_PASSWORD` | accepted as fallback for `SMTP_PASSWORD` |
| `EMAIL_FROM_ADDRESS` | accepted as fallback for `SMTP_FROM` |
| `EMAIL_REPLY_TO` | accepted as fallback for `SMTP_REPLY_TO` |
| `DARAJA_PARTY_A_SHORTCODE` | accepted as fallback for `DARAJA_SHORTCODE` |
| `DARAJA_PARTY_B_BUYGOODS_TILL` / `DARAJA_BUYGOODS_TILL` | accepted as fallback for `DARAJA_TILL_NUMBER` |
| `MPESA_TRANSACTION_TYPE` | accepted as fallback for `DARAJA_TRANSACTION_TYPE` |
| `MAX_FILE_SIZE_MB` | accepted as fallback for request upload size |

## Important fixes

- `DARAJA_CALLBACK_URL` may be the hosted base URL; Python appends `/api/payments/callback` when needed.
- `GOOGLE_PRIVATE_KEY` supports escaped `\n` newlines.
- Google Drive binary files are downloaded separately from metadata and non-PDF files use download behavior.
- Health and readiness endpoints report configuration and database state separately.
- Admin path can be configured with `ADMIN_PATH`; `/admin` remains available as a compatibility route.

## Do not paste production secrets into source code

The environment values supplied during debugging contained credentials/private secrets. They should be rotated in their respective providers and replaced in Vercel with the new values. The project files generated here contain placeholders only.

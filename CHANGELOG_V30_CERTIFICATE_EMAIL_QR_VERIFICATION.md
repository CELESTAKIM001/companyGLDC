# V30 — Membership Certificate Email + QR Verification

## Approved member certificate delivery
- Approved memberships and approved renewals continue to generate an official PDF certificate.
- The certificate is automatically emailed to the member's registered email address after approval.
- If SMTP delivery fails, membership approval remains successful and the failed delivery is recorded so the certificate can be resent later.

## Admin certificate resend
- Admin Membership > Certificate history now has `SEND TO EMAIL`.
- Admin can enter any valid recipient email address and resend the selected certificate PDF.
- Each resend is recorded in `email_deliveries` and the audit log.

## Member self-service email
- Member Dashboard > Certificates now includes `EMAIL TO ME`.
- The member can resend their own certificate to the verified email address on their account.

## QR verification
- Every certificate PDF contains a QR code linked to the official GLDC certificate verification page.
- The verification page reads the certificate from the GLDC database using its unique certificate number.
- Verification shows the member, membership number, plan, certificate number, validity dates, issue date and current certificate status.
- Expired/replaced certificates remain verifiable as historical records and are not falsely shown as active.
- Email messages include the official verification link and explain that the QR code on the PDF is the verification method.

## Security
- Admin resend requires admin authentication and CSRF protection.
- Member resend is restricted to the authenticated member owning that certificate.
- Certificate PDFs are regenerated from the database if the archived Drive copy is unavailable.

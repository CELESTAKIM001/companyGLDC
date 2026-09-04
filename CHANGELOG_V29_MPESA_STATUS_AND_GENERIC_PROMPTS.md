# V29 — M-Pesa Result Handling + Generic Payment Prompts

## M-Pesa result states
- `SUCCESSFUL` is recorded when Daraja `ResultCode = 0`.
- `CANCELLED` is recorded when the customer cancels the STK prompt (Daraja code `1032`, or a cancellation result description).
- Other non-zero callback results are recorded as `FAILED`.
- Failed/cancelled member payments move the application back to `PAYMENT_FAILED` so the user can retry.
- Successful member payments continue to `PENDING_REVIEW`.
- A secure payment-status endpoint lets authenticated members/admins see the exact final result.

## User-facing messages
- Successful: **Payment successful** + receipt/reference.
- Cancelled: **User cancelled the transaction** + **TRY PAYMENT AGAIN**.
- Failed: **Payment failed** + **RECHECK & TRY AGAIN**.
- Pending: keeps polling while the STK prompt is outstanding.

## Generic admin M-Pesa prompts
The existing admin endpoint `/api/admin/membership/payments/prompt` is now backward-compatible but works as a **general GLDC payment prompt**. A member is optional.

Admin can enter:
- payer name
- Kenyan phone number
- optional email
- any positive KES amount
- reference
- description
- optional existing member association

This supports membership fees, invoices, project deposits, consultation fees, orders and other GLDC payments.

Successful generic prompts can email the payer an official receipt when an email address is supplied. The receipt remains linked to the database-backed public verification page.

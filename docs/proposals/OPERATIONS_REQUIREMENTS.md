# Operations modules: minimum decisions (Q7)

Status: **REQUIREMENTS FOR DECISION — nothing built.** 12 Nov 2026. Recommendations below are
suggestions only, not approvals.

## What already exists

| Module | Existing |
|---|---|
| Maintenance | Reimbursement expenses per contract (expense types, review, invoicing). Space operational status (e.g. under maintenance) on spaces. No maintenance requests or work orders. |
| Support | Nothing. The client app has a help entry point only. |
| Notifications | Email for client-app invitations and sign-in codes (via Supabase Auth). In-app activity history for customers. No other emails, SMS or push. |
| Reports | Dashboard figures and Accounts lists (balances, deposits, invoices, payments). No exports. |

## Minimum decisions per module

### Maintenance requests
1. Who can raise one: customers in the client app, admins, or both? *(Recommended: both.)*
2. Categories (e.g. electrical, plumbing, cleaning, IT) and priorities. *(Recommended: a fixed list the admin can edit.)*
3. Statuses: Open → In progress → Resolved → Closed? *(Recommended.)*
4. Response/resolution time targets? *(Recommended: none until agreed.)*
5. Can a request become a chargeable expense? *(Only through the existing reimbursement review.)*

Impact: new tables (requests, comments, attachments via existing file storage); admin and client screens.

### Support
1. Is support separate from maintenance (billing/contract questions)? *(Recommended: one "requests" module with a type.)*
2. Contact channels shown to customers (phone, email, WhatsApp)? Needs the actual details.

### Notifications
1. Which events notify the customer: booking confirmed/cancelled, contract approved, hold ending, invoice issued, payment confirmed? *(Recommended: these, by email.)*
2. Channels: email only first; SMS/WhatsApp need a provider and consent.
3. Admin alerts: holds ending, contracts ending in 60 days, payments awaiting verification. *(Recommended: in-app list, which partly exists.)*

Impact: an email provider configuration; a notification log table; no change to existing data.

### Reports
1. Which reports: occupancy by property, revenue invoiced/collected by month, outstanding by tenant, contracts ending, lead funnel? *(Recommended: these five.)*
2. Format: on-screen with CSV export first.
3. Definitions of occupancy and revenue (e.g. invoiced vs collected) — must be confirmed, related to Q3 and D5–D7.

## Dependencies

- Reports on money depend on D5–D7 (tax, advances, due dates).
- Notifications about holds depend on M-1–M-4.

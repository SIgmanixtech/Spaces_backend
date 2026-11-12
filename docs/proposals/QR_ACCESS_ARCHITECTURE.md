# QR access: technical architecture (proposal)

Status: **ARCHITECTURE ONLY — nothing is built or enabled.** The QR business rules (recorded
12 Nov, not confirmed) and the door/device requirements need owner approval first. The client
app states plainly that QR access is not available (`qr_access: false`). No physical access
control exists or is claimed.

## 1. What the rules (to confirm) ask for

- A booking-specific QR for short-term bookings, valid only for the booking's time.
- A permanent customer QR for customers with a completed contract, valid while the contract holds.
- The backend checks every entry; no grace period; extensions need approval.
- Revocation when a booking is cancelled or a contract ends, and an audit trail.

## 2. Components

```
Client app ──(shows QR)──▶ Door reader/kiosk ──(token)──▶ Backend /access/verify ──▶ allow/deny
                                                              │
Admin portal ◀── access log, passes, revocations ────────────┘
```

- **Access pass** (new table `access_passes`): id, kind (BOOKING | CUSTOMER), tenant_id,
  booking_id or contract_id, property_id, space scope, valid_from, valid_until, status
  (ACTIVE | REVOKED | EXPIRED), revoked_at/by/reason, created_at.
- **QR content**: a short random token (opaque, ≥128 bits) bound to the pass, never personal
  data. Stored hashed. Rotating QR (time-based, e.g. 30 s) is optional and recommended for
  permanent customer passes to stop screenshots being shared.
- **Verification** `POST /access/verify` (device-authenticated with a per-device key, not a
  customer token): token + device id → allow/deny + reason. Checks pass status, validity
  window (business time zone), that the booking/contract still holds (allocation engine),
  and that the device's door is inside the pass's space scope. No grace period.
- **Devices** (new table `access_devices`): id, property, door/space scope, key hash, status.
- **Audit** (new table `access_events`, append-only): pass, device, result, reason, time.
  Every decision is recorded, allowed or denied.

## 3. Lifecycle

| Event | Effect |
|---|---|
| Booking confirmed | Booking pass created for start–end. |
| Booking moved | Pass window moved. |
| Booking cancelled / converted | Pass revoked. |
| Contract completed (approved + signed) | Customer pass created for the contract dates. |
| Contract extended / renewed | Window extended only after approval (extension rule). |
| Contract terminated / expired | Pass revoked / expires on the last day. |
| Admin revokes | Immediate; reason required. |

## 4. Hardware integration (later)

Readers either call `/access/verify` online (preferred) or receive a signed allow-list for
offline operation (needs the device vendor's API). Door release is the vendor's controller;
the backend only answers allow/deny. Nothing here works until a device is chosen and tested.

## 5. Impact

- Database: three new tables (migration, additive). API: client endpoint to fetch the current
  pass/QR; admin endpoints for passes, devices, revocation, access log; device verify endpoint.
- Security: device keys, rate limiting on verify, hashed tokens, no personal data in the QR.

## 6. Decisions needed

1. Confirm the QR rules above (booking QR, customer QR, no grace period, extension approval).
2. Which doors/devices, vendor, online or offline verification.
3. Should a customer pass cover the whole property or only the contracted spaces?
4. Rotating QR for customer passes: yes/no.

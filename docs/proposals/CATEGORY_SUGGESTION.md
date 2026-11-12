# Proposal: contract category suggested from the space type (D8.2, M-6)

Status: **PROPOSAL — not implemented. Owner decision required.** 12 Nov 2026.

D8.2 (2 Nov): "the admin picks the category; the space suggests one". The suggestion needs a
mapping from space type to category, which was never confirmed. It overlaps Q5 (which space
types need a contract).

## Proposed mapping (suggestion only; the admin can always change it)

| Space type | Suggested category | Confidence | Note |
|---|---|---|---|
| Parking slot | Parking | Clear | Only parking slots are parking. |
| Private office, Cabin | Office | Likely | Rooms for one team. |
| Floor (whole floor), Portion | Office | Likely | Could also be Commercial; admin decides. |
| Desk, Seat, Dedicated workstation, Flexible workstation, Coworking | Space | Likely | Coworking seats. |
| Meeting room, Conference room, Training room | No suggestion | Open (Q5) | Monthly use of these is undecided. |
| Other | No suggestion | — | |
| Non-rentable | Never contracted | — | |
| Whole property (no space) | No suggestion | — | |
| Government, Commercial | Never suggested | — | They describe the customer and the purpose, not the space (D8.3). |

## How it would work (if approved)

- The contract form pre-selects the suggested category when a space is chosen and the
  category is still empty. It never overwrites a category the admin has chosen.
- The backend stores only what the admin saves; nothing is inferred later.
- No data change and no migration. API: an optional `suggested_category` in the existing
  pricing-suggestion response (additive).

## Decisions needed

1. Approve, change or reject the mapping above (especially Floor and Portion).
2. Meeting, conference and training rooms: suggest nothing (proposed), or a category?

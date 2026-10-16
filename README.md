# SIGMANIX SPACES — Backend

REST API for SIGMANIX SPACES, a commercial property and workspace management
platform. **Phase 1 covers the Property module only.** CRM, Accounts, AI Agents
and the Client Portal are later phases and are not part of this codebase.

The frontend is a separate project (`D:\Space\pixel-perfect-view`, React +
TypeScript + Vite). It talks to this API over HTTP and never accesses the
database directly.

## Implementation status

| Phase | Scope | Status |
| --- | --- | --- |
| A | Inspection and plan | Done |
| B | Foundation: app, config, DB sessions, logging, health, Alembic | Done |
| C | Database schema and initial migration | Done. Revision `0001` applied to Sigmanix_spaces |
| D | Auth plus property, building, floor, layout, space, amenity, tenant, asset APIs | Done |
| E | Booking APIs with overlap prevention | Done |
| F | Dashboard metrics and Supabase Storage | Dashboard done; Storage not started |
| G | Authentication, authorization, full test suite | Not started |
| H | Frontend integration readiness | Frontend connected for local development |

File uploads are implemented (`/files`, see [Storage configuration](#storage-configuration)). The schema lives in
`app/models/` and `migrations/versions/`.

## Technology stack

- Python 3.11
- FastAPI, Uvicorn
- Pydantic v2, pydantic-settings
- SQLAlchemy 2.x with the psycopg 3 driver
- Alembic
- PostgreSQL (Supabase), Supabase Storage, Supabase Auth
- pytest, httpx, Ruff

Pinned versions are in `requirements.txt`.

## Architecture

```
Frontend → REST (/api/v1) → FastAPI routers → auth → services → repositories → SQLAlchemy → Supabase PostgreSQL
```

```
app/
  main.py            application factory, middleware, CORS
  core/              config.py, logging.py, errors.py
  api/
    dependencies.py  shared FastAPI dependencies
    v1/router.py     version 1 router
    v1/endpoints/    one module per resource
  db/                base.py (declarative base), session.py (engine, sessions)
  models/            SQLAlchemy ORM models
  schemas/           Pydantic request/response schemas
  cli.py             admin commands (grant-admin, list-users)
  repositories/      data access
  services/          business logic
  storage/           Supabase Storage client (added in Phase F)
migrations/          Alembic environment and versions
tests/
```

Route handlers stay thin: validation in schemas, business rules in services,
queries in repositories.

## Setup (Windows PowerShell)

Run everything from `D:\Space\backend`.

```powershell
cd D:\Space\backend

# Create and activate the virtual environment (Python 3.11 is required)
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version          # must print Python 3.11.x

# Install dependencies
python -m pip install -r requirements.txt

# Create your local configuration
Copy-Item .env.example .env
```

If activation is blocked by the execution policy, run this once:
`Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.

Never install these dependencies into the frontend project.

## Environment configuration

Settings are read from environment variables and `.env` (see `.env.example`).
The application validates them at startup and exits with a message naming each
missing or invalid setting. Secret values are never printed.

| Variable | Required | Purpose |
| --- | --- | --- |
| `APP_ENV` | no (`development`) | `development`, `test` or `production` |
| `APP_HOST`, `APP_PORT` | no | Bind address for local runs |
| `LOG_LEVEL` | no (`INFO`) | Logging level |
| `DATABASE_URL` | **yes** | Supabase PostgreSQL connection string |
| `SUPABASE_URL` | **yes** | `https://<project-ref>.supabase.co` |
| `SUPABASE_PUBLISHABLE_KEY` | for sign-in | Browser-safe key used to call Supabase Auth |
| `SUPABASE_SECRET_KEY` | for Storage | Server-side key. Never send to the browser |
| `PROPERTY_STORAGE_BUCKET` | no (`property-files`) | Private bucket for images and documents |
| `STORAGE_BACKEND` | no (`supabase`) | `supabase`, or `local` for development and tests only |
| `LOCAL_STORAGE_DIR` | no (`var/uploads`) | Folder used when `STORAGE_BACKEND=local` |
| `MAX_UPLOAD_MB` | no (`10`) | Largest upload accepted |
| `SUPABASE_JWT_AUDIENCE` | no (`authenticated`) | Expected audience of access tokens |
| `SUPABASE_JWT_SECRET` | legacy projects only | Shared secret for HS256-signed tokens |
| `CORS_ALLOWED_ORIGINS` | yes for browsers | Comma-separated exact origins |
| `TEST_DATABASE_URL` | for DB tests | Disposable database, different from `DATABASE_URL` |

## Supabase connection setup

Get the connection string from the Supabase dashboard (**Connect**). Percent-encode
special characters in the password (for example `@` becomes `%40`).

| Mode | Host / port | Use |
| --- | --- | --- |
| Session pooler | `aws-0-<region>.pooler.supabase.com:5432` | Recommended for local development and migrations. Works over IPv4 |
| Direct | `db.<project-ref>.supabase.co:5432` | Works too, but is IPv6-only unless the IPv4 add-on is enabled |
| Transaction pooler | `...pooler.supabase.com:6543` | For serverless-style deployments. Prepared statements are disabled automatically |

Either `postgresql://` or `postgresql+psycopg://` is accepted; the backend always
uses psycopg 3. `sslmode=require` is added automatically for non-local hosts
unless the URL already sets `sslmode`.

## Migration workflow

All schema changes go through Alembic. `alembic.ini` contains no URL; the target
is always `DATABASE_URL`.

```powershell
# Create a migration from model changes, then review the generated file
alembic revision --autogenerate -m "describe the change"

# Preview the SQL without connecting
alembic upgrade head --sql

# Apply (only after the migration and the target database are reviewed)
alembic upgrade head

# Inspect state
alembic current
alembic history
```

Before applying a migration: confirm which database `DATABASE_URL` points to,
check what tables it already contains, and read the generated migration. Never
reset or drop the Supabase database.

## Running locally

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- Swagger UI: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc
- OpenAPI schema: http://127.0.0.1:8000/api/v1/openapi.json
- Health check: http://127.0.0.1:8000/api/v1/health

## API

All routes are under `/api/v1`. The generated OpenAPI schema is the authoritative
contract for the frontend.

Swagger UI (`/docs`) lists every operation with its request body, query
parameters, response schema and error responses. Summary:

| Resource | Base path | Operations | Notes |
| --- | --- | --- | --- |
| Health | `/health`, `/health/ready` | GET | Public |
| Auth | `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/me` | POST, POST, POST, GET | Login and refresh are public |
| Dashboard | `/property/dashboard/summary`, `/activity`, `/availability` | GET | Computed from saved records on every request |
| Properties | `/properties` | list, create, get, update, archive | `occupancy_status`, `occupancy_rate`, `tenant_name`, `amenities` are derived and read-only |
| Buildings | `/buildings` | list, create, get, update, delete | Filter by `property_id` |
| Floors | `/floors` | list, create, get, update, delete | Filter by `building_id` |
| Layouts | `/layouts`, `/layouts/{id}/spaces` | list, create, get, update, archive | Floor plan images arrive with file uploads |
| Spaces | `/spaces`, `/spaces/{id}/move` | list, create, get, update, archive, move | `status` is derived; `operational_status` is set by an admin. See [Inventory hierarchy](#inventory-hierarchy) |
| Bookings | `/bookings`, `/bookings/availability`, `/bookings/{id}/confirm` / `complete` / `cancel` | list, create, get, reschedule, confirm, complete, cancel | `DELETE` cancels; bookings are never deleted |
| Amenities | `/amenities` | list, create, get, update, delete | One item per property; names map to shared definitions |
| Tenants | `/tenants`, `/tenants/{id}/occupancies`, `/occupancies/{id}` | list, create, get, update, archive; occupancy create/list/update/delete | Occupancy dates are inclusive calendar days |
| Assets | `/assets` | list, create, get, update, delete | Physical inventory only |

Conventions for every resource:

- **Auth:** all routes except Health, login and refresh need `Authorization: Bearer <access token>`
  for a user registered as an admin (see [Authentication](#authentication)).
- **Lists** return `{ "items": [...], "total": n, "page": 1, "page_size": 20 }`.
  Query parameters: `page` (from 1), `page_size` (1-100), `search`, `sort`
  (a field name, `-` prefix for descending) plus the filters shown in Swagger.
- **Create** returns `201`; **update** is `PATCH` and changes only the fields
  sent; **delete/archive** returns `204`.
- **Request bodies** reject unknown and read-only fields with `422`.
- **Errors:** `400` rule violation, `401` no/invalid token, `403` not an admin,
  `404` missing or archived, `409` conflict with existing data, `422` validation.
- **Archive vs delete:** properties, spaces, tenants and layouts are archived and
  kept. Buildings, floors, amenity assignments, occupancies and assets are
  deleted, and the delete is refused with `409` while other records reference them.

### Inventory hierarchy

Property -> Building -> Floor -> Space. Revision `0002` adds this to `spaces`;
it is **proposed and not yet applied to the Supabase database**.

- A space is either **unassigned** (no floor: every space created before `0002`,
  and any created without `floor_id`) or **on a floor**.
- On a floor, one space of type `FLOOR` is the whole floor. Create it first.
  Every other space on the floor has a parent: the whole-floor space by default,
  or `parent_space_id`. A space's floor, building and property always agree.
- Allowed nesting: `PORTION` in `FLOOR`; offices, coworking areas, cabins, rooms
  and `NON_RENTABLE` spaces in `FLOOR` or `PORTION`; `DESK` in `FLOOR`, `PORTION`,
  `PRIVATE_OFFICE` or `COWORKING`.
- `POST /spaces/{id}/move` assigns an unassigned space to a floor, or moves a
  space with everything inside it. Moving a space that is already on a floor is
  refused (`409`) while it, or a space inside it, has open bookings or a current
  or upcoming occupancy. `PATCH` never changes the floor or parent.
- `GET /spaces` leaves out whole-floor and non-rentable spaces unless
  `include_structural=true`. Filters: `building_id`, `floor_id`,
  `parent_space_id`, `is_rentable`, `unassigned`.
- Bookings and occupancies still apply to one space at a time. Conflicts between
  a space and the spaces inside it are not checked until the allocation engine
  is added.

### Derived status

Stored status is operational (can this be used at all). Occupancy is computed
at request time and never stored:

- **Property `occupancy_status`:** `UNAVAILABLE` if not operationally `ACTIVE`;
  else `OCCUPIED` if a tenant occupancy covers today; else `RESERVED` if a
  `PENDING`/`CONFIRMED` booking has not ended; else `VACANT`.
- **Space `status`:** `UNAVAILABLE` if not operationally `ACTIVE`; else `OCCUPIED`
  if occupied today (directly or via a whole-property occupancy); else `BOOKED`
  if a `CONFIRMED` booking is in progress; else `RESERVED` if a
  `PENDING`/`CONFIRMED` booking has not ended; else `AVAILABLE`.

"Today" is the calendar date in `BUSINESS_TIMEZONE`. Timestamps are UTC.

### Bookings

A booking reserves one space for the half-open range `[start_at, end_at)`, so one
booking may start exactly when another ends. Times must carry a UTC offset and
are returned in UTC.

- **Statuses:** `PENDING` -> `CONFIRMED` or `CANCELLED`; `CONFIRMED` -> `COMPLETED`
  or `CANCELLED`. `CANCELLED` and `COMPLETED` are final.
- **What blocks a slot:** `PENDING`, `CONFIRMED` and `COMPLETED` bookings. Cancelling
  releases the slot immediately.
- **Rejected with `409`:** an overlapping booking, a property or space that is not
  operationally `ACTIVE`, or a space occupied by a different tenant on any day
  the booking touches.
- **Rejected with `400`:** a start more than 5 minutes in the past, a duration over
  366 days, or a space that belongs to another property.
- **Concurrency:** creating or rescheduling locks the space row and re-checks
  availability inside the transaction; a PostgreSQL exclusion constraint is the
  final guard. `/bookings/availability` is advisory only.
- **Not included:** payments, access control, recurring bookings.

## Authentication

Passwords are checked by Supabase Auth, never by this API and never stored here.

1. The frontend posts the email and password to `POST /auth/login`.
2. The backend forwards them to Supabase Auth (using `SUPABASE_PUBLISHABLE_KEY`)
   and returns the session Supabase issues: `access_token`, `refresh_token`,
   `expires_in` and the user.
3. Every other request carries `Authorization: Bearer <access_token>`. The API
   verifies it against the project's published signing keys (signature, issuer,
   audience, expiry). Projects still on the legacy shared secret set
   `SUPABASE_JWT_SECRET` instead.
4. Access tokens last about an hour. `POST /auth/refresh` exchanges the refresh
   token for a new session; the old refresh token stops working.
5. `POST /auth/logout` revokes the session's refresh token.

Wrong password and unknown email return the same `401`, so the response does
not reveal which accounts exist. Sign-in attempts reach Supabase from the
backend's address, so Supabase's per-IP rate limits apply to the backend as a
whole; put a rate limiter in front of `/auth/login` before production use.

A valid Supabase login is not enough. The user must also be in the backend's
`app_users` table with an active role; roles are never read from the token or
the request, and login returns no session to anyone else. To set up the first admin:

1. Supabase dashboard > Authentication > Users > add the user (email + password).
2. Register them with this backend:

```powershell
python -m app.cli grant-admin you@example.com
python -m app.cli list-users
python -m app.cli revoke-access you@example.com
```
### Error responses

Every error uses the same body:

```json
{
  "detail": "Human-readable message.",
  "code": "validation_error",
  "request_id": "9f1c…",
  "errors": [{ "field": "body.name", "message": "Field required" }]
}
```

`detail` is always a string. `errors` appears only for validation failures (`422`).
Every response carries an `X-Request-ID` header that matches the server logs.
Responses never include stack traces, SQL, or connection details.

## Testing

```powershell
pytest            # run the suite
ruff check .      # lint
ruff format .     # format
```

Tests set their own environment and do not read `.env` settings other than
`TEST_DATABASE_URL`. Tests that need PostgreSQL run only when `TEST_DATABASE_URL`
is set; without it they are reported as skipped. Database tests migrate that
database to the latest revision and truncate every table after each test, so
the suite refuses to start if `TEST_DATABASE_URL` equals `DATABASE_URL` or names
a database without "test" in its name.

The local test database is a scoop-installed PostgreSQL on port 5433 (5432 was
already in use on the development machine). It does not start automatically:

```powershell
# Start (once per reboot) / stop
pg_ctl start -D "$env:USERPROFILE\scoop\persist\postgresql\data" -o "-p 5433 -c listen_addresses=127.0.0.1"
pg_ctl stop  -D "$env:USERPROFILE\scoop\persist\postgresql\data"

# One-time: create the test database
createdb -h 127.0.0.1 -p 5433 -U postgres spaces_test
```

`.env`: `TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5433/spaces_test`

## CORS

`CORS_ALLOWED_ORIGINS` is a comma-separated allowlist of exact origins, for
example `http://localhost:5173,https://app.example.com`. Wildcards are rejected
at startup because the frontend sends credentials. Add the origin your frontend
dev server actually prints when it starts.

## Storage configuration

Images and documents are stored as objects; PostgreSQL keeps only their metadata
(the `files` table: owner, kind, name, type, size). Links to the bytes are signed
and expire after ten minutes, so the frontend asks the API again for fresh ones.

| Endpoint | Purpose |
| --- | --- |
| `POST /files` | Multipart upload: `file`, `kind`, `property_id`, and optionally one of `building_id`, `layout_id`, `space_id` |
| `GET /files` | List, filtered by `property_id`, `building_id`, `layout_id`, `space_id`, `kind`; each item has a fresh `url` |
| `GET /files/{id}` | One file with a fresh `url` |
| `DELETE /files/{id}` | Removes the record, then the stored bytes |

- **Accepted:** JPG, PNG and WebP; PDF too for `FLOOR_PLAN` and `DOCUMENT`. The
  type is read from the file's content, not its name. Larger than `MAX_UPLOAD_MB`
  answers `413`, any other type `415`.
- **Kinds:** `MAIN`, `GALLERY`, `BUILDING`, `INTERIOR`, `WORKSPACE`, `FLOOR_PLAN`, `DOCUMENT`.
- **Supabase (production):** set `SUPABASE_SECRET_KEY` (server-side only) and create a
  private bucket named by `PROPERTY_STORAGE_BUCKET`. Without the key every file
  endpoint answers `503`; nothing else is affected.
- **Local (`STORAGE_BACKEND=local`):** files go to `LOCAL_STORAGE_DIR` and the API
  serves them at `/files/local/...` to holders of a valid signed link. For
  development and tests only. The e2e test server uses a throwaway folder.

## Frontend integration

The frontend (`D:\Space\pixel-perfect-view`) reads the API location from
`.env.local`:

```
VITE_API_BASE_URL=http://127.0.0.1:8000/api/v1
```

Run both for local development:

```powershell
# Terminal 1 - backend
cd D:\Space\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# Terminal 2 - frontend (serves http://localhost:8080)
cd D:\Space\pixel-perfect-view
npm run dev
```

`http://localhost:8080` must be listed in `CORS_ALLOWED_ORIGINS`. The frontend
stores the session in the browser, attaches the access token to every request,
renews it through `/auth/refresh` shortly before it expires, and returns to the
login page when the backend rejects the session.

The Lovable-hosted preview cannot reach a backend on `localhost`. It needs the
backend deployed at a public HTTPS URL, that URL set as `VITE_API_BASE_URL` in
Lovable, and the preview's origin added to `CORS_ALLOWED_ORIGINS`.
## Security notes

- `.env` is git-ignored. Commit only `.env.example`.
- `SUPABASE_SECRET_KEY` and `DATABASE_URL` are server-side only.
- Request logs record method, path, status and duration. Query strings, headers
  and bodies are not logged.
- Unhandled errors return a generic `500`; the traceback goes to the logs only.

## Known limitations

- Captions, categories, ordering and a primary flag for images are not stored yet; only the file's kind and owner are.
- Every table has row level security enabled with no policies, so Supabase's
  Data API cannot read or write them. All access goes through this backend.
- Starlette's test client warns that its `httpx` backend is deprecated in favour
  of `httpx2`. Tests pass; the warning is cosmetic.

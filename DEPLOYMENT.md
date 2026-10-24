# Deploying the backend on Railway

1. In Railway: **New Project → Deploy from GitHub repo → `SIgmanixtech/Spaces_backend`**, and pick the branch to deploy.
2. Railway installs `requirements.txt` with Python 3.11 (`.python-version`) and starts the app with the command in `railway.json` (Uvicorn on Railway's `$PORT`). The health check is `/api/v1/health`.
3. Add these **Variables** in the Railway service. Copy the values from your local `.env`; never commit them.

| Variable | Value |
|---|---|
| `APP_ENV` | `production` |
| `DATABASE_URL` | Supabase connection string (session pooler, port 5432) |
| `SUPABASE_URL` | `https://<project-ref>.supabase.co` |
| `SUPABASE_PUBLISHABLE_KEY` | Supabase publishable key |
| `SUPABASE_SECRET_KEY` | Supabase secret key (server only) |
| `SUPABASE_JWT_AUDIENCE` | Same as your local `.env` (normally `authenticated`) |
| `PROPERTY_STORAGE_BUCKET` | `property-files` |
| `STORAGE_BACKEND` | `supabase` |
| `BUSINESS_TIMEZONE` | `Asia/Kolkata` |
| `CORS_ALLOWED_ORIGINS` | The two Vercel URLs, comma-separated, e.g. `https://space-client-app.vercel.app,https://space-frontend.vercel.app` |

4. Open **Settings → Networking → Generate Domain**. The API base URL for both frontends is `https://<that-domain>/api/v1`.

## Database

The Supabase database is already at migration **0008** (head). This code needs no new migration, so the start command does **not** run migrations. When a future change adds one, run `alembic upgrade head` deliberately, after a backup.

## After the frontends are deployed

Put their exact Vercel URLs in `CORS_ALLOWED_ORIGINS` and redeploy. Wildcards are refused. Without this, browsers block every request from the apps.

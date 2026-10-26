# ChronoGuard dashboard connectivity fix

## Changes in this package
- `index.html` now uses `http://127.0.0.1:8000` as the default API base when opened as a local file or served separately. Override it before the page script if your backend uses another address: `window.CHRONOGUARD_API_BASE = 'http://127.0.0.1:8000'`.
- CORS now permits `PUT`, needed by the monitor configuration UI.
- Added PostgreSQL-backed endpoints for `/api/overview`, `/api/servers`, `/api/requests/stats`, `/api/incidents`, and `/api/logs` using the existing SQLAlchemy models/tables.
- No `.env`, `.git`, or `.venv` is included in this ZIP. Restore your own local `.env` and `.venv` from your working copy; never publish secrets.

## Run and verify on Windows
From the project root in PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/api/health` and confirm it reports `api: healthy` and `database: connected`. Then open `http://127.0.0.1:8000/docs` and test `/api/overview`, `/api/servers`, `/api/requests/stats`, `/api/incidents`, and `/api/logs`.

Open `index.html` or serve the frontend. If using VS Code Live Server on port 5500, keep the API running on port 8000.

## Honest test status / limitations
- Python syntax compilation passed for `app/`.
- Inline JavaScript syntax check passed with Node.js.
- A live PostgreSQL connection could not be verified in this packaging environment; run `/api/health` on your machine.
- This patch connects the main overview, server list, incident list, request stats, and audit logs to the current database schema. It does not create new database tables or fabricate request history. Some Simulation Lab controls and per-server/per-request history endpoints may still need implementation if your UI uses them; inspect `/docs` and the browser Network tab for any remaining 404s.
- The server list reflects rows actually present in `service_health`. If that table is empty, the UI may correctly show no server records until the monitor registers services.

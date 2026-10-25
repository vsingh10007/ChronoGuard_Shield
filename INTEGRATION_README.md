# ChronoGuard backend + PostgreSQL integration

This integration adds a durable `request_logs` table and connects the Simulation page to FastAPI.

## Security
- Do not commit `.env`; it contains local credentials.
- `.env.example` is a template only. Copy it to `.env` and set your own database password.
- Demo payload fields whose names contain `password`, `token`, `secret`, `authorization`, or `api_key` are omitted from normal simulation logs.
- Primary A and Backup B are simulated dependencies, not separate physical servers.

## 1. Install packages
Open PowerShell in the ChronoGuard project folder, activate your existing virtual environment, then run:

```powershell
python -m pip install -r requirements.txt
```

## 2. Confirm `.env`
Keep your existing `.env` file locally. It must contain a valid `DATABASE_URL` for the `chronoguard_db` PostgreSQL database. Never paste its password into chat or commit it to Git.

## 3. Create the new table
Run from the project root (the folder containing `init_db.py`):

```powershell
python init_db.py
```

This creates any missing SQLAlchemy tables, including `request_logs`, without deleting existing rows.

## 4. Start the API
From the same project root:

```powershell
python -m uvicorn app.main:app --reload
```

Check `http://127.0.0.1:8000/api/health`. It should report API healthy and database connected.

## 5. Open the frontend
Open `simulation.html` using VS Code Live Server (usually `http://127.0.0.1:5500/simulation.html`). Click **Run event**. The page posts the selected simulated event to FastAPI, and the API writes a request record to PostgreSQL. The SQL table preview refreshes from `GET /api/requests`.

If Live Server uses a different port, add both `localhost` and `127.0.0.1` origins for that port to the CORS `allow_origins` list in `app/main.py`.

## API endpoints
- `GET /api/health` — checks PostgreSQL connectivity.
- `POST /api/simulation/run` — runs one simulated event and persists its outcome.
- `GET /api/requests?limit=10` — newest persisted request records.
- `GET /api/requests/{request_id}` — a single persisted request record.
- Existing shield endpoints remain at `/api/shield/validate-request` and `/api/shield/validate-age`.

## Important scope note
This is a college simulation. Failover and server A/B are simulated in the API; no actual second server or automatic failover of a production service is created. A database outage can prevent request logging, and the API returns an error instead of claiming the record was saved.

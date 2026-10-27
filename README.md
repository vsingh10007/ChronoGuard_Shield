# ChronoGuard — Self-Healing Dependency Guardian

> **Monitor · Detect · Contain · Recover · Verify · Log**

ChronoGuard is an educational web application that demonstrates how a service can validate incoming requests, monitor simulated dependencies, contain known failures, switch work to a healthy backup when appropriate, and record outcomes for review. It also provides bounded chaos/simulation scenarios for safely demonstrating failure behavior.

## 1. Project at a glance

| Area | Technology / approach |
|---|---|
| Frontend | HTML, CSS, JavaScript |
| API/backend | Python, FastAPI, Uvicorn |
| Database | PostgreSQL |
| Database access | SQLAlchemy ORM and parameterized SQL |
| Configuration | `.env` file loaded with `python-dotenv` |
| Monitoring | Background Python thread with periodic simulated health probes |
| Simulations | Bounded scenario engine; runs and outcomes persisted in PostgreSQL |
| API documentation | FastAPI OpenAPI docs at `/docs` |

**Scope:** Server A (primary) and Server B (backup) are simulated dependencies in this college project. This is not a production-grade distributed load balancer, and it does not provision real servers. “Load” and capacity values shown by the dashboard are simulation values, not the computer's actual CPU or memory utilization.

## 2. The problem ChronoGuard addresses

Web applications depend on multiple components and accept requests from users. Problems can occur when:
1. a request is missing required fields or contains invalid data;
2. a timestamp or another value is malformed;
3. a dependency responds slowly, times out, or becomes unavailable;
4. repeated failures go unnoticed and continue affecting requests;
5. the primary dependency is unhealthy while a backup may still be available;
6. a simulated server reaches capacity and new work should not be accepted blindly;
7. teams cannot easily see what happened because decisions and outcomes were not logged.

ChronoGuard demonstrates a structured response to these problems. It validates requests, checks dependency health, applies controlled simulation policies, makes a recovery/failover decision where implemented, and stores request/health/incident information so a reviewer can inspect the result.

## 3. Main concept and lifecycle

The core lifecycle is:

1. **Monitor** — periodically probe simulated primary and backup services and collect health results.
2. **Detect** — count failed probes and mark a service degraded or unhealthy according to the configured failure threshold.
3. **Contain** — reject invalid requests safely, avoid selecting an unavailable destination, or stop admitting new requests in the capacity simulation when both servers are full.
4. **Recover** — where the scenario and Health Shield logic support it, direct simulated work to a healthy backup or wait for capacity to recover.
5. **Verify** — perform another health check or read the resulting state to confirm that recovery actually happened.
6. **Log** — save request outcomes, health-check history, simulation runs, and incidents in PostgreSQL.

A system must not report “recovered” merely because a recovery action was attempted. The state should be verified first.

## 4. Architecture

```text
Browser (index.html + style.css + script.js)
                 |
                 | HTTP requests / JSON
                 v
          FastAPI application
          app/main.py
                 |
       +---------+-----------+
       |         |           |
       v         v           v
 Request       Monitor    Simulation engine
 validation    app/monitor app/simulation_engine
 and Shield                 |
       |                    |
       +---------+----------+
                 |
                 v
        SQLAlchemy / database layer
             app/database.py
                 |
                 v
             PostgreSQL
  request_logs | incidents | service_health
  health_check_logs | simulation_runs | shield_settings
```

### 4.1 Frontend
- `index.html` contains the dashboard markup and page-level browser logic.
- `style.css` contains the interface styling.
- `script.js` contains shared frontend behavior, where used.
- The frontend calls the API over HTTP and renders returned JSON. The backend is the source of truth for validations and simulation decisions; changing a visual label alone does not enforce a rule.

### 4.2 API/backend
- `app/main.py` creates the FastAPI app, registers API routes, validates payload shapes, coordinates database writes, and starts/stops the monitor through the application lifespan.
- `app/request_validator.py` contains request validation logic.
- `app/shield/` contains focused guard modules for input, required fields, timestamps, exceptions, loops, and shield control.
- `app/monitor.py` simulates health probes, tracks consecutive failures, and persists health state/history.
- `app/simulation_engine.py` defines scenario metadata and runs bounded test scenarios in background workers.
- `app/config_store.py` reads and updates persistent Shield/monitor/capacity settings.
- `app/models.py` defines SQLAlchemy models.
- `app/database.py` creates the database engine/session factory using `DATABASE_URL`.
- `init_db.py` explicitly creates tables defined by the SQLAlchemy models.

### 4.3 Database
The project uses PostgreSQL for persistent records. The model definitions include:
- `request_logs` — request/simulation input summary and outcome;
- `incidents` — detected events and recovery-action notes;
- `service_health` — latest recorded state for each simulated service;
- `health_check_logs` — individual probe results and consecutive failure counts;
- `simulation_runs` — run status, configuration, and result details;
- `shield_settings` — persisted Shield, monitoring, and capacity-simulation settings.

The application creates missing model tables at startup. Existing database tables are not automatically migrated in a general-purpose way; if the model schema changes, use a proper migration or carefully review the project's startup migration logic before deployment.

## 5. Important features

### 5.1 Overview and operational visibility
The dashboard surfaces request activity, service health, recent incidents, logs, and simulated server/load state through API endpoints. Data that comes from PostgreSQL should be distinguished from calculated or simulated values.

### 5.2 Request validation and guards
The backend exposes request-validation routes and uses guard modules to check input and required fields. Invalid input should lead to a controlled validation response rather than an unhandled server exception.

### 5.3 Health Shield and monitoring
The Shield/monitor configuration is stored in PostgreSQL. The background monitor checks the simulated `primary` and `backup` services on a recurring interval. The current monitor uses a five-second check interval, a two-second simulated timeout, and a configurable failure threshold (bounded by the API).

A failed probe increases the consecutive-failure count. A healthy probe resets it. The service becomes `degraded` before it reaches `unhealthy` at the configured threshold. Health checks and latest service status are persisted when the database is available.

### 5.4 Simulated failover
The application can simulate primary/backup service states and demonstrate choosing a healthy backup under supported scenarios. The primary and backup are logical test dependencies, not separate physical machines. A failover result must be read as a demonstration of decision logic, not proof of production high availability.

### 5.5 Simulation Lab
The simulation engine lists scenarios by category and indicates whether each scenario is supported. Supported scenarios include bounded validation tests, controlled in-process load tests, simulated health failures, capacity/queue exercises, selected database checks, and end-to-end workflow checks. A scenario marked **NOT IMPLEMENTED** is descriptive only and must not be presented as an executed test.

The engine places safety bounds on requests and concurrency (currently `MAX_REQUESTS = 500` and `MAX_CONCURRENCY = 10`). These limits help prevent a demonstration from accidentally becoming an unbounded load generator.

### 5.6 Dual-server capacity exhaustion
The capacity API stores capacity/utilization and admission-control state. The intended demonstration is:
1. increase the simulated utilization of Server A and Server B;
2. when both have no available capacity, stop accepting new form requests;
3. return HTTP `503 Service Unavailable` with a `Retry-After` header for blocked submissions;
4. allow requests again after the configured recovery condition is satisfied (the default recovery threshold is 75%);
5. record counters and capacity incidents for inspection.

The 75% value is a **recovery threshold**, not the point at which the server becomes full. Full capacity is the overload condition; recovery is deliberately delayed until utilization drops below the threshold. Check the API's current settings/status before a live demonstration.

### 5.7 Auditability
The application records request and simulation outcomes in PostgreSQL. Sensitive-looking payload keys such as password, token, secret, authorization, and API key are filtered from normal simulation logs. This is a basic safeguard, not a substitute for a comprehensive data-classification and security policy.

## 6. Repository structure

```text
ChronoGuard/
├── app/
│   ├── main.py
│   ├── database.py
│   ├── models.py
│   ├── config_store.py
│   ├── monitor.py
│   ├── request_validator.py
│   ├── simulation_engine.py
│   └── shield/
│       ├── controller.py
│       ├── exception_guard.py
│       ├── input_guard.py
│       ├── loop_guard.py
│       ├── required_fields_guard.py
│       └── timestamp_guard.py
├── index.html
├── style.css
├── script.js
├── init_db.py
├── requirements.txt
├── README.md
├── INTEGRATION_README.md
├── CHATGPT_PROJECT_CONTEXT.md
└── chaos_script.py
```

## 7. Requirements

Before running the project, install:
1. **Python 3.10 or newer** (the code uses modern Python type syntax; Python 3.11+ is a sensible choice).
2. **PostgreSQL** running locally or on a reachable database server.
3. **Git** (optional, only if cloning the repository).
4. **VS Code Live Server** (optional) or another static-file server for the frontend.

You need a PostgreSQL database and a database role that can connect to it and create/use the project's tables.

## 8. Windows setup — step by step

### Step 1 — Open PowerShell in the project folder

Change the path to the actual folder where you extracted ChronoGuard:

```powershell
cd "C:\path\to\ChronoGuard"
```

Confirm the project root contains `init_db.py`, `requirements.txt`, and the `app` folder:

```powershell
Get-ChildItem
```

### Step 2 — Create and activate a virtual environment

Create it once:

```powershell
py -m venv .venv
```

Activate it in PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, for the current terminal only run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

You should see `(.venv)` at the beginning of your terminal prompt. Do not run this policy change as Administrator or change the machine-wide policy just for this project.

### Step 3 — Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Step 4 — Configure PostgreSQL and `.env`

Create a database and user if you have not already created them. Example using `psql` (replace the example password with your own secure password):

```sql
CREATE USER chronoguard_user WITH PASSWORD 'REPLACE_WITH_YOUR_PASSWORD';
CREATE DATABASE chronoguard_db OWNER chronoguard_user;
```

Run SQL in `psql` or a PostgreSQL SQL tool, not as a PowerShell command. If the user or database already exists, do not repeat the creation blindly.

In the project root, create a local file named `.env` (same folder as `init_db.py`) with a connection string:

```dotenv
DATABASE_URL=postgresql+psycopg://chronoguard_user:YOUR_URL_ENCODED_PASSWORD@localhost:5432/chronoguard_db
```

Replace the placeholder. If your password contains URL-reserved characters, URL-encode it. Keep `.env` private and do not commit it to GitHub or paste it into a public issue/chat.

Verify PostgreSQL is running in PowerShell:

```powershell
Get-Service *postgres*
```

If the service is stopped, start the actual service name shown on your machine from an elevated PowerShell window, for example:

```powershell
Start-Service postgresql-x64-18
```

Use the service name installed on your computer; it may differ.

### Step 5 — Initialize database tables

With `(.venv)` active and the terminal at the project root:

```powershell
python init_db.py
```

The script creates missing model tables. It should not be treated as a database reset command.

### Step 6 — Start the backend API

In the same terminal:

```powershell
python -m uvicorn app.main:app --reload
```

Keep this terminal open. Expected local addresses:
- API root: `http://127.0.0.1:8000/`
- Health check: `http://127.0.0.1:8000/api/health`
- Interactive API docs: `http://127.0.0.1:8000/docs`

Open the health URL in a browser. Confirm that the API is healthy and the database is connected. If it reports a database error, check PostgreSQL service status, `.env` location, `DATABASE_URL`, username/password, database name, and port.

### Step 7 — Open the frontend

Option A — VS Code:
1. Open the ChronoGuard folder in VS Code.
2. Open `index.html`.
3. Use **Go Live** from the Live Server extension.
4. If Live Server opens the page on a different port, make sure that origin is allowed by CORS in `app/main.py`.

Option B — Python static server:
Open a **second** PowerShell terminal, change to the project folder, and run:

```powershell
py -m http.server 5500
```

Then open `http://127.0.0.1:5500/index.html`. Keep this terminal open as well. The current API CORS configuration includes common local origins on ports 5500 and 5501; adjust it only for the origin you actually use.

### Step 8 — Test the system

1. Open `http://127.0.0.1:8000/api/health` and verify API/database status.
2. Open `http://127.0.0.1:8000/docs` and expand a `GET` route such as `/api/monitor/status` or `/api/capacity/status`.
3. Open the frontend and check whether the dashboard data loads.
4. Use one small, supported simulation first.
5. For health testing, simulate one service as unavailable, run/check health, then restore it to healthy.
6. For capacity testing, start with small capacity values, verify the admission status, and reset the simulation after the demonstration.
7. Review request and incident records in the dashboard/API and PostgreSQL.
8. Restore both simulated services to `healthy` and reset capacity state when finished.

### Step 9 — Stop the project

- Press `Ctrl+C` in the Uvicorn terminal.
- Press `Ctrl+C` in the static server terminal.
- Deactivate the virtual environment if desired:

```powershell
deactivate
```

## 9. Useful Windows commands

| Task | Command |
|---|---|
| Activate environment | `.\.venv\Scripts\Activate.ps1` |
| Install packages | `python -m pip install -r requirements.txt` |
| Initialize tables | `python init_db.py` |
| Run backend | `python -m uvicorn app.main:app --reload` |
| Start static frontend | `py -m http.server 5500` |
| Check Python version | `python --version` |
| Check PostgreSQL service | `Get-Service *postgres*` |
| Check local API port | `Test-NetConnection 127.0.0.1 -Port 8000` |
| Deactivate venv | `deactivate` |

## 10. Useful API endpoints

Open `/docs` for the authoritative interactive schema and payload requirements. Important routes in this codebase include:

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/health` | API and PostgreSQL connectivity check |
| `GET` / `PUT` | `/api/shield/config` | Read/update Shield configuration |
| `GET` | `/api/monitor/status` | Read monitor state |
| `PUT` | `/api/monitor/config` | Enable/disable scheduled monitoring |
| `POST` | `/api/monitor/check` | Run health checks immediately |
| `POST` | `/api/monitor/simulate/{service}/{mode}` | Set simulated service mode |
| `GET` | `/api/capacity/status` | Read capacity/admission state |
| `GET` | `/api/capacity/history` | Read capacity history if available |
| `GET` | `/api/capacity/incidents` | Read capacity incidents |
| `POST` | `/api/capacity/simulation/start` | Start capacity simulation |
| `POST` | `/api/capacity/simulation/stop` | Stop capacity simulation |
| `POST` | `/api/capacity/form` | Submit through simulated capacity admission control |
| `POST` | `/api/capacity/reset` | Reset capacity state |
| `POST` | `/api/shield/validate-request` | Validate required request data |
| `POST` | `/api/simulation/run` | Run a legacy single-event simulation |
| `GET` | `/api/simulations/scenarios` | List scenarios and implementation status |
| `POST` | `/api/simulations` | Start a supported scenario |
| `GET` | `/api/simulations/history` | Read recent simulation history |
| `GET` | `/api/requests` | Read persisted request records |
| `GET` | `/api/incidents` | Read incident records |

Not every scenario displayed in the Simulation Lab is implemented. Use the `implemented`/`availability` field returned by the scenarios endpoint rather than assuming all listed tests can run.

## 11. Chaos testing with `chaos_script.py`

The included chaos script is a **safe, local demonstration helper**. It uses the application's simulated health-control API; it does not kill processes, modify the operating system, attack external hosts, or generate an unbounded traffic flood.

First start ChronoGuard's backend and confirm `/api/health` works. In a second PowerShell terminal:

```powershell
# Inspect the steps without changing service state
python chaos_script.py

# Execute the bounded simulated failure/restore sequence
python chaos_script.py --run
```

Optional custom base URL:

```powershell
python chaos_script.py --run --base-url http://127.0.0.1:8000
```

The script sets the simulated primary to unavailable, asks the monitor to check, restores the primary, asks the monitor to check again, and prints each HTTP result. It attempts to restore the primary to healthy even if an intermediate step fails. It does not guarantee a particular failover outcome; the configured Shield, health state, and API behavior determine that. Review the output and `/api/monitor/status` afterward.

## 12. Troubleshooting

### `DATABASE_URL is missing`
- Ensure `.env` is in the project root next to `init_db.py`.
- Ensure the key is exactly `DATABASE_URL`.
- Restart Uvicorn after changing environment configuration.

### PostgreSQL connection refused
- Check `Get-Service *postgres*`.
- Verify the host/port and database name.
- Confirm PostgreSQL is listening on port 5432 (unless configured otherwise).

### `ModuleNotFoundError: No module named 'fastapi'`
- Activate `.venv`.
- Run `python -m pip install -r requirements.txt` using the same Python interpreter that runs Uvicorn.

### Browser CORS error
- Use a supported local origin/port or add your exact development origin to `allow_origins` in `app/main.py`.
- Restart Uvicorn after changing CORS configuration.

### Dashboard is blank or some cards are empty
- Check the browser developer console and Network tab.
- Check that the API is running and inspect the failing endpoint response.
- Check `/api/health` and database connection.
- Remember that an empty table legitimately returns no records; do not replace missing backend data with fake success values.
- Confirm the page is being served from the project root and the expected HTML file is open.

### Simulation says not implemented
This is intentional for unsupported scenarios. Implement and test the backend behavior before marking a scenario as supported.

## 13. Safety, security, and limitations

- This is a local educational simulation, not production infrastructure.
- Do not run chaos tests against production services or systems you do not own/control.
- Never commit `.env`, database passwords, tokens, or other secrets.
- The primary and backup are simulated in-process dependencies, not separate physical servers.
- A database failure can prevent persistent logging; the application should not claim a record was saved unless the write succeeded.
- Basic payload filtering does not guarantee that all sensitive data is removed.
- In-memory monitor/simulation state may reset when the API process restarts, even though database history persists.
- Before deployment, add authentication/authorization, rate limits, secret management, structured migrations, stronger observability, concurrency review, and production-grade failover/load-balancing infrastructure.

## 14. Academic explanation / viva summary

**What is ChronoGuard?**  
ChronoGuard is a Python/FastAPI and PostgreSQL-backed web project that demonstrates request validation, simulated dependency health monitoring, controlled failure simulation, primary/backup decision logic, capacity admission control, and persistent event logging.

**What problem does it solve?**  
It demonstrates how to avoid blindly processing invalid requests or routing work to a simulated unhealthy dependency, and how to make failure behavior visible and testable.

**What concepts does it use?**  
REST APIs, JSON, request validation, modular guard components, health checks, consecutive-failure thresholds, simulated failover, admission control, bounded chaos testing, relational persistence, and audit logging.

**What is the key limitation?**  
It demonstrates these concepts in a controlled simulation. It does not automatically repair arbitrary production software or create real redundant servers.

---

**Project status note:** This README documents the code and routes present in this repository snapshot. Use the live API's `/docs` and the scenario metadata as the final authority for exact request schemas and which simulations are implemented.

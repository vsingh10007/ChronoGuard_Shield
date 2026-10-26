# ⏱️ ChronoGuard

**Monitor. Simulate. Improve.**

ChronoGuard is a web-based request monitoring and failure simulation platform designed to track service uptime, visualize request activity, inspect request data, and simulate failures for system testing and analysis.

## ✨ Features

- **📊 Overview Dashboard** — Visualize request activity and uptime trends.
- **📋 Request Monitoring** — View, search, and filter request records.
- **⚡ Simulation Lab** — Run supported, bounded validation, load, health-check, failover, and PostgreSQL verification scenarios. Unsupported scenarios are shown but cannot be started.
- **🔁 Server failover** — At the configured 65% simulated-load threshold, the Health Shield can route from the overloaded primary to the healthy backup, then fail back once primary load drops below the threshold.
- **🔗 API Integration** — Integrate with a FastAPI backend and PostgreSQL database.

On startup, FastAPI creates any missing tables defined by the SQLAlchemy models. This does not alter existing tables; use a migration for schema changes. `python init_db.py` remains available for explicit database initialization.

Server load displayed in the dashboard is simulated workload pressure, not host CPU utilization. The Simulation Lab uses the backend's `/api/simulations/scenarios`, `/api/simulations/history`, and `/api/simulations` endpoints; live database connectivity is shown separately from demo data.

## 🛠️ Tech Stack

- **Frontend:** HTML, Tailwind CSS, JavaScript
- **Backend:** Python, FastAPI
- **Database:** PostgreSQL

---

*Built for reliable monitoring and controlled failure testing.*

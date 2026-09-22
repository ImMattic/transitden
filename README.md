# TransitDen

**Real-time and historical transit tracking for Denver's RTD network.**

TransitDen pulls live vehicle positions from the RTD GTFS-RT feed every 30 seconds and stores them in a time-series database. A live map shows every rail car and Flatiron Flyer bus moving in real time, and a dashboard surfaces on-time performance, frequency stats, and delay incidents — data that RTD's own tools don't make easy to explore.

---

## Features

- **Live map** — vehicle positions update every 10 seconds via Leaflet; click any vehicle for route, next stop, and on-time status
- **Historical data** — all positions are persisted in TimescaleDB for trend analysis and export
- **Dashboard** — on-time performance charts, frequency tables, and service delivery (trips operated vs. scheduled) by route
- **GTFS static data** — route shapes, stop names, and schedule info parsed directly from RTD's static feed

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Frontend** | Next.js 14 (App Router), TypeScript, Tailwind CSS, TanStack Query, Leaflet |
| **Backend** | Python 3.11+, FastAPI, Uvicorn, APScheduler |
| **Database** | PostgreSQL 16 + TimescaleDB, SQLAlchemy 2.0 (async), Alembic |
| **Infrastructure** | Docker, Docker Compose |

---

## Project Structure

```
transitden/
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI route handlers
│   │   ├── models/       # SQLAlchemy ORM models
│   │   ├── schemas/      # Pydantic request/response schemas
│   │   ├── services/     # GTFS ingestion, scheduler, business logic
│   │   └── main.py       # App entry point
│   └── alembic/          # Database migrations
├── frontend/
│   ├── app/              # Next.js App Router pages
│   │   ├── dashboard/    # Analytics dashboard
│   │   └── historical/   # Historical data explorer
│   └── components/
│       ├── map/          # VehicleMap, VehicleDialog
│       └── dashboard/    # Charts, stats cards, frequency tables
├── gtfs-static/          # RTD static GTFS data (routes, stops, shapes)
├── gtfs-realtime/        # Protobuf definitions for GTFS-RT
├── tests/
└── docker-compose.yml
```

---

## Getting Started

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- [Node.js 20+](https://nodejs.org/) _(only needed for frontend-only development)_
- [Python 3.11+](https://www.python.org/) _(only needed for backend-only development)_

### Run with Docker

This is the fastest way to get a fully working local environment.

```bash
git clone https://github.com/ImMattic/transitden.git
cd transitden

# Start all services (database, backend, frontend)
docker compose up --build
```

The backend automatically runs Alembic migrations and starts polling the RTD GTFS-RT feed on startup.

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| Backend API | http://localhost:8000 |
| API docs (Swagger) | http://localhost:8000/docs |
| Health check | http://localhost:8000/health |

### Run without Docker

If you'd prefer to run services individually:

**1. Start the database**

You'll need a running PostgreSQL 16 instance with the TimescaleDB extension. Docker is the easiest way:

```bash
docker compose up db
```

**2. Run the backend**

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r ../requirements.txt

export DATABASE_URL=postgresql+asyncpg://transitden:transitden@localhost:5432/transitden

alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**3. Run the frontend**

```bash
cd frontend
npm install
BACKEND_URL=http://localhost:8000 npm run dev
```

---

## Configuration

The `docker-compose.yml` file contains all default environment variables for local development. Key settings:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://transitden:transitden@db:5432/transitden` | TimescaleDB connection string |
| `GTFS_RT_VEHICLE_URL` | RTD vehicle positions feed | GTFS-RT protobuf endpoint |
| `GTFS_RT_TRIP_URL` | RTD trip updates feed | GTFS-RT protobuf endpoint |
| `POLLING_INTERVAL_SECONDS` | `10` | How often to fetch live positions |
| `CORS_ORIGINS` | `["http://localhost:3000"]` | Allowed frontend origins |

---

## Running Tests

```bash
# From the repo root
python -m pytest tests/
```

---

## Contributing

Contributions are welcome. Here's how to get involved:

### 1. Fork and clone

```bash
git clone https://github.com/ImMattic/transitden.git
cd transitden
```

### 2. Create a branch

Use a descriptive name:

```bash
git checkout -b feature/on-time-performance-chart
git checkout -b fix/vehicle-dialog-crash
```

### 3. Make your changes

- **Backend changes** live in `backend/app/`
- **Frontend changes** live in `frontend/`
- **Database schema changes** require a new Alembic migration:
  ```bash
  docker compose exec backend alembic revision --autogenerate -m "describe your change"
  docker compose exec backend alembic upgrade head
  ```

### 4. Test your changes

```bash
python -m pytest tests/
```

Make sure the app builds and runs cleanly with `docker compose up --build`.

### 5. Open a PR

Push your branch and open a PR against `main`. Include a short description of what you changed and why.

---

## Roadmap

The items below are the current priorities. See [TODO.md](TODO.md) for the full list.

**Map**
- [ ] Rail schematic with moving vehicle icons
- [ ] Icon color coding by service frequency
- [ ] Vehicle click dialog (next stop, on-time status, upcoming stops table)

**Data**
- [ ] Historical performance explorer
- [ ] CSV/JSON data export
- [ ] On-time performance calculations

**Dashboard**
- [ ] On-time performance charts
- [ ] Headway adherence and missed stops
- [ ] Delay incident log

**Backend**
- [ ] Persist GTFS-RT data to TimescaleDB (in progress)
- [ ] WebSocket support for push-based updates

---

## Data Sources

- **[RTD GTFS-RT](https://www.rtd-denver.com/open-records/open-spatial-information/gtfs)** — live vehicle positions and trip updates (protobuf, updated every 10–30 seconds)
- **RTD GTFS Static** — routes, stops, shapes, and schedule data (bundled in `gtfs-static/`)

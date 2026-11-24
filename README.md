# quantyERM

Enterprise risk management with quantitative risk analysis (Monte Carlo).
Risks are described by how often they happen and what each event costs; the
model simulates many years and reports expected annual loss, P90, VaR/TVaR 99
per risk, per business unit (rolled up through the hierarchy) and in total.

## Architecture: three tiers

```
presentation/   REST API (FastAPI) for web front ends, e.g. the Lovable app
                CLI for local use
      │  calls
services/       RiskService: all business rules, validation, cascades,
                import/export, simulation   (+ domain/: models, risk_model)
      │  calls
storage/        Repository interface
                ├── JsonRepository   one JSON file (default)
                ├── SqlRepository    SQLAlchemy: SQLite, PostgreSQL, ...
                └── Neo4jRepository  nodes + relationships
```

Each tier only knows the one below it. The service layer contains every rule,
so all backends behave identically, and the same tests run against all of them.
Data can be moved between backends with `export` / `import` (IDs are kept).

## Getting started

```powershell
py -m pip install -r requirements.txt
py -m quanterm demo                 # example organisation with 7 risks
py -m quanterm tree
py -m quanterm risks
py -m quanterm simulate --compare   # inherent vs. residual (after controls)
py -m quanterm serve                # API on http://127.0.0.1:8000/docs
py -m pytest                        # tests
```

## Choosing a backend

Set environment variables (PowerShell: `$env:NAME = "value"`):

| Variable | Meaning | Default |
|---|---|---|
| `QUANTERM_BACKEND` | `json`, `sql`, `neo4j` or `memory` | `json` |
| `QUANTERM_JSON_PATH` | data file for the JSON backend | `quanterm_data.json` |
| `QUANTERM_SQL_URL` | SQLAlchemy URL, e.g. `postgresql+psycopg://user:pw@host/db` | `sqlite:///quanterm.db` |
| `QUANTERM_NEO4J_URI` / `_USER` / `_PASSWORD` / `_DATABASE` | Neo4j connection | `neo4j://localhost:7687`, `neo4j` |
| `QUANTERM_API_KEY` | if set, API requests need header `X-API-Key` | off |
| `QUANTERM_CORS_ORIGINS` | comma-separated origins allowed to call the API | none |

Credentials live only in the environment of the server, never in code or in
the browser.

## Describing a risk

```json
{
  "name": "Ransomware attack",
  "business_unit_id": "...",
  "frequency": {"type": "poisson", "rate": 0.15},
  "impact": {"dist": "lognorm_p10_p90", "p10": 200000, "p90": 5000000},
  "residual_frequency": {"type": "poisson", "rate": 0.05},
  "controls": ["EDR on all endpoints", "Offline backups"]
}
```

* **frequency** (events per year): `poisson` (`rate`) or `bernoulli` (`p`, at most one event).
* **impact** (loss per event): `lognorm_p10_p90` (low/high estimate, 80% interval),
  `pert` / `triangular` (`min`, `mode`, `max`), `uniform`, `fixed`, or any
  `scipy.stats` distribution via `{"dist": "gamma", "params": {...}}`.
* **residual_*** (optional): the same after controls. Simulations use them by
  default; `--inherent` / `"residual": false` ignores them.

Risks are simulated as independent of each other.

## Connecting the Lovable front end

The web app should call this API instead of connecting to Neo4j from the
browser. Main endpoints (all under `/api`, full list at `/docs`):

| Page | Endpoint |
|---|---|
| Dashboard | `GET /api/dashboard` |
| Organisation chart | `GET /api/business-units/tree` |
| Risks | `GET/POST /api/risks`, `PATCH/DELETE /api/risks/{id}` |
| Assets, persons | `/api/assets`, `/api/persons` |
| Advanced analytics | `POST /api/simulations`, `POST /api/simulations/compare` |
| Import/Export | `GET /api/export`, `POST /api/import` |

Start the API with the app's origin allowed:

```powershell
$env:QUANTERM_CORS_ORIGINS = "https://doserman.lovable.app"
py -m quanterm serve
```

For use by others the API must be hosted somewhere reachable (not localhost)
and protected (`QUANTERM_API_KEY` as a minimum, real user login later).

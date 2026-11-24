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
  "residual_frequency": {"type": "poisson", "rate": 0.05}
}
```

* **frequency** (events per year): `poisson` (`rate`) or `bernoulli` (`p`, at most one event).
* **impact** (loss per event): `lognorm_p10_p90` (low/high estimate, 80% interval),
  `pert` / `triangular` (`min`, `mode`, `max`), `uniform`, `fixed`, or any
  `scipy.stats` distribution via `{"dist": "gamma", "params": {...}}`.
* **residual_*** (optional): the same after controls. Simulations use them by
  default; `--inherent` / `"residual": false` ignores them.

Risks are simulated as independent of each other.

### Derived 1-5 scores

The qualitative heat-map scores are calculated from the estimates, never typed in:

* **likelihood** from P(at least one event per year): thresholds 5%, 20%, 50%, 80%
* **impact** from the median loss of one event: 50k, 250k, 1M, 5M
* **score** = likelihood x impact (1-25); 16+ Very High, 9+ High, 4+ Medium, 2+ Low

The thresholds are in `ScoringScale` (`quanterm/services/risk_service.py`) and exposed at `GET /api/meta`.

## Other entities

* **Controls** (`/api/controls`): type, implementation status, 1-5 self-assessed
  effectiveness, and the risks they mitigate. Their numeric effect is entered as
  the residual estimate on each risk.
* **Control events** (`/api/control-events`): tests/reviews/audits with result
  PASSED / PARTIAL / FAILED. Control effectiveness on the dashboard is based on
  the latest completed test of each control.
* **Risk events** (`/api/risk-events`): incidents, near misses and audit findings
  with the actual financial impact.
* **Audit log** (`/api/audit-log`): written by the service on every create,
  update, delete and import. The `X-User-Id` header is recorded as the user
  (informational until real authentication exists).

Deleting keeps the data consistent: deleting a unit removes its assets, risks,
controls and events; deleting a risk removes its events and unlinks it from
controls; deleting a person clears the roles they held.

## Connecting the Lovable front end

The web app should call this API instead of connecting to Neo4j from the
browser. Main endpoints (all under `/api`, full list at `/docs`):

| Page | Endpoint |
|---|---|
| Dashboard | `GET /api/dashboard` |
| Organisation chart, risk overview | `GET /api/business-units/risk-summary`, `GET /api/business-units/tree` |
| Risks | `GET/POST /api/risks`, `PATCH/DELETE /api/risks/{id}` (includes derived `scores`) |
| Controls, control events | `/api/controls`, `/api/control-events` |
| Risk events | `/api/risk-events` |
| Audit log | `GET /api/audit-log` |
| Assets, persons | `/api/assets`, `/api/persons` |
| Advanced analytics | `POST /api/simulations`, `POST /api/simulations/compare` |
| Import/Export, Excel upload | `GET /api/export`, `POST /api/import?replace=false` |
| Form values | `GET /api/meta` |

The front end lives in https://github.com/mfalck/doserman (branch `api-backend`).

Start the API with the app's origin allowed:

```powershell
$env:QUANTERM_CORS_ORIGINS = "https://doserman.lovable.app"
py -m quanterm serve
```

For use by others the API must be hosted somewhere reachable (not localhost)
and protected (`QUANTERM_API_KEY` as a minimum, real user login later).

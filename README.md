# RiskMesh

Open source enterprise risk management with quantitative risk analysis.
Risks are described by how often they happen and what each event costs; the
model computes the distribution of annual losses and reports expected annual
loss, P90, VaR/TVaR 99 per risk, per business unit (rolled up through the
hierarchy) and in total.

## Architecture: three tiers

```
presentation/   REST API (FastAPI) for web front ends, e.g. the Lovable app
                CLI for local use
gui/            Desktop application (PySide6/Qt), runs the services in-process
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
py -m pip install -e ".[all]"       # or: -r requirements.txt
py -m riskmesh config init          # writes a commented riskmesh.toml in this folder
py -m riskmesh demo                 # example organisation with 7 risks
py -m riskmesh gui                  # desktop application
py -m riskmesh tree
py -m riskmesh risks
py -m riskmesh simulate --compare   # inherent vs. residual (after controls)
py -m riskmesh serve                # API on http://127.0.0.1:8000/docs
py -m pytest                        # tests
```

## Configuration file and backend

Settings live in `riskmesh.toml`; `riskmesh config init` writes a commented
template (`--user-dir` puts it in `%APPDATA%\RiskMesh` / `~/.config/riskmesh`).
It is looked up in this order: `--config FILE`, `RISKMESH_CONFIG`, the current
folder, your user config folder. `riskmesh config show` prints what is in effect.

```toml
[storage]
backend = "sql"                     # json | sql | neo4j | memory
sql_url = "sqlite:///riskmesh.db"   # relative paths are relative to this file

[storage.neo4j]
password_env = "RISKMESH_NEO4J_PASSWORD"   # secrets via environment variables

[api]
port = 8000
cors_origins = ["http://localhost:8080"]

[analysis]
engine = "analytic"                 # or monte_carlo (n_trials, seed)

[scoring]
likelihood_bands = [0.05, 0.20, 0.50, 0.80]
impact_bands = [50000, 250000, 1000000, 5000000]

[user]
name = "Martin"                     # name in the audit log for GUI/CLI changes
```

Environment variables override the file (PowerShell: `$env:NAME = "value"`):

| Variable | Meaning | Default |
|---|---|---|
| `RISKMESH_BACKEND` | `json`, `sql`, `neo4j` or `memory` | `json` |
| `RISKMESH_JSON_PATH` | data file for the JSON backend | `riskmesh_data.json` |
| `RISKMESH_SQL_URL` | SQLAlchemy URL, e.g. `postgresql+psycopg://user:pw@host/db` | `sqlite:///riskmesh.db` |
| `RISKMESH_NEO4J_URI` / `_USER` / `_PASSWORD` / `_DATABASE` | Neo4j connection | `neo4j://localhost:7687`, `neo4j` |
| `RISKMESH_API_KEY` | shared key for the API when `auth.mode = "none"` (header `X-API-Key`) | off |
| `RISKMESH_AUTH_MODE` | `none`, `local`, `oidc` or `local+oidc` | `none` |
| `RISKMESH_ADMIN_USER` / `_PASSWORD` | create the first admin when the API starts and there are no users | |
| `RISKMESH_CORS_ORIGINS` | comma-separated origins allowed to call the API | none |

Credentials live only in the environment of the server, never in code or in
the browser.

## Desktop application

`riskmesh gui` (or `riskmesh-gui`, or `py -m riskmesh.gui --config FILE`) opens
a desktop window that works directly on the configured backend - no server
needed. Pages: overview, organisation tree, risks (with a live preview of the
derived scores while you edit), controls, analysis with run history and loss
exceedance chart, and the audit log with integrity check. Changes are recorded
with source `gui`, your configured name and an optional reason. File menu:
import/export JSON, load demo data, open another configuration file.

Requires `pip install "riskmesh[gui]"` (PySide6). Not yet in the desktop app:
risk/control events and asset editing (use the web app, CLI or API).

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

Risks are treated as independent of each other.

### Two calculation engines

| | `analytic` (default) | `monte_carlo` |
|---|---|---|
| Method | Exact compound distribution via FFT (`riskmesh/domain/analytic.py`) | Simulation of many years |
| Precision | Grid step reported as `grid_step` (a few thousand on the demo total, finer per risk) | Sampling noise, shrinks with `n_trials` |
| Repeatability | Always identical | Identical with the same `seed` |
| Speed (demo) | ~1 s, independent of precision | ~2 s for 1 million years |
| Limits | Independent risks only (reports a warning when correlations are set); the std of very heavy tails can be a few % low | Applies correlations between risks |

On the demo data both agree within 0.4% for mean, P50, P90, P99 and TVaR 99
(Monte Carlo with 1 million years). Choose with `"engine"` in
`POST /api/simulations`, `engine=` on `/api/simulations/compare`, or
`riskmesh simulate --engine monte_carlo`.

### Stored results and the run register

Every analysis is stored as a numbered **run** with a SHA-256 fingerprint of
exactly what it depends on: the risks and units in scope (only the fields the
calculation uses), the method, its settings and the engine version. Asking
again while that data is unchanged returns the stored report instantly
(`"cached": true`); a change to a description does not count, a change to a
frequency does. Runs are never deleted: when the data changes a run is marked
`stale`, but it remains the record of what was reported.

Each run records who made it, when, the package version and the audit chain
head at that moment; creating and naming runs are entries in the audit trail.
A run that is not stale can be recalculated to show the result is identical.
Monte Carlo without a seed is not repeatable and therefore never reused.

```powershell
py -m riskmesh simulate --compare --label "Board meeting 2026-12-01"
py -m riskmesh runs list
py -m riskmesh runs show 17
py -m riskmesh runs reproduce 17
```

API: `POST /api/simulations` and `/api/simulations/compare` (`cache`, `label`),
`GET /api/runs`, `GET/PATCH /api/runs/{id}`, `POST /api/runs/{id}/reproduce`.
Reports are stored in the configured backend; storing whole distributions per
risk (to recalculate only what changed) is a later step.

### Derived 1-5 scores

The qualitative heat-map scores are calculated from the estimates, never typed in:

* **likelihood** from P(at least one event per year): thresholds 5%, 20%, 50%, 80%
* **impact** from the median loss of one event: 50k, 250k, 1M, 5M
* **score** = likelihood x impact (1-25); 16+ Very High, 9+ High, 4+ Medium, 2+ Low

The thresholds are in `ScoringScale` (`riskmesh/services/risk_service.py`) and exposed at `GET /api/meta`.

## Other entities

* **Controls** (`/api/controls`): type, implementation status, 1-5 self-assessed
  effectiveness, and the risks they mitigate. Their numeric effect is entered as
  the residual estimate on each risk.
* **Control events** (`/api/control-events`): tests/reviews/audits with result
  PASSED / PARTIAL / FAILED. Control effectiveness on the dashboard is based on
  the latest completed test of each control.
* **Risk events** (`/api/risk-events`): incidents, near misses and audit findings
  with the actual financial impact.
* **Audit log** (`/api/audit-log`): see below.

Deleting keeps the data consistent: deleting a unit removes its assets, risks,
controls and events; deleting a risk removes its events and unlinks it from
controls; deleting a person clears the roles they held.

## Third parties: suppliers, services and customers

For organisations that are a supplier themselves (for example to customers
under NIS2 or DORA) and depend on suppliers in turn:

```
supplier ─┬─ its subcontractors (our fourth parties)
          └─> service we deliver ──> customers (regulation, contract terms)
```

* **Suppliers** (`/api/suppliers`): tier (critical / important / standard),
  country, data locations, certifications or assurance reports, contract and
  review dates, exit plan, and their own subcontractors.
* **Services** (`/api/services`): what we deliver, who delivers it, criticality,
  RTO/RPO, the suppliers and assets it needs.
* **Customers** (`/api/customers`): sector, regulation (NIS2 essential/important,
  DORA, CER, GDPR), the services they use, whether we support a critical or
  important function for them (DORA), contract value, contractual incident
  notification deadline, audit rights and exit support.
* Risks can be linked to the services they can disrupt and the suppliers they
  come from (`service_ids`, `supplier_ids`).

What it answers:

* `/api/suppliers/{id}/exposure`: if this supplier fails, which services and
  customers are hit, including when it is only a subcontractor of one of our
  suppliers; the contract value at stake, regulated customers and the shortest
  notification deadline.
* `/api/services/{id}/exposure`: the customers of a service and its whole
  supplier chain.
* `/api/customers/{id}/profile`: what a customer depends on (services,
  suppliers incl. fourth parties, risks, controls), the basis of the
  information DORA customers ask for.
* `/api/third-parties/overview`: concentration (suppliers and services ranked by
  what depends on them) and **findings** a NIS2/DORA review would ask about:
  overdue reviews, contracts ending, critical suppliers without an exit plan,
  personal data in unknown locations, DORA contracts missing audit rights, exit
  support or a notification deadline, critical services without an RTO.

```bash
riskmesh third-party overview
riskmesh third-party findings
riskmesh third-party exposure "Nordic Cloud A/S"
riskmesh third-party customer "East Savings Bank"
```

### Outages: one event, losses at many customers (common shock)

A risk can say how long one event takes its services down (`outage_hours`,
a distribution in hours, same formats as an impact; `residual_outage_hours`
after controls). Services have our own cost per hour (`outage_cost_per_hour`),
customers a contractual penalty per hour after a grace period, up to a cap
(`penalty_per_hour`, `penalty_grace_hours`, `penalty_cap`). One event then costs

```
impact  +  Σ services  cost/hour × D  +  Σ customers  min(cap, penalty/hour × max(0, D − grace))
```

with **one** duration D drawn per event and shared by everyone it hits: the
services the risk names, and every service behind a supplier it names
(including through subcontractors). So an outage of a supplier behind three
customers costs all three penalties at once, and the tail of the loss
distribution shows that concentration instead of averaging it away as
independent risks would. Both engines handle it (the analytic engine exactly
on its grid); reports list the annual consequences per customer and service
under `consequences`, and the derived 1-5 scores include them.

`/api/third-parties/stress-test?hours=24&supplier_id=...` (or `service_id`)
gives the deterministic cost of an outage of a given length, per service and
customer, and names customers whose notification deadline is shorter than the
outage:

```bash
riskmesh third-party stress "Nordic Cloud A/S" 24
```

Risks without `outage_hours` are calculated exactly as before, so stored runs
of existing data stay valid.

The desktop application and the web app have a *Third parties* page with the
same views, the stress test for a selected supplier or service, and the
outage fields in the risk, service and customer forms. The regulatory findings are a checklist, not legal advice: confirm
the scope of NIS2 and DORA for your customers with your compliance function.

## Decisions: what controls are worth, and what to fund

A control can carry its cost (`implementation_cost` one-off, `annual_cost`,
`lifetime_years`) and its effect: the share of events it prevents
(`frequency_reduction`) and the share of the loss per event it removes
(`impact_reduction`, which also shortens outages). Effects of several controls
on one risk multiply.

`GET /api/decisions/controls` values every control on the current estimates:

- a planned control by what it would remove: its effect added to the risks'
  current (residual) estimates;
- an implemented control by what we would lose without it: its effect taken
  out again.

Per control: expected annual loss removed, cost per year (annual cost plus the
implementation cost spread over the lifetime), net, return (ROSI), payback,
and the P99 effect on the total. The P99 effect is where e.g. backups show
their worth: little on the average, a lot on the 1-in-100 year.

`GET /api/decisions/plan?budget=400000` picks planned (and in-progress)
controls within a first-year budget, by loss removed per unit of cost, each on
top of those already picked (so two controls on one risk are not counted
twice), and shows the expected loss, P90 and P99 before and after, and which
appetites and objective tolerances that changes. Tail figures come from the
analytic engine (risks independent).

`POST /api/controls/{id}/implement` marks a planned control implemented and
builds its effect into the residual estimates of its risks, recorded in the
audit log, so it is not counted twice afterwards.

```bash
riskmesh decisions --budget 400000
```

The desktop application and the web app have a *Decisions* page; the control
forms and the Excel workbook (*Controls*) have the cost and effect fields.

## Objectives: what the risks threaten

An **objective** is something the organisation must achieve: "keep the citizen
portal available", "deliver meter data to the market on time", "grow sales
10%". It has a criticality (`MISSION_CRITICAL`, `IMPORTANT`, `SUPPORTING`), an
owner, a KPI, optionally a business unit and a target date, the services it is
delivered through, and a tolerance: at most `tolerance_probability` chance per
year of losing more than `tolerance_loss` against it.

A risk threatens an objective when

- it names it (`Risk.objective_ids`), or
- it can disrupt a service the objective is delivered through - directly
  (`Risk.service_ids`) or because it comes from a supplier behind that service,
  also as a fourth party (`Risk.supplier_ids`).

Every report (both engines, inherent and residual) then has `objectives[]`:
per objective the annual loss distribution of exactly the risks that threaten
it (expected loss, P90, P99, TVaR, the loss exceedance curve; in Monte Carlo
with the correlations between them) and whether the tolerance is kept. A
threatening risk counts with its whole loss.

`GET /api/objectives/overview` adds the mapping (which risk, and how) and what
a review would ask: objectives without mapped risks ("is it really
risk-free?"), mission-critical objectives without owner, KPI or tolerance,
objectives outside tolerance, and risks that threaten no objective.

```bash
riskmesh objectives
```

The desktop application and the web app have an *Objectives* page, the risk
forms a "threatens objectives" list, and the Excel workbook an *Objectives*
sheet plus a "Threatens objectives" column on *Risks*.

## Data and calibration: observed events and public benchmarks

Estimates are where a risk analysis starts; data is how it stays honest.

**Observations** are events that actually happened: a date, a category
(`"data breach"`, `"hosting outage"` ...), a count and their total loss, and
a level that follows from what the row names:

| Row names | Level | Counts for risks ... |
|---|---|---|
| a supplier | supplier | that name the supplier, or a supplier that subcontracts to it |
| a business unit | unit | of that unit or a unit above it |
| neither | group | of a top-level unit |

An observation counts for a risk when its category is one of the risk's
`evidence_categories` (or the risk's own category), or when it names the risk.
The period the data covers starts with the first entry of each source (an
incident log kept since January 2025 also says when nothing happened).

**Benchmarks** are public figures for organisations like ours, e.g. from a
national statistics office, the data protection authority or an industry
report: events per year and/or a loss range per event, with a source, a year
and a `weight` (how much it counts: years of our own data for the frequency,
events for the loss). RiskMesh does not fetch them: you choose the sources
and how far to trust them. The demo's benchmarks are illustrative, not real
figures.

**Calibration** weighs the three (see `riskmesh/domain/calibration.py`):

- frequency, Poisson-Gamma: the estimate is worth `expert_years` years of
  data (default 2), each benchmark its weight, and our data n events in T
  years; the suggestion is the posterior mean, with a 90% interval;
- loss per event (lognormal P10/P90 estimates), credibility weighting on the
  log scale: the estimate counts as 5 events, a benchmark as its weight, each
  observed loss as one. Other loss shapes are compared, not adjusted.

Observed events happen with the controls in place, so the residual estimate
is calibrated when the risk has one. Nothing changes by itself:
`POST /api/risks/{id}/calibration/apply` (or the button in the GUI and web
app) replaces the estimate and records the basis in the audit log.

```bash
riskmesh calibrate                       # every risk with evidence: estimate, data, suggestion
riskmesh calibrate "Database outage"     # the details; --apply to accept, --expert-years 4 to trust it more
```

Load many rows through the Excel workbook: the sheets *Observations* and
*Benchmarks* (a workbook with only those sheets leaves everything else as it
is). API: `/api/observations`, `/api/benchmarks`, `/api/calibration`,
`/api/risks/{id}/calibration`. The desktop application has a *Data* page and
the web app a *Data & Calibration* page.

## Incidents: the reporting clock

An **incident** records when we became aware of it (`detected_at`, local time
`YYYY-MM-DDTHH:MM`), the services it hits (`service_ids`), the suppliers where
it started (`supplier_ids`; every service behind them counts as hit, also
through subcontractors), whether it is significant under NIS2
(`nis2_significant`: true, false or null for not assessed yet), and the risk
that materialised.

From that RiskMesh derives the deadlines:

| To | Deadline |
|---|---|
| CSIRT / authority (NIS2 art. 23, if significant) | early warning 24 h, incident notification 72 h, final report one month after the notification |
| Each affected customer | its contractual `notification_hours` from detection, or "no deadline agreed" |

Customers under DORA get a note: they must classify the incident themselves
and, if it is major, report within 4 hours of classifying it (no later than 24
hours after becoming aware), so they need our information early. Each deadline
is `pending`, `due_soon` (within 12 h), `overdue`, `sent` or `late`. Recording
a notification (`POST /api/incidents/{id}/notifications {"key": ...}`) stamps
the time; the final NIS2 report then counts from the actual notification. A
closed incident stops the clock: anything not sent shows as `not_sent`.

```bash
riskmesh incidents        # open incidents and every deadline not met yet, soonest first
```

The desktop application and the web app have an *Incidents* page with the
register, the next deadlines across all incidents and a "sent now" button per
deadline. Incidents are in the Excel workbook and the audit log. A working aid,
not legal advice: check the national NIS2 rules and the customers' contracts.

## DORA: what a financial customer's register needs from us

A financial entity's register of information covers its ICT providers and the
chain behind them. For a customer we are rank 1, our suppliers rank 2 and
their subcontractors rank 3 and beyond. `GET /api/customers/{id}/dora`
(and `/dora.xlsx`) collects it per customer:

- the provider (us): the top company unit's `lei` and `country`;
- the services the customer uses, with `ict_service_type`, criticality,
  RTO/RPO and `data_locations`;
- every provider in the chain with LEI or company number, country, ultimate
  parent, data locations, certifications and exit plan (sheet *Providers
  (B_05.01)*), and who serves whom per service (*Supply chain (B_05.02)*);
- the contract terms and the last 12 months' incidents that hit the customer;
- `gaps`: what is still missing (an LEI, a country, a service type ...).

LEIs are checked (ISO 17442 check digits). The workbook follows the register
templates but is not the regulatory file format: it is the basis the customer
reports from. Check the columns against the current EBA taxonomy.

```bash
riskmesh third-party dora "East Savings Bank" --xlsx dora-east.xlsx
```

In the web app the customer's dependency panel (Third parties) shows the
chain, the gaps and a download button for DORA customers; the desktop
application has *DORA register extract…* on the Third parties page.

## Risk appetite and correlation

### Correlation: bad years that come together

Two risks can share a cause: the same attackers, a shared platform, an
economic downturn. A **correlation** (`risk_a_id`, `risk_b_id`, `coefficient`
from -0.99 to 0.99, `rationale`) says how strongly their bad years tend to
coincide (0.1-0.3 weak, 0.3-0.6 moderate, above 0.6 strong; negative means
one tends to be good when the other is bad).

The Monte Carlo engine simulates every risk on its own as before and then
reorders the simulated years (Iman & Conover, a Gaussian copula on ranks), so

- each risk keeps exactly its own distribution and expected loss,
- risks that are in no correlation are not touched,
- the total gets a heavier tail: on the demo P99 rises from about 5.8m to 6.9m
  when all risks are chained with 0.8.

Pairwise estimates do not always fit together (A~B and B~C strong, A~C strongly
negative); then the nearest consistent set is used and the report says
`"adjusted": true`. The report's `correlation.pairs` also shows the correlation
of the simulated annual losses (`achieved`). It is lower than the coefficient
when a risk has many loss-free years, since those zeros tie.

The analytic engine adds up independent risks. With correlations set it still
calculates, but the report carries
`"correlation": {"applied": false, ...}` and the GUI and web app say so and
offer Monte Carlo.

### Risk appetite: a tolerance curve

An **appetite** is a set of points "at most this chance per year of losing more
than this amount", for the whole organisation or for one business unit
including its sub-units:

```json
{"name": "Group risk appetite",
 "points": [{"loss": 1000000, "probability": 0.25}, {"loss": 5000000, "probability": 0.05},
            {"loss": 20000000, "probability": 0.005}],
 "statement": "Losing more than 5m at most once in 20 years"}
```

A larger loss cannot be accepted more often than a smaller one. Every report
(both engines, inherent and residual) checks each appetite in its scope against
the loss exceedance curve of that scope. `appetite[]` gives per point the
accepted and the actual chance, whether it is `breached`, and the curve itself
for plotting. A report for one unit only includes the appetites for that unit
or units below it.

```bash
riskmesh portfolio                        # correlations and appetites
riskmesh simulate --engine monte_carlo    # ... with "Appetite: ... within / OUTSIDE"
```

API: `/api/correlations` and `/api/appetites` (CRUD). The desktop application
has an *Appetite* page, and the web app an *Appetite & Correlation* page. Each
appetite is shown as its tolerance curve over the curve with controls, with a
table of accepted vs actual chances. The *Analysis* pages draw the appetite on
the loss exceedance curve. Correlations and appetites are part of the Excel
workbook (sheets *Correlations* and *Appetite*, tolerance written as
`1000000 @ 25%; 5000000 @ 5%`), the JSON export and the audit log.

Both are inputs to a stored run's fingerprint only when set, so runs made before
they existed stay valid. Changing a correlation or an appetite marks runs as
"data changed". Deleting a risk deletes its correlations. Deleting a unit deletes
its appetite, so it never silently becomes organisation-wide.

## Audit trail (tamper-evident)

Every change made through RiskMesh appends one entry per affected entity:

| Field | Meaning |
|---|---|
| `seq` | 1, 2, 3, ... without gaps |
| `user_id` | `X-User-Id` header (API) or `--user` (CLI); informational until real authentication exists |
| `source` | `api`, `cli`, or `import:<file>:sha256:<hash of the imported content>` |
| `reason` | why: `X-Change-Reason` header (URL-encoded) or `--reason` |
| `changes` | field-level `{field: [before, after]}` |
| `state_hash` | SHA-256 of the entity after the change |
| `prev_hash`, `hash` | SHA-256 chain: each entry's hash covers all its fields and the previous hash |

`GET /api/audit-log/verify` or `riskmesh audit verify` checks that

* no entry was altered, removed, inserted or reordered (hash chain and `seq`), and
* the stored data equals the last audited state of every entity, i.e. nothing
  was changed, added or deleted directly in the database.

With the SQL backend the database refuses `UPDATE` and `DELETE` on the audit
table (triggers on SQLite; PostgreSQL trigger included but not yet tested).
The JSON and Neo4j backends cannot block edits, but `verify` detects them.

### Anchors: proof from outside the chain

The chain alone cannot reveal that its *newest* entries were removed. An
**anchor** fixes "entry #n had hash H" in two independent ways (configure one
or both in `[audit]`):

* **Signature** (Ed25519): `riskmesh audit keygen` creates a key; set
  `signing_key`. Keep the key file away from the database and note its key id
  elsewhere - with full server access someone could otherwise re-sign.
* **RFC 3161 time stamp**: set `tsa_url` (e.g. `https://freetsa.org/tsr` or a
  qualified EU/Danish TSA). Only the chain-head hash is sent. The authority's
  signature cannot be back-dated, so this is the stronger proof; it can be
  checked independently with `openssl ts -verify`.

Anchors are made with `riskmesh audit anchor` / `POST /api/audit-log/anchor`
and automatically when a period is closed. `verify` then also reports removed
newest entries, forged anchors and time stamps for another hash.
`riskmesh demo` and `reset_all()` start a new chain.

## Excel workbook

All data can be downloaded as one Excel workbook, edited and uploaded again;
with no data yet the download is an empty template.

* One sheet per kind of data (organisation, people, assets, suppliers,
  services, customers, risks, controls, risk events, control tests) and a
  *Read me* sheet that explains the columns.
* References are names (with drop-down lists); the ID column keeps the link
  to stored rows, and an ID can be used wherever a name is not unique. Lists
  are separated by `;`, yes/no columns take yes or no.
* A frequency is either *events per year* or *chance per year %*; a
  distribution is a model (`lognormal`, `pert`, `triangular`, `uniform`,
  `fixed`) and its numbers (low P10 / high P90, min / most likely / max, or
  fixed). Numbers may be typed as `250000`, `250.000` or `2,5`.
* Upload checks the whole workbook first and reports every problem with sheet,
  row and column. A preview shows what would be created, changed or removed
  before anything is stored; the import itself is recorded in the audit log
  with the workbook's SHA-256.
* Merge (default, editors) adds and updates rows and keeps everything else.
  Replace (administrators) also deletes stored rows that are missing from a
  sheet. Sheets removed from the workbook are never touched.

```bash
riskmesh excel export riskmesh.xlsx
riskmesh excel import riskmesh.xlsx --dry-run     # only show what would change
riskmesh excel import riskmesh.xlsx               # shows the changes and asks before storing
```

API: `GET /api/export.xlsx`, `POST /api/import.xlsx?dry_run=true&replace=false`
with the file as the request body. The desktop application has *Import from
Excel* and *Export to Excel* in the File menu, the web app an *Excel workbook*
page.

## Periods and reporting

Closing a period freezes its figures:

```powershell
py -m riskmesh period close 2026-Q4 --note "Q4 risk assessment"   # also 2026-11 or 2026
py -m riskmesh period compare 2026-Q3 2026-Q4
py -m riskmesh period trend --metric p99
py -m riskmesh period export facts.csv
```

Closing runs the analysis (stored as run "Period 2026-Q4"), writes a flat
**fact table** for the total, every business unit (incl. sub-units), every
category and every risk - expected loss with and without controls, P50/P90/
P99/TVaR, 1-5 scores, number of controls, recorded events and actual losses in
the period - records the facts' SHA-256 in the audit trail (so `verify`
detects later edits) and anchors the chain if configured. Reopening (admin)
removes the facts and is recorded too.

The comparison of two periods explains the change in expected annual loss
after controls as a **bridge**: new risks, removed risks, changed estimates
(inherent loss of continuing risks) and the change in control effect; plus a
risk-by-risk table and heat-map moves. The web app has a *Reports* page
(trend, waterfall, comparison) and the desktop app a *Reports* tab.

**Power BI / Excel.** With the SQL backend these read-only views are always
up to date: `v_periods`, `v_period_total`, `v_period_unit`,
`v_period_category`, `v_period_risk`. Connect Power BI to the database with a
read-only user, or use the CSV from `GET /api/reports/facts.csv` /
`riskmesh period export`.

```powershell
py -m riskmesh --reason "annual review" import --merge backup.json
py -m riskmesh audit log --limit 10
py -m riskmesh audit verify
```

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

The web front end (Lovable) lives in https://github.com/mfalck/riskmesh-web.

Start the API with the app's origin allowed:

```powershell
$env:RISKMESH_CORS_ORIGINS = "https://doserman.lovable.app"
py -m riskmesh serve
```

For use by others, host the API (see *Running a server*) and turn on login.

## Users, roles and login

Set `[auth] mode` in `riskmesh.toml` (or `RISKMESH_AUTH_MODE`):

| mode | |
|---|---|
| `none` | no login (default): for one person on their own machine; the API is open to anyone who can reach it |
| `local` | RiskMesh accounts with passwords (scrypt-hashed) |
| `oidc` | single sign-on via Microsoft Entra ID or any OpenID Connect provider |
| `local+oidc` | both |

Roles: **viewer** (see everything, run analyses), **editor** (also change
risks, controls, events, assets, organisation), **admin** (also users, API
tokens, and replacing all data on import). There is always at least one admin.

```powershell
py -m riskmesh users create-admin martin        # asks for a password
py -m riskmesh users create anna --role editor
py -m riskmesh users token reportbot --name "Power BI"   # API token for scripts, shown once
```

Clients send `Authorization: Bearer <token>`: a login session from
`POST /api/auth/login` (expires after `session_hours`) or an API token. The
audit log records the authenticated user - a client can no longer choose the
name. Five wrong passwords lock that username for a minute. Admins manage users
in the web app (*Users*) or via `/api/users`.

**Single sign-on (Entra ID).** Register an app in Entra ID with the redirect
URI `https://<your-api>/api/auth/oidc/callback`, create app roles
`RiskMesh.Admin`, `RiskMesh.Editor`, `RiskMesh.Viewer`, assign them, and fill
in `[auth.oidc]` (issuer `https://login.microsoftonline.com/<tenant>/v2.0`,
`client_id`, the client secret in `RISKMESH_OIDC_CLIENT_SECRET`,
`frontend_url`). Users are created on first sign-in and their role follows the
claims on every login. The API handles the code exchange (with PKCE), so the
client secret never reaches the browser.

**Desktop app.** With login enabled it asks for a local account. It works
directly on the database, so its login decides who is recorded and what the
window allows; it is not a barrier for someone who can open the database file.
Single-sign-on accounts are for the web app.

## Running a server

```powershell
copy .env.example .env      # set POSTGRES_PASSWORD and RISKMESH_ADMIN_PASSWORD
docker compose up -d --build
```

This starts PostgreSQL, the API (two worker processes, login on, first admin
from `RISKMESH_ADMIN_USER`/`RISKMESH_ADMIN_PASSWORD`) and a nightly `pg_dump`
into `./backups` (30 days kept). Extra settings go in `./config/riskmesh.toml`.
Put a TLS-terminating reverse proxy (Caddy, nginx, Azure App Gateway, ...) in
front before exposing it to the internet.

**Several users at once.** With the SQL backend any number of API workers,
desktop apps and CLI sessions can work on the same database: every change runs
in one transaction that holds the database's write lock, so data and the audit
chain stay consistent (tested with parallel processes on SQLite and
PostgreSQL). The JSON file backend is for one process: a second one gets a
clear "in use" error instead of overwriting changes.

**Upgrades.** On start-up the SQL backend creates new tables and adds new
columns automatically and records the schema version; a database written by a
newer RiskMesh is refused. Data in JSON files and Neo4j picks up new fields
with their defaults.

**Backups.** Besides database dumps:

```powershell
py -m riskmesh backup riskmesh-backup.json      # everything: data, audit trail, runs, users
py -m riskmesh restore riskmesh-backup.json     # into empty storage; verifies the audit chain
```

A RiskMesh backup can be restored on a different backend (e.g. JSON to
PostgreSQL) with the audit chain intact. It contains password and token
hashes: keep it as confidential as the database.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).

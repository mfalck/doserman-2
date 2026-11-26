"""
REST API (presentation tier for web front ends such as the Lovable app).

Run:  py -m riskmesh serve            -> http://127.0.0.1:8000/docs

The browser talks only to this API; database credentials stay on the
server side. Endpoints are async and therefore handled one at a time,
which keeps the file-based backend safe without extra locking.
"""

from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import __version__
from ..bootstrap import make_service
from ..config import Settings, load_settings
from ..domain import audit
from ..domain import models as m
from ..domain.errors import ConflictError, NotFoundError, ValidationError
from ..services import RiskService
from . import schemas as s


def _changes(model) -> Dict[str, Any]:
    return model.model_dump(exclude_unset=True)


def create_app(service: Optional[RiskService] = None, settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    svc = service or make_service(settings, actor="anonymous", source="api")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        svc.repo.close()

    app = FastAPI(
        lifespan=lifespan,
        title="RiskMesh API",
        version=__version__,
        description="Enterprise risk management with quantitative (Monte Carlo) risk analysis.",
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # -- errors & auth ---------------------------------------------------

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _invalid(_: Request, exc: ValidationError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def _conflict(_: Request, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    async def require_api_key(x_api_key: Optional[str] = Header(None),
                              x_user_id: Optional[str] = Header(None),
                              x_change_reason: Optional[str] = Header(None)):
        if settings.api_key and x_api_key != settings.api_key:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-API-Key header")
        # Audit context for this request. The user is informational until real
        # authentication exists; the reason explains why a change was made.
        svc.actor = unquote(x_user_id or "anonymous")[:100] or "anonymous"
        svc.source = "api"
        svc.reason = (unquote(x_change_reason)[:500] or None) if x_change_reason else None

    # every /api route shares the API-key check
    api = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])

    @api.get("/meta", tags=["meta"])
    async def meta():
        """Allowed values and scoring thresholds, for building forms."""
        return {
            "organization_types": m.ORGANIZATION_TYPES,
            "control_types": m.CONTROL_TYPES,
            "implementation_statuses": m.IMPLEMENTATION_STATUSES,
            "risk_event_types": m.RISK_EVENT_TYPES,
            "risk_event_statuses": m.RISK_EVENT_STATUSES,
            "control_event_types": m.CONTROL_EVENT_TYPES,
            "control_event_statuses": m.CONTROL_EVENT_STATUSES,
            "frequency_types": ["poisson", "bernoulli"],
            "impact_distributions": ["lognorm_p10_p90", "pert", "triangular", "uniform", "fixed"],
            "scoring": {"likelihood_bands": svc.scoring.likelihood_bands,
                        "impact_bands": svc.scoring.impact_bands},
        }

    @app.get("/health", tags=["meta"])
    async def health():
        return {"status": "ok", "backend": settings.backend if service is None else type(svc.repo).__name__}

    # -- business units ----------------------------------------------------

    @api.get("/business-units", response_model=List[s.BusinessUnitOut], tags=["organisation"])
    async def list_business_units():
        return [u.to_dict() for u in svc.list_business_units()]

    @api.get("/business-units/tree", tags=["organisation"])
    async def organisation_tree():
        return svc.organisation_tree()

    @api.get("/business-units/risk-summary", tags=["organisation"])
    async def unit_risk_summary():
        return svc.unit_risk_summary()

    @api.post("/business-units", response_model=s.BusinessUnitOut, status_code=201, tags=["organisation"])
    async def create_business_unit(body: s.BusinessUnitCreate):
        return svc.create_business_unit(**body.model_dump()).to_dict()

    @api.get("/business-units/{unit_id}", response_model=s.BusinessUnitOut, tags=["organisation"])
    async def get_business_unit(unit_id: str):
        return svc.get_business_unit(unit_id).to_dict()

    @api.patch("/business-units/{unit_id}", response_model=s.BusinessUnitOut, tags=["organisation"])
    async def update_business_unit(unit_id: str, body: s.BusinessUnitUpdate):
        return svc.update_business_unit(unit_id, **_changes(body)).to_dict()

    @api.delete("/business-units/{unit_id}", tags=["organisation"])
    async def delete_business_unit(unit_id: str, cascade: bool = False):
        return {"deleted": svc.delete_business_unit(unit_id, cascade=cascade)}

    @api.put("/business-units/{unit_id}/risk-owner", response_model=s.BusinessUnitOut, tags=["organisation"])
    async def assign_risk_owner(unit_id: str, body: s.RiskOwnerAssign):
        return svc.assign_risk_owner(unit_id, body.person_id).to_dict()

    @api.put("/business-units/{unit_id}/delegates/{person_id}", response_model=s.BusinessUnitOut,
             tags=["organisation"])
    async def add_delegate(unit_id: str, person_id: str):
        return svc.add_delegate(unit_id, person_id).to_dict()

    @api.delete("/business-units/{unit_id}/delegates/{person_id}", response_model=s.BusinessUnitOut,
                tags=["organisation"])
    async def remove_delegate(unit_id: str, person_id: str):
        return svc.remove_delegate(unit_id, person_id).to_dict()

    # -- assets -------------------------------------------------------------

    @api.get("/assets", response_model=List[s.AssetOut], tags=["assets"])
    async def list_assets(business_unit_id: Optional[str] = None):
        return [a.to_dict() for a in svc.list_assets(business_unit_id)]

    @api.post("/assets", response_model=s.AssetOut, status_code=201, tags=["assets"])
    async def create_asset(body: s.AssetCreate):
        d = body.model_dump()
        return svc.create_asset(d.pop("business_unit_id"), **d).to_dict()

    @api.get("/assets/{asset_id}", response_model=s.AssetOut, tags=["assets"])
    async def get_asset(asset_id: str):
        return svc.get_asset(asset_id).to_dict()

    @api.patch("/assets/{asset_id}", response_model=s.AssetOut, tags=["assets"])
    async def update_asset(asset_id: str, body: s.AssetUpdate):
        return svc.update_asset(asset_id, **_changes(body)).to_dict()

    @api.delete("/assets/{asset_id}", status_code=204, tags=["assets"])
    async def delete_asset(asset_id: str):
        svc.delete_asset(asset_id)

    # -- persons ------------------------------------------------------------

    @api.get("/persons", response_model=List[s.PersonOut], tags=["persons"])
    async def list_persons():
        return [p.to_dict() for p in svc.list_persons()]

    @api.post("/persons", response_model=s.PersonOut, status_code=201, tags=["persons"])
    async def create_person(body: s.PersonCreate):
        return svc.create_person(**body.model_dump()).to_dict()

    @api.get("/persons/{person_id}", response_model=s.PersonOut, tags=["persons"])
    async def get_person(person_id: str):
        return svc.get_person(person_id).to_dict()

    @api.patch("/persons/{person_id}", response_model=s.PersonOut, tags=["persons"])
    async def update_person(person_id: str, body: s.PersonUpdate):
        return svc.update_person(person_id, **_changes(body)).to_dict()

    @api.delete("/persons/{person_id}", status_code=204, tags=["persons"])
    async def delete_person(person_id: str):
        svc.delete_person(person_id)

    # -- risks --------------------------------------------------------------

    @api.get("/risks", response_model=List[s.RiskOut], tags=["risks"])
    async def list_risks(business_unit_id: Optional[str] = None, category: Optional[str] = None,
                         include_children: bool = False):
        return svc.list_risk_views(unit_id=business_unit_id, category=category,
                                   include_children=include_children)

    @api.post("/risks", response_model=s.RiskOut, status_code=201, tags=["risks"])
    async def create_risk(body: s.RiskCreate):
        d = body.model_dump()
        return svc.risk_view(svc.create_risk(d.pop("business_unit_id"), d.pop("name"),
                                             d.pop("frequency"), d.pop("impact"), **d))

    @api.get("/risks/{risk_id}", response_model=s.RiskOut, tags=["risks"])
    async def get_risk(risk_id: str):
        return svc.risk_view(svc.get_risk(risk_id))

    @api.patch("/risks/{risk_id}", response_model=s.RiskOut, tags=["risks"])
    async def update_risk(risk_id: str, body: s.RiskUpdate):
        return svc.risk_view(svc.update_risk(risk_id, **_changes(body)))

    @api.delete("/risks/{risk_id}", status_code=204, tags=["risks"])
    async def delete_risk(risk_id: str):
        svc.delete_risk(risk_id)

    # -- controls -----------------------------------------------------------

    @api.get("/controls", response_model=List[s.ControlOut], tags=["controls"])
    async def list_controls(business_unit_id: Optional[str] = None, risk_id: Optional[str] = None):
        return [c.to_dict() for c in svc.list_controls(business_unit_id, risk_id)]

    @api.post("/controls", response_model=s.ControlOut, status_code=201, tags=["controls"])
    async def create_control(body: s.ControlCreate):
        d = body.model_dump()
        return svc.create_control(d.pop("business_unit_id"), d.pop("name"), **d).to_dict()

    @api.get("/controls/{control_id}", response_model=s.ControlOut, tags=["controls"])
    async def get_control(control_id: str):
        return svc.get_control(control_id).to_dict()

    @api.patch("/controls/{control_id}", response_model=s.ControlOut, tags=["controls"])
    async def update_control(control_id: str, body: s.ControlUpdate):
        return svc.update_control(control_id, **_changes(body)).to_dict()

    @api.delete("/controls/{control_id}", status_code=204, tags=["controls"])
    async def delete_control(control_id: str):
        svc.delete_control(control_id)

    # -- risk events --------------------------------------------------------

    @api.get("/risk-events", response_model=List[s.RiskEventOut], tags=["events"])
    async def list_risk_events(risk_id: Optional[str] = None, business_unit_id: Optional[str] = None):
        return [e.to_dict() for e in svc.list_risk_events(risk_id, business_unit_id)]

    @api.post("/risk-events", response_model=s.RiskEventOut, status_code=201, tags=["events"])
    async def create_risk_event(body: s.RiskEventCreate):
        d = body.model_dump()
        return svc.create_risk_event(d.pop("risk_id"), d.pop("name"), d.pop("date"), **d).to_dict()

    @api.patch("/risk-events/{event_id}", response_model=s.RiskEventOut, tags=["events"])
    async def update_risk_event(event_id: str, body: s.RiskEventUpdate):
        return svc.update_risk_event(event_id, **_changes(body)).to_dict()

    @api.delete("/risk-events/{event_id}", status_code=204, tags=["events"])
    async def delete_risk_event(event_id: str):
        svc.delete_risk_event(event_id)

    # -- control events -----------------------------------------------------

    @api.get("/control-events", response_model=List[s.ControlEventOut], tags=["events"])
    async def list_control_events(control_id: Optional[str] = None, business_unit_id: Optional[str] = None,
                                  status: Optional[str] = None):
        return [e.to_dict() for e in svc.list_control_events(control_id, business_unit_id, status)]

    @api.post("/control-events", response_model=s.ControlEventOut, status_code=201, tags=["events"])
    async def create_control_event(body: s.ControlEventCreate):
        d = body.model_dump()
        return svc.create_control_event(d.pop("control_id"), d.pop("date"), **d).to_dict()

    @api.patch("/control-events/{event_id}", response_model=s.ControlEventOut, tags=["events"])
    async def update_control_event(event_id: str, body: s.ControlEventUpdate):
        return svc.update_control_event(event_id, **_changes(body)).to_dict()

    @api.delete("/control-events/{event_id}", status_code=204, tags=["events"])
    async def delete_control_event(event_id: str):
        svc.delete_control_event(event_id)

    # -- audit log ----------------------------------------------------------

    @api.get("/audit-log", response_model=List[s.AuditEntryOut], tags=["audit"])
    async def audit_log(limit: int = 1000, entity_type: Optional[str] = None, action: Optional[str] = None,
                        entity_id: Optional[str] = None):
        return [a.to_dict() for a in svc.list_audit(limit, entity_type, action, entity_id)]

    @api.get("/audit-log/verify", tags=["audit"])
    async def verify_audit(check_data: bool = True):
        """Checks the hash chain and that stored data matches the last audited state."""
        return svc.verify_audit(check_data)

    # -- analysis -----------------------------------------------------------

    @api.get("/dashboard", tags=["analysis"])
    async def dashboard():
        return svc.dashboard_summary()

    @api.post("/simulations", tags=["analysis"])
    async def simulate(body: s.SimulationRequest):
        """One report (residual or inherent). Stored as a run; reused while the data is unchanged."""
        d = body.model_dump()
        return svc.run_analysis(mode="single", engine=d["engine"], n_trials=d["n_trials"], seed=d["seed"],
                                residual=d["residual"], unit_id=d["unit_id"], use_cache=d["cache"],
                                label=d["label"])

    @api.post("/simulations/compare", tags=["analysis"])
    async def compare(n_trials: int = 10_000, seed: int = 42, unit_id: Optional[str] = None,
                      engine: str = "analytic", cache: bool = True, label: Optional[str] = None):
        """Inherent vs residual. Stored as a run; reused while the data is unchanged."""
        if not 100 <= n_trials <= 1_000_000:
            raise ValidationError("n_trials must be between 100 and 1,000,000.")
        return svc.run_analysis(mode="compare", engine=engine, n_trials=n_trials, seed=seed, unit_id=unit_id,
                                use_cache=cache, label=label)

    # -- run register ---------------------------------------------------------

    @api.get("/runs", tags=["analysis"])
    async def list_runs(limit: int = 100):
        """Stored analysis runs, newest first, without reports; `stale` = data changed since."""
        return svc.list_runs(limit)

    @api.get("/runs/{run_id}", tags=["analysis"])
    async def get_run(run_id: str):
        return svc.get_run(run_id)

    @api.patch("/runs/{run_id}", tags=["analysis"])
    async def label_run(run_id: str, body: s.RunLabel):
        return svc.run_meta(svc.label_run(run_id, body.label))

    @api.post("/runs/{run_id}/reproduce", tags=["analysis"])
    async def reproduce_run(run_id: str):
        """Recalculates the run and checks that the result is identical."""
        return svc.reproduce_run(run_id)

    # -- import / export ----------------------------------------------------

    @api.get("/export", tags=["data"])
    async def export_data():
        return svc.export_data()

    @api.post("/import", tags=["data"])
    async def import_data(data: Dict[str, Any], replace: bool = True):
        # the audit log records exactly which content was imported
        svc.source = f"import:sha256:{audit.sha256(data)}"
        return {"imported": svc.import_data(data, replace=replace)}

    app.include_router(api)
    return app

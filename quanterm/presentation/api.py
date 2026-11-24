"""
REST API (presentation tier for web front ends such as the Lovable app).

Run:  py -m quanterm serve            -> http://127.0.0.1:8000/docs

The browser talks only to this API; database credentials stay on the
server side. Endpoints are async and therefore handled one at a time,
which keeps the file-based backend safe without extra locking.
"""

from typing import Any, Dict, List, Optional

from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import Settings
from ..domain.errors import ConflictError, NotFoundError, ValidationError
from ..services import RiskService
from ..storage import create_repository
from . import schemas as s


def _changes(model) -> Dict[str, Any]:
    return model.model_dump(exclude_unset=True)


def create_app(service: Optional[RiskService] = None, settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()
    svc = service or RiskService(create_repository(settings))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        svc.repo.close()

    app = FastAPI(
        lifespan=lifespan,
        title="quantyERM API",
        version="2.0",
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

    async def require_api_key(x_api_key: Optional[str] = Header(None)):
        if settings.api_key and x_api_key != settings.api_key:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-API-Key header")

    # every /api route shares the API-key check
    api = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])

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
        return [r.to_dict() for r in svc.list_risks(business_unit_id, category, include_children)]

    @api.post("/risks", response_model=s.RiskOut, status_code=201, tags=["risks"])
    async def create_risk(body: s.RiskCreate):
        d = body.model_dump()
        return svc.create_risk(d.pop("business_unit_id"), d.pop("name"),
                               d.pop("frequency"), d.pop("impact"), **d).to_dict()

    @api.get("/risks/{risk_id}", response_model=s.RiskOut, tags=["risks"])
    async def get_risk(risk_id: str):
        return svc.get_risk(risk_id).to_dict()

    @api.patch("/risks/{risk_id}", response_model=s.RiskOut, tags=["risks"])
    async def update_risk(risk_id: str, body: s.RiskUpdate):
        return svc.update_risk(risk_id, **_changes(body)).to_dict()

    @api.delete("/risks/{risk_id}", status_code=204, tags=["risks"])
    async def delete_risk(risk_id: str):
        svc.delete_risk(risk_id)

    # -- analysis -----------------------------------------------------------

    @api.get("/dashboard", tags=["analysis"])
    async def dashboard():
        return svc.dashboard_summary()

    @api.post("/simulations", tags=["analysis"])
    async def simulate(body: s.SimulationRequest):
        return svc.simulation_report(**body.model_dump())

    @api.post("/simulations/compare", tags=["analysis"])
    async def compare(n_trials: int = 10_000, seed: int = 42):
        return svc.compare_inherent_residual(n_trials, seed)

    # -- import / export ----------------------------------------------------

    @api.get("/export", tags=["data"])
    async def export_data():
        return svc.export_data()

    @api.post("/import", tags=["data"])
    async def import_data(data: Dict[str, Any], replace: bool = True):
        return {"imported": svc.import_data(data, replace=replace)}

    app.include_router(api)
    return app

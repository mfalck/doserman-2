"""
REST API (presentation tier for web front ends such as the Lovable app).

Run:  py -m riskmesh serve            -> http://127.0.0.1:8000/docs

The browser talks only to this API; database credentials stay on the
server side. Endpoints are async and therefore handled one at a time per
process; several processes on one SQL database are serialised by the
storage write lock.

Access ([auth] mode in riskmesh.toml):
  none        open API (X-API-Key if configured); X-User-Id names the user
  local/oidc  every /api request needs "Authorization: Bearer <token>" (a
              login session or an API token; X-API-Key: <token> also works).
              Roles: viewer = read and run analyses, editor = change risk
              data, admin = users, tokens and replacing all data on import.
"""

from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

import logging
import os
import re
import time
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import __version__
from ..bootstrap import make_service
from ..config import Settings, load_settings
from ..domain import audit
from ..domain import models as m
from ..domain.errors import (AuthenticationError, ConflictError, NotFoundError, PermissionDenied,
                             ValidationError)
from ..domain.models import User
from ..services import RiskService
from . import schemas as s


def _changes(model) -> Dict[str, Any]:
    return model.model_dump(exclude_unset=True)


log = logging.getLogger(__name__)
MAX_FAILED_LOGINS = 5
MAX_UPLOAD_BYTES = 20_000_000
LOCKOUT_SECONDS = 60


def required_role(method: str, path: str, query: Dict[str, str]) -> str:
    """The whole access policy in one place."""
    if path.startswith(("/api/users", "/api/tokens")):
        return "admin"
    if path.startswith("/api/periods/") and path.endswith(("/close", "/reopen")):
        return "admin"  # official figures
    if path == "/api/audit-log/anchor" and method == "POST":
        return "admin"
    if path == "/api/import":
        return "editor" if query.get("replace", "true").lower() == "false" else "admin"
    if path == "/api/import.xlsx":  # merge by default; replacing (deleting rows not in the workbook) is for admins
        return "admin" if query.get("replace", "false").lower() == "true" else "editor"
    if method in ("GET", "HEAD", "OPTIONS"):
        return "viewer"
    if path in ("/api/simulations", "/api/simulations/compare") or (
            path.startswith("/api/runs/") and path.endswith("/reproduce")):
        return "viewer"  # running an analysis does not change risk data
    return "editor"


def _bootstrap_admin(svc: RiskService) -> None:
    """For containers: RISKMESH_ADMIN_USER/RISKMESH_ADMIN_PASSWORD create the first admin if there are no users."""
    user, password = os.environ.get("RISKMESH_ADMIN_USER"), os.environ.get("RISKMESH_ADMIN_PASSWORD")
    if user and password and not svc.list_users():
        with svc.context(actor="system", source="bootstrap"):
            svc.create_user(user, "admin", password)
        log.warning("Created the first administrator '%s' from RISKMESH_ADMIN_USER", user)


def create_app(service: Optional[RiskService] = None, settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    svc = service or make_service(settings, actor="anonymous", source="api")
    auth_on = settings.auth_mode != "none"
    if auth_on:
        _bootstrap_admin(svc)
    else:
        log.warning("auth.mode = none: the API is open to anyone who can reach it")
    oidc = None
    if settings.oidc_login:
        from .oidc import OidcClient
        oidc = OidcClient(settings)
    failed_logins: Dict[str, List[float]] = {}

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

    @app.exception_handler(AuthenticationError)
    async def _unauthenticated(_: Request, exc: AuthenticationError):
        return JSONResponse(status_code=401, content={"detail": str(exc)}, headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(PermissionDenied)
    async def _forbidden(_: Request, exc: PermissionDenied):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    def _token_from(authorization: Optional[str], x_api_key: Optional[str]) -> Optional[str]:
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return x_api_key

    async def authorize(request: Request, authorization: Optional[str] = Header(None),
                        x_api_key: Optional[str] = Header(None), x_user_id: Optional[str] = Header(None),
                        x_change_reason: Optional[str] = Header(None)) -> Optional[User]:
        """Who is calling, may they do this, and the audit context for the request."""
        svc.source = "api"
        svc.reason = (unquote(x_change_reason)[:500] or None) if x_change_reason else None
        if not auth_on:
            if settings.api_key and x_api_key != settings.api_key:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-API-Key header")
            svc.actor = unquote(x_user_id or "anonymous")[:100] or "anonymous"
            return None
        user = svc.resolve_token(_token_from(authorization, x_api_key))
        svc.require_role(user, required_role(request.method, request.url.path, dict(request.query_params)))
        svc.actor = user.username
        request.state.user = user
        return user

    # every /api route (except /api/auth/*) goes through authorize()
    api = APIRouter(prefix="/api", dependencies=[Depends(authorize)])
    auth = APIRouter(prefix="/api/auth", tags=["auth"])

    @auth.get("/config")
    async def auth_config():
        """What the login page should offer."""
        return {"mode": settings.auth_mode, "local": settings.local_login, "oidc": settings.oidc_login,
                "oidc_login_url": "/api/auth/oidc/login" if settings.oidc_login else None}

    @auth.post("/login")
    async def login(body: s.LoginRequest, request: Request):
        if not settings.local_login:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Password login is not enabled")
        key = f"{body.username.lower()}|{request.client.host if request.client else ''}"
        now = time.time()
        recent = [t for t in failed_logins.get(key, []) if now - t < LOCKOUT_SECONDS]
        if len(recent) >= MAX_FAILED_LOGINS:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts; wait a minute.")
        try:
            user = svc.authenticate(body.username, body.password)
        except AuthenticationError:
            failed_logins[key] = recent + [now]
            raise
        failed_logins.pop(key, None)
        with svc.context(actor=user.username, source="api"):
            token, record = svc.issue_token(user.id, "session", hours=settings.session_hours)
        return {"token": token, "expires_at": record.expires_at, "user": svc.user_view(user)}

    @auth.post("/logout")
    async def logout(authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)):
        token = _token_from(authorization, x_api_key)
        if token and auth_on:
            svc.logout(token)
        return {"ok": True}

    @auth.get("/me")
    async def me(authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)):
        if not auth_on:
            return {"username": "anonymous", "role": "admin", "auth_mode": "none"}
        user = svc.resolve_token(_token_from(authorization, x_api_key))
        if user is None:
            raise AuthenticationError("Please log in.")
        return {**svc.user_view(user), "auth_mode": settings.auth_mode}

    @auth.post("/password")
    async def change_own_password(body: s.PasswordChange, authorization: Optional[str] = Header(None)):
        user = svc.resolve_token(_token_from(authorization, None))
        if user is None:
            raise AuthenticationError("Please log in.")
        svc.authenticate(user.username, body.current_password)
        with svc.context(actor=user.username, source="api"):
            svc.set_password(user.id, body.new_password)
        return {"ok": True}

    @auth.get("/oidc/login")
    async def oidc_login():
        if oidc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Single sign-on is not enabled")
        return RedirectResponse(oidc.login_url(), status_code=302)

    @auth.get("/oidc/callback")
    async def oidc_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None,
                            error_description: Optional[str] = None):
        if oidc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Single sign-on is not enabled")
        target = settings.oidc_frontend_url.rstrip("/") + "/login"
        try:
            if error:
                raise AuthenticationError(error_description or error)
            ident = oidc.identity(oidc.complete(code or "", state or ""))
            with svc.context(actor=ident["username"], source="api:oidc"):
                user = svc.upsert_external_user(ident["external_id"], ident["username"], ident["role"],
                                                ident["display_name"], ident["email"])
                token, _ = svc.issue_token(user.id, "session", hours=settings.session_hours)
        except AuthenticationError as e:
            return RedirectResponse(f"{target}#error={quote(str(e))}", status_code=302)
        return RedirectResponse(f"{target}#token={token}", status_code=302)

    # -- users and API tokens (admin) -------------------------------------------

    @api.get("/users", tags=["users"])
    async def list_users():
        return [svc.user_view(u) for u in svc.list_users()]

    @api.post("/users", status_code=201, tags=["users"])
    async def create_user(body: s.UserCreate):
        return svc.user_view(svc.create_user(body.username, body.role, body.password, body.display_name, body.email))

    @api.patch("/users/{user_id}", tags=["users"])
    async def update_user(user_id: str, body: s.UserUpdate):
        return svc.user_view(svc.update_user(user_id, **_changes(body)))

    @api.put("/users/{user_id}/password", tags=["users"])
    async def set_user_password(user_id: str, body: s.PasswordSet):
        svc.set_password(user_id, body.password)
        return {"ok": True}

    @api.delete("/users/{user_id}", status_code=204, tags=["users"])
    async def delete_user(user_id: str):
        svc.delete_user(user_id)

    @api.get("/users/{user_id}/tokens", tags=["users"])
    async def list_tokens(user_id: str):
        return [{k: v for k, v in t.to_dict().items() if k != "token_hash"} for t in svc.list_tokens(user_id)]

    @api.post("/users/{user_id}/tokens", status_code=201, tags=["users"])
    async def create_token(user_id: str, body: s.TokenCreate):
        """An API token for scripts. The token is shown only in this response."""
        token, record = svc.issue_token(user_id, "api", body.name, hours=body.days * 24 if body.days else None)
        return {"token": token, **{k: v for k, v in record.to_dict().items() if k != "token_hash"}}

    @api.delete("/tokens/{token_id}", status_code=204, tags=["users"])
    async def revoke_token(token_id: str):
        svc.revoke_token(token_id)

    @api.get("/meta", tags=["meta"])
    async def meta():
        """Allowed values and scoring thresholds, for building forms."""
        return {
            "organization_types": m.ORGANIZATION_TYPES,
            "supplier_tiers": m.SUPPLIER_TIERS,
            "service_criticalities": m.SERVICE_CRITICALITIES,
            "regulations": m.REGULATIONS,
            "incident_statuses": m.INCIDENT_STATUSES,
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

    # -- third parties --------------------------------------------------------

    @api.get("/third-parties/overview", tags=["third parties"])
    async def third_party_overview():
        """Concentration (who depends on which supplier and service) and review findings."""
        return svc.third_party_overview()

    @api.get("/third-parties/stress-test", tags=["third parties"])
    async def stress_test(hours: float, supplier_id: Optional[str] = None, service_id: Optional[str] = None):
        """What an outage of exactly `hours` costs per service and customer if a supplier or service goes down."""
        return svc.stress_test(hours, supplier_id=supplier_id, service_id=service_id)

    @api.get("/risks/{risk_id}/consequences", tags=["third parties"])
    async def risk_consequences(risk_id: str):
        """The services and customers one event of the risk costs money, per hour of outage."""
        return svc.consequence_terms(svc.get_risk(risk_id))

    @api.get("/suppliers", response_model=List[s.SupplierOut], tags=["third parties"])
    async def list_suppliers(tier: Optional[str] = None):
        return [x.to_dict() for x in svc.list_suppliers(tier)]

    @api.post("/suppliers", response_model=s.SupplierOut, status_code=201, tags=["third parties"])
    async def create_supplier(body: s.SupplierCreate):
        d = body.model_dump()
        return svc.create_supplier(d.pop("name"), **d).to_dict()

    @api.get("/suppliers/{supplier_id}", response_model=s.SupplierOut, tags=["third parties"])
    async def get_supplier(supplier_id: str):
        return svc.get_supplier(supplier_id).to_dict()

    @api.get("/suppliers/{supplier_id}/exposure", tags=["third parties"])
    async def supplier_exposure(supplier_id: str):
        """Services and customers hit if the supplier fails (also via suppliers that subcontract to it)."""
        return svc.supplier_exposure(supplier_id)

    @api.patch("/suppliers/{supplier_id}", response_model=s.SupplierOut, tags=["third parties"])
    async def update_supplier(supplier_id: str, body: s.SupplierUpdate):
        return svc.update_supplier(supplier_id, **_changes(body)).to_dict()

    @api.delete("/suppliers/{supplier_id}", status_code=204, tags=["third parties"])
    async def delete_supplier(supplier_id: str):
        svc.delete_supplier(supplier_id)

    @api.get("/incidents/overview", tags=["incidents"])
    async def incident_overview():
        """Open incidents and every reporting / notification deadline not met yet, soonest first."""
        return svc.incident_overview()

    @api.get("/incidents", response_model=List[s.IncidentOut], tags=["incidents"])
    async def list_incidents(open_only: bool = False):
        return [x.to_dict() for x in svc.list_incidents(open_only)]

    @api.post("/incidents", response_model=s.IncidentOut, status_code=201, tags=["incidents"])
    async def create_incident(body: s.IncidentCreate):
        d = body.model_dump()
        return svc.create_incident(d.pop("title"), d.pop("detected_at"), **d).to_dict()

    @api.get("/incidents/{incident_id}", response_model=s.IncidentOut, tags=["incidents"])
    async def get_incident(incident_id: str):
        return svc.get_incident(incident_id).to_dict()

    @api.get("/incidents/{incident_id}/deadlines", tags=["incidents"])
    async def incident_deadlines(incident_id: str):
        """What it affects and the deadlines it starts (NIS2 and each affected customer's contract)."""
        return svc.incident_deadlines(incident_id)

    @api.post("/incidents/{incident_id}/notifications", response_model=s.IncidentOut, tags=["incidents"])
    async def record_notification(incident_id: str, body: s.NotificationSent):
        return svc.record_notification(incident_id, body.key, body.sent_at).to_dict()

    @api.patch("/incidents/{incident_id}", response_model=s.IncidentOut, tags=["incidents"])
    async def update_incident(incident_id: str, body: s.IncidentUpdate):
        return svc.update_incident(incident_id, **_changes(body)).to_dict()

    @api.delete("/incidents/{incident_id}", status_code=204, tags=["incidents"])
    async def delete_incident(incident_id: str):
        svc.delete_incident(incident_id)

    @api.get("/correlations", response_model=List[s.CorrelationOut], tags=["portfolio"])
    async def list_correlations(risk_id: Optional[str] = None):
        return [x.to_dict() for x in svc.list_correlations(risk_id)]

    @api.post("/correlations", response_model=s.CorrelationOut, status_code=201, tags=["portfolio"])
    async def create_correlation(body: s.CorrelationCreate):
        d = body.model_dump()
        return svc.create_correlation(d.pop("risk_a_id"), d.pop("risk_b_id"), d.pop("coefficient"), **d).to_dict()

    @api.patch("/correlations/{correlation_id}", response_model=s.CorrelationOut, tags=["portfolio"])
    async def update_correlation(correlation_id: str, body: s.CorrelationUpdate):
        return svc.update_correlation(correlation_id, **_changes(body)).to_dict()

    @api.delete("/correlations/{correlation_id}", status_code=204, tags=["portfolio"])
    async def delete_correlation(correlation_id: str):
        svc.delete_correlation(correlation_id)

    @api.get("/appetites", response_model=List[s.AppetiteOut], tags=["portfolio"])
    async def list_appetites():
        return [x.to_dict() for x in svc.list_appetites()]

    @api.post("/appetites", response_model=s.AppetiteOut, status_code=201, tags=["portfolio"])
    async def create_appetite(body: s.AppetiteCreate):
        d = body.model_dump()
        return svc.create_appetite(d.pop("name"), d.pop("points"), **d).to_dict()

    @api.get("/appetites/{appetite_id}", response_model=s.AppetiteOut, tags=["portfolio"])
    async def get_appetite(appetite_id: str):
        return svc.get_appetite(appetite_id).to_dict()

    @api.patch("/appetites/{appetite_id}", response_model=s.AppetiteOut, tags=["portfolio"])
    async def update_appetite(appetite_id: str, body: s.AppetiteUpdate):
        return svc.update_appetite(appetite_id, **_changes(body)).to_dict()

    @api.delete("/appetites/{appetite_id}", status_code=204, tags=["portfolio"])
    async def delete_appetite(appetite_id: str):
        svc.delete_appetite(appetite_id)

    @api.get("/services", response_model=List[s.ServiceOut], tags=["third parties"])
    async def list_services(business_unit_id: Optional[str] = None):
        return [x.to_dict() for x in svc.list_services(business_unit_id)]

    @api.post("/services", response_model=s.ServiceOut, status_code=201, tags=["third parties"])
    async def create_service(body: s.ServiceCreate):
        d = body.model_dump()
        return svc.create_service(d.pop("business_unit_id"), d.pop("name"), **d).to_dict()

    @api.get("/services/{service_id}", response_model=s.ServiceOut, tags=["third parties"])
    async def get_service(service_id: str):
        return svc.get_service(service_id).to_dict()

    @api.get("/services/{service_id}/exposure", tags=["third parties"])
    async def service_exposure(service_id: str):
        return svc.service_exposure(service_id)

    @api.patch("/services/{service_id}", response_model=s.ServiceOut, tags=["third parties"])
    async def update_service(service_id: str, body: s.ServiceUpdate):
        return svc.update_service(service_id, **_changes(body)).to_dict()

    @api.delete("/services/{service_id}", status_code=204, tags=["third parties"])
    async def delete_service(service_id: str):
        svc.delete_service(service_id)

    @api.get("/customers", response_model=List[s.CustomerOut], tags=["third parties"])
    async def list_customers(regulation: Optional[str] = None):
        return [x.to_dict() for x in svc.list_customers(regulation)]

    @api.post("/customers", response_model=s.CustomerOut, status_code=201, tags=["third parties"])
    async def create_customer(body: s.CustomerCreate):
        d = body.model_dump()
        return svc.create_customer(d.pop("name"), **d).to_dict()

    @api.get("/customers/{customer_id}", response_model=s.CustomerOut, tags=["third parties"])
    async def get_customer(customer_id: str):
        return svc.get_customer(customer_id).to_dict()

    @api.get("/customers/{customer_id}/dora", tags=["third parties"])
    async def customer_dora(customer_id: str):
        """What the customer's DORA register of information needs from us (providers, supply chain, gaps)."""
        return svc.dora_extract(customer_id)

    @api.get("/customers/{customer_id}/dora.xlsx", tags=["third parties"])
    async def customer_dora_xlsx(customer_id: str):
        name = svc.get_customer(customer_id).name
        safe = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-") or "customer"
        return Response(svc.export_dora_extract(customer_id),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="dora-{safe}.xlsx"'})

    @api.get("/customers/{customer_id}/profile", tags=["third parties"])
    async def customer_profile(customer_id: str):
        """What the customer depends on: services, suppliers incl. fourth parties, risks, controls, findings."""
        return svc.customer_profile(customer_id)

    @api.patch("/customers/{customer_id}", response_model=s.CustomerOut, tags=["third parties"])
    async def update_customer(customer_id: str, body: s.CustomerUpdate):
        return svc.update_customer(customer_id, **_changes(body)).to_dict()

    @api.delete("/customers/{customer_id}", status_code=204, tags=["third parties"])
    async def delete_customer(customer_id: str):
        svc.delete_customer(customer_id)

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

    # -- periods and reports ------------------------------------------------

    @api.get("/periods", tags=["reports"])
    async def list_periods():
        return [p.to_dict() for p in svc.list_periods()]

    @api.post("/periods/{key}/close", tags=["reports"])
    async def close_period(key: str, body: s.PeriodClose):
        """Freezes the period's figures (admin). Anchors the audit chain if configured."""
        return svc.close_period(key, body.note).to_dict()

    @api.post("/periods/{key}/reopen", tags=["reports"])
    async def reopen_period(key: str):
        return svc.reopen_period(key).to_dict()

    @api.get("/periods/{key}/facts", tags=["reports"])
    async def period_facts(key: str, level: Optional[str] = None):
        return [f.to_dict() for f in svc.period_facts(key, level)]

    @api.get("/reports/facts.csv", tags=["reports"])
    async def facts_csv():
        """All frozen period figures as CSV (for Excel / Power BI)."""
        from ..services.reporting import facts_to_csv
        return Response(facts_to_csv(svc.period_facts()), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="riskmesh-period-facts.csv"'})

    @api.get("/reports/trend", tags=["reports"])
    async def trend(metric: str = "eal_residual", level: str = "total", entity_id: Optional[str] = None,
                    period_type: Optional[str] = None):
        return svc.trend(metric, level, entity_id, period_type)

    @api.get("/reports/compare", tags=["reports"])
    async def compare_periods(a: str, b: str):
        return svc.compare_periods(a, b)

    # -- audit anchors ----------------------------------------------------------

    @api.get("/audit-log/anchors", tags=["audit"])
    async def list_anchors():
        return [a.to_dict() for a in svc.list_anchors()]

    @api.post("/audit-log/anchor", status_code=201, tags=["audit"])
    async def create_anchor(body: s.AnchorCreate):
        """Signs and/or time-stamps the current chain head (admin)."""
        return svc.create_anchor(body.reason).to_dict()

    # -- import / export ----------------------------------------------------

    @api.get("/export", tags=["data"])
    async def export_data():
        return svc.export_data()

    @api.get("/export.xlsx", tags=["data"])
    async def export_excel():
        """All data as an Excel workbook (also the template: edit it and upload it again)."""
        from datetime import date as _date
        return Response(svc.export_excel(),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition":
                                 f'attachment; filename="riskmesh-{_date.today().isoformat()}.xlsx"'})

    @api.post("/import.xlsx", tags=["data"])
    async def import_excel(request: Request, replace: bool = False, dry_run: bool = False):
        """
        Upload a workbook (the request body is the .xlsx file). dry_run=true only checks it and
        returns what would change. replace=true deletes stored rows that are missing from a sheet.
        """
        content = await request.body()
        if not content:
            raise ValidationError("Send the .xlsx file as the request body.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValidationError(f"The file is larger than {MAX_UPLOAD_BYTES // 1_000_000} MB.")
        import hashlib
        svc.source = f"excel:sha256:{hashlib.sha256(content).hexdigest()}"
        return svc.import_excel(content, replace=replace, dry_run=dry_run)

    @api.post("/import", tags=["data"])
    async def import_data(data: Dict[str, Any], replace: bool = True):
        # the audit log records exactly which content was imported
        svc.source = f"import:sha256:{audit.sha256(data)}"
        return {"imported": svc.import_data(data, replace=replace)}

    app.include_router(auth)
    app.include_router(api)
    return app

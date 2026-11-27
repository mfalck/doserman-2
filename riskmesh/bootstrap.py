"""One place that turns Settings into a ready RiskService (used by the CLI, API and GUI)."""

import getpass
from typing import Optional

from .config import Settings
from .services import RiskService
from .services.risk_service import ScoringScale
from .storage import create_repository


def default_user(settings: Settings) -> str:
    try:
        return settings.user_name or getpass.getuser()
    except Exception:  # no login name available (e.g. some services)
        return settings.user_name or "unknown"


def make_service(settings: Settings, actor: Optional[str] = None, source: str = "system") -> RiskService:
    repo = create_repository(settings)
    scoring = ScoringScale(likelihood_bands=tuple(settings.likelihood_bands),
                           impact_bands=tuple(settings.impact_bands))
    svc = RiskService(repo, scoring=scoring, actor=actor or default_user(settings), source=source)
    svc.anchor_key_path = settings.audit_signing_key
    svc.tsa_url = settings.audit_tsa_url
    svc.anchor_on_period_close = settings.anchor_on_period_close
    return svc

"""One place that turns Settings into a ready RiskService (used by the CLI, API and GUI)."""

import getpass
from typing import Any, Dict, List, Optional

from .config import Settings
from .domain.errors import NotFoundError
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
    svc.significant_loss = settings.significant_loss
    svc.significant_outage_hours = settings.significant_outage_hours
    return svc


class Workspaces:
    """
    The services of all configured workspaces, opened when first used. Each
    workspace has its own repository, so data, users, tokens and audit log
    never mix: there is no shared database with a workspace column.
    """

    def __init__(self, settings: Settings, actor: Optional[str] = None, source: str = "system",
                 first: Optional[RiskService] = None):
        self.settings = settings
        self.actor = actor
        self.source = source
        self._services: Dict[str, RiskService] = {}
        if first is not None:
            self._services[settings.workspace] = first

    @property
    def default(self) -> str:
        return self.settings.workspace

    def ids(self) -> List[str]:
        return self.settings.workspace_ids()

    def __contains__(self, workspace_id: object) -> bool:
        return workspace_id in self.ids()

    def info(self) -> List[Dict[str, Any]]:
        return self.settings.workspace_info()

    def settings_for(self, workspace_id: str) -> Settings:
        return self.settings.for_workspace(workspace_id)

    def service(self, workspace_id: Optional[str] = None) -> RiskService:
        wid = workspace_id or self.default
        svc = self._services.get(wid)
        if svc is None:
            if wid not in self:
                raise NotFoundError(f"Unknown workspace {wid!r}")
            svc = self._services[wid] = make_service(self.settings_for(wid), actor=self.actor, source=self.source)
        return svc

    def close(self) -> None:
        for svc in self._services.values():
            svc.repo.close()
        self._services.clear()

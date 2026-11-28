"""
Incident register: what happened, what it hits, and the clock it starts.

From the moment we become aware of an incident (`detected_at`) deadlines run:

- NIS2 (Article 23), when the incident is significant: an early warning to the
  CSIRT / competent authority within 24 hours, an incident notification within
  72 hours, and a final report within one month of the notification.
- Every affected customer's contractual notification deadline
  (`Customer.notification_hours`). Affected means: using a service the
  incident names, or a service that depends on a supplier it names (also
  through subcontractors).

Customers under DORA get a note: as financial entities they must classify the
incident themselves and, if it is major, send their initial report within 4
hours of classifying it and no later than 24 hours after becoming aware, so
they need our information early.

Deadlines are derived, never stored; `Incident.notifications` records when
each was met. This is a working aid, not legal advice: check the national
implementation of NIS2 and the customers' contracts.
"""

import calendar
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..domain.errors import ValidationError
from ..domain.models import INCIDENT_STATUSES, Customer, Incident
from .core import ServiceCore

DUE_SOON_HOURS = 12
NIS2_STAGES = (("nis2:early_warning", "NIS2 early warning", 24),
               ("nis2:notification", "NIS2 incident notification", 72))
NIS2_FINAL = ("nis2:final", "NIS2 final report")
DORA_NOTE = ("DORA customer: must classify the incident and, if major, report within 4 h of classifying it "
             "(at the latest 24 h after becoming aware) - give them what they need early")


def parse_time(value: Any, field_name: str) -> datetime:
    """'YYYY-MM-DDTHH:MM' (seconds and a space instead of T are fine; a time zone is dropped)."""
    try:
        t = datetime.fromisoformat(str(value).strip().replace(" ", "T"))
    except ValueError:
        raise ValidationError(f"{field_name} must be a date and time like 2026-11-27T14:30, got {value!r}.") from None
    return t.replace(tzinfo=None, second=0, microsecond=0)


def fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M")


def add_month(t: datetime) -> datetime:
    """Same day next month (the last day when that month is shorter)."""
    year, month = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
    return t.replace(year=year, month=month, day=min(t.day, calendar.monthrange(year, month)[1]))


class Incidents(ServiceCore):
    """Mixin for RiskService (uses _create/_update/_delete/_require/repo and ThirdParty._graph)."""

    if TYPE_CHECKING:  # members of other service areas this one uses
        def _customer_brief(self, c: Customer) -> Dict[str, Any]: ...
        def _graph(self) -> Dict[str, Any]: ...
        def _service_ids_hit_by_supplier(self, supplier_id: str, g: Dict[str, Any]) -> Dict[str, int]: ...


    # -- validation (called from RiskService._validate) --------------------------------

    def _validate_incident(self, e) -> bool:
        if not isinstance(e, Incident):
            return False
        if not e.title or not str(e.title).strip():
            raise ValidationError("Incident title cannot be empty.")
        e.title = str(e.title).strip()
        self._enum(e.status, INCIDENT_STATUSES, "status")
        detected = parse_time(e.detected_at, "detected_at")
        e.detected_at = fmt(detected)
        if e.resolved_at:
            resolved = parse_time(e.resolved_at, "resolved_at")
            if resolved < detected:
                raise ValidationError("resolved_at cannot be before detected_at.")
            e.resolved_at = fmt(resolved)
        self._non_negative(e.financial_impact, "financial_impact")
        e.service_ids = list(dict.fromkeys(e.service_ids or []))
        e.supplier_ids = list(dict.fromkeys(e.supplier_ids or []))
        notes = {}
        for key, sent in (e.notifications or {}).items():
            if sent:
                notes[str(key)] = fmt(parse_time(sent, f"notification {key}"))
        e.notifications = notes
        return True

    # -- CRUD ---------------------------------------------------------------------------------

    def create_incident(self, title: str, detected_at: str, **optional: Any) -> Incident:
        return self._create(Incident, title=title, detected_at=detected_at, **optional)

    def get_incident(self, incident_id: str) -> Incident:
        return self._require(Incident, incident_id, "Incident")

    def list_incidents(self, open_only: bool = False) -> List[Incident]:
        out = self.repo.list(Incident)
        if open_only:
            out = [i for i in out if i.status != "CLOSED"]
        return sorted(out, key=lambda i: i.detected_at, reverse=True)

    def update_incident(self, incident_id: str, **changes: Any) -> Incident:
        return self._update(Incident, incident_id, changes)

    def delete_incident(self, incident_id: str) -> None:
        self._delete(Incident, incident_id)

    def record_notification(self, incident_id: str, key: str, sent_at: Optional[str] = None) -> Incident:
        """Marks a deadline as met (sent_at defaults to now); sent_at='' undoes it."""
        incident = self.get_incident(incident_id)
        valid = {d["key"] for d in self.incident_deadlines(incident_id)["deadlines"]}
        if key not in valid:
            raise ValidationError(f"Unknown notification '{key}' for this incident.")
        notes = dict(incident.notifications)
        if sent_at == "":
            notes.pop(key, None)
        else:
            notes[key] = sent_at or fmt(self._now())
        return self.update_incident(incident_id, notifications=notes)

    @staticmethod
    def _now() -> datetime:
        return datetime.now().replace(second=0, microsecond=0)

    # -- the clock ----------------------------------------------------------------------------

    def _affected(self, incident: Incident, g: Dict[str, Any]) -> Dict[str, Any]:
        """Services hit (named, or behind a named supplier) and the customers that use them."""
        services: Dict[str, str] = {sid: "named" for sid in incident.service_ids if sid in g["services"]}
        for sup in incident.supplier_ids:
            for sid in self._service_ids_hit_by_supplier(sup, g):
                services.setdefault(sid, f"via {g['suppliers'][sup].name}" if sup in g["suppliers"] else "via supplier")
        customers: Dict[str, Customer] = {}
        for sid in services:
            for cid in g["customers_of"].get(sid, ()):
                customers[cid] = g["customers"][cid]
        return {"services": services, "customers": customers}

    @staticmethod
    def _deadline(key: str, label: str, due: Optional[datetime], sent: Optional[str], now: datetime,
                  **extra: Any) -> Dict[str, Any]:
        if sent:
            status = "late" if due and parse_time(sent, key) > due else "sent"
        elif due is None:
            status = "no_deadline"
        elif now > due:
            status = "overdue"
        elif due - now <= timedelta(hours=DUE_SOON_HOURS):
            status = "due_soon"
        else:
            status = "pending"
        hours_left = round((due - now).total_seconds() / 3600, 1) if due and not sent else None
        return {"key": key, "label": label, "due": fmt(due) if due else None, "sent_at": sent or None,
                "status": status, "hours_left": hours_left, **extra}

    def incident_deadlines(self, incident_id: str, now: Optional[datetime] = None,
                           g: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        The incident with what it affects and every deadline it starts:
        status sent | late | overdue | due_soon | pending | no_deadline | not_required.
        """
        incident = self.get_incident(incident_id)
        now = now or self._now()
        g = g or self._graph()
        detected = parse_time(incident.detected_at, "detected_at")
        sent = incident.notifications
        affected = self._affected(incident, g)
        deadlines: List[Dict[str, Any]] = []

        if incident.nis2_significant:
            for key, label, hours in NIS2_STAGES:
                deadlines.append(self._deadline(key, label, detected + timedelta(hours=hours), sent.get(key), now,
                                                to="authority"))
            notified = sent.get("nis2:notification")
            base = parse_time(notified, "notification") if notified else detected + timedelta(hours=72)
            deadlines.append(self._deadline(NIS2_FINAL[0], NIS2_FINAL[1], add_month(base), sent.get(NIS2_FINAL[0]),
                                            now, to="authority"))
        for c in sorted(affected["customers"].values(), key=lambda c: c.name):
            key = f"customer:{c.id}"
            due = detected + timedelta(hours=c.notification_hours) if c.notification_hours is not None else None
            deadlines.append(self._deadline(
                key, f"Notify {c.name}", due, sent.get(key), now, to="customer", customer_id=c.id,
                regulations=c.regulations, critical_function=c.critical_function,
                note=DORA_NOTE if "DORA" in c.regulations else
                (None if due else "No deadline in the contract: notify without undue delay")))
        if incident.status == "CLOSED":  # a closed incident has no running clock, only a record
            for d in deadlines:
                if d["status"] in ("overdue", "due_soon", "pending"):
                    d["status"], d["hours_left"] = "not_sent", None
        order = {"overdue": 0, "due_soon": 1, "pending": 2, "no_deadline": 3, "not_sent": 4, "late": 5, "sent": 6}
        deadlines.sort(key=lambda d: (order[d["status"]], d["due"] or "9999"))
        return {
            "incident": incident.to_dict(),
            "services": [{"id": sid, "name": g["services"][sid].name, "how": how}
                         for sid, how in affected["services"].items()],
            "customers": [self._customer_brief(c) for c in affected["customers"].values()],
            "deadlines": deadlines,
            "nis2_assessed": incident.nis2_significant is not None,
            "now": fmt(now),
        }

    def incident_overview(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Open incidents with their next deadline, and every deadline not met yet, soonest first."""
        now = now or self._now()
        g = self._graph()
        rows, upcoming = [], []
        for inc in self.list_incidents(open_only=True):
            d = self.incident_deadlines(inc.id, now, g)
            todo = [x for x in d["deadlines"] if x["status"] in ("overdue", "due_soon", "pending")]
            for x in todo:
                upcoming.append({**x, "incident_id": inc.id, "incident": inc.title})
            rows.append({"id": inc.id, "title": inc.title, "status": inc.status, "detected_at": inc.detected_at,
                         "nis2_significant": inc.nis2_significant, "customer_count": len(d["customers"]),
                         "overdue": sum(1 for x in todo if x["status"] == "overdue"),
                         "next_due": min((x["due"] for x in todo), default=None)})
        upcoming.sort(key=lambda x: x["due"])
        return {"open": rows, "deadlines": upcoming,
                "overdue": sum(1 for x in upcoming if x["status"] == "overdue"),
                "unassessed": sum(1 for r in rows if r["nis2_significant"] is None), "now": fmt(now)}


INCIDENT_WRITE_METHODS = ("create_incident", "update_incident", "delete_incident", "record_notification")

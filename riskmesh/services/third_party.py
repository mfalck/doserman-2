"""
Third-party risk: our suppliers, the services we deliver and the customers
that use them, and how they depend on each other.

    supplier (and its subcontractors) -> service -> customer

The questions this answers are "who is hit if supplier X or service Y fails",
"what does customer Z depend on" (incl. fourth parties, as DORA customers ask)
and "where is the dependency concentrated". Findings flag what a NIS2/DORA
review would ask about: overdue reviews, contracts running out, critical
suppliers without an exit plan, and so on.
"""

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Set

from ..domain import consequences
from ..domain.errors import ValidationError
from ..domain.models import (
    REGULATIONS,
    SERVICE_CRITICALITIES,
    SUPPLIER_TIERS,
    Control,
    Customer,
    Risk,
    Service,
    Supplier,
)
from .core import ServiceCore

CONTRACT_WARNING_DAYS = 90


class ThirdParty(ServiceCore):
    """Mixin for RiskService (uses _create/_update/_delete/_require/repo)."""

    # -- validation (called from RiskService._validate) --------------------------------

    def _validate_third_party(self, e) -> None:
        if isinstance(e, Supplier):
            self._name(e, "Supplier")
            self._enum(e.tier, SUPPLIER_TIERS, "tier")
            for f in ("contract_start", "contract_end", "next_review"):
                if getattr(e, f):
                    self._date(getattr(e, f), f)
            e.subcontractor_ids = list(dict.fromkeys(e.subcontractor_ids or []))
            if e.id in e.subcontractor_ids:
                raise ValidationError("A supplier cannot be its own subcontractor.")
            e.data_locations = [str(x).strip() for x in e.data_locations or [] if str(x).strip()]
            e.certifications = [str(x).strip() for x in e.certifications or [] if str(x).strip()]
            self._lei(e)
        elif isinstance(e, Service):
            self._name(e, "Service")
            self._enum(e.criticality, SERVICE_CRITICALITIES, "criticality")
            self._non_negative(e.rto_hours, "rto_hours")
            self._non_negative(e.rpo_hours, "rpo_hours")
            self._non_negative(e.outage_cost_per_hour, "outage_cost_per_hour")
            e.asset_ids = list(dict.fromkeys(e.asset_ids or []))
            e.supplier_ids = list(dict.fromkeys(e.supplier_ids or []))
            e.data_locations = [str(x).strip() for x in e.data_locations or [] if str(x).strip()]
        elif isinstance(e, Customer):
            self._name(e, "Customer")
            e.regulations = list(dict.fromkeys(e.regulations or []))
            for r in e.regulations:
                self._enum(r, REGULATIONS, "regulations")
            for f in ("contract_start", "contract_end"):
                if getattr(e, f):
                    self._date(getattr(e, f), f)
            self._non_negative(e.contract_value, "contract_value")
            self._non_negative(e.notification_hours, "notification_hours")
            for f in ("penalty_per_hour", "penalty_grace_hours", "penalty_cap"):
                self._non_negative(getattr(e, f), f)
            e.service_ids = list(dict.fromkeys(e.service_ids or []))

    # -- suppliers --------------------------------------------------------------------------

    def create_supplier(self, name: str, **optional: Any) -> Supplier:
        return self._create(Supplier, name=name, **optional)

    def get_supplier(self, supplier_id: str) -> Supplier:
        return self._require(Supplier, supplier_id, "Supplier")

    def list_suppliers(self, tier: Optional[str] = None) -> List[Supplier]:
        return sorted(self.repo.list(Supplier, **({"tier": tier} if tier else {})), key=lambda s: s.name)

    def update_supplier(self, supplier_id: str, **changes: Any) -> Supplier:
        return self._update(Supplier, supplier_id, changes)

    def delete_supplier(self, supplier_id: str) -> None:
        """Removes it from services, risks and other suppliers' subcontractor lists; its observations go with it."""
        self.get_supplier(supplier_id)
        self._delete_observations_at("supplier_id", supplier_id)
        self._delete(Supplier, supplier_id)

    # -- services ---------------------------------------------------------------------------

    def create_service(self, unit_id: str, name: str, **optional: Any) -> Service:
        return self._create(Service, name=name, business_unit_id=unit_id, **optional)

    def get_service(self, service_id: str) -> Service:
        return self._require(Service, service_id, "Service")

    def list_services(self, unit_id: Optional[str] = None) -> List[Service]:
        filters = {"business_unit_id": unit_id} if unit_id else {}
        return sorted(self.repo.list(Service, **filters), key=lambda s: s.name)

    def update_service(self, service_id: str, **changes: Any) -> Service:
        return self._update(Service, service_id, changes)

    def delete_service(self, service_id: str) -> None:
        self._delete(Service, service_id)

    # -- customers --------------------------------------------------------------------------

    def create_customer(self, name: str, **optional: Any) -> Customer:
        return self._create(Customer, name=name, **optional)

    def get_customer(self, customer_id: str) -> Customer:
        return self._require(Customer, customer_id, "Customer")

    def list_customers(self, regulation: Optional[str] = None) -> List[Customer]:
        customers = self.repo.list(Customer)
        if regulation:
            customers = [c for c in customers if regulation in c.regulations]
        return sorted(customers, key=lambda c: c.name)

    def update_customer(self, customer_id: str, **changes: Any) -> Customer:
        return self._update(Customer, customer_id, changes)

    def delete_customer(self, customer_id: str) -> None:
        self._delete(Customer, customer_id)

    # -- the dependency graph ----------------------------------------------------------------

    def _graph(self) -> Dict[str, Any]:
        suppliers = {s.id: s for s in self.repo.list(Supplier)}
        services = {s.id: s for s in self.repo.list(Service)}
        customers = {c.id: c for c in self.repo.list(Customer)}
        risks = self.repo.list(Risk)
        customers_of: Dict[str, List[str]] = defaultdict(list)
        for c in customers.values():
            for sid in c.service_ids:
                customers_of[sid].append(c.id)
        direct_services: Dict[str, Set[str]] = defaultdict(set)   # supplier -> services that name it
        for svc in services.values():
            for sup in svc.supplier_ids:
                direct_services[sup].add(svc.id)
        contractors_of: Dict[str, Set[str]] = defaultdict(set)    # subcontractor -> suppliers that use it
        for s in suppliers.values():
            for sub in s.subcontractor_ids:
                contractors_of[sub].add(s.id)
        return {"suppliers": suppliers, "services": services, "customers": customers, "risks": risks,
                "customers_of": customers_of, "direct_services": direct_services, "contractors_of": contractors_of,
                "driver_links": self._driver_links()}

    @staticmethod
    def _chain(supplier_ids, suppliers: Dict[str, Supplier]) -> Dict[str, int]:
        """The suppliers and all their subcontractors, with depth (1 = direct, 2 = fourth party, ...)."""
        depth: Dict[str, int] = {}
        frontier = [(s, 1) for s in supplier_ids]
        while frontier:
            sid, d = frontier.pop(0)
            if sid in depth or sid not in suppliers:
                continue
            depth[sid] = d
            frontier.extend((sub, d + 1) for sub in suppliers[sid].subcontractor_ids)
        return depth

    @staticmethod
    def _upstream(supplier_id: str, g: Dict[str, Any]) -> Dict[str, int]:
        """supplier_id and every supplier that (directly or further up) subcontracts to it, with distance."""
        dist, frontier = {}, [(supplier_id, 0)]
        while frontier:
            sid, d = frontier.pop(0)
            if sid in dist:
                continue
            dist[sid] = d
            frontier.extend((up, d + 1) for up in g["contractors_of"].get(sid, ()))
        return dist

    def _service_ids_hit_by_supplier(self, supplier_id: str, g: Dict[str, Any]) -> Dict[str, int]:
        """Services that fail if the supplier fails, with how far down the chain it sits (1 = direct)."""
        hit: Dict[str, int] = {}
        for sid, d in self._upstream(supplier_id, g).items():
            for service_id in g["direct_services"].get(sid, ()):
                hit[service_id] = min(hit.get(service_id, 99), d + 1)
        return hit

    def _customer_brief(self, c: Customer) -> Dict[str, Any]:
        return {"id": c.id, "name": c.name, "sector": c.sector, "regulations": c.regulations,
                "critical_function": c.critical_function, "contract_value": c.contract_value,
                "notification_hours": c.notification_hours}

    def _impact_summary(self, service_ids, g: Dict[str, Any]) -> Dict[str, Any]:
        customers = {cid for sid in service_ids for cid in g["customers_of"].get(sid, ())}
        cs = [g["customers"][c] for c in customers]
        by_reg: Dict[str, int] = defaultdict(int)
        for c in cs:
            for r in c.regulations:
                by_reg[r] += 1
        deadlines = [c.notification_hours for c in cs if c.notification_hours is not None]
        return {
            "customers": sorted((self._customer_brief(c) for c in cs), key=lambda c: c["name"]),
            "customer_count": len(cs),
            "contract_value": sum(c.contract_value or 0 for c in cs),
            "regulated_customers": sum(1 for c in cs if c.regulations),
            "critical_function_customers": sum(1 for c in cs if c.critical_function),
            "by_regulation": dict(sorted(by_reg.items())),
            "shortest_notification_hours": min(deadlines) if deadlines else None,
        }

    def _risk_briefs(self, risks: List[Risk], g: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        out = []
        for r in sorted(risks, key=lambda r: r.name):
            sc = self.risk_scores(r, g)
            out.append({"id": r.id, "name": r.name, "category": r.category,
                        "expected_annual_loss": sc["residual"]["expected_annual_loss"],
                        "level": sc["residual"]["level"]})
        return out

    def supplier_exposure(self, supplier_id: str) -> Dict[str, Any]:
        """Everything that is hit if this supplier fails, also through the suppliers that subcontract to it."""
        supplier = self.get_supplier(supplier_id)
        g = self._graph()
        hit = self._service_ids_hit_by_supplier(supplier_id, g)
        upstream = self._upstream(supplier_id, g)
        risks = [r for r in g["risks"] if supplier_id in r.supplier_ids]
        return {
            "supplier": supplier.to_dict(),
            "via_suppliers": sorted(({"id": s, "name": g["suppliers"][s].name, "distance": d}
                                     for s, d in upstream.items() if d > 0), key=lambda x: (x["distance"], x["name"])),
            "services": sorted(({"id": s, "name": g["services"][s].name, "criticality": g["services"][s].criticality,
                                 "depth": d} for s, d in hit.items()), key=lambda x: x["name"]),
            "subcontractors": self._chain_list(supplier.subcontractor_ids, g),
            "risks": self._risk_briefs(risks, g),             # risks that originate at this supplier
            **self._impact_summary(hit, g),
        }

    def service_exposure(self, service_id: str) -> Dict[str, Any]:
        """The customers of a service and everything the service depends on."""
        service = self.get_service(service_id)
        g = self._graph()
        risks = [r for r in g["risks"] if service_id in r.service_ids
                 or set(r.supplier_ids) & set(self._chain(service.supplier_ids, g["suppliers"]))]
        return {
            "service": service.to_dict(),
            "suppliers": self._chain_list(service.supplier_ids, g),
            "risks": self._risk_briefs(risks, g),
            **self._impact_summary([service_id], g),
        }

    def _chain_list(self, supplier_ids, g) -> List[Dict[str, Any]]:
        chain = self._chain(supplier_ids, g["suppliers"])
        return sorted(({"id": s, "name": g["suppliers"][s].name, "tier": g["suppliers"][s].tier, "depth": d,
                        "country": g["suppliers"][s].country, "data_locations": g["suppliers"][s].data_locations,
                        "certifications": g["suppliers"][s].certifications}
                       for s, d in chain.items()), key=lambda x: (x["depth"], x["name"]))

    def customer_profile(self, customer_id: str) -> Dict[str, Any]:
        """
        What a customer depends on: our services, the suppliers behind them
        (incl. fourth parties), the risks that can disrupt them and the
        controls on those risks. The basis of a customer assurance report.
        """
        customer = self.get_customer(customer_id)
        g = self._graph()
        services = [g["services"][s] for s in customer.service_ids if s in g["services"]]
        supplier_ids = [sup for svc in services for sup in svc.supplier_ids]
        chain = self._chain(supplier_ids, g["suppliers"])
        service_set = {s.id for s in services}
        risks = [r for r in g["risks"] if set(r.service_ids) & service_set or set(r.supplier_ids) & set(chain)]
        risk_ids = {r.id for r in risks}
        controls = [c for c in self.repo.list(Control) if set(c.risk_ids) & risk_ids]
        return {
            "customer": customer.to_dict(),
            "services": [{"id": s.id, "name": s.name, "criticality": s.criticality, "rto_hours": s.rto_hours,
                          "rpo_hours": s.rpo_hours, "supplier_ids": s.supplier_ids} for s in services],
            "suppliers": self._chain_list(supplier_ids, g),
            "risks": self._risk_briefs(risks, g),
            "controls": sorted(({"id": c.id, "name": c.name, "control_type": c.control_type,
                                 "implementation_status": c.implementation_status,
                                 "effectiveness_rating": c.effectiveness_rating,
                                 "risk_ids": [r for r in c.risk_ids if r in risk_ids]} for c in controls),
                               key=lambda c: c["name"]),
            "findings": [f for f in self.third_party_findings() if f["entity_id"] in chain
                         or f["entity_id"] in service_set or f["entity_id"] == customer.id],
        }

    def third_party_overview(self) -> Dict[str, Any]:
        """Concentration: suppliers and services ranked by how much depends on them."""
        g = self._graph()
        suppliers = []
        for s in g["suppliers"].values():
            hit = self._service_ids_hit_by_supplier(s.id, g)
            summary = self._impact_summary(hit, g)
            suppliers.append({
                "id": s.id, "name": s.name, "tier": s.tier, "country": s.country,
                "service_count": len(hit), "direct": any(d == 1 for d in hit.values()),
                "subcontractor_count": len(self._chain(s.subcontractor_ids, g["suppliers"])),
                **{k: summary[k] for k in ("customer_count", "contract_value", "regulated_customers",
                                           "critical_function_customers", "by_regulation")},
            })
        services = []
        for svc in g["services"].values():
            summary = self._impact_summary([svc.id], g)
            services.append({
                "id": svc.id, "name": svc.name, "criticality": svc.criticality,
                "supplier_count": len(self._chain(svc.supplier_ids, g["suppliers"])),
                **{k: summary[k] for k in ("customer_count", "contract_value", "regulated_customers",
                                           "critical_function_customers", "by_regulation",
                                           "shortest_notification_hours")},
            })
        key = lambda x: (-x["contract_value"], -x["customer_count"], x["name"])
        customers = list(g["customers"].values())
        return {
            "suppliers": sorted(suppliers, key=key),
            "services": sorted(services, key=key),
            "totals": {
                "suppliers": len(suppliers), "services": len(services), "customers": len(customers),
                "contract_value": sum(c.contract_value or 0 for c in customers),
                "regulated_customers": sum(1 for c in customers if c.regulations),
                "critical_function_customers": sum(1 for c in customers if c.critical_function),
            },
            "findings": self.third_party_findings(g),
        }

    def third_party_findings(self, g: Optional[Dict[str, Any]] = None,
                             today: Optional[date] = None) -> List[Dict[str, Any]]:
        """What a NIS2/DORA review would ask about. severity: high | medium | low."""
        g = g or self._graph()
        today = today or date.today()
        soon = today + timedelta(days=CONTRACT_WARNING_DAYS)
        out: List[Dict[str, Any]] = []

        def add(severity, entity, kind, text):
            out.append({"severity": severity, "entity_type": kind, "entity_id": entity.id,
                        "name": entity.name, "finding": text})

        def d(v):
            return date.fromisoformat(v[:10]) if v else None

        for s in g["suppliers"].values():
            important = s.tier in ("CRITICAL", "IMPORTANT")
            if d(s.next_review) and d(s.next_review) < today:
                add("high" if important else "medium", s, "supplier", f"Review overdue since {s.next_review}")
            elif important and not s.next_review:
                add("medium", s, "supplier", "No review date set")
            if d(s.contract_end) and d(s.contract_end) < today:
                add("high", s, "supplier", f"Contract ended {s.contract_end}")
            elif d(s.contract_end) and d(s.contract_end) <= soon:
                add("medium", s, "supplier", f"Contract ends {s.contract_end}")
            if s.tier == "CRITICAL" and not s.exit_plan:
                add("high", s, "supplier", "Critical supplier without an exit plan")
            if s.processes_personal_data and not s.data_locations:
                add("medium", s, "supplier", "Processes personal data but the data location is unknown")
            if important and not s.certifications:
                add("low", s, "supplier", "No certification or assurance report registered")
            if not self._service_ids_hit_by_supplier(s.id, g):
                add("low", s, "supplier", "Not linked to any service")
        for svc in g["services"].values():
            if svc.criticality in ("CRITICAL", "HIGH") and svc.rto_hours is None:
                add("medium", svc, "service", "Critical service without a recovery time objective (RTO)")
            if not g["customers_of"].get(svc.id):
                add("low", svc, "service", "No customers linked")
            critical_customers = [g["customers"][c] for c in g["customers_of"].get(svc.id, ())
                                  if g["customers"][c].critical_function]
            if critical_customers and svc.criticality not in ("CRITICAL", "HIGH"):
                add("medium", svc, "service", f"Supports a critical function for {len(critical_customers)} "
                                              "customer(s) but is not rated critical or high")
        for c in g["customers"].values():
            if "DORA" in c.regulations:
                missing = [label for flag, label in ((c.audit_rights, "audit rights"),
                                                     (c.exit_support, "exit support"),
                                                     (c.notification_hours is not None, "incident notification deadline"))
                           if not flag]
                if missing:
                    add("high" if c.critical_function else "medium", c, "customer",
                        "DORA customer, contract lacks: " + ", ".join(missing))
            if d(c.contract_end) and today <= d(c.contract_end) <= soon:
                add("low", c, "customer", f"Contract ends {c.contract_end}")
            if not c.service_ids:
                add("low", c, "customer", "Uses no registered service")
        rank = {"high": 0, "medium": 1, "low": 2}
        return sorted(out, key=lambda f: (rank[f["severity"]], f["entity_type"], f["name"]))


    # -- common shock: what an outage costs us and our customers ---------------------------------

    def _services_disrupted(self, risk: Risk, g: Dict[str, Any]) -> Set[str]:
        """Services a risk takes down: the ones named, and those behind each named supplier's chain."""
        hit = {s for s in risk.service_ids if s in g["services"]}
        for sup in risk.supplier_ids:
            hit |= set(self._service_ids_hit_by_supplier(sup, g))
        return hit

    def _terms_for_services(self, service_ids, g: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Consequence terms of an outage of these services: their own hourly cost, and each affected
        customer's penalty once (a customer using two of them is still down once)."""
        terms = []
        for sid in sorted(service_ids, key=lambda s: g["services"][s].name):
            s = g["services"][sid]
            if s.outage_cost_per_hour:
                terms.append({"key": f"service:{sid}", "kind": "service", "id": sid, "name": s.name,
                              "per_hour": s.outage_cost_per_hour, "grace": 0.0, "cap": None})
        customers = {cid for sid in service_ids for cid in g["customers_of"].get(sid, ())}
        for cid in sorted(customers, key=lambda c: g["customers"][c].name):
            c = g["customers"][cid]
            if c.penalty_per_hour:
                terms.append({"key": f"customer:{cid}", "kind": "customer", "id": cid, "name": c.name,
                              "per_hour": c.penalty_per_hour, "grace": c.penalty_grace_hours or 0.0,
                              "cap": c.penalty_cap})
        return terms

    def consequence_terms(self, risk: Risk, g: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """What one event of the risk costs per hour of outage, per service and customer (empty without outage_hours)."""
        if not (risk.outage_hours or risk.residual_outage_hours):
            return []
        g = g or self._graph()
        return self._terms_for_services(self._services_disrupted(risk, g), g)

    def engine_risk(self, risk: Risk, g: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The risk as the calculation engines see it: its fields plus resolved consequences."""
        d = risk.to_dict()
        if risk.outage_hours or risk.residual_outage_hours:
            terms = self.consequence_terms(risk, g if g and "services" in g else None)
            if terms:
                d["consequences"] = {"terms": terms}
        links = (g["driver_links"] if g and "driver_links" in g else self._driver_links()).get(risk.id)
        if links:
            d = self._apply_driver_levels(d, links)
        return d

    def engine_risks(self, risks: List[Risk]) -> Dict[str, Dict[str, Any]]:
        g = self._graph() if any(r.outage_hours or r.residual_outage_hours for r in risks) else             {"driver_links": self._driver_links()}
        return {r.id: self.engine_risk(r, g) for r in risks}

    def risk_severity(self, risk: Risk, residual: bool, g: Optional[Dict[str, Any]] = None):
        """The loss of one event incl. consequences (a spec, or a Severity when there are consequences)."""
        sev = consequences.Severity.from_risk(self.engine_risk(risk, g), residual)
        return sev if sev.has_consequences else sev.impact

    def stress_test(self, hours: float, supplier_id: Optional[str] = None,
                    service_id: Optional[str] = None) -> Dict[str, Any]:
        """
        What an outage of exactly `hours` costs if a supplier (with everything behind it) or a
        service goes down: our own cost per service and the penalty per customer.
        """
        if hours is None or hours < 0:
            raise ValidationError("hours must be 0 or more.")
        if bool(supplier_id) == bool(service_id):
            raise ValidationError("Give either a supplier or a service.")
        g = self._graph()
        if supplier_id:
            source = self.get_supplier(supplier_id)
            services = set(self._service_ids_hit_by_supplier(supplier_id, g))
        else:
            source = self.get_service(service_id)
            services = {service_id}
        rows = consequences.stress(self._terms_for_services(services, g), hours)
        affected = {cid for sid in services for cid in g["customers_of"].get(sid, ())}
        no_terms = sorted(g["customers"][c].name for c in affected
                          if not g["customers"][c].penalty_per_hour)
        return {
            "source": {"id": source.id, "name": source.name, "kind": "supplier" if supplier_id else "service"},
            "hours": hours,
            "services": [{"id": s, "name": g["services"][s].name} for s in sorted(services)],
            "costs": rows,
            "own_cost": sum(r["loss"] for r in rows if r["kind"] == "service"),
            "penalties": sum(r["loss"] for r in rows if r["kind"] == "customer"),
            "total": sum(r["loss"] for r in rows),
            "customers_without_penalty_terms": no_terms,
            "notification_deadlines_missed": sorted(
                g["customers"][c].name for c in affected
                if g["customers"][c].notification_hours is not None and g["customers"][c].notification_hours < hours),
        }


THIRD_PARTY_WRITE_METHODS = (
    "create_supplier", "update_supplier", "delete_supplier", "create_service", "update_service",
    "delete_service", "create_customer", "update_customer", "delete_customer",
)

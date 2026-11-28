"""
What a financial customer needs from us for its DORA register of information.

A financial entity must keep a register of every ICT third-party arrangement,
including the providers further down the chain that support its critical or
important functions. For a customer we are the provider at rank 1; our
suppliers are rank 2 and their subcontractors rank 3 and beyond. This module
collects, per customer, what it will ask about us, the services it uses, the
supply chain behind them and the contract. It is laid out along the register
templates on providers (B_05.01) and ICT service supply chains (B_05.02), as a
basis for the customer's own reporting. It is not the regulatory file format:
check the columns against the current EBA taxonomy.
"""

import io
import re
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..domain.errors import ValidationError
from ..domain.models import BusinessUnit, Customer, Incident
from .core import ServiceCore

LEI_PATTERN = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")


def lei_is_valid(lei: str) -> bool:
    """ISO 17442: 20 characters, the last two are ISO 7064 MOD 97-10 check digits."""
    if not LEI_PATTERN.match(lei):
        return False
    return int("".join(str(int(ch, 36)) for ch in lei)) % 97 == 1


class DoraExtract(ServiceCore):
    """Mixin for RiskService (uses ThirdParty._graph/_chain and repo)."""

    if TYPE_CHECKING:  # members of other service areas this one uses
        def _affected(self, incident: Incident, g: Dict[str, Any]) -> Dict[str, Any]: ...
        def _graph(self) -> Dict[str, Any]: ...
        def get_customer(self, customer_id: str) -> Customer: ...


    @staticmethod
    def _lei(entity) -> None:
        if getattr(entity, "lei", None):
            entity.lei = str(entity.lei).strip().upper().replace(" ", "")
            if not lei_is_valid(entity.lei):
                raise ValidationError(f"'{entity.lei}' is not a valid LEI (20 characters with check digits).")

    def our_entity(self) -> Optional[BusinessUnit]:
        """The legal entity customers contract with: the top unit of type COMPANY (else the first top unit)."""
        tops = sorted((u for u in self.repo.list(BusinessUnit) if not u.parent_id), key=lambda u: u.name)
        return next((u for u in tops if u.organization_type == "COMPANY"), tops[0] if tops else None)

    def dora_extract(self, customer_id: str) -> Dict[str, Any]:
        customer: Customer = self.get_customer(customer_id)
        g = self._graph()
        us = self.our_entity()
        provider_rows: Dict[str, Dict[str, Any]] = {}
        chain_rows: List[Dict[str, Any]] = []
        services = []
        gaps: List[str] = []
        us_row = {"rank": 1, "name": us.name if us else None, "lei": us.lei if us else None,
                  "company_number": None, "country": us.country if us else None, "parent_company": None,
                  "type": "Direct ICT provider (us)"}
        if not us or not us.lei:
            gaps.append("Our LEI is not registered (top company unit).")

        for sid in customer.service_ids:
            svc = g["services"].get(sid)
            if svc is None:
                continue
            services.append({"id": svc.id, "name": svc.name, "ict_service_type": svc.ict_service_type,
                             "criticality": svc.criticality, "supports_critical_function": customer.critical_function,
                             "rto_hours": svc.rto_hours, "rpo_hours": svc.rpo_hours,
                             "data_locations": svc.data_locations})
            if not svc.ict_service_type:
                gaps.append(f"Service '{svc.name}': ICT service type not set.")
            if not svc.data_locations:
                gaps.append(f"Service '{svc.name}': data locations not set.")
            chain_rows.append({"service": svc.name, "rank": 1, "provider": us_row["name"], "lei": us_row["lei"],
                               "recipient": customer.name})
            # walk the chain: our suppliers (rank 2) and their subcontractors (3, ...), with who they serve
            frontier = [(sup, 2, us_row["name"]) for sup in svc.supplier_ids]
            seen = set()
            while frontier:
                sup_id, rank, recipient = frontier.pop(0)
                sup = g["suppliers"].get(sup_id)
                if sup is None or (sup_id, recipient) in seen:
                    continue
                seen.add((sup_id, recipient))
                chain_rows.append({"service": svc.name, "rank": rank, "provider": sup.name, "lei": sup.lei,
                                   "recipient": recipient})
                provider_rows.setdefault(sup.id, {
                    "rank": rank, "name": sup.name, "lei": sup.lei, "company_number": sup.company_number,
                    "country": sup.country, "parent_company": sup.parent_company,
                    "type": "Subcontractor" if rank == 2 else f"Subcontractor (rank {rank})",
                    "tier": sup.tier, "data_locations": sup.data_locations, "certifications": sup.certifications,
                    "exit_plan": sup.exit_plan})
                provider_rows[sup.id]["rank"] = min(provider_rows[sup.id]["rank"], rank)
                frontier.extend((sub, rank + 1, sup.name) for sub in sup.subcontractor_ids)
        for row in provider_rows.values():
            if not row["lei"] and not row["company_number"]:
                gaps.append(f"Supplier '{row['name']}': neither LEI nor company number registered.")
            if not row["country"]:
                gaps.append(f"Supplier '{row['name']}': country not set.")
        one_year_ago = (date.today() - timedelta(days=365)).isoformat()
        incidents = [i for i in self.repo.list(Incident) if i.detected_at[:10] >= one_year_ago
                     and customer.id in {c.id for c in self._affected(i, g)["customers"].values()}]
        return {
            "customer": {"id": customer.id, "name": customer.name, "regulations": customer.regulations,
                         "critical_function": customer.critical_function},
            "provider": us_row,
            "contract": {"contract_start": customer.contract_start, "contract_end": customer.contract_end,
                         "contract_value": customer.contract_value, "notification_hours": customer.notification_hours,
                         "audit_rights": customer.audit_rights, "exit_support": customer.exit_support},
            "services": services,
            "providers": [us_row] + sorted(provider_rows.values(), key=lambda r: (r["rank"], r["name"])),
            "supply_chain": chain_rows,
            "incidents": [{"title": i.title, "detected_at": i.detected_at, "status": i.status,
                           "nis2_significant": i.nis2_significant} for i in sorted(incidents, key=lambda i: i.detected_at)],
            "gaps": list(dict.fromkeys(gaps)),
            "generated": date.today().isoformat(),
        }

    def export_dora_extract(self, customer_id: str) -> bytes:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter

        x = self.dora_extract(customer_id)
        wb = Workbook()
        ws = wb.active
        ws.title = "Read me"
        lines = [f"Information for {x['customer']['name']}'s DORA register of information",
                 f"Prepared {x['generated']} from RiskMesh by {x['provider']['name'] or 'the provider'}.",
                 "",
                 "Laid out along the register templates on ICT third-party providers (B_05.01) and ICT service",
                 "supply chains (B_05.02). We are the provider at rank 1, our suppliers rank 2, their",
                 "subcontractors rank 3. A basis for your own reporting, not the regulatory file format.",
                 ""]
        if x["gaps"]:
            lines += ["Still missing:"] + [f"- {gap}" for gap in x["gaps"]]
        for line in lines:
            ws.append([line])
        ws["A1"].font = Font(bold=True, size=14)
        ws.column_dimensions["A"].width = 100

        def yn(v):
            return "yes" if v else "no"

        def sheet(title: str, headers: List[str], rows: List[List[Any]]) -> None:
            s = wb.create_sheet(title)
            s.append(headers)
            for r in rows:
                s.append(r)
            for i, h in enumerate(headers, start=1):
                c = s.cell(row=1, column=i)
                c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1E3A5F")
                s.column_dimensions[get_column_letter(i)].width = max(12, min(40, len(h) + 6))
            s.freeze_panes = "A2"

        c = x["contract"]
        sheet("Contract", ["Customer", "Contract start", "Contract end", "Annual value", "Notify incidents within hours",
                           "Audit rights", "Exit support", "Supports a critical or important function"],
              [[x["customer"]["name"], c["contract_start"], c["contract_end"], c["contract_value"],
                c["notification_hours"], yn(c["audit_rights"]), yn(c["exit_support"]),
                yn(x["customer"]["critical_function"])]])
        sheet("Services", ["Service", "ICT service type", "Criticality", "RTO hours", "RPO hours", "Data locations"],
              [[s["name"], s["ict_service_type"], s["criticality"].title(), s["rto_hours"], s["rpo_hours"],
                "; ".join(s["data_locations"])] for s in x["services"]])
        sheet("Providers (B_05.01)", ["Rank", "Name", "LEI", "Company number", "Country", "Ultimate parent", "Role",
                                      "Data locations", "Certifications / reports", "Exit plan"],
              [[p["rank"], p["name"], p["lei"], p.get("company_number"), p["country"], p.get("parent_company"),
                p["type"], "; ".join(p.get("data_locations") or []), "; ".join(p.get("certifications") or []),
                yn(p["exit_plan"]) if "exit_plan" in p else None] for p in x["providers"]])
        sheet("Supply chain (B_05.02)", ["Service", "Rank", "Provider", "LEI", "Serves"],
              [[r["service"], r["rank"], r["provider"], r["lei"], r["recipient"]] for r in x["supply_chain"]])
        sheet("Incidents (12 months)", ["Incident", "Detected", "Status", "NIS2 significant"],
              [[i["title"], i["detected_at"], i["status"].title(),
                {True: "yes", False: "no", None: "not assessed"}[i["nis2_significant"]]] for i in x["incidents"]])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

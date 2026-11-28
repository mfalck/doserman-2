# RiskMesh roadmap

RiskMesh follows a few principles, after Graeme Keith (Stochastic):
risk management should be **actionable** (decisions are built into the model),
**causal** (it predicts the effect of interventions), **stochastic** (risks are
uncertainties) and **simple, but adequate** (granularity aligned to decisions,
empirically adequate). Every item below is checked against them.

## Where we are (version 1, on main)

- Quantified risks with two engines: an exact analytic engine and Monte Carlo. Every run is reproducible through the run register and its fingerprint.
- Assessment methods as plugins: quantitative, ISO/IEC 27005 (likelihood x consequence 1-5) and FAIR.
- Portfolio view: correlations, drivers (common causes with what-if and tornado), scenarios, risk appetite and objectives with tolerances.
- Decisions: what each control is worth, investment plans within a budget, and controls that work through drivers.
- Data: observations and public benchmarks calibrate the estimates with Bayesian updating.
- Third parties: suppliers and fourth parties, common-shock outages, stress tests, the DORA register extract and the customer assurance report.
- Governance: tamper-evident audit trail, signed anchors, closed reporting periods, roles, SSO and the incident register with NIS2 deadlines.
- Clients: REST API, CLI, desktop application and web app, plus the Excel workbook for bulk data.

## Version 1 - next, in this order

1. **Better numbers**
   - P80 in all reports, with P50 and P80 as the headline figures. P99 and TVaR move to the tail section and appetite. The percentiles shown are configurable.
   - A negative binomial frequency: a Poisson whose rate is itself uncertain. Experts can give a range of events per year, and calibration suggests it directly from the Poisson-Gamma posterior.
   - ISO 27005 uses Poisson by default. It keeps P(at least one event) per level and allows repeats. Bernoulli remains an option, and stored assessments keep their method version.
   - NIS2 significance: a per-event threshold (amount, outage hours), giving the expected significant incidents per year and the chance of at least one. This links to the incident register.
2. **Customer scope**
   - Run an analysis for exactly one customer's part of the graph: its services, the suppliers behind them, and the risks to them.
   - Two perspectives: our loss at the customer, and the customer's own loss per outage hour.
   - Extracts for that customer only: the report with the analysis figures, and an Excel workbook of the customer's part.
3. **Split `RiskService`**
   - Turn the mixins into explicit application services per area: organisation and risks, third parties, portfolio, decisions, evidence, incidents, reporting and admin.
   - Then make mypy blocking in CI.
4. **Workspaces**
   - Independent analyses with watertight separation, one repository (file, database or schema) per workspace, e.g. per consulting engagement.
   - Workspace switch in the clients, and user access per workspace.
   - No tenant column inside a shared database.
5. **KRIs and findings**
   - Import vulnerability scans (e.g. Qualys CSV) and findings lists (audit, ISAE 3402, penetration tests) into an immutable raw archive: one file per import, with source, date and id.
   - KRIs with trends, such as critical patches within 14 days, age of open critical vulnerabilities and finding recurrence.
   - KRIs suggest driver levels, and findings mapped to ISO 27001 controls give control failure rates.

Smaller items to fit in along the way:

- **Methods, part 2**
  - A web form built from a method's fields.
  - Threats, vulnerabilities and threat actors as objects, with MITRE ATT&CK on threats.
  - A bow-tie view.
- **Control objectives**: from a provision (NIS2 art. 21, DORA) to a measurable target, its evidence and who is responsible, linked to controls and to the customer contracts that demand it.
- **Reports**: Word and PowerPoint, and a read-only token for one customer's report.
- **Desktop application**
  - A scenarios page.
  - Driver fields in the control dialog.
- **Release**
  - Version tag, CHANGELOG, PyPI package and Docker image.
  - CONTRIBUTING.md and SECURITY.md.
  - A decision on whether the web app becomes open source.

## Version 2 - advanced analyses

An advanced analysis belongs in RiskMesh only if it

1. answers a decision question, not "here is a score";
2. is causal and explainable, working through drivers, controls and risks;
3. shows its uncertainty as intervals and distributions;
4. is empirically adequate, saying whether data or experts carry the result;
5. is reproducible, stored in the run register with its fingerprint.

Analyses are plugins, like assessment methods, so version 2 does not change
the core.

Candidates, roughly in order of value:

- **Value of information**: which estimate is worth improving because it could change a decision (EVPI/EVSI per estimate). The first one to build.
- **Control portfolio optimisation under a tail constraint**: e.g. the cheapest set of controls that keeps P80 within appetite. It goes beyond the greedy investment plan.
- **Hierarchical Bayesian calibration**: units, customers and benchmarks share strength according to how alike they are.
- **Driver networks**: drivers that affect each other, with interventions propagated through the network.
- **Global sensitivity analysis** (e.g. Sobol): which assumptions drive the result, including their interactions.
- **Trends and change points in KRIs**: an early signal of emerging risk to look into, not a score.
- **Machine learning only where data is plentiful** (e.g. vulnerability scans), for triage and suggestions, never as the estimate itself.

## Deliberately not planned

- **Composite indices** ("overall risk score", "risk momentum"). RiskMesh expresses risk as probabilities and money, and measurements act through drivers.
- **Black-box predictions of findings or breaches from sparse data.** They are hard to defend to auditors and supervisors.
- **A rewrite onto another framework.**

"""
Risk assessment methods as plugins.

A method turns the inputs people give in its own terms - an ISO 27005
likelihood and consequence on 1-5, FAIR's threat event frequency,
vulnerability and loss magnitudes - into the common model every engine works
on: a frequency spec and a loss-per-event spec (inherent, and residual when
given). The risk keeps its method and inputs, so the assessment can be read
back in the method's own words, and stores the derived specs, so simulation,
appetite, objectives, decisions and reports work unchanged.

A method declares its input fields (`fields`), so clients can build forms
without knowing the method. Methods are registered with `register`; other
packages can add theirs through the entry point group "riskmesh.methods"
(an object or class with the RiskMethod interface) without touching the core.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

QUANTITATIVE = "quantitative"


class RiskMethod:
    """The interface. `version` goes into the stored inputs, so a result can be traced to the method version."""
    key: str = ""
    label: str = ""
    version: str = "1"
    description: str = ""
    fields: List[Dict[str, Any]] = []   # {"name", "label", "kind": "level"|"number"|"share"|"range", "required", "help"}

    def validate(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Checked and normalised inputs (raise ValueError with a plain message)."""
        raise NotImplementedError

    def to_specs(self, inputs: Dict[str, Any], scale) -> Dict[str, Optional[Dict[str, Any]]]:
        """{"frequency", "impact", "residual_frequency", "residual_impact"} in the common spec format."""
        raise NotImplementedError

    def summary(self, inputs: Dict[str, Any]) -> str:
        """One line in the method's own terms, e.g. 'L4 x C3 = 12 (High)'."""
        return ""

    def describe(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "version": self.version, "description": self.description,
                "fields": self.fields}


_REGISTRY: Dict[str, RiskMethod] = {}


def register(method: RiskMethod) -> RiskMethod:
    if not method.key or method.key == QUANTITATIVE:
        raise ValueError("A method needs a key other than 'quantitative'.")
    _REGISTRY[method.key] = method
    return method


def get(key: str) -> RiskMethod:
    _load_entry_points()
    if key not in _REGISTRY:
        raise ValueError(f"Unknown assessment method '{key}' (known: {', '.join(sorted(_REGISTRY)) or 'none'}).")
    return _REGISTRY[key]


def all_methods() -> List[RiskMethod]:
    _load_entry_points()
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


_loaded = False


def _load_entry_points() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="riskmesh.methods"):
            obj = ep.load()
            register(obj() if isinstance(obj, type) else obj)
    except Exception:  # a broken third-party plugin must not break the core
        pass


# -- helpers -----------------------------------------------------------------------------------------

def _num(inputs: Dict[str, Any], name: str, lo: float, hi: float, required: bool = True) -> Optional[float]:
    v = inputs.get(name)
    if v is None or v == "":
        if required:
            raise ValueError(f"{name} is required.")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{name} must be a number.")
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be from {lo:g} to {hi:g}.")
    return float(v)


def _range(inputs: Dict[str, Any], name: str, lo: float, hi: float, required: bool = True) -> Optional[Dict[str, float]]:
    """A three-point estimate {"min", "mode", "max"} (a single number means min = mode = max)."""
    v = inputs.get(name)
    if v is None or v == "":
        if required:
            raise ValueError(f"{name} is required.")
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = {"min": v, "mode": v, "max": v}
    if not isinstance(v, dict):
        raise ValueError(f"{name} must be a number or a min / most likely / max estimate.")
    try:
        r = {k: float(v[k]) for k in ("min", "mode", "max")}
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{name} needs min, mode (most likely) and max.") from None
    if not lo <= r["min"] <= r["mode"] <= r["max"] <= hi:
        raise ValueError(f"{name}: need {lo:g} <= min <= most likely <= max <= {hi:g}.")
    return r


def _pert_mean(r: Dict[str, float]) -> float:
    return (r["min"] + 4 * r["mode"] + r["max"]) / 6


def _pert(min_: float, mode: float, max_: float) -> Dict[str, Any]:
    if max_ <= min_:
        return {"dist": "fixed", "value": round(min_, 2)}
    return {"dist": "pert", "min": round(min_, 2), "mode": round(min(max(mode, min_), max_), 2), "max": round(max_, 2)}


# -- ISO 27005: likelihood and consequence on a 1-5 scale ------------------------------------------------

class ISO27005(RiskMethod):
    """
    Likelihood and consequence levels 1-5 (inherent, and after controls). The
    levels are translated with the organisation's scoring scale - the same
    bands that turn quantitative estimates into 1-5 - so a qualitative risk
    lands in the band it was rated, and can take part in totals as an
    indicative figure: the frequency is the middle of the likelihood band,
    the loss per event a lognormal range spanning the consequence band.
    """
    key = "iso27005"
    version = "2"   # 2: Poisson by default (1: Bernoulli, at most one event a year)
    label = "ISO/IEC 27005 (likelihood x consequence, 1-5)"
    description = ("Qualitative assessment on a 5x5 scale. Converted with the scoring scale to an indicative "
                   "frequency and loss range, so qualitative and quantitative risks can be compared.")
    fields = [
        {"name": "likelihood", "label": "Likelihood (1-5)", "kind": "level", "required": True},
        {"name": "consequence", "label": "Consequence (1-5)", "kind": "level", "required": True},
        {"name": "residual_likelihood", "label": "Likelihood after controls", "kind": "level", "required": False},
        {"name": "residual_consequence", "label": "Consequence after controls", "kind": "level", "required": False},
        {"name": "frequency_model", "label": "Events", "kind": "choice", "required": False,
         "choices": [["poisson", "can happen several times a year"], ["bernoulli", "at most once a year"]]},
        {"name": "rationale", "label": "Rationale", "kind": "text", "required": False},
    ]

    def validate(self, inputs):
        out = {}
        model = inputs.get("frequency_model")
        if model is None:  # assessments made with version 1 keep the at-most-once model
            model = "bernoulli" if str(inputs.get("method_version", "")) == "1" else "poisson"
        if model not in ("poisson", "bernoulli"):
            raise ValueError("frequency_model must be 'poisson' or 'bernoulli'.")
        out["frequency_model"] = model
        for f in ("likelihood", "consequence", "residual_likelihood", "residual_consequence"):
            v = _num(inputs, f, 1, 5, required=f in ("likelihood", "consequence"))
            if v is not None:
                if not float(v).is_integer():
                    raise ValueError(f"{f} must be a whole level 1-5.")
                out[f] = int(v)
        if ("residual_likelihood" in out) != ("residual_consequence" in out):
            raise ValueError("Give both levels after controls, or neither.")
        if inputs.get("rationale"):
            out["rationale"] = str(inputs["rationale"]).strip()
        return out

    @staticmethod
    def _probability(level: int, bands) -> float:
        edges = [0.0, *bands, 1.0]
        lo, hi = edges[level - 1], edges[level]
        return (max(lo, 0.005) * min(hi, 0.95)) ** 0.5 if level < 5 else min((lo + 1.0) / 2, 0.95)

    @staticmethod
    def _loss(level: int, bands) -> Dict[str, Any]:
        lo = bands[level - 2] if level > 1 else bands[0] / 5
        hi = bands[level - 1] if level <= len(bands) else bands[-1] * 4
        return {"dist": "lognorm_p10_p90", "p10": round(lo, 2), "p90": round(hi, 2)}

    def to_specs(self, inputs, scale):
        lb, ib = scale.likelihood_bands, scale.impact_bands

        def freq(level):
            p = self._probability(level, lb)
            if inputs.get("frequency_model", "poisson") == "poisson":
                # the same chance of at least one event a year, but repeats are possible
                return {"type": "poisson", "rate": round(-math.log(1.0 - p), 6)}
            return {"type": "bernoulli", "p": round(p, 6)}
        out = {"frequency": freq(inputs["likelihood"]), "impact": self._loss(inputs["consequence"], ib),
               "residual_frequency": None, "residual_impact": None}
        if "residual_likelihood" in inputs:
            out["residual_frequency"] = freq(inputs["residual_likelihood"])
            out["residual_impact"] = self._loss(inputs["residual_consequence"], ib)
        return out

    def summary(self, inputs):
        def one(lk, c):
            s = lk * c
            level = "Very High" if s >= 20 else "High" if s >= 12 else "Medium" if s >= 6 else "Low" if s >= 3 else "Very Low"
            return f"L{lk} x C{c} = {s} ({level})"
        text = one(inputs["likelihood"], inputs["consequence"])
        if "residual_likelihood" in inputs:
            text += " -> after controls " + one(inputs["residual_likelihood"], inputs["residual_consequence"])
        return text


# -- FAIR: threat event frequency x vulnerability, primary + secondary loss ---------------------------------

class FAIR(RiskMethod):
    """
    FAIR's top of the taxonomy: loss event frequency = threat event frequency
    x vulnerability (the share of threat events that become loss events), and
    loss magnitude = primary loss + (probability of secondary loss x secondary
    loss). Estimates are three-point (min, most likely, max). The frequency
    becomes a Poisson rate of the expected LEF; the loss per event a PERT with
    the combined range and the combined expected value. Residual: the
    vulnerability and/or threat event frequency after controls (FAIR-CAM's
    resistance), and optionally lower losses.
    """
    key = "fair"
    label = "FAIR (threat event frequency, vulnerability, loss magnitude)"
    description = ("Open FAIR: LEF = TEF x Vulnerability; loss = primary + P(secondary) x secondary. "
                   "Three-point estimates; residual via vulnerability or TEF after controls.")
    fields = [
        {"name": "tef", "label": "Threat event frequency per year", "kind": "range", "required": True},
        {"name": "vulnerability", "label": "Vulnerability (share of threat events that cause loss)", "kind": "share_range", "required": True},
        {"name": "primary_loss", "label": "Primary loss per event", "kind": "range", "required": True},
        {"name": "secondary_probability", "label": "Probability of secondary loss", "kind": "share", "required": False},
        {"name": "secondary_loss", "label": "Secondary loss per event", "kind": "range", "required": False},
        {"name": "residual_vulnerability", "label": "Vulnerability after controls", "kind": "share_range", "required": False},
        {"name": "residual_tef", "label": "Threat event frequency after controls", "kind": "range", "required": False},
        {"name": "residual_primary_loss", "label": "Primary loss after controls", "kind": "range", "required": False},
    ]

    def validate(self, inputs):
        out: Dict[str, Any] = {
            "tef": _range(inputs, "tef", 0, 10_000),
            "vulnerability": _range(inputs, "vulnerability", 0, 1),
            "primary_loss": _range(inputs, "primary_loss", 0, 1e13),
        }
        p = _num(inputs, "secondary_probability", 0, 1, required=False)
        sl = _range(inputs, "secondary_loss", 0, 1e13, required=False)
        if (p is None) != (sl is None):
            raise ValueError("Give both the probability of secondary loss and its size, or neither.")
        if p is not None:
            out["secondary_probability"], out["secondary_loss"] = p, sl
        for f, hi in (("residual_vulnerability", 1), ("residual_tef", 10_000), ("residual_primary_loss", 1e13)):
            r = _range(inputs, f, 0, hi, required=False)
            if r is not None:
                out[f] = r
        if out["primary_loss"]["max"] <= 0:
            raise ValueError("primary_loss must be above 0.")
        return out

    @staticmethod
    def _parts(tef, vuln, primary, p_sec, secondary) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        lef = _pert_mean(tef) * _pert_mean(vuln)
        mean = _pert_mean(primary) + (p_sec or 0) * (_pert_mean(secondary) if secondary else 0)
        lo = primary["min"]
        hi = primary["max"] + (secondary["max"] if secondary and p_sec else 0)
        # a PERT over the combined range whose mean is the combined expected loss
        mode = (6 * mean - lo - hi) / 4
        return {"type": "poisson", "rate": round(lef, 6)}, _pert(lo, mode, hi)

    def to_specs(self, inputs, scale):
        p_sec, sec = inputs.get("secondary_probability"), inputs.get("secondary_loss")
        freq, impact = self._parts(inputs["tef"], inputs["vulnerability"], inputs["primary_loss"], p_sec, sec)
        out = {"frequency": freq, "impact": impact, "residual_frequency": None, "residual_impact": None}
        if any(k in inputs for k in ("residual_vulnerability", "residual_tef", "residual_primary_loss")):
            rf, ri = self._parts(inputs.get("residual_tef", inputs["tef"]),
                                 inputs.get("residual_vulnerability", inputs["vulnerability"]),
                                 inputs.get("residual_primary_loss", inputs["primary_loss"]), p_sec, sec)
            out["residual_frequency"], out["residual_impact"] = rf, ri
        return out

    def summary(self, inputs):
        lef = _pert_mean(inputs["tef"]) * _pert_mean(inputs["vulnerability"])
        text = f"LEF {lef:.2f}/yr = TEF {_pert_mean(inputs['tef']):.2f} x Vuln {_pert_mean(inputs['vulnerability']):.0%}"
        if "residual_vulnerability" in inputs or "residual_tef" in inputs:
            r = _pert_mean(inputs.get("residual_tef", inputs["tef"])) * \
                _pert_mean(inputs.get("residual_vulnerability", inputs["vulnerability"]))
            text += f" -> {r:.2f}/yr after controls"
        return text


register(ISO27005())
register(FAIR())


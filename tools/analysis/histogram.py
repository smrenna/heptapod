"""
# histogram.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

Fill a luminosity-normalised histogram from one or more reco-level JSONL
event files (schema {"data": {<collection>: [...]}}). Several files, such as
the per-job outputs of a condor cluster after NanoAODToJSONLTool, are merged
by concatenation before filling, so the normalisation uses the TOTAL number of
generated events. Normalising each job separately and adding is the usual way
to get this wrong.

    weight = sigma_pb * 1000 * lumi_fb / n_generated
    N_i    = weight * counts_i,    err_i = weight * sqrt(counts_i)
"""

import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple

from orchestral.tools.base.tool import BaseTool
from orchestral.tools.base.field_utils import RuntimeField, StateField

SCHEMA_VERSION = "histogram-1.0"

_ALIASES = {"electron": "electrons", "muon": "muons", "photon": "photons",
            "jet": "jets"}


def _norm_name(name: str) -> str:
    return _ALIASES.get(name, name)


def _pt(o: Dict[str, Any]) -> float:
    if "pt" in o:
        return float(o["pt"])
    return math.hypot(float(o.get("px", 0.0)), float(o.get("py", 0.0)))


def _inv_mass(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    e = a["E"] + b["E"]
    px, py, pz = a["px"] + b["px"], a["py"] + b["py"], a["pz"] + b["pz"]
    return math.sqrt(max(0.0, e * e - px * px - py * py - pz * pz))


def _select(objs: List[Dict[str, Any]], index: Optional[int]
            ) -> List[Dict[str, Any]]:
    """index=None -> all objects; index=k -> only the k-th (0 = leading)."""
    if index is None:
        return list(objs)
    return [objs[index]] if 0 <= index < len(objs) else []


def values_for_event(
    data: Dict[str, Any],
    obj: str,
    variable: str,
    index: Optional[int],
    obj2: Optional[str] = None,
    index2: Optional[int] = None,
    min_pt: float = 0.0,
) -> List[float]:
    """Observable value(s) contributed by one event (possibly none)."""
    first = [o for o in data.get(_norm_name(obj), []) if _pt(o) >= min_pt]
    if variable == "inv_mass":
        second_name = _norm_name(obj2) if obj2 else _norm_name(obj)
        second = [o for o in data.get(second_name, []) if _pt(o) >= min_pt]
        a = _select(first, index)
        b = _select(second, index2)
        if not a or not b:
            return []
        if obj2 is None or _norm_name(obj2) == _norm_name(obj):
            # same collection: pair distinct objects only
            if index is None and index2 is None:
                return [_inv_mass(first[i], first[j])
                        for i in range(len(first))
                        for j in range(i + 1, len(first))]
            if index == index2:
                return []
        return [_inv_mass(a[0], b[0])]
    out = []
    for o in _select(first, index):
        if variable == "pt":
            out.append(_pt(o))
        elif variable in o:
            out.append(float(o[variable]))
    return out


def bin_edges(nbins: int, lo: float, hi: float) -> List[float]:
    step = (hi - lo) / nbins
    return [lo + i * step for i in range(nbins)] + [hi]


def fill(values: List[float], nbins: int, lo: float, hi: float
         ) -> Tuple[List[int], int, int]:
    counts = [0] * nbins
    under = over = 0
    step = (hi - lo) / nbins
    for v in values:
        if v < lo:
            under += 1
        elif v >= hi:
            over += 1
        else:
            counts[min(int((v - lo) / step), nbins - 1)] += 1
    return counts, under, over


class NormalizedHistogramTool(BaseTool):
    """
    Fill a luminosity-normalised histogram from reco-level JSONL events.

    Merges several JSONL files (e.g. one per condor job, from
    NanoAODToJSONLTool) and normalises with the TOTAL generated-event count:
    yield_i = counts_i * sigma_pb * 1000 * lumi_fb / n_generated, with
    Poisson errors weight*sqrt(counts_i).

    Observables:
      - variable pt/eta/phi/m (or any per-object key): filled once per selected
        object. `index` picks the k-th object of `object` (0 = leading); omit it
        to fill every object.
      - variable inv_mass: invariant mass of two objects, `object`[index] and
        `object2`[index2] (object2 defaults to `object`; with the same
        collection and no indices, every distinct pair is filled).
        Example: electron + leading jet mass is object=electrons, index=0,
        object2=jets, index2=0. This is a pseudo-resonance mass: it is the
        right pairing only if the event really contains that pair.

    n_generated is the number of events GENERATED, before any selection.
    Leave it unset only if every generated event is in the input files; it then
    defaults to the number of events read. Events failing `require` still count.

    Input:
        input_files: JSONL files (relative to base_directory).
        object / variable / index / object2 / index2: the observable.
        bins, range_min, range_max: histogram binning.
        cross_section_pb, luminosity_fb: normalisation inputs.
        n_generated: generated events (default: events read).
        min_pt: ignore objects below this pT (GeV).
        require: optional {collection: min_count}, event must have at least
            that many objects (after min_pt) to be filled, e.g. {"electrons": 1}.
        output_path: optional JSON file for the result.
    """

    # ======================== Runtime fields ======================== #
    input_files: List[str] = RuntimeField(
        description="JSONL event files to merge, relative to base_directory.")
    object: str = RuntimeField(
        description="Collection: electrons, muons, photons, jets, ...")
    variable: str = RuntimeField(
        description="pt, eta, phi, m, any per-object key, or inv_mass.")
    bins: int = RuntimeField(description="Number of bins.")
    range_min: float = RuntimeField(description="Lower edge (GeV for pt/mass).")
    range_max: float = RuntimeField(description="Upper edge.")
    cross_section_pb: float = RuntimeField(
        description="Process cross section in pb.")
    luminosity_fb: float = RuntimeField(
        description="Target integrated luminosity in fb^-1.")
    index: Optional[int] = RuntimeField(
        default=None, description="k-th object (0 = leading); omit for all.")
    object2: Optional[str] = RuntimeField(
        default=None, description="Second collection for inv_mass.")
    index2: Optional[int] = RuntimeField(
        default=None, description="k-th object of object2 for inv_mass.")
    n_generated: Optional[int] = RuntimeField(
        default=None,
        description="Events generated before selection; default: events read.")
    min_pt: float = RuntimeField(
        default=0.0, description="Ignore objects below this pT (GeV).")
    require: Optional[Dict[str, int]] = RuntimeField(
        default=None,
        description="{collection: min_count} an event must satisfy to be filled.")
    output_path: Optional[str] = RuntimeField(
        default=None, description="Optional JSON file to write the result to.")
    # ================================================================ #

    # ========================= State fields ========================= #
    base_directory: str = StateField(
        default=".", description="Base sandbox directory for file operations")
    # ================================================================ #

    def _setup(self):
        self.base_directory = os.path.abspath(self.base_directory)

    def _resolve(self, path: str) -> Optional[str]:
        full = path if os.path.isabs(path) else os.path.join(
            self.base_directory, path)
        full = os.path.abspath(full)
        if os.path.commonpath([full, self.base_directory]) != self.base_directory:
            return None
        return full

    def _run(self) -> str:
        try:
            self._setup()
        except Exception as e:  # noqa: BLE001
            return self.format_error(error="Setup Error", reason=str(e))

        if not self.input_files:
            return self.format_error(
                error="No input files", reason="input_files is empty.")
        if self.bins < 1 or not self.range_max > self.range_min:
            return self.format_error(
                error="Bad binning",
                reason="need bins >= 1 and range_max > range_min.")
        if self.cross_section_pb < 0 or self.luminosity_fb < 0:
            return self.format_error(
                error="Bad normalisation",
                reason="cross_section_pb and luminosity_fb must be >= 0.")
        if self.n_generated is not None and self.n_generated < 1:
            return self.format_error(
                error="Bad n_generated", reason="n_generated must be >= 1.")
        if self.variable == "inv_mass" and self.object2 is None \
                and (self.index is None) != (self.index2 is None):
            return self.format_error(
                error="Ambiguous pairing",
                reason="For inv_mass within one collection give both index "
                       "and index2, or neither.")
        paths = []
        for f in self.input_files:
            full = self._resolve(f)
            if full is None:
                return self.format_error(
                    error="Path outside sandbox",
                    reason=f"{f!r} resolves outside base_directory.")
            if not os.path.isfile(full):
                return self.format_error(
                    error="Input not found", reason=f"{f!r} does not exist.")
            paths.append(full)
        out = None
        if self.output_path:
            out = self._resolve(self.output_path)
            if out is None:
                return self.format_error(
                    error="Path outside sandbox",
                    reason=f"{self.output_path!r} resolves outside "
                           "base_directory.")

        require = {_norm_name(k): v for k, v in (self.require or {}).items()}
        values: List[float] = []
        n_read = n_pass = 0
        for path in paths:
            with open(path) as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    data = json.loads(line).get("data", {})
                    n_read += 1
                    if any(sum(1 for o in data.get(k, [])
                               if _pt(o) >= self.min_pt) < need
                           for k, need in require.items()):
                        continue
                    n_pass += 1
                    values += values_for_event(
                        data, self.object, self.variable, self.index,
                        self.object2, self.index2, self.min_pt)

        if n_read == 0:
            return self.format_error(
                error="No events", reason="The input files contain no events.")

        n_gen = self.n_generated if self.n_generated is not None else n_read
        warnings = []
        if self.n_generated is None:
            warnings.append(
                f"n_generated not given: used the {n_read} events read. This "
                "is only right if the input holds every generated event.")
        elif self.n_generated < n_read:
            warnings.append(
                f"n_generated ({self.n_generated}) is smaller than the "
                f"events read ({n_read}).")
        counts, under, over = fill(values, self.bins, self.range_min,
                                   self.range_max)
        if not values:
            warnings.append(
                "No entries: the selection or observable matched nothing.")
        elif not any(counts):
            warnings.append(
                f"No entries in range: all {len(values)} value(s) fall "
                f"outside [{self.range_min}, {self.range_max}) "
                f"({under} below, {over} above).")
        weight = self.cross_section_pb * 1000.0 * self.luminosity_fb / n_gen
        result = {
            "status": "ok",
            "schema": SCHEMA_VERSION,
            "observable": {
                "object": self.object, "variable": self.variable,
                "index": self.index, "object2": self.object2,
                "index2": self.index2, "min_pt": self.min_pt,
            },
            "n_files": len(paths),
            "n_events_read": n_read,
            "n_events_passing": n_pass,
            "n_generated": n_gen,
            "n_entries": len(values),
            "per_event_weight": weight,
            "bin_edges": bin_edges(self.bins, self.range_min, self.range_max),
            "raw_counts": counts,
            "normalized_counts": [c * weight for c in counts],
            "errors": [weight * math.sqrt(c) for c in counts],
            "underflow_raw": under,
            "overflow_raw": over,
            "expected_yield_in_range": sum(counts) * weight,
        }
        if warnings:
            result["warnings"] = warnings
        if out:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with open(out, "w") as fh:
                json.dump(result, fh, indent=2)
            result["output_path"] = os.path.relpath(out, self.base_directory)
        return json.dumps(result, indent=2)

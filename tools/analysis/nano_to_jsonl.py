"""
# nano_to_jsonl.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

Convert a flat NanoAOD file (NANOAODSIM, i.e. an `Events` TTree of plain
branches) into the reco-level JSONL the other analysis tools read
(CutflowTool, GetHardestN, ResonanceReconstruction, ...):

    {"schema": "evtjsonl-1.0", "event_id": N,
     "data": {"electrons": [{"pt","eta","phi","m","px","py","pz","E"}, ...],
              "muons": [...], "photons": [...], "jets": [...],
              "met": {"met": ..., "phi": ..., "px": ..., "py": ...}}}

Needs `uproot` (pure python; no CMSSW, no ROOT). It reads the flat NANOAODSIM
format only: the EDM-wrapped NANOEDMAODSIM that cmsDriver gives by default for
campaign chains must be re-made with `--eventcontent NANOAODSIM`
(CMSDriverChainTool's eventcontent_overrides does this).
"""

import json
import math
import os
from typing import Any, Dict, List, Optional

from orchestral.tools.base.tool import BaseTool
from orchestral.tools.base.field_utils import RuntimeField, StateField

SCHEMA_VERSION = "evtjsonl-1.0"

# JSONL collection name -> NanoAOD branch prefix. Branches are <prefix>_pt etc.
DEFAULT_COLLECTIONS = {
    "electrons": "Electron",
    "muons": "Muon",
    "photons": "Photon",
    "jets": "Jet",
}
_KINEMATICS = ("pt", "eta", "phi", "mass")
# Extra per-object branches carried through when present (name -> output key).
_EXTRA = {
    "Electron": ("charge", "pdgId"),
    "Muon": ("charge", "pdgId"),
    "Photon": (),
    "Jet": ("btagDeepFlavB",),
}


def _p4(pt: float, eta: float, phi: float, mass: float) -> Dict[str, float]:
    """(pt, eta, phi, m) -> Cartesian components and energy, in GeV."""
    px = pt * math.cos(phi)
    py = pt * math.sin(phi)
    pz = pt * math.sinh(eta)
    e = math.sqrt(max(0.0, px * px + py * py + pz * pz + mass * mass))
    return {"px": px, "py": py, "pz": pz, "E": e}


def build_event(
    columns: Dict[str, List[Any]],
    index: int,
    collections: Dict[str, str],
    event_id: int,
    met_prefix: Optional[str] = "MET",
) -> Dict[str, Any]:
    """
    Build one JSONL event from per-branch columns.

    `columns[branch][index]` is a list (jagged branches such as Electron_pt)
    or a number (flat branches such as MET_pt, event). Objects are written in
    the order NanoAOD stores them, which is pT-descending for the standard
    collections.
    """
    data: Dict[str, Any] = {}
    for name, prefix in collections.items():
        if f"{prefix}_pt" not in columns:
            data[name] = []
            continue
        n = len(columns[f"{prefix}_pt"][index])
        objs = []
        for i in range(n):
            pt = float(columns[f"{prefix}_pt"][index][i])
            eta = float(columns[f"{prefix}_eta"][index][i])
            phi = float(columns[f"{prefix}_phi"][index][i])
            mb = f"{prefix}_mass"
            mass = float(columns[mb][index][i]) if mb in columns else 0.0
            obj = {"pt": pt, "eta": eta, "phi": phi, "m": mass}
            obj.update(_p4(pt, eta, phi, mass))
            for extra in _EXTRA.get(prefix, ()):
                eb = f"{prefix}_{extra}"
                if eb in columns:
                    obj[extra] = float(columns[eb][index][i])
            objs.append(obj)
        data[name] = objs
    if met_prefix and f"{met_prefix}_pt" in columns:
        met = float(columns[f"{met_prefix}_pt"][index])
        phi = float(columns[f"{met_prefix}_phi"][index])
        data["met"] = {"met": met, "phi": phi,
                       "px": met * math.cos(phi), "py": met * math.sin(phi)}
    data["n"] = sum(len(v) for v in data.values() if isinstance(v, list))
    return {"schema": SCHEMA_VERSION, "event_id": int(event_id), "data": data}


class NanoAODToJSONLTool(BaseTool):
    """
    Convert flat NanoAOD (NANOAODSIM) files into reco-level JSONL events that
    CutflowTool, GetHardestNTool and the reconstruction tools can read.

    Writes electrons, muons, photons and jets (pt, eta, phi, m, px, py, pz, E,
    plus charge/pdgId/b-tag where present) and MET. Several input files are
    concatenated in order, so per-job outputs from a condor cluster can be
    converted in one call; `event_id` is a running index over all files, and
    each event also carries its (run, luminosityBlock, event) when present so
    duplicated events across jobs can be detected.

    Requires `uproot` (pure python; no ROOT/CMSSW needed). The input must be the
    flat NANOAODSIM format; an EDM-wrapped NANOEDMAODSIM file has no `Events`
    branches of this form and is rejected with an explanation.

    Input:
        input_files: list of .root files (relative to base_directory or absolute
            within it).
        output_path: JSONL to write.
        collections: optional {jsonl_name: NanoAOD prefix}; default electrons,
            muons, photons, jets.
        max_events: optional cap on events converted.
        chunk_size: events read per batch (memory bound), default 5000.
    """

    # ======================== Runtime fields ======================== #
    input_files: List[str] = RuntimeField(
        description="NanoAOD .root files to convert, in order.")
    output_path: str = RuntimeField(
        description="JSONL file to write, relative to base_directory.")
    collections: Optional[Dict[str, str]] = RuntimeField(
        default=None,
        description="Optional {jsonl_name: NanoAOD branch prefix}, e.g. "
        "{'electrons': 'Electron', 'jets': 'Jet'}. Default: electrons, muons, "
        "photons, jets.")
    max_events: Optional[int] = RuntimeField(
        default=None, description="Stop after this many events.")
    chunk_size: int = RuntimeField(
        default=5000, description="Events read from the file per batch.")
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
        if self.chunk_size < 1:
            return self.format_error(
                error="Bad chunk_size", reason="chunk_size must be >= 1.")
        out = self._resolve(self.output_path)
        if out is None:
            return self.format_error(
                error="Path outside sandbox",
                reason=f"{self.output_path!r} resolves outside base_directory.")
        inputs = []
        for f in self.input_files:
            full = self._resolve(f)
            if full is None:
                return self.format_error(
                    error="Path outside sandbox",
                    reason=f"{f!r} resolves outside base_directory.")
            if not os.path.isfile(full):
                return self.format_error(
                    error="Input not found", reason=f"{f!r} does not exist.")
            inputs.append(full)

        try:
            import uproot  # deferred: heavy import, optional dependency
        except ImportError:
            return self.format_error(
                error="uproot not installed",
                reason="Reading NanoAOD needs the `uproot` package.",
                suggestion="pip install uproot")

        collections = dict(self.collections or DEFAULT_COLLECTIONS)
        wanted = []
        for prefix in collections.values():
            wanted += [f"{prefix}_{k}" for k in _KINEMATICS]
            wanted += [f"{prefix}_{x}" for x in _EXTRA.get(prefix, ())]
        wanted += ["MET_pt", "MET_phi", "run", "luminosityBlock", "event"]

        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        n_written = 0
        seen_ids = set()
        n_dup = 0
        counts: Dict[str, int] = {k: 0 for k in collections}
        try:
            with open(out, "w") as fh:
                for path in inputs:
                    with uproot.open(path) as rf:
                        if "Events" not in rf:
                            return self.format_error(
                                error="No Events tree",
                                reason=f"{os.path.basename(path)} has no "
                                       "`Events` tree.")
                        tree = rf["Events"]
                        have = set(tree.keys())
                        if not any(f"{p}_pt" in have
                                   for p in collections.values()):
                            return self.format_error(
                                error="Not flat NanoAOD",
                                reason=f"{os.path.basename(path)} has no "
                                       "Electron_pt/Jet_pt/... branches; it "
                                       "looks like EDM-wrapped "
                                       "NANOEDMAODSIM.",
                                suggestion="Re-make with --eventcontent "
                                           "NANOAODSIM.")
                        use = [b for b in wanted if b in have]
                        for batch in tree.iterate(
                                use, step_size=self.chunk_size,
                                library="np"):
                            n = len(next(iter(batch.values())))
                            for i in range(n):
                                if (self.max_events is not None
                                        and n_written >= self.max_events):
                                    break
                                ev = build_event(batch, i, collections,
                                                 n_written)
                                if "event" in batch:
                                    key = (int(batch["run"][i]) if "run" in batch else 0,
                                           int(batch["luminosityBlock"][i]) if "luminosityBlock" in batch else 0,
                                           int(batch["event"][i]))
                                    ev["run"], ev["lumi"], ev["event"] = key
                                    if key in seen_ids:
                                        n_dup += 1
                                    seen_ids.add(key)
                                for k in counts:
                                    counts[k] += len(ev["data"].get(k, []))
                                fh.write(json.dumps(ev) + "\n")
                                n_written += 1
                            if (self.max_events is not None
                                    and n_written >= self.max_events):
                                break
                    if (self.max_events is not None
                            and n_written >= self.max_events):
                        break
        except Exception as e:  # noqa: BLE001
            return self.format_error(
                error="NanoAOD read failed", reason=f"{type(e).__name__}: {e}")

        result: Dict[str, Any] = {
            "status": "ok",
            "schema": SCHEMA_VERSION,
            "n_events": n_written,
            "n_files": len(inputs),
            "events_jsonl": os.path.relpath(out, self.base_directory),
            "objects_written": counts,
        }
        if n_dup:
            result["warning"] = (
                f"{n_dup} event(s) share (run, lumi, event) with an earlier "
                "one: jobs probably reused a seed or lumi block, so those "
                "events are duplicates.")
        return json.dumps(result, indent=2)

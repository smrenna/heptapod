#!/usr/bin/env python3
"""
# test_nano_to_jsonl.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

Tests for NanoAODToJSONLTool. The conversion arithmetic is tested without
uproot; the file-reading tests write a small synthetic flat-NanoAOD file with
uproot and are skipped (not failed) when uproot/numpy are absent.

Run with:
    python test_nano_to_jsonl.py
"""

import json
import math
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools.analysis.nano_to_jsonl import (
    DEFAULT_COLLECTIONS, NanoAODToJSONLTool, _p4, build_event)

try:
    import numpy as np
    import uproot
    HAVE_UPROOT = True
except ImportError:
    HAVE_UPROOT = False


def test_p4_kinematics():
    p = _p4(100.0, 0.0, 0.0, 0.0)
    assert abs(p["px"] - 100) < 1e-9 and abs(p["pz"]) < 1e-9
    assert abs(p["E"] - 100) < 1e-9
    p = _p4(50.0, 1.0, math.pi / 2, 5.0)
    assert abs(p["py"] - 50) < 1e-9
    assert abs(p["E"] ** 2 - (p["px"] ** 2 + p["py"] ** 2 + p["pz"] ** 2 + 25)) < 1e-6
    print("[OK] test_p4_kinematics")


def test_build_event_from_columns():
    cols = {
        "Electron_pt": [[120.0, 30.0], []],
        "Electron_eta": [[0.5, -1.0], []],
        "Electron_phi": [[0.1, 2.0], []],
        "Electron_mass": [[0.0, 0.0], []],
        "Electron_charge": [[-1, 1], []],
        "Jet_pt": [[200.0], [80.0]],
        "Jet_eta": [[0.0], [2.0]],
        "Jet_phi": [[3.0], [-1.0]],
        "Jet_mass": [[10.0], [8.0]],
        "MET_pt": [40.0, 10.0],
        "MET_phi": [0.0, math.pi / 2],
    }
    ev = build_event(cols, 0, DEFAULT_COLLECTIONS, event_id=7)
    d = ev["data"]
    assert ev["event_id"] == 7 and ev["schema"] == "evtjsonl-1.0"
    assert [e["pt"] for e in d["electrons"]] == [120.0, 30.0]
    assert d["electrons"][0]["charge"] == -1.0
    assert d["muons"] == [] and d["photons"] == []
    assert len(d["jets"]) == 1 and d["jets"][0]["m"] == 10.0
    assert abs(d["met"]["px"] - 40.0) < 1e-9
    assert d["n"] == 3
    ev1 = build_event(cols, 1, DEFAULT_COLLECTIONS, event_id=8)
    assert ev1["data"]["electrons"] == [] and len(ev1["data"]["jets"]) == 1
    assert abs(ev1["data"]["met"]["py"] - 10.0) < 1e-9
    print("[OK] test_build_event_from_columns")


def _write_nano(path, events, lumi=1, flat=True):
    """events: list of (electron_pts, run, event). Writes a minimal Events tree."""
    import awkward as ak
    ept = ak.Array([e[0] for e in events])
    n = len(events)
    branches = {
        "run": np.array([e[1] for e in events], dtype=np.uint32),
        "luminosityBlock": np.full(n, lumi, dtype=np.uint32),
        "event": np.array([e[2] for e in events], dtype=np.uint64),
        "MET_pt": np.full(n, 25.0, dtype=np.float32),
        "MET_phi": np.zeros(n, dtype=np.float32),
    }
    if flat:
        branches["Electron_pt"] = ept
        branches["Electron_eta"] = ak.Array([[0.3] * len(e[0]) for e in events])
        branches["Electron_phi"] = ak.Array([[1.0] * len(e[0]) for e in events])
        branches["Electron_mass"] = ak.Array([[0.0] * len(e[0]) for e in events])
    types = {k: ("var * float32" if isinstance(v, ak.Array) else v.dtype)
             for k, v in branches.items()}
    with uproot.recreate(path) as f:
        f.mktree("Events", types)  # a real TTree (a bare dict makes an RNTuple)
        f["Events"].extend(branches)


def test_tool_converts_file_and_counts():
    if not HAVE_UPROOT:
        print("[SKIP] test_tool_converts_file_and_counts (no uproot)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        _write_nano(Path(tmp, "a.root"), [([100.0, 20.0], 1, 1), ([], 1, 2),
                                          ([55.0], 1, 3)])
        res = json.loads(NanoAODToJSONLTool(base_directory=tmp).execute(
            input_files=["a.root"], output_path="out/ev.jsonl"))
        assert res["status"] == "ok" and res["n_events"] == 3
        assert res["objects_written"]["electrons"] == 3
        lines = [json.loads(l) for l in Path(tmp, "out/ev.jsonl").read_text().splitlines()]
        assert len(lines) == 3
        assert abs(lines[0]["data"]["electrons"][0]["pt"] - 100.0) < 1e-4
        assert lines[2]["event"] == 3 and lines[2]["lumi"] == 1
        assert abs(lines[0]["data"]["met"]["met"] - 25.0) < 1e-4
        assert "warning" not in res
    print("[OK] test_tool_converts_file_and_counts")


def test_tool_max_events_and_multiple_files_and_duplicates():
    if not HAVE_UPROOT:
        print("[SKIP] test_tool_max_events_and_multiple_files_and_duplicates (no uproot)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        _write_nano(Path(tmp, "a.root"), [([10.0], 1, 1), ([11.0], 1, 2)])
        _write_nano(Path(tmp, "b.root"), [([12.0], 1, 2), ([13.0], 1, 3)])  # event 2 repeats
        res = json.loads(NanoAODToJSONLTool(base_directory=tmp).execute(
            input_files=["a.root", "b.root"], output_path="ev.jsonl"))
        assert res["n_events"] == 4 and res["n_files"] == 2
        assert "duplicates" in res["warning"] and "1 event" in res["warning"]
        res = json.loads(NanoAODToJSONLTool(base_directory=tmp).execute(
            input_files=["a.root", "b.root"], output_path="ev3.jsonl",
            max_events=3, chunk_size=1))
        assert res["n_events"] == 3
    print("[OK] test_tool_max_events_and_multiple_files_and_duplicates")


def test_tool_rejects_edm_wrapped_file():
    if not HAVE_UPROOT:
        print("[SKIP] test_tool_rejects_edm_wrapped_file (no uproot)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        _write_nano(Path(tmp, "edm.root"), [([1.0], 1, 1)], flat=False)
        out = NanoAODToJSONLTool(base_directory=tmp).execute(
            input_files=["edm.root"], output_path="ev.jsonl")
        assert out.startswith("Error:") and "NANOAODSIM" in out
    print("[OK] test_tool_rejects_edm_wrapped_file")


def test_tool_rejects_bad_inputs():
    with tempfile.TemporaryDirectory() as tmp:
        t = NanoAODToJSONLTool(base_directory=tmp)
        assert "No input files" in t.execute(input_files=[], output_path="o.jsonl")
        assert "Input not found" in t.execute(input_files=["nope.root"], output_path="o.jsonl")
        Path(tmp, "x.root").write_bytes(b"")
        assert "outside sandbox" in t.execute(input_files=["x.root"], output_path="../o.jsonl")
        assert "outside sandbox" in t.execute(input_files=["../x.root"], output_path="o.jsonl")
    print("[OK] test_tool_rejects_bad_inputs")


def main():
    tests = [
        test_p4_kinematics,
        test_build_event_from_columns,
        test_tool_converts_file_and_counts,
        test_tool_max_events_and_multiple_files_and_duplicates,
        test_tool_rejects_edm_wrapped_file,
        test_tool_rejects_bad_inputs,
    ]
    ok = True
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {t.__name__}: {type(e).__name__}: {e}")
            ok = False
    print("Test suite completed successfully! [OK]" if ok
          else "Test suite completed with failures! [FAIL]")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

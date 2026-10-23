#!/usr/bin/env python3
"""
# test_histogram.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

Tests for NormalizedHistogramTool.

Run with:
    python test_histogram.py
"""

import json
import math
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools.analysis.histogram import (
    NormalizedHistogramTool, fill, values_for_event)


def _obj(pt, eta=0.0, phi=0.0, m=0.0):
    px, py, pz = pt * math.cos(phi), pt * math.sin(phi), pt * math.sinh(eta)
    return {"pt": pt, "eta": eta, "phi": phi, "m": m, "px": px, "py": py,
            "pz": pz, "E": math.sqrt(px * px + py * py + pz * pz + m * m)}


def _write(path: Path, events):
    path.write_text("".join(json.dumps({"data": e}) + "\n" for e in events))


def test_fill_edges_and_overflow():
    counts, under, over = fill([0.0, 0.99, 1.0, 9.99, 10.0, -1.0], 10, 0.0, 10.0)
    assert counts[0] == 2 and counts[1] == 1 and counts[9] == 1
    assert under == 1 and over == 1
    print("[OK] test_fill_edges_and_overflow")


def test_values_leading_all_and_alias():
    d = {"electrons": [_obj(100), _obj(50)], "jets": [_obj(80)]}
    assert values_for_event(d, "electron", "pt", 0) == [100.0]
    assert values_for_event(d, "electrons", "pt", None) == [100.0, 50.0]
    assert values_for_event(d, "electrons", "pt", 5) == []
    assert values_for_event(d, "muons", "pt", 0) == []
    assert values_for_event(d, "electrons", "pt", None, min_pt=60) == [100.0]
    print("[OK] test_values_leading_all_and_alias")


def test_inv_mass_back_to_back():
    d = {"electrons": [_obj(500, phi=0.0)], "jets": [_obj(500, phi=math.pi)]}
    m = values_for_event(d, "electrons", "inv_mass", 0, "jets", 0)
    assert len(m) == 1 and abs(m[0] - 1000.0) < 1e-6
    assert values_for_event({"electrons": [_obj(5)]}, "electrons", "inv_mass", 0, "jets", 0) == []
    pairs = values_for_event({"jets": [_obj(10), _obj(10, phi=math.pi), _obj(10, phi=1)]},
                             "jets", "inv_mass", None)
    assert len(pairs) == 3
    assert values_for_event({"jets": [_obj(10)]}, "jets", "inv_mass", 0, None, 0) == []
    print("[OK] test_inv_mass_back_to_back")


def test_tool_merges_files_and_normalises_with_total_n():
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp, "a.jsonl"), [{"electrons": [_obj(120)]}, {"electrons": []}])
        _write(Path(tmp, "b.jsonl"), [{"electrons": [_obj(130)]}, {"electrons": [_obj(950)]}])
        res = json.loads(NormalizedHistogramTool(base_directory=tmp).execute(
            input_files=["a.jsonl", "b.jsonl"], object="electrons",
            variable="pt", index=0, bins=10, range_min=0, range_max=1000,
            cross_section_pb=2.0, luminosity_fb=3.0, n_generated=4))
        w = 2.0 * 1000 * 3.0 / 4
        assert res["n_events_read"] == 4 and res["n_entries"] == 3
        assert abs(res["per_event_weight"] - w) < 1e-9
        assert res["raw_counts"][1] == 2 and res["raw_counts"][9] == 1
        assert abs(res["normalized_counts"][1] - 2 * w) < 1e-9
        assert abs(res["errors"][1] - w * math.sqrt(2)) < 1e-9
        assert abs(res["expected_yield_in_range"] - 3 * w) < 1e-9
        assert "warnings" not in res
    print("[OK] test_tool_merges_files_and_normalises_with_total_n")


def test_n_generated_default_and_warnings():
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp, "a.jsonl"), [{"electrons": [_obj(10)]}] * 5)
        t = NormalizedHistogramTool(base_directory=tmp)
        res = json.loads(t.execute(
            input_files=["a.jsonl"], object="electrons", variable="pt",
            index=0, bins=2, range_min=0, range_max=100,
            cross_section_pb=1.0, luminosity_fb=1.0))
        assert res["n_generated"] == 5 and "n_generated not given" in res["warnings"][0]
        res = json.loads(t.execute(
            input_files=["a.jsonl"], object="electrons", variable="pt",
            index=0, bins=2, range_min=500, range_max=600,
            cross_section_pb=1.0, luminosity_fb=1.0, n_generated=2))
        assert any("No entries in range" in w for w in res["warnings"])
        assert any("smaller than" in w for w in res["warnings"])
    print("[OK] test_n_generated_default_and_warnings")


def test_require_still_counts_failed_events_in_normalisation():
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp, "a.jsonl"), [{"electrons": [_obj(10)]},
                                      {"electrons": []}, {"electrons": []}])
        res = json.loads(NormalizedHistogramTool(base_directory=tmp).execute(
            input_files=["a.jsonl"], object="electrons", variable="pt",
            bins=1, range_min=0, range_max=100, cross_section_pb=3.0,
            luminosity_fb=1.0, require={"electron": 1}, output_path="o/h.json"))
        assert res["n_events_passing"] == 1 and res["n_generated"] == 3
        assert abs(res["per_event_weight"] - 1000.0) < 1e-9
        assert json.loads(Path(tmp, "o/h.json").read_text())["n_entries"] == 1
    print("[OK] test_require_still_counts_failed_events_in_normalisation")


def test_rejects_bad_inputs():
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp, "a.jsonl"), [{"electrons": []}])
        t = NormalizedHistogramTool(base_directory=tmp)
        base = dict(input_files=["a.jsonl"], object="electrons", variable="pt",
                    bins=2, range_min=0, range_max=1, cross_section_pb=1.0,
                    luminosity_fb=1.0)
        assert "Bad binning" in t.execute(**{**base, "bins": 0})
        assert "Bad binning" in t.execute(**{**base, "range_max": 0})
        assert "Bad n_generated" in t.execute(**{**base, "n_generated": 0})
        assert "Input not found" in t.execute(**{**base, "input_files": ["x.jsonl"]})
        assert "outside sandbox" in t.execute(**{**base, "output_path": "../o.json"})
        assert "Ambiguous" in t.execute(**{**base, "variable": "inv_mass", "index": 0})
        assert "No input files" in t.execute(**{**base, "input_files": []})
        Path(tmp, "e.jsonl").write_text("")
        assert "No events" in t.execute(**{**base, "input_files": ["e.jsonl"]})
    print("[OK] test_rejects_bad_inputs")


def main():
    tests = [
        test_fill_edges_and_overflow,
        test_values_leading_all_and_alias,
        test_inv_mass_back_to_back,
        test_tool_merges_files_and_normalises_with_total_n,
        test_n_generated_default_and_warnings,
        test_require_still_counts_failed_events_in_normalisation,
        test_rejects_bad_inputs,
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

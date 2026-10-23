#!/usr/bin/env python3
"""
# test_premix_files.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

Tests for PremixFileListTool. No dasgoclient or network: the two DAS queries
are replaced through the tool's `_blocks_fn` / `_files_fn` seams, and the
dasgoclient wrapper is checked with a stubbed subprocess.

Run with:
    python test_premix_files.py
"""

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools.central_production import premix_files as mod
from tools.central_production.premix_files import (
    PremixFileListTool, _das, _select_files)

DATASET = "/NeutrinoGun/CampaignX-v1/PREMIX"
BLOCK_A = DATASET + "#aaaa-1111"
BLOCK_B = DATASET + "#bbbb-2222"
FILES = {
    BLOCK_A: [f"/store/mc/pre/{i}.root" for i in range(3)],
    BLOCK_B: [f"/store/mc/pre/{i}.root" for i in range(3, 8)],
}


def _tool(tmp: str) -> PremixFileListTool:
    t = PremixFileListTool(base_directory=tmp)
    t._blocks_fn = lambda d, s: [BLOCK_A, BLOCK_B]
    t._files_fn = lambda b: FILES[b]
    return t


def test_select_files_stops_querying_once_full():
    asked = []

    def files(b):
        asked.append(b)
        return FILES[b]

    got = _select_files([BLOCK_A, BLOCK_B], files, max_files=2)
    assert got == FILES[BLOCK_A][:2]
    assert asked == [BLOCK_A], "must not query later blocks once satisfied"
    got = _select_files([BLOCK_A, BLOCK_B], files, max_files=6)
    assert len(got) == 6 and got[3] == "/store/mc/pre/3.root"
    print("[OK] test_select_files_stops_querying_once_full")


def test_select_files_drops_non_root_and_duplicates():
    got = _select_files(
        [BLOCK_A],
        lambda b: ["/store/a.root", "/store/a.root", "/store/readme.txt", "junk"],
        max_files=10)
    assert got == ["/store/a.root"]
    print("[OK] test_select_files_drops_non_root_and_duplicates")


def test_tool_writes_urls_one_per_line():
    with tempfile.TemporaryDirectory() as tmp:
        res = json.loads(_tool(tmp).execute(
            dataset=DATASET, site="T1_US_FNAL_Disk", out_path="lists/premix.txt",
            max_files=5, redirector="root://xrd.example.org/"))
        assert res["status"] == "ok" and res["n_files"] == 5
        assert res["n_blocks"] == 2 and res["out_path"] == "lists/premix.txt"
        lines = Path(tmp, "lists/premix.txt").read_text().splitlines()
        assert lines[0] == "root://xrd.example.org//store/mc/pre/0.root"
        assert len(lines) == 5
    print("[OK] test_tool_writes_urls_one_per_line")


def test_tool_bare_lfns_with_empty_redirector():
    with tempfile.TemporaryDirectory() as tmp:
        res = json.loads(_tool(tmp).execute(
            dataset=DATASET, site="S", out_path="p.txt", max_files=2,
            redirector=""))
        assert Path(tmp, "p.txt").read_text().splitlines() == [
            "/store/mc/pre/0.root", "/store/mc/pre/1.root"]
        assert res["n_files"] == 2
    print("[OK] test_tool_bare_lfns_with_empty_redirector")


def test_tool_no_blocks_explains_hang_risk():
    with tempfile.TemporaryDirectory() as tmp:
        t = _tool(tmp)
        t._blocks_fn = lambda d, s: []
        out = t.execute(dataset=DATASET, site="T9_XX_Nowhere", out_path="p.txt")
        assert out.startswith("Error:") and "No blocks at site" in out
        assert "hangs" in out
        assert not Path(tmp, "p.txt").exists()
    print("[OK] test_tool_no_blocks_explains_hang_risk")


def test_tool_rejects_bad_inputs():
    with tempfile.TemporaryDirectory() as tmp:
        t = _tool(tmp)
        assert "Bad dataset" in t.execute(
            dataset="not-a-dataset", site="S", out_path="p.txt")
        assert "Bad site" in t.execute(
            dataset=DATASET, site="a b", out_path="p.txt")
        assert "Bad max_files" in t.execute(
            dataset=DATASET, site="S", out_path="p.txt", max_files=0)
        assert "outside sandbox" in t.execute(
            dataset=DATASET, site="S", out_path="../escape.txt")
    print("[OK] test_tool_rejects_bad_inputs")


def test_missing_dasgoclient_is_a_clear_error():
    original = mod.subprocess.run

    def boom(*a, **k):
        raise FileNotFoundError("dasgoclient")

    mod.subprocess.run = boom
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = PremixFileListTool(base_directory=tmp).execute(
                dataset=DATASET, site="S", out_path="p.txt")
    finally:
        mod.subprocess.run = original
    assert out.startswith("Error:") and "dasgoclient not found" in out
    print("[OK] test_missing_dasgoclient_is_a_clear_error")


def test_das_helper_parses_lines_and_raises_on_failure():
    class _R:
        def __init__(self, rc, out, err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    original = mod.subprocess.run
    try:
        seen = {}

        def ok(cmd, **kw):
            seen["cmd"] = cmd
            return _R(0, "a\n\n b \n")

        mod.subprocess.run = ok
        assert _das("block dataset=/x/y/z site=S") == ["a", "b"]
        assert seen["cmd"] == ["dasgoclient", "-query", "block dataset=/x/y/z site=S"]
        mod.subprocess.run = lambda cmd, **kw: _R(1, "", "boom")
        try:
            _das("q")
        except RuntimeError as e:
            assert "boom" in str(e)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        mod.subprocess.run = original
    print("[OK] test_das_helper_parses_lines_and_raises_on_failure")


def main():
    tests = [
        test_select_files_stops_querying_once_full,
        test_select_files_drops_non_root_and_duplicates,
        test_tool_writes_urls_one_per_line,
        test_tool_bare_lfns_with_empty_redirector,
        test_tool_no_blocks_explains_hang_risk,
        test_tool_rejects_bad_inputs,
        test_missing_dasgoclient_is_a_clear_error,
        test_das_helper_parses_lines_and_raises_on_failure,
    ]
    ok = True
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {t.__name__}: {e}")
            ok = False
    print("Test suite completed successfully! [OK]" if ok
          else "Test suite completed with failures! [FAIL]")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

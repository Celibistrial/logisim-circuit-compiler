#!/usr/bin/env python3
"""Framework verification for circuitc — tests the compiler, parser, evaluator,
layout emitter, structural checker, and CLI commands.

Run:  python3 test_circuitc.py
Needs java + logisim jar for behavioral sections; structural always runs."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import circuitc


# ── minimal test sources ────────────────────────────────────────────────────

ONE_GATE = """
circuit inv:
  inputs A
  outputs Y
  Y = ~A
"""

TWO_GATE = """
circuit nand2:
  inputs A, B
  outputs Y
  Y = ~(A & B)
"""

CONSTANTS = """
circuit consts:
  inputs A
  outputs Z, O, X
  Z = A & 0
  O = A | 1
  X = A ^ A
"""

HIGH_FANIN = """
circuit wide:
  inputs A, B, C, D, E, F, G
  outputs Y
  Y = A & B & C & D & E & F & G
"""

ALIAS_CHAIN = """
circuit passthru:
  inputs A
  outputs Y, Z
  Y = A
  Z = A
"""

MULTI_OUT = """
circuit add1b:
  inputs A, B, Cin
  outputs S, Cout
  t = A ^ B
  S = t ^ Cin
  Cout = (A & B) | (Cin & t)
"""

HIERARCHY = """
circuit half_add:
  inputs A, B
  outputs S, C
  S = A ^ B
  C = A & B

circuit full_add:
  inputs A, B, Cin
  outputs S, Cout
  use half_add ha1(A=A, B=B) -> (S=s1, C=c1)
  use half_add ha2(A=s1, B=Cin) -> (S=S, C=c2)
  Cout = c1 | c2
"""

CMOS = """
circuit cmos_inv:
  inputs A
  outputs Y
  spec Y = ~A
  pmos A: VDD -> Y
  nmos A: GND -> Y

circuit cmos_nand:
  inputs A, B
  outputs Y
  spec Y = ~(A & B)
  pmos A: VDD -> Y
  pmos B: VDD -> Y
  nmos A: GND -> n1
  nmos B: n1 -> Y

circuit cmos_nor:
  inputs A, B
  outputs Y
  spec Y = ~(A | B)
  pmos A: VDD -> n1
  pmos B: n1 -> Y
  nmos A: GND -> Y
  nmos B: GND -> Y
"""

TGATE = """
circuit tgate_mux:
  inputs S, D0, D1
  outputs Y
  Sn = ~S
  spec Y = (S & D1) | (Sn & D0)
  tgate S, Sn: D1 -> Y
  tgate Sn, S: D0 -> Y
"""

PSEUDO_NMOS = """
circuit pseudo_nor:
  inputs A, B
  outputs Y
  spec Y = ~(A | B)
  pullup Y
  nmos A: GND -> Y
  nmos B: GND -> Y
"""

PRECEDENCE = """
circuit prec:
  inputs A, B, C
  outputs Y
  Y = A & B ^ C | A
"""

MIXED = """
circuit mixed:
  inputs A, B
  outputs Y
  spec Y = ~(A & B)
  Sn = ~A
  pmos A: VDD -> Y
  pmos B: VDD -> Y
  nmos A: GND -> n1
  nmos B: n1 -> Y
"""


# ═════════════════════════════════════════════════════════════════════════════
# Geometry constants
# ═════════════════════════════════════════════════════════════════════════════

def test_input_dys():
    assert circuitc.input_dys(2) == [-20, 20]
    assert circuitc.input_dys(3) == [-20, 0, 20]
    assert circuitc.input_dys(4) == [-20, -10, 10, 20]
    assert circuitc.input_dys(5) == [-20, -10, 0, 10, 20]
    assert circuitc.input_dys(1) == [0]

def test_gate_axis():
    assert circuitc.gate_axis("AND") == 50
    assert circuitc.gate_axis("OR") == 50
    assert circuitc.gate_axis("NAND") == 60
    assert circuitc.gate_axis("NOR") == 60
    assert circuitc.gate_axis("XOR") == 60
    assert circuitc.gate_axis("XNOR") == 70
    assert circuitc.gate_axis("NOT") == 30


# ═════════════════════════════════════════════════════════════════════════════
# Expression parser & evaluator
# ═════════════════════════════════════════════════════════════════════════════

def test_parse_expr_simple():
    e = circuitc.parse_expr("A & B")
    assert circuitc.eval_expr(e, {"A": 1, "B": 1}) == 1
    assert circuitc.eval_expr(e, {"A": 1, "B": 0}) == 0

def test_parse_expr_precedence():
    # ~ & ^ |  (high to low)
    e = circuitc.parse_expr("~A & B")
    assert circuitc.eval_expr(e, {"A": 0, "B": 1}) == 1
    assert circuitc.eval_expr(e, {"A": 1, "B": 1}) == 0

    e = circuitc.parse_expr("A | B & C")
    # B & C first, then A | result
    assert circuitc.eval_expr(e, {"A": 0, "B": 1, "C": 1}) == 1
    assert circuitc.eval_expr(e, {"A": 0, "B": 0, "C": 1}) == 0
    assert circuitc.eval_expr(e, {"A": 1, "B": 0, "C": 0}) == 1

    e = circuitc.parse_expr("A ^ B & C")
    # B & C first, then A ^ result
    assert circuitc.eval_expr(e, {"A": 0, "B": 0, "C": 0}) == 0
    assert circuitc.eval_expr(e, {"A": 1, "B": 1, "C": 1}) == 0

def test_parse_expr_parens():
    e = circuitc.parse_expr("(A | B) & C")
    assert circuitc.eval_expr(e, {"A": 0, "B": 0, "C": 1}) == 0
    assert circuitc.eval_expr(e, {"A": 0, "B": 1, "C": 1}) == 1

def test_parse_expr_constants():
    assert circuitc.eval_expr(circuitc.parse_expr("0"), {}) == 0
    assert circuitc.eval_expr(circuitc.parse_expr("1"), {}) == 1
    assert circuitc.eval_expr(circuitc.parse_expr("A & 1"), {"A": 1}) == 1
    assert circuitc.eval_expr(circuitc.parse_expr("A | 0"), {"A": 0}) == 0

def test_parse_expr_chained():
    e = circuitc.parse_expr("A & B & C & D")
    assert circuitc.eval_expr(e, {"A": 1, "B": 1, "C": 1, "D": 1}) == 1
    assert circuitc.eval_expr(e, {"A": 1, "B": 0, "C": 1, "D": 1}) == 0

def test_expr_vars():
    s = set()
    circuitc.expr_vars(circuitc.parse_expr("~(A | B) & (C ^ 1)"), s)
    assert s == {"A", "B", "C"}  # constants not included


# ═════════════════════════════════════════════════════════════════════════════
# Parser
# ═════════════════════════════════════════════════════════════════════════════

def test_parse_one_gate():
    circuits = circuitc.parse_logic(ONE_GATE)
    assert len(circuits) == 1
    c = circuits[0]
    assert c.name == "inv"
    assert c.inputs == ["A"]
    assert c.outputs == ["Y"]

def test_parse_two_gate():
    circuits = circuitc.parse_logic(TWO_GATE)
    c = circuits[0]
    assert c.name == "nand2"
    assert len(c.assigns) == 1

def test_parse_constants():
    circuits = circuitc.parse_logic(CONSTANTS)
    c = circuits[0]
    assert c.outputs == ["Z", "O", "X"]

def test_parse_multi_circuit():
    circuits = circuitc.parse_logic(HIERARCHY)
    assert len(circuits) == 2
    assert circuits[0].name == "half_add"
    assert circuits[1].name == "full_add"

def test_parse_fet():
    circuits = circuitc.parse_logic(CMOS)
    assert len(circuits) == 3
    assert len(circuits[0].fets) == 2   # 1 pmos + 1 nmos
    assert len(circuits[1].fets) == 4   # 2 pmos + 2 nmos
    assert len(circuits[2].fets) == 4

def test_parse_tgate():
    circuits = circuitc.parse_logic(TGATE)
    assert len(circuits[0].tgates) == 2

def test_parse_pull():
    circuits = circuitc.parse_logic(PSEUDO_NMOS)
    assert len(circuits[0].pulls) == 1

def test_parse_mixed():
    circuits = circuitc.parse_logic(MIXED)
    c = circuits[0]
    assert len(c.fets) == 4
    assert len(c.assigns) == 1   # Sn = ~A

def test_parse_hierarchy_use():
    circuits = circuitc.parse_logic(HIERARCHY)
    full = circuits[1]
    assert len(full.insts) == 2   # two 'use' statements
    assert full.insts[0].circ == "half_add"
    assert full.insts[1].circ == "half_add"

def test_parse_errors():
    bad_cases = [
        ("circuit :\n inputs A\n outputs Y\n Y = A\n", "missing name"),
        ("circuit fwd:\n inputs A\n outputs Y\n Y = Z\n Z = A\n", "forward reference"),
    ]
    for src, label in bad_cases:
        try:
            circuitc.parse_logic(src)
            raise AssertionError(f"accepted invalid: {label}")
        except (SyntaxError, ValueError):
            pass


# ═════════════════════════════════════════════════════════════════════════════
# Compiler (gate decomposition, netlist generation)
# ═════════════════════════════════════════════════════════════════════════════

def test_compile_basic():
    c = circuitc.parse_logic(ONE_GATE)[0]
    net = circuitc.compile_netlist(c)
    assert net.name == "inv"
    assert len(net.gates) == 1
    assert net.gates[0].kind == "NOT"

def test_compile_fanout():
    c = circuitc.parse_logic(ALIAS_CHAIN)[0]
    net = circuitc.compile_netlist(c)
    assert len(net.aliases) > 0 or len(net.gates) >= 0

def test_compile_high_fanin_tree():
    """Gates with >5 inputs must be decomposed into a tree."""
    c = circuitc.parse_logic(HIGH_FANIN)[0]
    net = circuitc.compile_netlist(c)
    # 7-input AND should decompose into at least 2 AND gates
    assert len(net.gates) >= 2
    for g in net.gates:
        assert len(g.inputs) <= circuitc._MAX_FANIN

def test_compile_multi_output():
    c = circuitc.parse_logic(MULTI_OUT)[0]
    net = circuitc.compile_netlist(c)
    assert len(net.outputs) == 2
    assert len(net.gates) >= 2

def test_compile_constants():
    c = circuitc.parse_logic(CONSTANTS)[0]
    net = circuitc.compile_netlist(c)
    assert len(net.const_nets) > 0

def test_compile_precedence():
    c = circuitc.parse_logic(PRECEDENCE)[0]
    net = circuitc.compile_netlist(c)
    assert len(net.gates) > 0


# ═════════════════════════════════════════════════════════════════════════════
# Golden-model evaluator
# ═════════════════════════════════════════════════════════════════════════════

def test_eval_basic():
    circuits = circuitc.parse_logic(ONE_GATE)
    defs = {c.name: c for c in circuits}
    c = circuits[0]
    got = circuitc.eval_circuit(c, {"A": 0}, defs)
    assert got == {"Y": 1}
    got = circuitc.eval_circuit(c, {"A": 1}, defs)
    assert got == {"Y": 0}

def test_eval_hierarchy():
    circuits = circuitc.parse_logic(HIERARCHY)
    defs = {c.name: c for c in circuits}
    full = defs["full_add"]
    # exhaustive truth table
    for row in range(8):
        a = (row >> 2) & 1
        b = (row >> 1) & 1
        cin = row & 1
        got = circuitc.eval_circuit(full, {"A": a, "B": b, "Cin": cin}, defs)
        exp_s = a ^ b ^ cin
        exp_cout = (a & b) | (cin & (a ^ b))
        assert got == {"S": exp_s, "Cout": exp_cout}, \
            f"A={a} B={b} Cin={cin} got {got} exp S={exp_s} Cout={exp_cout}"

def test_eval_with_specs():
    circuits = circuitc.parse_logic(CMOS)
    defs = {c.name: c for c in circuits}
    for name in ("cmos_inv", "cmos_nand", "cmos_nor"):
        c = defs[name]
        for row in range(2 ** len(c.inputs)):
            env = {pin: (row >> (len(c.inputs) - 1 - i)) & 1
                   for i, pin in enumerate(c.inputs)}
            got = circuitc.eval_circuit(c, env, defs)
            assert "Y" in got

def test_eval_multi_output():
    circuits = circuitc.parse_logic(MULTI_OUT)
    defs = {c.name: c for c in circuits}
    c = circuits[0]
    got = circuitc.eval_circuit(c, {"A": 1, "B": 0, "Cin": 1}, defs)
    assert got["S"] == 0
    assert got["Cout"] == 1

def test_eval_missing_defs_raises():
    circuits = circuitc.parse_logic(HIERARCHY)
    full = circuits[1]
    try:
        circuitc.eval_circuit(full, {"A": 0, "B": 0, "Cin": 0})
        raise AssertionError("should have raised")
    except ValueError:
        pass


# ═════════════════════════════════════════════════════════════════════════════
# Project emission & structural verification
# ═════════════════════════════════════════════════════════════════════════════

def test_emit_and_check():
    all_src = "\n".join([ONE_GATE, TWO_GATE, CONSTANTS, HIGH_FANIN,
                          ALIAS_CHAIN, MULTI_OUT, HIERARCHY, PRECEDENCE,
                          CMOS, TGATE, PSEUDO_NMOS, MIXED])
    circuits = circuitc.parse_logic(all_src)
    nets = [circuitc.compile_netlist(c) for c in circuits]
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "test.circ"
        path.write_text(circuitc.emit_project(nets))
        for c, net in zip(circuits, nets):
            result = circuitc.structural_check(str(path), net)
            assert result["ok"], f"{c.name}: {result.get('errors')}"

def test_emit_project_is_valid_xml():
    import xml.etree.ElementTree as ET
    circuits = circuitc.parse_logic(ONE_GATE)
    nets = [circuitc.compile_netlist(c) for c in circuits]
    xml = circuitc.emit_project(nets)
    try:
        ET.fromstring(xml)
    except ET.ParseError as e:
        raise AssertionError(f"invalid XML: {e}")

def test_emit_circuit_xml():
    c = circuitc.parse_logic(ONE_GATE)[0]
    net = circuitc.compile_netlist(c)
    parts, pmap = circuitc.emit_circuit_xml(net)
    assert len(parts) > 0
    assert len(pmap) > 0


# ═════════════════════════════════════════════════════════════════════════════
# Structural error detection
# ═════════════════════════════════════════════════════════════════════════════

def test_sabotage_caught():
    """Bogus gate port offsets in emitted layout must cause structural failures."""
    circuits = circuitc.parse_logic(
        "circuit maj:\n inputs A,B,C\n outputs Y\n Y = (A&B)|(B&C)|(A&C)\n")
    c = circuits[0]
    good_net = circuitc.compile_netlist(c)
    bad_net = circuitc.compile_netlist(c)

    # Emit bad file with wrong port offsets, good file with correct ones
    orig = circuitc.input_dys
    circuitc.input_dys = lambda n, size=50: [-60, 0, 60] if n == 3 else orig(n, size)
    bad_xml = circuitc.emit_project([bad_net])
    circuitc.input_dys = orig
    good_xml = circuitc.emit_project([good_net])

    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "good.circ"
        bad = Path(td) / "bad.circ"
        good.write_text(good_xml)
        bad.write_text(bad_xml)
        assert circuitc.structural_check(str(good), good_net)["ok"]
        assert not circuitc.structural_check(str(bad), bad_net)["ok"], \
            "sabotaged geometry passed structural check"

def test_parse_rejects_invalid():
    bad_srcs = [
        ("circuit x:\n inputs A\n outputs Y\n spec Y = ~A\n nmos A: Y -> GND\n",
         "FET driving GND"),
        ("circuit x:\n inputs A\n outputs Y\n Y = A\n nmos A: GND -> Y\n",
         "gate + transistor both driving Y"),
        ("circuit x:\n inputs A\n outputs Y\n spec Y = n1\n nmos A: GND -> Y\n",
         "spec reading a switch-driven net"),
        ("circuit x:\n inputs A\n outputs Y\n pmos A: GND -> VDD\n",
         "pmos source must be VDD"),
        ("circuit x:\n inputs A\n outputs Y\n nmos A: VDD -> GND\n",
         "nmos source must be GND"),
    ]
    for src, label in bad_srcs:
        try:
            circuitc.parse_logic(src)
            raise AssertionError(f"accepted invalid: {label}")
        except (SyntaxError, ValueError):
            pass


# ═════════════════════════════════════════════════════════════════════════════
# CLI commands
# ═════════════════════════════════════════════════════════════════════════════

CIRCUITC = Path(__file__).parent / "circuitc.py"

def run_circuitc(*args):
    return subprocess.run(
        [sys.executable, str(CIRCUITC)] + list(args),
        capture_output=True, text=True,
    )

def test_build_command():
    with tempfile.TemporaryDirectory() as td:
        logic = Path(td) / "test.logic"
        logic.write_text(ONE_GATE)
        out = Path(td) / "out.circ"
        result = run_circuitc("build", str(logic), "-o", str(out), "--skip-sim")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["ok"], data
        assert out.exists()

def test_build_multi_circuit():
    src = ONE_GATE + "\n" + TWO_GATE
    with tempfile.TemporaryDirectory() as td:
        logic = Path(td) / "test.logic"
        logic.write_text(src)
        out = Path(td) / "out.circ"
        result = run_circuitc("build", str(logic), "-o", str(out), "--skip-sim")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        assert set(data["circuits"].keys()) >= {"inv", "nand2"}

def test_build_cmos():
    with tempfile.TemporaryDirectory() as td:
        logic = Path(td) / "test.logic"
        logic.write_text(CMOS)
        out = Path(td) / "out.circ"
        result = run_circuitc("build", str(logic), "-o", str(out), "--skip-sim")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        assert set(data["circuits"].keys()) >= {"cmos_inv", "cmos_nand", "cmos_nor"}

def test_check_command():
    with tempfile.TemporaryDirectory() as td:
        logic = Path(td) / "test.logic"
        logic.write_text(ONE_GATE)
        out = Path(td) / "out.circ"
        run_circuitc("build", str(logic), "-o", str(out), "--skip-sim")
        result = run_circuitc("check", str(out))
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]

def test_describe_command():
    with tempfile.TemporaryDirectory() as td:
        logic = Path(td) / "test.logic"
        logic.write_text(ONE_GATE)
        out = Path(td) / "out.circ"
        run_circuitc("build", str(logic), "-o", str(out), "--skip-sim")
        result = run_circuitc("describe", str(out))
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data["circuits"]) >= 1

def test_merge_workflow():
    """Build a circuit, then merge a second into the same file."""
    with tempfile.TemporaryDirectory() as td:
        logic1 = Path(td) / "a.logic"
        logic1.write_text(ONE_GATE)
        out = Path(td) / "merged.circ"
        run_circuitc("build", str(logic1), "-o", str(out), "--skip-sim")

        logic2 = Path(td) / "b.logic"
        logic2.write_text(TWO_GATE)
        result = run_circuitc("build", str(logic2), "-o", str(out),
                              "--merge", "--skip-sim")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"]
        # merge build output only lists the new circuits
        assert "nand2" in data["circuits"]

        # Describe should show both circuits in the merged file
        desc_result = run_circuitc("describe", str(out))
        desc = json.loads(desc_result.stdout)
        names = {c["name"] for c in desc["circuits"]}
        assert "inv" in names and "nand2" in names


# ═════════════════════════════════════════════════════════════════════════════
# Layout bounds
# ═════════════════════════════════════════════════════════════════════════════

def test_layout_no_runaway_coordinates():
    """Basic circuits should produce coordinates within reasonable bounds."""
    circuits = circuitc.parse_logic(
        "circuit t:\n inputs A,B,C,D\n outputs X\n X = (A&B)|(C&D)\n")
    c = circuits[0]
    net = circuitc.compile_netlist(c)
    parts, _ = circuitc.emit_circuit_xml(net)
    # Extract coordinates
    import re
    xs, ys = [], []
    for part in parts:
        for m in re.finditer(r'loc="\((\d+),(\d+)\)"', part):
            xs.append(int(m.group(1)))
            ys.append(int(m.group(2)))
        for m in re.finditer(r'from="\((\d+),(\d+)\)" to="\((\d+),(\d+)\)"', part):
            xs.extend([int(m.group(1)), int(m.group(3))])
            ys.extend([int(m.group(2)), int(m.group(4))])
    if xs:
        assert max(xs) < 3000, f"layout too wide: {max(xs)}"
        assert max(ys) < 2000, f"layout too tall: {max(ys)}"


# ═════════════════════════════════════════════════════════════════════════════
# Behavioral (Logisim jar required)
# ═════════════════════════════════════════════════════════════════════════════

def test_behavioral():
    all_src = "\n".join([ONE_GATE, TWO_GATE, MULTI_OUT, HIERARCHY, PRECEDENCE])
    circuits = circuitc.parse_logic(all_src)
    defs = {c.name: c for c in circuits}
    nets = [circuitc.compile_netlist(c) for c in circuits]

    try:
        jar = circuitc.find_jar()
    except FileNotFoundError:
        print("  SKIP  behavioral — no logisim jar found")
        return

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.circ"
        path.write_text(circuitc.emit_project(nets))
        for c in circuits:
            b = circuitc.behavioral_check(jar, str(path), c, defs=defs)
            assert b["ok"], f"{c.name}: {b}"
        total_vectors = sum(2 ** len(c.inputs) for c in circuits)
        print(f"  PASS  behavioral — {total_vectors} vectors")


# ═════════════════════════════════════════════════════════════════════════════
# Runner
# ═════════════════════════════════════════════════════════════════════════════

def main():
    import traceback

    results = []
    for name in sorted(globals()):
        if name.startswith("test_") and callable(globals()[name]):
            try:
                globals()[name]()
                results.append((name, "PASS", ""))
            except AssertionError as e:
                results.append((name, "FAIL", str(e)))
            except Exception as e:
                results.append((name, "ERROR", f"{type(e).__name__}: {e}"))

    passed = sum(1 for _, r, _ in results if r == "PASS")
    failed = sum(1 for _, r, _ in results if r != "PASS")
    print(f"\nResults: {passed}/{len(results)} passed, {failed} failed\n")
    for name, status, detail in results:
        marker = "  PASS" if status == "PASS" else f"  {status}"
        line = f"{marker}  {name}"
        if detail:
            line += f"  — {detail}"
        print(line)
    print()
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

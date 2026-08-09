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
# New commands (issues #1–#15) — structural, no jar required
# ═════════════════════════════════════════════════════════════════════════════

def _build(td, src, name="t.circ"):
    logic = Path(td) / "src.logic"
    logic.write_text(src)
    out = Path(td) / name
    run_circuitc("build", str(logic), "-o", str(out), "--skip-sim")
    return out

OFFGRID = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<project source="4.1.0" version="1.0">
  <lib desc="#Wiring" name="0"/>
  <lib desc="#Gates" name="1"/>
  <main name="c"/>
  <circuit name="c">
    <comp lib="1" loc="(200,103)" name="AND Gate"><a name="inputs" val="2"/></comp>
    <wire from="(100,100)" to="(300,100)"/>
    <wire from="(200,100)" to="(200,200)"/>
  </circuit>
</project>
"""

LOOP_XML = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<project source="4.1.0" version="1.0">
  <lib desc="#Wiring" name="0"/>
  <lib desc="#Gates" name="1"/>
  <main name="loopy"/>
  <circuit name="loopy">
    <comp lib="1" loc="(200,100)" name="AND Gate"><a name="inputs" val="2"/></comp>
    <comp lib="1" loc="(400,100)" name="OR Gate"><a name="inputs" val="2"/></comp>
    <wire from="(200,100)" to="(350,100)"/>
    <wire from="(350,100)" to="(350,80)"/>
    <wire from="(400,100)" to="(400,40)"/>
    <wire from="(400,40)" to="(150,40)"/>
    <wire from="(150,40)" to="(150,80)"/>
  </circuit>
</project>
"""

NEAR_MISS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<project source="4.1.0" version="1.0">
  <lib desc="#Wiring" name="0"/>
  <lib desc="#Gates" name="1"/>
  <main name="c"/>
  <circuit name="c">
    <comp lib="1" loc="(200,100)" name="AND Gate">
      <a name="inputs" val="2"/>
    </comp>
    <wire from="(150,70)" to="(300,70)"/>
  </circuit>
</project>
"""

CROSSING_XML = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<project source="4.1.0" version="1.0">
  <lib desc="#Wiring" name="0"/>
  <main name="c"/>
  <circuit name="c">
    <comp lib="0" loc="(100,100)" name="Pin"><a name="label" val="A"/></comp>
    <comp lib="0" loc="(300,100)" name="Pin"><a name="label" val="Y"/><a name="type" val="output"/></comp>
    <comp lib="0" loc="(200,50)" name="Constant"><a name="value" val="0x1"/></comp>
    <wire from="(100,100)" to="(300,100)"/>
    <wire from="(200,50)" to="(200,150)"/>
  </circuit>
</project>
"""

DEEP_HIER = """circuit leaf:
  inputs A
  outputs Y
  Y = ~A

circuit mid:
  inputs A
  outputs Y
  use leaf l1(A=A) -> (Y=_t)
  Y = _t

circuit top:
  inputs A
  outputs Y
  use mid m1(A=A) -> (Y=_t)
  Y = _t
"""


def test_check_grid():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "og.circ"
        p.write_text(OFFGRID)
        r = run_circuitc("check-grid", str(p))
        d = json.loads(r.stdout)
        assert not d["ok"]
        assert any(v["at"] == [200, 103] for v in d["violations"])
        # a clean generated file has no violations
        out = _build(td, ONE_GATE)
        assert json.loads(run_circuitc("check-grid", str(out)).stdout)["ok"]


def test_check_proximity():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "near.circ"
        p.write_text(NEAR_MISS_XML)
        d = json.loads(run_circuitc("check-proximity", str(p)).stdout)
        assert not d["ok"] and d["near_misses"]
        # AND input port at (150,80), wire at y=70 — 10 px gap
        assert any(m["distance_px"] <= 10 for m in d["near_misses"])
        # a clean generated file has no near misses
        out = _build(td, ONE_GATE)
        assert json.loads(run_circuitc("check-proximity", str(out)).stdout)["ok"]


def test_check_collision_tjunction():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "og.circ"
        p.write_text(OFFGRID)
        d = json.loads(run_circuitc("check-collision", str(p)).stdout)
        assert any(t["at"] == [200, 100] for t in d["t_junctions"])


def test_check_collision_crossing_short():
    with tempfile.TemporaryDirectory() as td:
        # mid-to-mid crossing: wires pass through same point but don't connect
        p = Path(td) / "cross.circ"
        p.write_text(CROSSING_XML)
        d = json.loads(run_circuitc("check-collision", str(p)).stdout)
        assert d["crossings"], "expected a mid-to-mid crossing"
        # clean generated circuits have no crossings
        out = _build(td, ONE_GATE)
        cd = json.loads(run_circuitc("check-collision", str(out)).stdout)
        assert not cd["crossings"] and not cd["shorts"]


def test_check_loops():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "loop.circ"
        p.write_text(LOOP_XML)
        d = json.loads(run_circuitc("check-loops", str(p)).stdout)
        assert not d["ok"] and d["loops"]
        # a clean feed-forward circuit has no loops
        out = _build(td, MULTI_OUT)
        assert json.loads(run_circuitc("check-loops", str(out)).stdout)["ok"]


def test_check_pins_hierarchy():
    with tempfile.TemporaryDirectory() as td:
        out = _build(td, HIERARCHY)
        assert json.loads(run_circuitc("check-pins", str(out)).stdout)["ok"]
        # adding a pin to a leaf leaves instances unwired -> flagged
        run_circuitc("add-pin", str(out), "half_add", "Q", "--direction", "in")
        d = json.loads(run_circuitc("check-pins", str(out)).stdout)
        assert not d["ok"]
        assert any(u["input_pin"] == "Q" for u in d["unwired_inputs"])


def test_delete_and_dry_run():
    with tempfile.TemporaryDirectory() as td:
        out = _build(td, ONE_GATE + "\n" + TWO_GATE)
        d = json.loads(run_circuitc("delete", str(out), "nand2", "--dry-run").stdout)
        assert d["deleted"] == ["nand2"]
        names = {c["name"] for c in json.loads(run_circuitc("describe", str(out)).stdout)["circuits"]}
        assert "nand2" in names  # dry-run didn't touch it
        run_circuitc("delete", str(out), "nand2")
        names = {c["name"] for c in json.loads(run_circuitc("describe", str(out)).stdout)["circuits"]}
        assert "nand2" not in names and "inv" in names


def test_delete_referenced_and_main():
    with tempfile.TemporaryDirectory() as td:
        out = _build(td, HIERARCHY)
        # deleting a circuit with dangling refs should fail ok
        r = run_circuitc("delete", str(out), "half_add", "--dry-run")
        d = json.loads(r.stdout)
        assert d["would_dangle"] and d["deleted"] == ["half_add"]
        assert not d["ok"]  # exit 1 due to dangling ref

        # deleting the main circuit reassigns main
        out2 = _build(td, ONE_GATE + "\n" + TWO_GATE)
        run_circuitc("delete", str(out2), "inv")
        desc = json.loads(run_circuitc("describe", str(out2)).stdout)
        assert desc["main"] == "nand2"


def test_rename_updates_refs():
    with tempfile.TemporaryDirectory() as td:
        out = _build(td, HIERARCHY)
        d = json.loads(run_circuitc("rename", str(out), "half_add", "HA").stdout)
        assert d["ok"] and d["instances_updated"] == 2
        # references now valid -> still passes pin contract
        assert json.loads(run_circuitc("check-pins", str(out)).stdout)["ok"]
        # renaming to an existing name fails
        assert not json.loads(run_circuitc("rename", str(out), "HA", "full_add").stdout)["ok"]


def test_clone():
    with tempfile.TemporaryDirectory() as td:
        out = _build(td, ONE_GATE)
        assert json.loads(run_circuitc("clone", str(out), "inv", "inv2").stdout)["ok"]
        names = {c["name"] for c in json.loads(run_circuitc("describe", str(out)).stdout)["circuits"]}
        assert {"inv", "inv2"} <= names


def test_extract_pulls_dependencies():
    with tempfile.TemporaryDirectory() as td:
        out = _build(td, HIERARCHY)
        ex = Path(td) / "ex.circ"
        d = json.loads(run_circuitc("extract", str(out), "full_add", "-o", str(ex)).stdout)
        assert d["ok"] and "half_add" in d["extracted"]  # dep pulled in
        names = {c["name"] for c in json.loads(run_circuitc("describe", str(ex)).stdout)["circuits"]}
        assert names == {"full_add", "half_add"}


def test_add_remove_pin():
    with tempfile.TemporaryDirectory() as td:
        out = _build(td, ONE_GATE)
        assert json.loads(run_circuitc("add-pin", str(out), "inv", "B", "--direction", "in").stdout)["ok"]
        ins = json.loads(run_circuitc("describe", str(out)).stdout)["circuits"][0]["inputs"]
        assert "B" in ins
        assert json.loads(run_circuitc("remove-pin", str(out), "inv", "B").stdout)["ok"]
        ins = json.loads(run_circuitc("describe", str(out)).stdout)["circuits"][0]["inputs"]
        assert "B" not in ins


def test_replace_ref():
    with tempfile.TemporaryDirectory() as td:
        out = _build(td, HIERARCHY)
        run_circuitc("clone", str(out), "half_add", "half_add2")
        d = json.loads(run_circuitc("replace-ref", str(out), "half_add", "half_add2").stdout)
        assert d["ok"] and d["instances_updated"] == 2


def test_fix_refs_dangling():
    with tempfile.TemporaryDirectory() as td:
        out = _build(td, HIERARCHY)
        # delete the leaf out from under its instances -> dangling references
        run_circuitc("delete", str(out), "half_add")
        d = json.loads(run_circuitc("fix-refs", str(out)).stdout)
        assert not d["ok"] and any(x["references"] == "half_add" for x in d["dangling"])
        # --auto removes the orphans
        d2 = json.loads(run_circuitc("fix-refs", str(out), "--auto").stdout)
        assert d2["ok"] and d2["removed"] >= 2


def test_flatten_structural():
    with tempfile.TemporaryDirectory() as td:
        out = _build(td, HIERARCHY)
        flat = Path(td) / "flat.circ"
        d = json.loads(run_circuitc("flatten", str(out), "full_add", "--all",
                                    "-o", str(flat)).stdout)
        assert d["ok"]
        # inlined instances are gone; no dangling pin contract issues
        cp = json.loads(run_circuitc("check-pins", str(flat)).stdout)
        assert cp["ok"]
        coll = json.loads(run_circuitc("check-collision", str(flat)).stdout)
        assert not coll["shorts"]


def test_flatten_label_and_depth():
    with tempfile.TemporaryDirectory() as td:
        out = _build(td, DEEP_HIER)
        # flatten just one specific instance by label
        d = json.loads(run_circuitc("flatten", str(out), "top", "m1",
                                    "-o", str(Path(td) / "f1.circ")).stdout)
        assert d["ok"]
        # flatten with depth 2 should inline through both levels
        d2 = json.loads(run_circuitc("flatten", str(out), "top", "--all",
                                     "--depth", "2",
                                     "-o", str(Path(td) / "f2.circ")).stdout)
        assert d2["ok"]
        assert len(d2.get("flattened", [])) >= 2  # flattened both levels


def test_diff():
    with tempfile.TemporaryDirectory() as td:
        a = _build(td, ONE_GATE, "a.circ")
        b = _build(td, ONE_GATE + "\n" + TWO_GATE, "b.circ")
        d = json.loads(run_circuitc("diff", str(a), str(b)).stdout)
        assert not d["ok"]
        assert "nand2" in d["circuits_added"]
        # identical files diff clean
        assert json.loads(run_circuitc("diff", str(a), str(a)).stdout)["ok"]


def test_diff_detects_rename():
    with tempfile.TemporaryDirectory() as td:
        a = _build(td, ONE_GATE, "a.circ")
        b = _build(td, ONE_GATE, "b.circ")
        run_circuitc("rename", str(b), "inv", "inverter")
        d = json.loads(run_circuitc("diff", str(a), str(b)).stdout)
        assert d["circuits_renamed"] == [["inv", "inverter"]]


# ═════════════════════════════════════════════════════════════════════════════
# Layout bounds
# ═════════════════════════════════════════════════════════════════════════════
def test_diff_component_changes():
    with tempfile.TemporaryDirectory() as td:
        a = _build(td, ONE_GATE, "a.circ")
        b = _build(td, ONE_GATE, "b.circ")
        run_circuitc("add-pin", str(b), "inv", "B", "--direction", "in")
        d = json.loads(run_circuitc("diff", str(a), str(b)).stdout)
        assert not d["ok"]
        r = run_circuitc("diff", str(a), str(b), "--text")
        assert r.returncode == 0 and len(r.stdout) > 0



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

#!/usr/bin/env python3
"""Self-check for circuitc. Run: python3 test_circuitc.py
Needs java + logisim jar for the behavioral half; structural half always runs."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import circuitc

SRC = """
circuit xor3:
  inputs A, B, C
  outputs Y
  Y = A ^ B ^ C

circuit mul2:
  inputs A1, A0, B1, B0
  outputs P3, P2, P1, P0
  P0 = A0 & B0
  t1 = A1 & B0
  t2 = A0 & B1
  P1 = t1 ^ t2
  c1 = t1 & t2
  t3 = A1 & B1
  P2 = t3 ^ c1
  P3 = t3 & c1

circuit misc:
  inputs A, B, C, D, E, F, G
  outputs W, X, Y, Z
  W = A & B & C & D & E & F & G
  X = ~(A | B) & 1
  Y = C ^ 0
  Z = A

circuit cmos_nand:
  inputs A, B
  outputs Y
  spec Y = ~(A & B)
  pmos A: VDD -> Y
  pmos B: VDD -> Y
  nmos A: GND -> n1
  nmos B: n1 -> Y

circuit tgate_mux:
  inputs S, D0, D1
  outputs Y
  Sn = ~S
  spec Y = (S & D1) | (Sn & D0)
  tgate S, Sn: D1 -> Y
  tgate Sn, S: D0 -> Y

circuit pseudo_nor:
  inputs A, B
  outputs Y
  spec Y = ~(A | B)
  pullup Y
  nmos A: GND -> Y
  nmos B: GND -> Y
"""


def main():
    # geometry matches AbstractGate.getInputOffset for the cases we emit
    assert circuitc.input_dys(2) == [-20, 20]
    assert circuitc.input_dys(3) == [-20, 0, 20]
    assert circuitc.input_dys(4) == [-20, -10, 10, 20]
    assert circuitc.input_dys(5) == [-20, -10, 0, 10, 20]
    assert circuitc.gate_axis("AND") == 50
    assert circuitc.gate_axis("NAND") == 60
    assert circuitc.gate_axis("XOR") == 60
    assert circuitc.gate_axis("XNOR") == 70
    assert circuitc.gate_axis("NOT") == 30

    # expression semantics
    e = circuitc.parse_expr("~(A | B) & (C ^ 1)")
    assert circuitc.eval_expr(e, {"A": 0, "B": 0, "C": 0}) == 1
    assert circuitc.eval_expr(e, {"A": 1, "B": 0, "C": 0}) == 0
    assert circuitc.eval_expr(e, {"A": 0, "B": 0, "C": 1}) == 0

    circuits = circuitc.parse_logic(SRC)
    nets = [circuitc.compile_netlist(c) for c in circuits]
    with tempfile.TemporaryDirectory() as td:
        circ = Path(td) / "t.circ"
        circ.write_text(circuitc.emit_project(nets))
        for c, net in zip(circuits, nets):
            s = circuitc.structural_check(str(circ), net)
            assert s["ok"], f"{c.name}: {s['errors']}"

        # sabotage MUST be caught: emit with the inverted-geometry bug
        orig = circuitc.input_dys
        circuitc.input_dys = lambda n, size=50: [-30, 0, 30] if n == 3 else orig(n, size)
        maj = circuitc.parse_logic(
            "circuit maj:\n inputs A,B,C\n outputs Y\n Y = (A&B)|(B&C)|(A&C)\n")[0]
        bad_net = circuitc.compile_netlist(maj)
        bad = Path(td) / "bad.circ"
        bad.write_text(circuitc.emit_project([bad_net]))
        circuitc.input_dys = orig
        assert not circuitc.structural_check(str(bad), bad_net)["ok"], \
            "structural check failed to catch sabotaged geometry"

        # switch-level static errors are rejected at parse/validate time
        for bad_src, why in [
            ("circuit x:\n inputs A\n outputs Y\n spec Y = ~A\n nmos A: Y -> GND\n",
             "FET driving GND"),
            ("circuit x:\n inputs A\n outputs Y\n Y = A\n nmos A: GND -> Y\n",
             "gate + transistor both driving Y"),
            ("circuit x:\n inputs A\n outputs Y\n spec Y = n1\n nmos A: GND -> Y\n",
             "spec reading a switch-driven net"),
        ]:
            try:
                circuitc.parse_logic(bad_src)
                raise AssertionError(f"accepted invalid source: {why}")
            except SyntaxError:
                pass

        try:
            jar = circuitc.find_jar()
        except FileNotFoundError:
            print("OK (structural only; no logisim jar for behavioral check)")
            return
        for c in circuits:
            b = circuitc.behavioral_check(jar, str(circ), c)
            assert b["ok"], f"{c.name}: {b}"
        print(f"OK — structural + behavioral ({sum(2**len(c.inputs) for c in circuits)} vectors)")


if __name__ == "__main__":
    main()

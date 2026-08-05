#!/usr/bin/env python3
"""circuitc — compile boolean-expression source into verified Logisim Evolution .circ files.

Built for LLM use: the model writes a tiny .logic file (no coordinates, no XML),
this tool does geometry, wiring, and three layers of verification:

  1. structural — union-find over the emitted wire geometry; proves the netlist
     Logisim will infer matches the netlist we intended (no shorts, no floats).
  2. load       — `java -jar logisim.jar --tty stats`; proves the XML parses.
  3. behavioral — auto-generated test vectors (evaluated from the same source
     expressions) run through `--test-vector`; proves the circuit computes
     the right function.

Source format (.logic):

    circuit full_adder:
      inputs A, B, Cin
      outputs S, Cout
      S = A ^ B ^ Cin
      Cout = (A & B) | (Cin & (A ^ B))

Operators: ~ (not), & (and), ^ (xor), | (or), parens, constants 0/1.
Assignment targets that are not declared outputs become named intermediate
nets, usable in later expressions. 1-bit signals only.

Switch-level primitives (CMOS coursework) mix freely with expressions:

    circuit cmos_nand:
      inputs A, B
      outputs Y
      spec Y = ~(A & B)     # golden model; drives test-vector generation
      pmos A: VDD -> Y      # gate: source -> drain (Logisim FETs pass one way)
      pmos B: VDD -> Y
      nmos A: GND -> n1     # series stack through internal net n1
      nmos B: n1 -> Y

    tgate C, Cn: X -> Y     # transmission gate: active-high, active-low ctrl
    pullup  Y               # pull resistor to 1
    pulldown Y              # pull resistor to 0

VDD / GND are reserved nets (auto-instantiate Power / Ground). N-FETs pass
when gate=1, P-FETs when gate=0; an off FET drives Hi-Z. Nets driven only
by FETs need a `spec` line per output so vectors can be generated; Logisim
itself acts as the switch-level simulator (contention -> E, floating -> U,
both fail the vector run).

Port geometry is transcribed from logisim-evolution source
(AbstractGate.getInputOffset, NotGate.configurePorts, XorGate ctor):
size=50 gates, <=3 inputs -> dy in {-20,0,+20}; 4..5 inputs -> 10px steps.

Usage:
    python3 circuitc.py build design.logic -o design.circ
    python3 circuitc.py check design.circ
    python3 circuitc.py describe design.circ
"""

from __future__ import annotations

import argparse
import glob
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

GRID = 10
GATE_SIZE = 50  # Logisim "medium", the default

# ---------------------------------------------------------------------------
# Geometry (transcribed from logisim-evolution Java source)
# ---------------------------------------------------------------------------

# axis length = size + bonusWidth + (negatedOutput ? 10 : 0)
_BONUS = {"XOR": 10, "XNOR": 10}
_NEG_OUT = {"NAND", "NOR", "XNOR"}
_CIRC_NAME = {
    "AND": "AND Gate", "OR": "OR Gate", "XOR": "XOR Gate",
    "NAND": "NAND Gate", "NOR": "NOR Gate", "XNOR": "XNOR Gate",
    "NOT": "NOT Gate",
}


def gate_axis(kind: str, size: int = GATE_SIZE) -> int:
    """x-distance from output anchor back to the input column (east-facing)."""
    if kind == "NOT":
        return 30  # NotGate.configurePorts: wide (size attr 30) -> dx = -30
    return size + _BONUS.get(kind, 0) + (10 if kind in _NEG_OUT else 0)


def input_dys(n: int, size: int = GATE_SIZE) -> list[int]:
    """Per-input dy offsets, AbstractGate.getInputOffset verbatim."""
    if n == 1:
        return [0]
    if n <= 3:
        if size < 40:
            ss, sd, sle = -5, 10, 10
        elif size < 60 or n <= 2:
            ss, sd, sle = -10, 20, 20
        else:
            ss, sd, sle = -15, 30, 30
    elif n == 4 and size >= 60:
        ss, sd, sle = -5, 20, 0
    else:
        ss, sd, sle = -5, 10, 10
    out = []
    for i in range(n):
        if n % 2 == 1:
            dy = ss * (n - 1) + sd * i
        else:
            dy = ss * n + sd * i
            if i >= n // 2:
                dy += sle
            if n == 4 and size >= 60:
                dy -= 10
        out.append(dy)
    return out


def gate_ports(kind: str, n_inputs: int) -> tuple[list[tuple[int, int]], tuple[int, int]]:
    """(input port offsets, output port offset) relative to the anchor loc."""
    ax = gate_axis(kind)
    ins = [(-ax, dy) for dy in input_dys(1 if kind == "NOT" else n_inputs)]
    return ins, (0, 0)


# ---------------------------------------------------------------------------
# Expression parsing / evaluation
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*|[01]|[~&^|()])")


def _tokenize(text: str) -> list[str]:
    toks, pos = [], 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise SyntaxError(f"bad character at ...{text[pos:pos+10]!r}")
        toks.append(m.group(1))
        pos = m.end()
    return toks


# AST: ("var", name) | ("const", 0|1) | ("not", x) | ("and"|"or"|"xor", a, b)
def parse_expr(text: str):
    toks = _tokenize(text)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def eat(tok=None):
        nonlocal pos
        t = peek()
        if t is None or (tok and t != tok):
            raise SyntaxError(f"expected {tok or 'token'}, got {t!r} in {text!r}")
        pos += 1
        return t

    def atom():
        t = peek()
        if t == "(":
            eat("(")
            e = expr_or()
            eat(")")
            return e
        if t == "~":
            eat("~")
            return ("not", atom())
        if t in ("0", "1"):
            eat()
            return ("const", int(t))
        if t and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t):
            eat()
            return ("var", t)
        raise SyntaxError(f"unexpected {t!r} in {text!r}")

    def expr_and():
        e = atom()
        while peek() == "&":
            eat("&")
            e = ("and", e, atom())
        return e

    def expr_xor():
        e = expr_and()
        while peek() == "^":
            eat("^")
            e = ("xor", e, expr_and())
        return e

    def expr_or():
        e = expr_xor()
        while peek() == "|":
            eat("|")
            e = ("or", e, expr_xor())
        return e

    e = expr_or()
    if pos != len(toks):
        raise SyntaxError(f"trailing tokens {toks[pos:]!r} in {text!r}")
    return e


def eval_expr(node, env: dict[str, int]) -> int:
    op = node[0]
    if op == "var":
        return env[node[1]]
    if op == "const":
        return node[1]
    if op == "not":
        return 1 - eval_expr(node[1], env)
    a, b = eval_expr(node[1], env), eval_expr(node[2], env)
    return {"and": a & b, "or": a | b, "xor": a ^ b}[op]


def expr_vars(node, out: set[str]):
    if node[0] == "var":
        out.add(node[1])
    elif node[0] == "not":
        expr_vars(node[1], out)
    elif node[0] in ("and", "or", "xor"):
        expr_vars(node[1], out)
        expr_vars(node[2], out)


# ---------------------------------------------------------------------------
# .logic source format
# ---------------------------------------------------------------------------

@dataclass
class Fet:
    kind: str    # "p" | "n"
    gate: str
    source: str
    drain: str


@dataclass
class TGate:
    ghigh: str   # active-high control (GATE1)
    glow: str    # active-low control (GATE0)
    source: str
    drain: str


@dataclass
class CircuitDef:
    name: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    assigns: list[tuple[str, object]] = field(default_factory=list)  # (target, ast)
    specs: list[tuple[str, object]] = field(default_factory=list)    # golden models
    fets: list[Fet] = field(default_factory=list)
    tgates: list[TGate] = field(default_factory=list)
    pulls: list[tuple[str, str]] = field(default_factory=list)       # (net, "0"|"1")


_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED = {"VDD", "GND"}
_FET_RE = re.compile(r"^(pmos|nmos)\s+(\w+)\s*:\s*(\w+)\s*->\s*(\w+)$")
_TGATE_RE = re.compile(r"^tgate\s+(\w+)\s*,\s*(\w+)\s*:\s*(\w+)\s*->\s*(\w+)$")
_PULL_RE = re.compile(r"^(pullup|pulldown)\s+(\w+)$")


def parse_logic(text: str) -> list[CircuitDef]:
    circuits: list[CircuitDef] = []
    cur: CircuitDef | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            if line.startswith("circuit "):
                name = line[len("circuit "):].rstrip(":").strip()
                if not _NAME_RE.match(name):
                    raise SyntaxError(f"bad circuit name {name!r}")
                cur = CircuitDef(name)
                circuits.append(cur)
                continue
            if cur is None:
                raise SyntaxError("statement before any 'circuit' header")
            if line.startswith("inputs ") or line.startswith("inputs:"):
                cur.inputs += _namelist(line.split(None, 1)[1] if " " in line else line.split(":", 1)[1])
            elif line.startswith("outputs ") or line.startswith("outputs:"):
                cur.outputs += _namelist(line.split(None, 1)[1] if " " in line else line.split(":", 1)[1])
            elif m := _FET_RE.match(line):
                kind, gate, source, drain = m.groups()
                cur.fets.append(Fet(kind[0], gate, source, drain))
            elif m := _TGATE_RE.match(line):
                ghigh, glow, source, drain = m.groups()
                cur.tgates.append(TGate(ghigh, glow, source, drain))
            elif m := _PULL_RE.match(line):
                cur.pulls.append((m.group(2), "1" if m.group(1) == "pullup" else "0"))
            elif line.startswith("spec "):
                target, rhs = line[len("spec "):].split("=", 1)
                target = target.strip()
                if not _NAME_RE.match(target):
                    raise SyntaxError(f"bad spec target {target!r}")
                cur.specs.append((target, parse_expr(rhs)))
            elif "=" in line:
                target, rhs = line.split("=", 1)
                target = target.strip()
                if not _NAME_RE.match(target):
                    raise SyntaxError(f"bad assignment target {target!r}")
                cur.assigns.append((target, parse_expr(rhs)))
            else:
                raise SyntaxError(f"unrecognized line: {line!r}")
        except SyntaxError as e:
            raise SyntaxError(f"line {lineno}: {e}") from None

    for c in circuits:
        _validate(c)
    if not circuits:
        raise SyntaxError("no 'circuit' blocks found")
    return circuits


def _namelist(s: str) -> list[str]:
    names = [n.strip() for n in s.replace(":", " ").split(",") if n.strip()]
    for n in names:
        if not _NAME_RE.match(n):
            raise SyntaxError(f"bad signal name {n!r}")
    return names


def _validate(c: CircuitDef):
    if not c.inputs:
        raise SyntaxError(f"{c.name}: no inputs declared")
    if not c.outputs:
        raise SyntaxError(f"{c.name}: no outputs declared")
    dup = [n for n in c.inputs if c.inputs.count(n) > 1]
    if dup:
        raise SyntaxError(f"{c.name}: duplicate input {dup[0]!r}")
    for n in c.inputs + c.outputs:
        if n in _RESERVED:
            raise SyntaxError(f"{c.name}: {n} is a reserved net name")

    # switch-driven nets are order-independent (FET stacks reference each
    # other's drains freely); expression assignments stay strictly ordered.
    switch_driven = {f.drain for f in c.fets} | {t.drain for t in c.tgates}
    switch_driven |= {net for net, _ in c.pulls}
    for f in c.fets + c.tgates:
        if f.drain in c.inputs or f.drain in _RESERVED:
            raise SyntaxError(f"{c.name}: cannot drive {f.drain!r} with a transistor")

    defined = set(c.inputs) | _RESERVED | switch_driven
    assigned: set[str] = set()
    for target, ast in c.assigns:
        if target in c.inputs:
            raise SyntaxError(f"{c.name}: cannot assign to input {target!r}")
        if target in _RESERVED:
            raise SyntaxError(f"{c.name}: cannot assign to reserved net {target!r}")
        if target in assigned:
            raise SyntaxError(f"{c.name}: {target!r} assigned twice")
        if target in switch_driven:
            raise SyntaxError(f"{c.name}: {target!r} driven by both a gate and a transistor")
        used: set[str] = set()
        expr_vars(ast, used)
        missing = used - defined
        if missing:
            raise SyntaxError(
                f"{c.name}: {target!r} uses undefined signal(s) {sorted(missing)} "
                "(assignments must come after their operands)"
            )
        assigned.add(target)
        defined.add(target)

    # FET terminals must reference known nets
    for f in c.fets:
        for term in (f.gate, f.source):
            if term not in defined and term not in switch_driven:
                raise SyntaxError(f"{c.name}: FET terminal {term!r} is undefined")
    for t in c.tgates:
        for term in (t.ghigh, t.glow, t.source):
            if term not in defined and term not in switch_driven:
                raise SyntaxError(f"{c.name}: tgate terminal {term!r} is undefined")

    # specs are golden models: evaluable from inputs/assigns/earlier specs only
    spec_ok = set(c.inputs) | _RESERVED | assigned
    for target, ast in c.specs:
        used = set()
        expr_vars(ast, used)
        bad = used - spec_ok
        if bad:
            raise SyntaxError(
                f"{c.name}: spec {target!r} uses non-evaluable signal(s) {sorted(bad)} "
                "(specs may only use inputs, assigned nets, and earlier specs)"
            )
        spec_ok.add(target)

    undriven = [o for o in c.outputs if o not in assigned and o not in switch_driven]
    if undriven:
        raise SyntaxError(f"{c.name}: output(s) never driven: {undriven}")


def eval_circuit(c: CircuitDef, input_values: dict[str, int]) -> dict[str, int]:
    """Golden-model evaluation. Raises ValueError if an output has no
    evaluable definition (switch-driven with no spec)."""
    env = {"VDD": 1, "GND": 0, **input_values}
    for target, ast in c.assigns + c.specs:
        try:
            env[target] = eval_expr(ast, env)
        except KeyError:
            pass  # depends on a switch-driven net; only fatal if an output needs it
    missing = [o for o in c.outputs if o not in env]
    if missing:
        raise ValueError(
            f"{c.name}: cannot generate vectors — output(s) {missing} are "
            "switch-driven with no `spec` line"
        )
    return {o: env[o] for o in c.outputs}


# ---------------------------------------------------------------------------
# Compile: expression AST -> gate netlist
# ---------------------------------------------------------------------------

@dataclass
class Gate:
    kind: str            # AND OR XOR NAND NOR XNOR NOT
    inputs: list[str]    # signal names
    output: str


@dataclass
class Netlist:
    name: str
    inputs: list[str]
    outputs: list[str]
    gates: list[Gate] = field(default_factory=list)
    # output/intermediate name -> source signal it aliases (for `Y = A` style)
    aliases: dict[str, str] = field(default_factory=dict)
    const_nets: dict[str, int] = field(default_factory=dict)  # signal -> 0/1
    fets: list[Fet] = field(default_factory=list)
    tgates: list[TGate] = field(default_factory=list)
    pulls: list[tuple[str, str]] = field(default_factory=list)

    def referenced(self) -> set[str]:
        s = set(self.inputs) | set(self.outputs) | set(self.const_nets)
        for g in self.gates:
            s.update(g.inputs)
            s.add(g.output)
        for f in self.fets:
            s.update((f.gate, f.source, f.drain))
        for t in self.tgates:
            s.update((t.ghigh, t.glow, t.source, t.drain))
        s.update(net for net, _ in self.pulls)
        for d, src in self.aliases.items():
            s.update((d, src))
        return s


_MAX_FANIN = 5  # keep gates in the pretty <=5-input regime


def compile_netlist(c: CircuitDef) -> Netlist:
    net = Netlist(c.name, list(c.inputs), list(c.outputs))
    tmp_counter = itertools.count(1)
    memo: dict[str, str] = {}  # structural expr key -> signal name

    def flatten(op: str, node) -> list:
        """Collect operands of a chain of the same associative op."""
        if node[0] == op:
            return flatten(op, node[1]) + flatten(op, node[2])
        return [node]

    def emit(node, want: str | None = None) -> str:
        key = repr(node)
        if want is None and key in memo:
            return memo[key]

        def fresh() -> str:
            return want or f"_t{next(tmp_counter)}"

        op = node[0]
        if op == "var":
            sig = node[1]
        elif op == "const":
            sig = f"_const{node[1]}"
            net.const_nets[sig] = node[1]
        elif op == "not":
            src = emit(node[1])
            sig = fresh()
            net.gates.append(Gate("NOT", [src], sig))
        else:  # and / or / xor
            ops = flatten(op, node) if op in ("and", "or") else [node[1], node[2]]
            if op == "xor":
                # binary XOR chain: multi-input XOR semantics depend on the
                # gate's one-hot/parity attribute; binary is unambiguous.
                src = emit(ops[0])
                for x in ops[1:-1]:
                    nxt = f"_t{next(tmp_counter)}"
                    net.gates.append(Gate("XOR", [src, emit(x)], nxt))
                    src = nxt
                sig = fresh()
                net.gates.append(Gate("XOR", [src, emit(ops[-1])], sig))
            else:
                srcs = [emit(x) for x in ops]
                kind = op.upper()
                while len(srcs) > _MAX_FANIN:  # tree-reduce oversized fanin
                    mid = f"_t{next(tmp_counter)}"
                    net.gates.append(Gate(kind, srcs[:_MAX_FANIN], mid))
                    srcs = [mid] + srcs[_MAX_FANIN:]
                sig = fresh()
                net.gates.append(Gate(kind, srcs, sig))
        if want and sig != want:
            # pure alias (Y = A, Y = t3): no gate needed, just tie the nets
            net.aliases[want] = sig
            sig = want
        memo.setdefault(key, sig)
        return sig

    for target, ast in c.assigns:
        emit(ast, want=target)
    net.fets = list(c.fets)
    net.tgates = list(c.tgates)
    net.pulls = list(c.pulls)
    return net


# ---------------------------------------------------------------------------
# Emit: netlist -> .circ XML
# ---------------------------------------------------------------------------
# Gate-level circuits use a textbook schematic layout ("routed"): input pins
# on the left feeding horizontal signal lanes, gate columns by logic depth,
# vertical drops tapping lanes into gate inputs, risers carrying outputs back
# up to new lanes, output pins on the right. Wire semantics measured against
# Logisim 4.1.0: crossings don't connect, T-junctions and ports-on-wires do —
# so lanes/drops may cross freely and only deliberate Ts join nets.
#
# Switch-level circuits (FETs/tgates/pulls) keep the cell-isolated tunnel
# layout: every component in a private cell, ports stubbed to labeled
# Tunnels, no wire ever leaves a cell.
#
# Both layouts also return the intended (location, signal, role) map for
# every component port, which structural verification checks against the
# wire geometry actually written to the file.

CELL_W, CELL_H = 220, 100
COLS = 6
ORIGIN = (200, 100)
STUB = 20


def _cell_anchor(index: int) -> tuple[int, int]:
    col, row = index % COLS, index // COLS
    # anchor sits right-of-center so gate bodies (extend <=90 left) stay inside
    return (ORIGIN[0] + col * CELL_W + 140, ORIGIN[1] + row * CELL_H + 40)


def _xml_comp(lib: str, name: str, loc: tuple[int, int], attrs: dict[str, str]) -> str:
    a = "".join(
        f'\n      <a name="{escape(k)}" val="{escape(v)}"/>' for k, v in attrs.items()
    )
    return f'    <comp lib="{lib}" loc="({loc[0]},{loc[1]})" name="{escape(name)}">{a}\n    </comp>'


def _xml_wire(a: tuple[int, int], b: tuple[int, int]) -> str:
    return f'    <wire from="({a[0]},{a[1]})" to="({b[0]},{b[1]})"/>'


PortMap = list[tuple[tuple[int, int], str, str]]  # (loc, signal, driver|soft-driver|sink)


def _canon(net: Netlist, sig: str) -> str:
    while sig in net.aliases:
        sig = net.aliases[sig]
    return sig


def _route_gates(net: Netlist, pin_outputs: list[str] | None = None,
                 descend: list[str] | None = None) -> dict:
    """Core schematic router for the gate-level part of a circuit.

    Textbook style: input pins left, horizontal signal lanes on top, gate
    columns by logic depth, vertical channel drops into gate inputs, risers
    from gate outputs up to fresh lanes, output pins right.

    Human touches: a signal with exactly one consumer in the next column is
    routed as a single straight wire (its consumer gate is row-aligned with
    the producer when possible) instead of going up-and-over the lane band;
    unconsumed signals get no riser.

    Collision safety: lanes have unique y; drops/risers unique x per channel;
    gate y-intervals disjoint within a column so port ys are unique; straight
    direct wires run at a producer-output y, which is never a drop endpoint
    (drop endpoints are tap ys of *other* signals — one port, one signal) and
    never a riser bottom (direct producers get no riser). Everything else
    that touches is a deliberate T. Crossings don't connect (measured).

    Returns a state dict for embedding (e.g. under a CMOS band layout):
    parts/pmap plus lane geometry, so callers may extend lane_need before
    finalize_lanes() is applied.
    """
    pin_outputs = net.outputs if pin_outputs is None else pin_outputs
    descend = descend or []
    parts: list[str] = []
    pmap: PortMap = []

    def W(a, b):
        if a != b:
            parts.append(_xml_wire(a, b))

    cn = lambda s: _canon(net, s)

    depth = {s: 0 for s in net.inputs}
    depth.update({s: 0 for s in net.const_nets})
    for g in net.gates:
        depth[g.output] = 1 + max(depth[cn(i)] for i in g.inputs)
    n_cols = max((depth[g.output] for g in net.gates), default=0)
    cols = [[g for g in net.gates if depth[g.output] == c] for c in range(1, n_cols + 1)]

    # ---- pre-pass: which signals are lane-promoted vs routed straight ----
    uses: dict[str, int] = {}
    span_far: dict[str, bool] = {}
    for ci, col in enumerate(cols, start=1):
        for g in col:
            for i in g.inputs:
                s = cn(i)
                uses[s] = uses.get(s, 0) + 1
                if ci - depth.get(s, 0) > 1:
                    span_far[s] = True
    always_lane = set(net.inputs) | set(net.const_nets) | {cn(o) for o in pin_outputs}
    always_lane |= {cn(s) for s in descend}

    # row planning per column: aligned rows for straight-wire candidates.
    # rel_out_y[sig] = producer output y relative to gate_top.
    rel_out_y = {}
    direct: set[str] = set()  # signals drawn as one straight wire
    col_rows: list[list[tuple]] = []  # per column: [(gate, rel_ay)]
    for ci, col in enumerate(cols, start=1):
        occupied: list[tuple[int, int]] = []  # (lo, hi) rel y intervals
        placed = []

        def fits(ay):
            return all(hi < ay - 25 or lo > ay + 25 for lo, hi in occupied)

        cursor = 20
        for g in col:
            ay = None
            # try row-aligning with a straight-wire input (center port only)
            ins, _ = gate_ports(g.kind, len(g.inputs))
            for (dx, dy), sig in zip(ins, g.inputs):
                s = cn(sig)
                if (dy == 0 and uses.get(s) == 1 and s not in always_lane
                        and not span_far.get(s) and s in rel_out_y
                        and depth.get(s, 0) == ci - 1 and fits(rel_out_y[s])):
                    ay = rel_out_y[s]
                    direct.add(s)
                    break
            if ay is None:
                while not fits(cursor):
                    cursor += 10
                ay = cursor
                cursor += 80
            occupied.append((ay - 25, ay + 25))
            placed.append((g, ay))
            rel_out_y[g.output] = ay
        col_rows.append(placed)

    promoted = {s for s in uses if s not in direct} | always_lane

    # ---- absolute geometry ----
    lane_sigs = [s for s in (list(net.inputs) + sorted(net.const_nets)
                             + [g.output for g in net.gates])
                 if s in promoted]
    lane_y = {s: 40 + 20 * i for i, s in enumerate(lane_sigs)}
    gate_top = 40 + 20 * len(lane_sigs) + 60

    X_PIN = 60
    lane_start: dict[str, int] = {}
    lane_need: dict[str, int] = {}
    anchor: dict[str, tuple[int, int]] = {}  # producer output port per signal
    max_y = gate_top

    for s in net.inputs:
        parts.append(_xml_comp("0", "Pin", (X_PIN, lane_y[s]), {
            "appearance": "classic", "label": s,
        }))
        pmap.append(((X_PIN, lane_y[s]), s, "driver"))
        lane_start[s] = X_PIN
        anchor[s] = (X_PIN, lane_y[s])
    for s, v in sorted(net.const_nets.items()):
        parts.append(_xml_comp("0", "Constant", (X_PIN, lane_y[s]), {"value": f"0x{v:x}"}))
        pmap.append(((X_PIN, lane_y[s]), s, "driver"))
        lane_start[s] = X_PIN
        anchor[s] = (X_PIN, lane_y[s])

    x = X_PIN
    for placed in col_rows:
        consumed: list[str] = []   # lane-promoted signals tapped in this column
        for g, _ in placed:
            for i in g.inputs:
                s = cn(i)
                if s in promoted and s not in consumed:
                    consumed.append(s)
        chan_x = x + 40
        drop_x = {s: chan_x + 20 * i for i, s in enumerate(consumed)}
        gate_in_x = chan_x + 20 * len(consumed) + 20

        taps: dict[str, list[int]] = {s: [] for s in consumed}
        col_right = gate_in_x
        risers = []
        for g, rel_ay in placed:
            ax = gate_in_x + gate_axis(g.kind)
            ay = gate_top + rel_ay
            attrs = {"size": "30"} if g.kind == "NOT" else {
                "size": str(GATE_SIZE), "inputs": str(len(g.inputs)),
            }
            parts.append(_xml_comp("1", _CIRC_NAME[g.kind], (ax, ay), attrs))
            ins, _ = gate_ports(g.kind, len(g.inputs))
            for (dx, dy), sig in zip(ins, g.inputs):
                s = cn(sig)
                px, py = ax + dx, ay + dy  # px == gate_in_x
                pmap.append(((px, py), s, "sink"))
                if s in direct:
                    W(anchor[s], (px, py))  # straight wire, same y by planning
                else:
                    W((drop_x[s], py), (px, py))
                    taps[s].append(py)
            pmap.append(((ax, ay), g.output, "driver"))
            anchor[g.output] = (ax, ay)
            if g.output in promoted and (uses.get(g.output) or g.output in always_lane):
                risers.append(g)
            col_right = max(col_right, ax)
            max_y = max(max_y, ay + 40)
        for s in consumed:
            W((drop_x[s], lane_y[s]), (drop_x[s], max(taps[s])))
            lane_need[s] = max(lane_need.get(s, 0), drop_x[s])
        for j, g in enumerate(risers):
            rx = col_right + 20 + 20 * j
            ax, ay = anchor[g.output]
            W((ax, ay), (rx, ay))
            W((rx, lane_y[g.output]), (rx, ay))
            lane_start[g.output] = rx
        x = col_right + 20 + 20 * len(risers)

    # output pins sit directly on their signal's lane at the right edge;
    # aliased duplicates step left, connecting port-on-wire (measured OK)
    out_x = x + 80
    dup: dict[str, int] = {}
    for o in pin_outputs:
        s = cn(o)
        px = out_x - 20 * dup.get(s, 0)
        dup[s] = dup.get(s, 0) + 1
        parts.append(_xml_comp("0", "Pin", (px, lane_y[s]), {
            "appearance": "classic", "facing": "west",
            "type": "output", "label": o,
        }))
        pmap.append(((px, lane_y[s]), s, "sink"))
        lane_need[s] = max(lane_need.get(s, 0), out_x)

    return {
        "parts": parts, "pmap": pmap, "lane_y": lane_y,
        "lane_start": lane_start, "lane_need": lane_need,
        "right": out_x + 40, "bottom": max_y,
    }


def _finalize_lanes(st: dict):
    for s, ly in st["lane_y"].items():
        if s in st["lane_need"] and st["lane_need"][s] > st["lane_start"][s]:
            st["parts"].append(_xml_wire((st["lane_start"][s], ly),
                                         (st["lane_need"][s], ly)))


def _layout_routed(net: Netlist) -> tuple[list[str], PortMap]:
    st = _route_gates(net)
    _finalize_lanes(st)
    return st["parts"], st["pmap"]


def _layout_switch(net: Netlist) -> tuple[list[str], PortMap]:
    """CMOS-style schematic for circuits with switch-level primitives.

    Band structure (top to bottom), after the Fable-5 design review:
      1. input pins / gate-level logic (reuses _route_gates), lanes on top
      2. Power + horizontal VDD subrail
      3. PMOS series stacks, facing south, drains descending toward
      4. horizontal net rails (one unique y per net) + tgate / pass-FET rows,
         with pull resistors and output pins sitting directly on rails
      5. NMOS series stacks, facing north, sources down on
      6. horizontal GND subrail + Ground

    Signals produced in band 1 descend on unique-x feed wires into their
    band-4 rail; FET gate ports are tapped from rails through per-column gap
    slots so no horizontal ever crosses another column's gate ports.
    All uniqueness is enforced by allocators; crossings don't connect.
    """
    cn = lambda s: _canon(net, s)
    switch_driven = {cn(f.drain) for f in net.fets} | {cn(t.drain) for t in net.tgates}
    for g in net.gates:
        for i in g.inputs:
            if cn(i) in switch_driven:
                raise ValueError(
                    f"{net.name}: gate input {i!r} is driven by a transistor; "
                    "routing gate logic below switch nets is not supported yet — "
                    "restructure so gates feed transistors, not the reverse"
                )

    # nets produced in band 1 and consumed by the switch region
    descend_set: list[str] = []

    def need_descend(s):
        s = cn(s)
        if s not in _RESERVED and s not in switch_driven and s not in descend_set:
            descend_set.append(s)

    for f in net.fets:
        need_descend(f.gate)
        if cn(f.source) not in switch_driven:
            need_descend(f.source)
    for t in net.tgates:
        for s in (t.ghigh, t.glow):
            need_descend(s)
        if cn(t.source) not in switch_driven:
            need_descend(t.source)
    for s, _ in net.pulls:
        if cn(s) not in switch_driven and cn(s) not in _RESERVED:
            need_descend(s)

    band1_producible = set(net.inputs) | set(net.const_nets) | {g.output for g in net.gates}
    for s in descend_set:
        if s not in band1_producible:
            raise ValueError(
                f"{net.name}: {s!r} feeds a transistor terminal but is not an "
                "input, constant, or gate output — cannot route"
            )

    pin_outputs = [o for o in net.outputs if cn(o) not in switch_driven]
    st = _route_gates(net, pin_outputs=pin_outputs, descend=descend_set)
    parts, pmap = st["parts"], st["pmap"]

    def W(a, b):
        if a != b:
            parts.append(_xml_wire(a, b))

    # ---- chain extraction (series stacks) ----
    term_uses: dict[str, list] = {}
    for f in net.fets:
        term_uses.setdefault(cn(f.source), []).append((f, "source"))
        term_uses.setdefault(cn(f.drain), []).append((f, "drain"))
    fixed = (set(net.inputs) | set(net.outputs) | _RESERVED | set(descend_set)
             | {cn(s) for s, _ in net.pulls} | {cn(t.drain) for t in net.tgates}
             | {cn(t.source) for t in net.tgates})

    def internal(s):
        u = term_uses.get(s, [])
        return (s not in fixed and len(u) == 2
                and {r for _, r in u} == {"source", "drain"})

    unused = list(net.fets)
    chains: list[list[Fet]] = []
    for f in list(unused):
        if f not in unused or internal(cn(f.source)):
            continue
        chain = [f]
        unused.remove(f)
        while internal(cn(chain[-1].drain)):
            nxt = next(g for g, r in term_uses[cn(chain[-1].drain)] if r == "source")
            chain.append(nxt)
            unused.remove(nxt)
        chains.append(chain)
    chains += [[f] for f in unused]  # cycles: draw as singleton pass rows

    up = [c for c in chains if cn(c[0].source) == "VDD"]
    dn = [c for c in chains if cn(c[0].source) == "GND"]
    pas = [c for c in chains if c not in up and c not in dn]

    # ---- x layout ----
    band1_right = st["right"]
    feed_x = {s: band1_right + 40 + 20 * i for i, s in enumerate(descend_set)}
    x_fet0 = band1_right + 40 + 20 * len(descend_set) + 40

    # columns: pair up/down chains that drive the same net onto shared x
    drains = []
    for c in up + dn:
        d = cn(c[-1].drain)
        if d not in drains:
            drains.append(d)
    columns: list[tuple] = []  # (bx, up_chain|None, dn_chain|None)
    bx = x_fet0
    for d in drains:
        ups = [c for c in up if cn(c[-1].drain) == d]
        dns = [c for c in dn if cn(c[-1].drain) == d]
        for k in range(max(len(ups), len(dns))):
            u = ups[k] if k < len(ups) else None
            n = dns[k] if k < len(dns) else None
            taps = (len(u) if u else 0) + (len(n) if n else 0)
            bx += max(80, 40 + 10 * taps + 20)
            columns.append((bx, u, n))
    cols_right = bx + 40

    # tgate / pass rows region
    max_row_len = max([len(c) for c in pas] + [1] if (pas or net.tgates) else [0])
    n_src_jogs = len(pas) + 3 * len(net.tgates)  # tgate: glow + ghigh + source
    x_tg = cols_right + 10 * n_src_jogs + 60
    rows_right = x_tg + 60 * max(0, max_row_len - 1) + 20
    n_dst_jogs = len(pas) + len(net.tgates) + 2 * len(net.pulls)
    x_right = rows_right + 20 + 10 * n_dst_jogs + 20

    # ---- y layout ----
    max_up = max([len(c) for c in up], default=0)
    max_dn = max([len(c) for c in dn], default=0)
    y_psrc = st["bottom"] + 60
    y_rail0 = y_psrc + (40 + 60 * (max_up - 1) + 40 if up else 20)

    rail_y: dict[str, int] = {}
    row_y: list[int] = []
    cur = y_rail0
    rail_nets: list[str] = []
    for d in drains:
        rail_nets.append(d)
    for s in descend_set:
        rail_nets.append(s)
    for c in pas:  # pass terminal nets
        for s in (cn(c[0].source), cn(c[-1].drain)):
            if s not in rail_nets and s not in _RESERVED:
                rail_nets.append(s)
    for t in net.tgates:
        for s in (cn(t.source), cn(t.drain)):
            if s not in rail_nets and s not in _RESERVED:
                rail_nets.append(s)
    for s, _ in net.pulls:
        if cn(s) not in rail_nets and cn(s) not in _RESERVED:
            rail_nets.append(cn(s))
    for s in rail_nets:
        rail_y[s] = cur
        cur += 20
    for _ in range(len(pas) + len(net.tgates)):
        row_y.append(cur + 30)
        cur += 60
    rails_end = cur
    y_gsrc = rails_end + (40 + 60 * (max_dn - 1) + 40 if dn else 20)
    rail_y["VDD"] = y_psrc
    rail_y["GND"] = y_gsrc

    touch: dict[str, list[int]] = {s: [] for s in rail_y}  # xs touching each rail

    # ---- feeds: band-1 lanes down into band-4 rails ----
    for s in descend_set:
        fx = feed_x[s]
        st["lane_need"][s] = max(st["lane_need"].get(s, 0), fx)
        W((fx, st["lane_y"][s]), (fx, rail_y[s]))
        touch[s].append(fx)

    # ---- FET stack columns ----
    def emit_fet(f, loc, facing):
        if facing == "south":  # pull-up: drain at loc, source above, gate left-up
            offs = {"source": (0, -40), "gate": (-20, -20)}
            attrs = {"type": f.kind, "facing": "south", "selloc": "bl"}
        elif facing == "north":  # pull-down: source below, gate left-down
            offs = {"source": (0, 40), "gate": (-20, 20)}
            attrs = {"type": f.kind, "facing": "north", "selloc": "bl"}
        else:  # east (pass rows): source left, gate left-up
            offs = {"source": (-40, 0), "gate": (-20, -20)}
            attrs = {"type": f.kind}
        parts.append(_xml_comp("0", "Transistor", loc, attrs))
        pmap.append((loc, cn(f.drain), "soft-driver"))
        pmap.append(((loc[0] + offs["source"][0], loc[1] + offs["source"][1]),
                     cn(f.source), "sink"))
        gp = (loc[0] + offs["gate"][0], loc[1] + offs["gate"][1])
        pmap.append((gp, cn(f.gate), "sink"))
        return gp

    for bx_c, u_chain, n_chain in columns:
        slot = bx_c - 30  # gap tap slots, stepping left
        if u_chain:
            for i, f in enumerate(u_chain):
                dy_ = y_psrc + 40 + 60 * i
                gp = emit_fet(f, (bx_c, dy_), "south")
                if i + 1 < len(u_chain):
                    W((bx_c, dy_), (bx_c, dy_ + 20))  # drain -> next source
                s = cn(f.gate)
                W((slot, gp[1]), (gp[0], gp[1]))
                W((slot, gp[1]), (slot, rail_y[s]))
                touch[s].append(slot)
                slot -= 10
            d = cn(u_chain[-1].drain)
            W((bx_c, y_psrc + 40 + 60 * (len(u_chain) - 1)), (bx_c, rail_y[d]))
            touch[d].append(bx_c)
            touch["VDD"].append(bx_c)  # head source sits on the VDD subrail
        if n_chain:
            for i, f in enumerate(n_chain):
                dy_ = y_gsrc - 40 - 60 * i
                gp = emit_fet(f, (bx_c, dy_), "north")
                if i + 1 < len(n_chain):
                    W((bx_c, dy_ - 20), (bx_c, dy_))
                s = cn(f.gate)
                W((slot, gp[1]), (gp[0], gp[1]))
                W((slot, rail_y[s]), (slot, gp[1]))
                touch[s].append(slot)
                slot -= 10
            d = cn(n_chain[-1].drain)
            W((bx_c, rail_y[d]), (bx_c, y_gsrc - 40 - 60 * (len(n_chain) - 1)))
            touch[d].append(bx_c)
            touch["GND"].append(bx_c)

    # ---- pass chains and transmission gates: east-facing rows in band 4 ----
    src_jog = cols_right + 10
    dst_jog = rows_right + 20
    row_i = 0
    for c in pas:
        ry = row_y[row_i]; row_i += 1
        for i, f in enumerate(c):
            fx_ = x_tg + 60 * i
            gp = emit_fet(f, (fx_, ry), "east")
            if i + 1 < len(c):
                W((fx_, ry), (fx_ + 20, ry))
            s = cn(f.gate)
            W((gp[0], rail_y[s]), (gp[0], gp[1]))  # gate drop straight from rail
            touch[s].append(gp[0])
        s = cn(c[0].source)
        W((src_jog, ry), (x_tg - 40, ry))
        W((src_jog, rail_y[s]), (src_jog, ry))
        touch[s].append(src_jog)
        src_jog += 10
        d = cn(c[-1].drain)
        tail_x = x_tg + 60 * (len(c) - 1)
        W((tail_x, ry), (dst_jog, ry))
        W((dst_jog, rail_y[d]), (dst_jog, ry))
        touch[d].append(dst_jog)
        dst_jog += 10
    for t in net.tgates:
        ry = row_y[row_i]; row_i += 1
        parts.append(_xml_comp("0", "Transmission Gate", (x_tg, ry), {}))
        pmap += [((x_tg, ry), cn(t.drain), "soft-driver"),
                 ((x_tg - 40, ry), cn(t.source), "sink"),
                 ((x_tg - 20, ry - 20), cn(t.glow), "sink"),
                 ((x_tg - 20, ry + 20), cn(t.ghigh), "sink")]
        for s, gy in ((cn(t.glow), ry - 20), (cn(t.ghigh), ry + 20)):
            xt = src_jog
            src_jog += 10
            W((xt, gy), (x_tg - 20, gy))
            W((xt, rail_y[s]), (xt, gy))
            touch[s].append(xt)
        s = cn(t.source)
        W((src_jog, ry), (x_tg - 40, ry))
        W((src_jog, rail_y[s]), (src_jog, ry))
        touch[s].append(src_jog)
        src_jog += 10
        d = cn(t.drain)
        W((x_tg, ry), (dst_jog, ry))
        W((dst_jog, rail_y[d]), (dst_jog, ry))
        touch[d].append(dst_jog)
        dst_jog += 10

    # ---- pull resistors: port directly on the net's rail (unique jog slot) ----
    for s, val in net.pulls:
        s = cn(s)
        parts.append(_xml_comp("0", "Pull Resistor", (dst_jog, rail_y[s]), {
            "facing": "north", "pull": val,
        }))
        pmap.append(((dst_jog, rail_y[s]), s, "soft-driver"))
        touch[s].append(dst_jog)
        dst_jog += 20

    # ---- power / ground rails ----
    xp = x_fet0 - 20
    if touch["VDD"]:
        parts.append(_xml_comp("0", "Power", (xp, y_psrc), {}))
        pmap.append(((xp, y_psrc), "VDD", "driver"))
        W((xp, y_psrc), (max(touch["VDD"]), y_psrc))
    if touch["GND"]:
        parts.append(_xml_comp("0", "Ground", (xp, y_gsrc), {}))
        pmap.append(((xp, y_gsrc), "GND", "driver"))
        W((xp, y_gsrc), (max(touch["GND"]), y_gsrc))

    # ---- net rails + output pins ----
    for s in rail_nets:
        xs = touch[s]
        if not xs:
            continue
        left, right = min(xs), max(xs)
        if s in {cn(o) for o in net.outputs}:
            k = 0
            for o in net.outputs:
                if cn(o) == s:
                    px = x_right + 40 - 20 * k  # duplicates step left, on-rail
                    k += 1
                    right = max(right, px)
                    parts.append(_xml_comp("0", "Pin", (px, rail_y[s]), {
                        "appearance": "classic", "facing": "west",
                        "type": "output", "label": o,
                    }))
                    pmap.append(((px, rail_y[s]), s, "sink"))
        W((left, rail_y[s]), (right, rail_y[s]))

    _finalize_lanes(st)
    return parts, pmap


def emit_circuit_xml(net: Netlist) -> tuple[str, PortMap]:
    if net.fets or net.tgates or net.pulls:
        parts, pmap = _layout_switch(net)
    else:
        parts, pmap = _layout_routed(net)
    body = "\n".join(parts)
    xml = (
        f'  <circuit name="{escape(net.name)}">\n'
        f'    <a name="appearance" val="logisim_evolution"/>\n'
        f'    <a name="circuit" val="{escape(net.name)}"/>\n'
        f'    <a name="circuitnamedboxfixedsize" val="true"/>\n'
        f'    <a name="simulationFrequency" val="1.0"/>\n'
        f"{body}\n"
        f"  </circuit>"
    )
    return xml, pmap


def emit_project(nets: list[Netlist]) -> str:
    # Skeleton mirrors what Logisim Evolution 4.1.0 itself saves: the full
    # standard-library list, mouse mappings, and a populated toolbar. An empty
    # <toolbar/>/<mappings/> loads and simulates fine but opens a GUI with no
    # tools and dead right-click — looks completely broken to a user.
    libs = "\n".join(
        f'  <lib desc="{d}" name="{i}"/>'
        for i, d in enumerate([
            "#Wiring", "#Gates", "#Plexers", "#Arithmetic", "#FPArithmetic",
            "#Memory", "#I/O", "#TTL", "#TCL", "#Base", "#BFH-Praktika",
            "#Input/Output-Extra", "#Soc",
        ])
    )
    circuits = "\n".join(emit_circuit_xml(n)[0] for n in nets)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<project source="4.1.0" version="1.0">\n'
        "  This file is intended to be loaded by Logisim-evolution "
        "(https://github.com/logisim-evolution/).\n\n"
        f"{libs}\n"
        f'  <main name="{escape(nets[0].name)}"/>\n'
        "  <options>\n"
        '    <a name="gateUndefined" val="ignore"/>\n'
        '    <a name="simlimit" val="1000"/>\n'
        '    <a name="simrand" val="0"/>\n'
        "  </options>\n"
        "  <mappings>\n"
        '    <tool lib="9" map="Button2" name="Poke Tool"/>\n'
        '    <tool lib="9" map="Button3" name="Menu Tool"/>\n'
        '    <tool lib="9" map="Ctrl Button1" name="Menu Tool"/>\n'
        "  </mappings>\n"
        "  <toolbar>\n"
        '    <tool lib="9" name="Poke Tool"/>\n'
        '    <tool lib="9" name="Edit Tool"/>\n'
        '    <tool lib="9" name="Wiring Tool"/>\n'
        '    <tool lib="9" name="Text Tool"/>\n'
        "    <sep/>\n"
        '    <tool lib="0" name="Pin"/>\n'
        '    <tool lib="0" name="Pin">\n'
        '      <a name="facing" val="west"/>\n'
        '      <a name="type" val="output"/>\n'
        "    </tool>\n"
        "    <sep/>\n"
        '    <tool lib="1" name="NOT Gate"/>\n'
        '    <tool lib="1" name="AND Gate"/>\n'
        '    <tool lib="1" name="OR Gate"/>\n'
        '    <tool lib="1" name="XOR Gate"/>\n'
        '    <tool lib="1" name="NAND Gate"/>\n'
        '    <tool lib="1" name="NOR Gate"/>\n'
        "  </toolbar>\n"
        f"{circuits}\n"
        "</project>\n"
    )


# ---------------------------------------------------------------------------
# Layer 1: structural verification (union-find over emitted geometry)
# ---------------------------------------------------------------------------

_GATE_KINDS = {v: k for k, v in _CIRC_NAME.items()}


def _parse_loc(s: str) -> tuple[int, int]:
    a, b = s.strip("()").split(",")
    return (int(a), int(b))


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def _wire_points(a: tuple[int, int], b: tuple[int, int]):
    if a[0] == b[0]:
        lo, hi = sorted((a[1], b[1]))
        return [(a[0], y) for y in range(lo, hi + 1, GRID)]
    if a[1] == b[1]:
        lo, hi = sorted((a[0], b[0]))
        return [(x, a[1]) for x in range(lo, hi + 1, GRID)]
    raise ValueError(f"diagonal wire {a}->{b}")


def analyze_circuit_xml(celem) -> dict:
    """Recompute the netlist Logisim will infer from raw geometry.

    Wire semantics (measured against Logisim 4.1.0 simulation):
      - two wires crossing mid-to-mid do NOT connect;
      - a wire ENDPOINT landing anywhere on another wire connects (T);
      - a component port connects to any wire passing through its location.
    Union-find nodes are wire ids ('w', i) and locations ('p', loc).
    """
    uf = _UF()
    cover: dict = {}   # grid point -> wire ids covering it
    wires = []
    for w in celem.findall("wire"):
        a, b = _parse_loc(w.get("from")), _parse_loc(w.get("to"))
        i = len(wires)
        wires.append((a, b))
        for p in _wire_points(a, b):
            cover.setdefault(p, []).append(i)
    for i, (a, b) in enumerate(wires):
        for end in (a, b):
            uf.union(("w", i), ("p", end))
            for j in cover.get(end, ()):  # endpoint touches wire j => same net
                uf.union(("w", i), ("w", j))
    wire_pts = set(cover)

    # port lists: (comp_descr, port_role, loc)
    ports = []
    tunnels = []  # (loc, label)
    for comp in celem.findall("comp"):
        name = comp.get("name")
        loc = _parse_loc(comp.get("loc"))
        attrs = {a.get("name"): a.get("val") for a in comp.findall("a")}
        if name == "Tunnel":
            tunnels.append((loc, attrs.get("label", "")))
        elif name == "Pin":
            is_out = attrs.get("type") == "output" or attrs.get("output") == "true"
            ports.append((f'pin:{attrs.get("label", "?")}', "sink" if is_out else "driver", loc))
        elif name == "Constant":
            ports.append((f"const@{loc}", "driver", loc))
        elif name in ("Power", "Ground"):
            ports.append((f"{name}@{loc}", "driver", loc))
        elif name == "Pull Resistor":
            ports.append((f"pull@{loc}", "soft-driver", loc))
        elif name in ("Transistor", "Transmission Gate"):
            # Transistor.updatePorts / TransmissionGate.updatePorts, all
            # orientations (validated against GUI-drawn wires in real files)
            facing = attrs.get("facing", "east")
            dx, dy = {"north": (0, 1), "east": (-1, 0),
                      "south": (0, -1), "west": (1, 0)}[facing]
            flip = (facing in ("north", "west")) == (attrs.get("selloc", "tr") == "tr")
            g_a = (loc[0] + 20 * (dx + dy), loc[1] + 20 * (-dx + dy))
            g_b = (loc[0] + 20 * (dx - dy), loc[1] + 20 * (dx + dy))
            gid = f"{'fet' if name == 'Transistor' else 'tgate'}@{loc}"
            ports.append((f"{gid}.drain", "soft-driver", loc))
            ports.append((f"{gid}.source", "sink", (loc[0] + 40 * dx, loc[1] + 40 * dy)))
            ports.append((f"{gid}.gate0", "sink", g_a if flip else g_b))
            if name == "Transmission Gate":
                ports.append((f"{gid}.gate1", "sink", g_b if flip else g_a))
        elif name in _GATE_KINDS:
            kind = _GATE_KINDS[name]
            n = 1 if kind == "NOT" else int(attrs.get("inputs", "5"))
            ins, out = gate_ports(kind, n)
            gid = f"{kind}@{loc}"
            for i, (dx, dy) in enumerate(ins):
                ports.append((f"{gid}.in{i}", "sink", (loc[0] + dx, loc[1] + dy)))
            ports.append((f"{gid}.out", "driver", (loc[0] + out[0], loc[1] + out[1])))

    # ports and tunnels connect to every wire passing through their location
    for _, _, loc in ports:
        for j in cover.get(loc, ()):
            uf.union(("p", loc), ("w", j))
    for loc, _ in tunnels:
        for j in cover.get(loc, ()):
            uf.union(("p", loc), ("w", j))

    # merge same-label tunnels into one electrical net, then collect the
    # full label-set per physical net (a net may legitimately carry several
    # labels when the netlist aliases them; the caller judges that).
    label_loc: dict[str, tuple[int, int]] = {}
    for loc, label in tunnels:
        if label in label_loc:
            uf.union(("p", loc), ("p", label_loc[label]))
        label_loc[label] = loc
    root_labels: dict = {}
    for loc, label in tunnels:
        root_labels.setdefault(uf.find(("p", loc)), set()).add(label)

    port_labels: dict[str, set] = {}   # port descr -> labels on its net
    floating: list[str] = []
    drivers: dict[str, list[tuple[str, str]]] = {}  # label -> [(descr, hard|soft)]
    loc_count: dict = {}               # ports may also touch port-to-port
    for _, _, loc in ports:
        loc_count[loc] = loc_count.get(loc, 0) + 1
    for descr, role, loc in ports:
        labels = root_labels.get(uf.find(("p", loc)), set())
        port_labels[descr] = labels
        if loc not in wire_pts and loc_count[loc] < 2:
            floating.append(f"FLOATING: {descr} ({role}) touches nothing")
        elif role.endswith("driver"):
            cls = "soft" if role == "soft-driver" else "hard"
            for lbl in labels:
                drivers.setdefault(lbl, []).append((descr, cls))
    return {
        "floating": floating,
        "port_labels": port_labels,
        "drivers": drivers,
        "label_groups": [sorted(v) for v in root_labels.values() if len(v) > 1],
    }


def structural_check(circ_path: str, net: Netlist) -> dict:
    """Verify the wires actually written to disk realize the intended netlist.

    The emitter deterministically maps every component port to (location,
    signal, role); this re-derives that map, then checks it against the
    file's wire geometry under measured Logisim connection semantics.
    Catches emitter/router bugs: splits, shorts, floats, driver conflicts.
    (Port-offset-table errors are the behavioral layer's job.)
    """
    root = ET.parse(circ_path).getroot()
    celem = next(
        (c for c in root.findall("circuit") if c.get("name") == net.name), None
    )
    if celem is None:
        return {"ok": False, "errors": [f"circuit {net.name!r} not in file"]}
    _, pmap = emit_circuit_xml(net)

    # union-find over the file's wires (same semantics as analyze_circuit_xml)
    uf = _UF()
    cover: dict = {}
    wires = []
    for w in celem.findall("wire"):
        a, b = _parse_loc(w.get("from")), _parse_loc(w.get("to"))
        i = len(wires)
        wires.append((a, b))
        for p in _wire_points(a, b):
            cover.setdefault(p, []).append(i)
    for i, (a, b) in enumerate(wires):
        for end in (a, b):
            uf.union(("w", i), ("p", end))
            for j in cover.get(end, ()):
                uf.union(("w", i), ("w", j))

    tunnels = []
    for comp in celem.findall("comp"):
        if comp.get("name") == "Tunnel":
            loc = _parse_loc(comp.get("loc"))
            label = next((a.get("val") for a in comp.findall("a")
                          if a.get("name") == "label"), "")
            tunnels.append((loc, label))
    label_loc: dict[str, tuple[int, int]] = {}
    for loc, label in tunnels:
        for j in cover.get(loc, ()):
            uf.union(("p", loc), ("w", j))
        if label in label_loc:
            uf.union(("p", loc), ("p", label_loc[label]))
        label_loc[label] = loc
    for loc, _, _ in pmap:
        for j in cover.get(loc, ()):
            uf.union(("p", loc), ("w", j))

    # alias classes: signals tied by `Y = A` style assignments are ONE net
    cls = _UF()
    for dst, src in net.aliases.items():
        cls.union(dst, src)

    errors: list[str] = []
    # group intended ports (and same-named tunnels) by alias class
    class_locs: dict[str, list] = {}
    class_roles: dict[str, list[tuple[str, str]]] = {}
    for loc, sig, role in pmap:
        c = cls.find(sig)
        class_locs.setdefault(c, []).append(loc)
        class_roles.setdefault(c, []).append((f"{sig}@{loc}", role))
    for loc, label in tunnels:  # tunnel labels are signal names in our files
        class_locs.setdefault(cls.find(label), []).append(loc)

    # every alias class must be exactly one electrical net
    net_of_class: dict[str, object] = {}
    for c, locs in sorted(class_locs.items()):
        roots = {uf.find(("p", l)) for l in locs}
        if len(roots) > 1:
            errors.append(f"SPLIT: net {c!r} is {len(roots)} disconnected pieces")
        net_of_class[c] = min(roots, key=repr)
    # ...and no two classes may share one
    seen: dict = {}
    for c, r in sorted(net_of_class.items()):
        if r in seen:
            errors.append(f"SHORT: nets {seen[r]!r} and {c!r} are connected")
        else:
            seen[r] = c

    # driver discipline per class: at most one hard driver (pin, gate output,
    # constant, Power/Ground), never mixed with switched drivers (FET drains,
    # pulls). Many switched drivers may share a net — that's CMOS; Logisim's
    # simulator polices dynamic contention during the vector run.
    for c, roles in sorted(class_roles.items()):
        hard = sorted(d for d, k in roles if k == "driver")
        soft = sorted(d for d, k in roles if k == "soft-driver")
        if len(hard) > 1:
            errors.append(f"MULTIDRIVE: net {c!r} driven by {hard}")
        elif hard and soft:
            errors.append(f"CONTENTION: net {c!r} has hard driver {hard} vs switched {soft}")
        elif not hard and not soft:
            errors.append(f"UNDRIVEN: net {c!r} has no driver")
    return {"ok": not errors, "errors": errors}


# ---------------------------------------------------------------------------
# Layers 2+3: Logisim headless simulation
# ---------------------------------------------------------------------------

def find_jar(explicit: str | None = None) -> str:
    cands = [explicit, os.environ.get("LOGISIM_JAR")]
    cands += sorted(
        glob.glob("/Applications/Logisim-evolution.app/Contents/app/logisim-evolution-*.jar"),
        reverse=True,
    )
    cands += sorted(glob.glob(str(Path.home() / "logisim-evolution-*.jar")), reverse=True)
    for c in cands:
        if c and Path(c).is_file():
            return c
    raise FileNotFoundError(
        "logisim-evolution jar not found; set $LOGISIM_JAR or pass --jar"
    )


def run_logisim(jar: str, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    cmd = ["java", "-jar", jar, *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, returncode=-1, stdout="",
            stderr=f"timeout: logisim did not finish within {timeout}s "
                   "(combinational loop? raise timeout?)",
        )
    except FileNotFoundError:
        raise FileNotFoundError("`java` not found on $PATH; install a JRE >= 16")


def load_check(jar: str, circ_path: str) -> dict:
    r = run_logisim(jar, ["--tty", "stats", circ_path], timeout=30)
    return {"ok": r.returncode == 0, "exit_code": r.returncode,
            "stderr": r.stderr[-2000:] if r.returncode else ""}


_MAX_EXHAUSTIVE_INPUTS = 11  # 2^11 = 2048 vector rows


def make_vectors(c: CircuitDef) -> tuple[str, list[dict]]:
    """Returns (vec file text, per-row {inputs, expected}) for failure echo."""
    n = len(c.inputs)
    header = " ".join(c.inputs + c.outputs)
    lines, rows = [], []
    if n <= _MAX_EXHAUSTIVE_INPUTS:
        combos = itertools.product((0, 1), repeat=n)
    else:  # deterministic pseudo-random sample
        import random
        rng = random.Random(0)
        combos = ([rng.randint(0, 1) for _ in range(n)] for _ in range(2048))
    for combo in combos:
        env = dict(zip(c.inputs, combo))
        outs = eval_circuit(c, env)
        lines.append(" ".join(str(v) for v in list(combo) + [outs[o] for o in c.outputs]))
        rows.append({"inputs": env, "expected": outs})
    return header + "\n" + "\n".join(lines) + "\n", rows


_RE_TOTAL = re.compile(r"Passed:\s*(\d+),\s*Failed:\s*(\d+)")


_RE_ROW_NUM = re.compile(r"^\s*(\d+)\s*$")
_RE_MISMATCH = re.compile(r"^\s+(?P<signal>\S+)\s*=\s*(?P<got>\S+)\s*\(expected\s+(?P<expected>\S+)\)")


def behavioral_check(jar: str, circ_path: str, c: CircuitDef, timeout: int = 120) -> dict:
    vec, rows = make_vectors(c)
    with tempfile.NamedTemporaryFile("w", suffix=".vec", delete=False) as fh:
        fh.write(vec)
        vec_path = fh.name
    try:
        r = run_logisim(jar, ["--test-vector", c.name, vec_path, circ_path], timeout=timeout)
    finally:
        os.unlink(vec_path)
    m = _RE_TOTAL.search(r.stdout + "\n" + r.stderr)
    passed, failed = (int(m.group(1)), int(m.group(2))) if m else (0, -1)

    # stdout interleaves "N" row markers with "  sig = got (expected want)"
    # mismatch lines; echo the driving inputs so the failure is actionable
    # without reasoning about row numbers. (got U = floating, E = contention)
    failures, row_no = [], None
    for ln in r.stdout.splitlines():
        if nm := _RE_ROW_NUM.match(ln):
            row_no = int(nm.group(1))
        elif (dm := _RE_MISMATCH.match(ln)) and row_no is not None:
            entry = dict(dm.groupdict())
            if 1 <= row_no <= len(rows):
                entry["row"] = row_no
                entry["inputs"] = rows[row_no - 1]["inputs"]
            failures.append(entry)
    result = {
        "ok": r.returncode == 0 and failed == 0 and passed > 0,
        "passed": passed, "failed": failed,
        "rows": len(rows),
        "failures": failures[:10],
    }
    if not result["ok"] and not failures:  # e.g. timeout or jar crash
        result["stderr"] = r.stderr[-1000:]
    return result


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------

def describe(circ_path: str) -> dict:
    root = ET.parse(circ_path).getroot()
    out = {"main": None, "circuits": []}
    main = root.find("main")
    if main is not None:
        out["main"] = main.get("name")
    for celem in root.findall("circuit"):
        a = analyze_circuit_xml(celem)
        comps = {}
        pins_in, pins_out = [], []
        for comp in celem.findall("comp"):
            name = comp.get("name")
            comps[name] = comps.get(name, 0) + 1
            if name == "Pin":
                attrs = {x.get("name"): x.get("val") for x in comp.findall("a")}
                is_out = attrs.get("type") == "output" or attrs.get("output") == "true"
                (pins_out if is_out else pins_in).append(attrs.get("label", "?"))
        out["circuits"].append({
            "name": celem.get("name"),
            "inputs": pins_in,
            "outputs": pins_out,
            "components": comps,
            "nets": {k: v for k, v in sorted(a["drivers"].items())},
            "bridged_labels": a["label_groups"],
            "structural_errors": a["floating"],
        })
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_build(args) -> int:
    src = Path(args.source).read_text()
    circuits = parse_logic(src)
    nets = [compile_netlist(c) for c in circuits]
    out_path = args.output or str(Path(args.source).with_suffix(".circ"))
    Path(out_path).write_text(emit_project(nets))

    report = {"circ": str(Path(out_path).resolve()), "circuits": {}}
    ok = True
    jar = None
    if not args.skip_sim:
        try:
            jar = find_jar(args.jar)
        except FileNotFoundError as e:
            report["warning"] = f"{e}; skipping load/behavioral checks"

    for c, net in zip(circuits, nets):
        entry = {"gates": len(net.gates)}
        s = structural_check(out_path, net)
        entry["structural"] = s
        ok &= s["ok"]
        if jar:
            try:
                b = behavioral_check(jar, out_path, c)
                entry["behavioral"] = b
                ok &= b["ok"]
            except ValueError as e:  # switch-driven output without a spec
                entry["behavioral"] = {"skipped": str(e)}
        report["circuits"][c.name] = entry
    if jar:
        l = load_check(jar, out_path)
        report["load"] = l
        ok &= l["ok"]
    report["ok"] = ok
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


def cmd_check(args) -> int:
    d = describe(args.circ)
    errs = [e for c in d["circuits"] for e in c["structural_errors"]]
    result = {"ok": not errs, "structural_errors": errs}
    try:
        jar = find_jar(args.jar)
        l = load_check(jar, args.circ)
        result["load"] = l
        result["ok"] = result["ok"] and l["ok"]
    except FileNotFoundError as e:
        result["warning"] = str(e)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def cmd_describe(args) -> int:
    print(json.dumps(describe(args.circ), indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="circuitc", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="compile .logic -> verified .circ")
    b.add_argument("source")
    b.add_argument("-o", "--output")
    b.add_argument("--jar", help="path to logisim-evolution jar")
    b.add_argument("--skip-sim", action="store_true",
                   help="structural check only (no java)")
    b.set_defaults(fn=cmd_build)

    ck = sub.add_parser("check", help="verify an existing .circ structurally + load")
    ck.add_argument("circ")
    ck.add_argument("--jar")
    ck.set_defaults(fn=cmd_check)

    d = sub.add_parser("describe", help="JSON netlist summary of a .circ")
    d.add_argument("circ")
    d.set_defaults(fn=cmd_describe)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (SyntaxError, ValueError, FileNotFoundError, ET.ParseError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

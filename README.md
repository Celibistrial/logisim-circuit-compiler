# circuitc

Compile boolean expressions and CMOS transistor netlists into **verified**
[Logisim Evolution](https://github.com/logisim-evolution/logisim-evolution)
`.circ` files. One file, stdlib-only Python, no dependencies.

Built so an LLM (or a tired student) never touches coordinates or XML:
you write expressions, the tool does geometry, wiring, and three layers of
verification — ending with exhaustive simulation in Logisim itself.

## Install

```bash
curl -O https://raw.githubusercontent.com/Celibistrial/logisim-circuit-compiler/main/circuitc.py
```

Requirements: Python ≥ 3.10. For the simulation layers: Java ≥ 16 and the
Logisim Evolution jar (auto-found in the macOS `.app`, or set `$LOGISIM_JAR`).
Without Java you still get structural verification.

## Quick start

```bash
python3 circuitc.py build examples/full_adder.logic -o full_adder.circ
```

Exit code 0 + `"ok": true` in the JSON report means the circuit **provably
computes the source expressions** (verified by exhaustive simulation in
Logisim itself, not just by construction).

## Source format (`.logic`)

```
# comments with '#'
circuit full_adder:
  inputs A, B, Cin
  outputs S, Cout
  AxB = A ^ B                      # intermediate net (any non-output target)
  S = AxB ^ Cin
  Cout = (A & B) | (Cin & AxB)
```

- Operators: `~` not, `&` and, `^` xor, `|` or, parens, constants `0` `1`.
  Precedence (high→low): `~  &  ^  |` (Python-like).
- Assignments must come after their operands (no forward refs, no cycles).
- Multiple `circuit` blocks per file; the first becomes the project's main.
- 1-bit signals only. No buses, no sequential logic (yet).

### Hierarchy (subcircuit instances)

```
circuit adder2:
  inputs A1, A0, B1, B0
  outputs S1, S0, C
  use full_adder fa0(A=A0, B=B0, Cin=0) -> (S=S0, Cout=c0)
  use full_adder fa1(A=A1, B=B1, Cin=c0) -> (S=S1, Cout=c1)
  C = c1
```

`use CIRC label(In=sig,...) -> (Out=sig,...)` places CIRC as a subcircuit
box. CIRC must be defined earlier in the file or already exist in a
`--merge` target. All input pins must be wired (constants `0`/`1` allowed);
outputs may be left off. Box port geometry is transcribed from Logisim's
DefaultEvolutionAppearance and proven by the behavioral layer.

### Switch-level primitives (transistors)

```
circuit cmos_nand:
  inputs A, B
  outputs Y
  spec Y = ~(A & B)     # golden model — test vectors come from this line
  pmos A: VDD -> Y      # gate: source -> drain
  pmos B: VDD -> Y
  nmos A: GND -> n1     # series stack via internal net n1
  nmos B: n1 -> Y

tgate CH, CL: X -> Y    # transmission gate (active-high ctrl, active-low ctrl)
pullup Y  /  pulldown Y # pull resistor to 1 / 0
```

- `VDD` / `GND` are reserved nets; Power/Ground components appear automatically.
- Logisim FETs are **unidirectional**: source passes to drain when the gate is
  active (N: gate=1, P: gate=0), else the drain floats. So N pull-down networks
  read `GND -> out` (source at GND, drain at the output — real CMOS terminology).
- Any net driven only by FETs/pulls needs a `spec` for each output that depends
  on it, or the behavioral layer is skipped (structural + load still run).
- Gates and transistors mix freely in one circuit (e.g. `~S` for a tgate mux).
- Static driver rules enforced structurally: max one hard driver (pin, gate
  output, constant, Power/Ground) per net, never mixed with switched drivers.
  Dynamic contention/floating is Logisim's job: it yields `E`/`U`, which fails
  the vector run with the exact row.

## Commands

| Command | Does |
|---|---|
| `build SRC [-o OUT] [--merge] [--lib L.logic] [--jar J] [--skip-sim]` | compile + all 3 verification layers, JSON report |
| `verify FILE.circ --spec SPEC.logic [--circuit NAME]` | test ANY .circ (incl. hand-drawn) against golden models |
| `check FILE.circ` | structural + load check of an existing file |
| `describe FILE.circ` | JSON netlist summary (pins, components, nets) |

`--merge` adds/replaces circuits inside an existing .circ, keeping everything
else in it — and new circuits may instance the file's existing circuits.

`--skip-sim` skips the behavioral layer (useful when no Logisim jar is
available). The structural check still runs, so the circuit is guaranteed
electrically correct — just not simulated exhaustively.

Jar discovery: `--jar`, `$LOGISIM_JAR`, then
`/Applications/Logisim-evolution.app/Contents/app/logisim-evolution-*.jar`.

Example failure report — behavioral mismatches echo the exact inputs, so
fixing the `.logic` never requires decoding row numbers:

```json
"failures": [
  {"signal": "Y", "got": "U", "expected": "1", "row": 2, "inputs": {"A": 0, "B": 1}}
]
```

(`U` = floating net, `E` = contention — Logisim's switch-level simulator
polices both during the vector run.)

## Why verification is the whole point

Logisim connectivity is defined by pixel coordinates: two wires that touch are
one electrical net, and a wire that misses a port by 10px silently floats.
LLM-placed coordinates get this wrong constantly (a popular logisim MCP server
shorts unrelated nets on any 3-input gate — its port table is off by 10px).

`build` therefore:

1. **structural** — re-derives the netlist from raw emitted geometry
   (union-find over wire pixels + port offsets transcribed from
   logisim-evolution's Java source) and diffs it against the intended netlist.
   Catches shorts, floating ports, double-driven nets.
2. **load** — `--tty stats` headless load in the real Logisim. Catches XML rot.
3. **behavioral** — generates a test vector for **every input combination**
   (≤11 inputs; 2048 random rows above that) by evaluating the source
   expressions, then runs Logisim's `--test-vector`. Catches everything else.

All circuits are drawn schematic-style with real routed wires — **zero
tunnels**. Gate-level: input pins left, horizontal signal lanes, gate columns
by logic depth, output pins right. Switch-level: Power rail on top, PMOS
stacks facing down, horizontal net rails, NMOS stacks facing up, Ground rail
at the bottom — the way CMOS is drawn in textbooks. Wire-connection semantics
(crossings don't connect; T-junctions and ports-on-wires do) were measured
against Logisim 4.1.0 simulation, and the structural layer re-checks every
emitted file against them.

## For LLMs: recommended loop

1. Write `design.logic` (expressions only — never edit `.circ` XML by hand).
2. `python3 circuitc.py build design.logic -o design.circ`
3. Read the JSON. `"ok": true` → done, ship the `.circ`.
   Failures name the circuit, the failing vector rows, or the exact
   shorted/floating port — fix the `.logic`, rebuild.

## Test

```bash
python3 test_circuitc.py   # geometry, parser, roundtrip, sabotage-detection, sim
```

## Further documentation

- [DEVELOPMENT.md](DEVELOPMENT.md) — architecture, data structures, layout
  engines, verification layers, adding new features.
- [AGENTS.md](AGENTS.md) — concise instructions for LLM agents using this tool.

```bash
python3 test_circuitc.py   # geometry, parser, roundtrip, sabotage-detection, sim
```

## Not implemented (deliberately)

- Buses / multi-bit signals — add a width syntax + splitters when needed.
- Sequential logic (registers, clocks, cross-coupled latches) — needs
  `--test-circuit` benches instead of vectors.
- Instances inside switch-level (FET) circuits — keep `use` and transistors
  in separate circuits.

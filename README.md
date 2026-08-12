# circuitc

Compile boolean expressions and CMOS transistor netlists into **verified**
[Logisim Evolution](https://github.com/logisim-evolution/logisim-evolution)
`.circ` files. The core compiler is single-file, stdlib-only Python with no
dependencies.

The core `circuitc.py` compiler remains a single file. The optional
structure-aware workflow uses the stdlib-only companion modules
`schematic.py`, `datapath_templates.py`, and `datapathc.py` from this repo.

Built so an LLM (or a tired student) never touches coordinates or XML:
you write expressions, the tool does geometry, wiring, and three layers of
verification — ending with exhaustive simulation in Logisim itself.

## Install

```bash
curl -O https://raw.githubusercontent.com/Celibistrial/logisim-circuit-compiler/main/circuitc.py
```

Clone the repository instead when using declarative datapath templates.

Requirements: Python ≥ 3.10. For Logisim Evolution 4.1 simulation: Java ≥ 21 and the
Logisim Evolution jar (auto-found in the macOS `.app`, or set `$LOGISIM_JAR`).
Without Java you still get structural verification.

## Quick start

```bash
python3 circuitc.py build examples/full_adder.logic -o full_adder.circ
# Numbered public pins (A0,A1,...) become real bus pins + splitters:
python3 circuitc.py build design.logic -o design.circ --busify
```

For regular multi-bit datapaths, use the deterministic template compiler. It
needs only JSON data—no LLM, XML editing, or coordinate choices:

```bash
python3 datapathc.py templates
python3 datapathc.py build examples/compare_minmax.json \
  -o examples/compare_minmax.circ
```

```json
{
  "version": 1,
  "main": "minmax8",
  "circuits": [
    {"name": "compare8", "template": "unsigned_comparator", "width": 8},
    {"name": "minmax8", "template": "unsigned_minmax", "width": 8}
  ]
}
```

`datapathc` currently provides `unsigned_comparator` and `unsigned_minmax`.
It emits a scalar `.logic` golden model and a bus-aware `.circ`, runs all six
physical checks, and evaluates the emitted wire geometry against integer
semantics. Widths 2–16 are accepted; changing width or circuit name is a
data-only regeneration.

For `circuitc build`, exit code 0 plus successful `behavioral` and `load`
entries means the scalar source passed Logisim simulation. If the report has a
missing-jar warning or `--skip-sim` was used, only the offline structural layer
ran. For `datapathc build`, `"ok": true` means all offline physical checks and
the template's independent physical-net vectors passed; this does not pretend
that Logisim itself ran when no compatible jar was available.

## What changed, and what did not

The original compiler has not been replaced. Existing `.logic` files and
existing `circuitc.py build`, `verify`, CMOS, hierarchy, editing, and analysis
workflows remain valid. The change has two parts:

1. The core compiler gained deterministic layout hashing, real splitter-aware
   geometry, width checking, hierarchy-safe automatic busification, `--main`,
   and `--busify`.
2. An optional structure-aware layer was added for datapaths whose scalar
   lowering is electrically correct but visually poor.

This is a large extension in capability, but a small compatibility change:
there is no new `.logic` syntax, no replacement parser, and no requirement to
use templates. Generic designs still go through `circuitc.py`; registered
regular datapaths may go through `datapathc.py`.

| File | Responsibility |
|---|---|
| `circuitc.py` | Original scalar parser/compiler/router and verification CLI; now splitter- and width-aware |
| `schematic.py` | Circuit-family-neutral, grid-checked XML emitter and physical wire/tunnel/splitter/hierarchy evaluator |
| `compact_arithmetic.py` | Rule-based adder, add/subtract, and array-multiplier layouts used by the arithmetic example generator |
| `datapath_templates.py` | Versioned JSON validation, scalar-oracle expansion, comparator/min/max layout rules, and integer verification |
| `datapathc.py` | Small JSON command-line frontend; lists templates and builds verified artifacts |
| `examples/compare_minmax.json` | Editable source of truth for the generated comparator/min/max example |
| `examples/compare_minmax.logic` | Deterministically generated scalar golden model; do not hand-edit |
| `examples/compare_minmax.circ` | Deterministically generated Logisim project; do not hand-edit |

The template layer is a registry, not general-purpose synthesis. A genuinely
new visual family still needs one reviewed topology/layout implementation.
After registration, widths and names become inexpensive JSON-only builds.

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
| `build SRC [-o OUT] [--main C] [--busify] [--merge] [--lib L.logic] [--jar J] [--skip-sim]` | compile + verify; optionally choose main and busify public pins |
| `busify FILE.circ [CIRCUIT]` | replace numbered scalar boundary pins with a bus pin + wired Splitter |
| `verify FILE.circ --spec SPEC.logic [--circuit NAME]` | test ANY .circ (incl. hand-drawn) against golden models |
| `check FILE.circ` | structural + load check of an existing file |
| `check-widths FILE.circ` | detect incompatible bit widths on a connected net |
| `describe FILE.circ` | JSON netlist summary (pins, components, nets) |

Companion CLI:

| Command | Does |
|---|---|
| `datapathc.py templates` | list registered declarative circuit families |
| `datapathc.py build SPEC.json -o OUT.circ [--logic OUT.logic]` | generate scalar oracle + structured circuit and run offline physical verification |

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

With `--busify`, scalar behavioral verification runs first. The compiler then
converts only public (unreferenced) circuit boundaries, inserts real Splitter
components, rewires every former bit pin, and reruns all offline geometry,
width, and hierarchy checks. Reusable cores remain scalar; expose a busified
wrapper when a core is also instantiated elsewhere in the project.

Generic circuits are drawn schematic-style with real routed wires and no
tunnels. Gate-level: input pins left, horizontal signal lanes, gate columns
by logic depth, output pins right. Structure-specific datapath backends may
keep inter-block buses wide and use short, labelled tunnels for carries or
repeated partial-product nets; this avoids turning arithmetic into a wall of
scalar return wires. Switch-level: Power rail on top, PMOS
stacks facing down, horizontal net rails, NMOS stacks facing up, Ground rail
at the bottom — the way CMOS is drawn in textbooks. Wire-connection semantics
(crossings don't connect; T-junctions and ports-on-wires do) were measured
against Logisim 4.1.0 simulation, and the structural layer re-checks every
emitted file against them.

The structure-aware path is not an LLM post-processor. `schematic.py` owns the
grid-safe XML primitives and physical evaluator; `datapath_templates.py` owns
registered topology/layout rules; and `datapathc.py` is the data-only CLI.
Generated `.circ` files are disposable artifacts and should be rebuilt from
their JSON/`.logic` sources.

## For LLMs: recommended loop

1. Write `design.logic` (expressions only — never edit `.circ` XML by hand).
2. `python3 circuitc.py build design.logic -o design.circ --busify`
3. Read the JSON. `"ok": true` → done, ship the `.circ`.
   Failures name the circuit, the failing vector rows, or the exact
   shorted/floating port — fix the `.logic`, rebuild.

For a supported multi-bit family, prefer the cheaper loop:

1. Write or edit the small JSON spec.
2. Run `python3 datapathc.py build spec.json -o design.circ`.
3. Ship only after the JSON report says `"ok": true`.

An LLM is only needed to implement a genuinely new circuit family that has no
registered template yet. Once that family is registered, all widths and names
are generated reproducibly without model involvement.

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

- Native bus expressions in `.logic` (public numbered pins can still be
  converted automatically with `--busify`).
- Sequential logic (registers, clocks, cross-coupled latches) — needs
  `--test-circuit` benches instead of vectors.
- Instances inside switch-level (FET) circuits — keep `use` and transistors
  in separate circuits.

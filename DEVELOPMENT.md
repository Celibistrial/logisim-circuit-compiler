# circuitc — developer guide

Architecture reference for contributors. For usage, see [README.md](README.md).

## Architecture overview

```
.logic source
     │
     ▼
 parse_logic()         ── text → list[CircuitDef]
     │
     ▼
 compile_netlist()     ── CircuitDef → Netlist (gates, FETs, instances, aliases)
     │
     ▼
 emit_circuit_xml()    ── Netlist → XML parts + PortMap
     │  ├─ _route_gates()    gate-level schematic router
     │  └─ _layout_switch()  CMOS switch-level layout
     ▼
 emit_project()        ── list[Netlist] → complete .circ XML string
     │
     ▼
 ┌─ structural_check()   checks emitted geometry against intended netlist
 ├─ load_check()         headless load in Logisim Evolution
 └─ behavioral_check()   exhaustive simulation in Logisim Evolution
     │
     ▼ (--busify, after scalar behavioral verification)
 _busify_tree()        ── public numbered pins → bus pins + Splitters
     │
     └─ check_all()       geometry + width + hierarchy re-check

 schematic.py          ── reusable grid-safe emitter + physical XML evaluator
      │
      ├─ compact_arithmetic.py ── adder/addsub/array-multiplier templates
      └─ datapath_templates.py ── JSON-registered comparator/minmax templates
                                   │
                                   └─ datapathc.py data-only CLI
```

The `build` command chains all stages end-to-end. Every stage can also be
called independently (e.g. `verify` skips compilation and only runs
behavioral checks).

### Compatibility boundary

`circuitc.py` is still the generic scalar compiler. Its parser, AST, netlist,
gate/switch layout engines, and existing CLI remain the default path. The
structure-aware modules are companions rather than a replacement:

- They consume generated scalar semantics instead of introducing native bus
  expressions into `.logic`.
- They replace presentation circuits by circuit name before `_rewrite()`;
  they never patch an existing `.circ` by guessing coordinates.
- They finish with `check_all()` plus a physical-net evaluator independent of
  the scalar evaluator.
- `datapathc.py` only accepts registered families and validated parameters; it
  is not an unrestricted netlist-to-pretty-schematic claim.

The core modifications are deliberately reusable prerequisites: stable CRC32
layout jitter, splitter port geometry, width-constraining ports,
hierarchy-safe `_busify_tree()`, and compact check summaries. Existing source
programs do not need migration.

## Key data structures

### `CircuitDef` (parse phase)

```python
@dataclass
class CircuitDef:
    name: str
    inputs: list[str]
    outputs: list[str]
    assigns: list[tuple[str, object]]   # (target, expression AST)
    specs: list[tuple[str, object]]     # golden-model expressions
    fets: list[Fet]                     # CMOS transistors
    tgates: list[TGate]                 # transmission gates
    pulls: list[tuple[str, str]]        # pull-up/pull-down nets
    insts: list[Inst]                   # subcircuit instantiations
    stmts: list[tuple]                  # ordered statements (assign | inst)
```

Expression ASTs are nested tuples:
- `("const", 0|1)` — constant
- `("var", name)` — variable reference
- `("not", child)`, `("and", l, r)`, `("or", l, r)`, `("xor", l, r)`

### `Netlist` (compile phase)

```python
@dataclass
class Netlist:
    name: str
    inputs: list[str]
    outputs: list[str]
    insts: list[Inst]
    gates: list[Gate]                # AND, OR, XOR, NAND, NOR, NOT
    aliases: dict[str, str]          # Y = A → {"Y": "A"}
    const_nets: dict[str, int]       # signal → 0/1
    fets: list[Fet]
    tgates: list[TGate]
    pulls: list[tuple[str, str]]
    nodes: list[tuple]               # ordered ("gate",Gate) | ("inst",Inst)
```

`compile_netlist()` decomposes high-fanin gates (>5 inputs, see `_MAX_FANIN`)
into trees and propagates constants where possible.

### `Inst` (subcircuit instance)

```python
@dataclass
class Inst:
    circ: str                         # subcircuit name
    label: str                        # unique label in parent circuit
    ins: list[tuple[str, str]]        # (subcircuit_pin, parent_signal)
    outs: list[tuple[str, str]]       # (subcircuit_pin, parent_signal)
```

### `PortMap`

`list[tuple[(x,y), signal, role]]` where `role` is `"driver"`, `"sink"`,
or `"soft-driver"` (FET drain). This is the contract between the layout
emitter and the structural verifier.

## Layout engines

The tool has two layout strategies, dispatched automatically by
`emit_circuit_xml()`:

### Gate-level (`_route_gates`)

For circuits with only gates and subcircuit instances. Produces a
"textbook" schematic:

- Input pins on the left (x=60), each on its own horizontal lane.
- Horizontal signal lanes above the gate area.
- Gate columns ordered by logic depth.
- Vertical **drops** from lanes into gate inputs, vertical **risers** from
  gate outputs back to lanes.
- Output pins on the right edge, facing west.

Key algorithm:
1. Compute logic depth for every signal.
2. Group nodes into columns by depth.
3. Classify signals as lane-promoted (seen by multiple consumers or
   spanning columns) vs straight-wire (single consumer in next column,
   drawn as one wire with no lane hop).
4. Plan rows within each column: attempt to row-align gates with
   straight-wire inputs; failing that, place at the next free y.
5. Place components, wires, and output pins.

### Switch-level (`_layout_switch`)

For circuits containing FETs, transmission gates, or pull resistors.
Produces a banded CMOS layout:

1. Input pins / gate-level logic (reuses `_route_gates`) — lanes on top.
2. VDD horizontal subrail.
3. PMOS stacks facing south.
4. Horizontal net rails + tgate/pass-FET rows + output pins.
5. NMOS stacks facing north.
6. GND horizontal subrail.

PMOS and NMOS stacks sharing the same output net are paired into columns.

### Layout spacing constants

All in `_route_gates()`, tuned in 20 px multiples (grid is 10 px):

| Constant | Default | Controls |
|---|---|---|
| Lane Y spacing | 20 px | Vertical gap between signal lanes |
| `X_PIN` | 60 | X position of input pins |
| `chan_x` gap | 30 px | Horizontal gap from previous column |
| Drop X spacing | 20 px | Horizontal gap between vertical drop wires |
| Riser X spacing | 20 px | Horizontal gap between vertical riser wires |
| Output advance | 60 px | Gap before output pin column |
| Gate vertical clearance | 25 px | Minimum gap above/below each gate |
| Gate cursor advance | 70 px | Vertical step after placing a gate |

Switch-level cell grid:

| Constant | Default | Controls |
|---|---|---|
| `CELL_W`, `CELL_H` | 200, 90 | Size of each FET placement cell |
| `COLS` | 6 | Grid columns |
| `ORIGIN` | (200, 100) | Top-left baseline offset |
| `STUB` | 20 | Stub wire length from port to tunnel |

### Gate geometry constants

Transcribed from Logisim Evolution's Java source (`AbstractGate.getInputOffset`,
`NotGate.configurePorts`, `XorGate`):

| Function | Returns |
|---|---|
| `gate_axis(kind)` | X-distance from output anchor back to input column (NOT=30, AND/OR=50, NAND/NOR/XOR=60, XNOR=70) |
| `input_dys(n)` | Per-input Y-offsets relative to anchor (2 inputs: [-20,20]; 3: [-20,0,20]; 4: [-20,-10,10,20]; 5: [-20,-10,0,10,20]) |
| `gate_ports(kind, n)` | Combined (input port offsets, output port offset) |

**Do not change these** unless the Logisim Evolution version you target uses
different dimensions. Misaligned port offsets cause silent floating/short
errors that structural verification will catch.

## Verification layers

### 1. Structural (`structural_check`)

- Rebuilds the PortMap from the Netlist (using current geometry functions).
- Parses the emitted .circ file: union-find over all wire grid points.
- Groups port locations by signal (honoring aliases).
- Checks every signal group forms exactly one electrical net (no splits).
- Checks distinct signals don't share a net (no shorts).
- Checks driver discipline: max one hard driver per net, no hard+switched mix.
- Checks no undriven nets.

### 2. Load (`load_check`)

Runs `java -jar logisim.jar --tty stats` — catches XML schema rot,
missing components, etc.

### 3. Behavioral (`behavioral_check`)

- Generates test vectors from `eval_circuit()` (golden model).
- For ≤11 inputs: exhaustive (all 2^n combinations).
- For >11 inputs: 2048 random rows.
- Runs `java -jar logisim.jar --test-vector`.
- Compares simulation output to expected values.
- Reports mismatching rows with exact input assignments.

### Public bus conversion (`_busify_tree`)

The language and golden evaluator remain scalar. After those circuits pass
their normal checks, `--busify` finds zero-based numbered boundary groups,
moves one pin to a bus trunk, inserts an oriented Logisim Splitter, and retargets
the former pin wires to its scalar ends. Splitter endpoint geometry is mirrored
from Logisim Evolution's `SplitterParameters` in `_splitter_ports()`.

Referenced circuit interfaces are never converted: reducing their pin count
would move every port on every existing subcircuit instance. Use a public,
unreferenced wrapper around a scalar reusable core instead. `check_widths()`
constrains pin, gate, splitter, and subcircuit-port widths per physical net and
catches the historical broken state where a wide pin drove a scalar wire with
no splitter.

### Compact arithmetic backend

`compact_arithmetic.py` demonstrates the preferred architecture for large
datapaths that would be unreadable after scalar lowering:

- Wide subcircuit ports stay wide between arithmetic blocks.
- Splitters are placed only at bit-level logic boundaries.
- The ripple adder is a regular vertical bit slice with locally labelled carry
  nets instead of global U-shaped wires.
- The multiplier uses 16 partial-product AND gates followed by three staggered
  rows containing four half adders and eight full adders.
- `schematic.Schematic` rejects off-grid and diagonal geometry while emitting.
- `schematic.evaluate_circuit()` independently simulates the emitted XML, including
  wires, tunnels, splitters, gates, and hierarchy. This catches geometric
  shorts that a source-level golden evaluator cannot see.

The scalar `.logic` build still runs first and remains the behavioral oracle.
Only after it passes is the compact structural version installed and checked.

### Declarative datapath templates

`datapath_templates.py` turns versioned JSON requests into scalar golden
models plus structure-aware layouts. It is intentionally narrower than logic
synthesis: a registered family owns its semantic expansion and visual grammar,
while the request supplies only a name and width.

Current families:

| Template | Structure |
|---|---|
| `unsigned_comparator` | MSB-first bit-slice priority chain carrying LT/EQ/GT |
| `unsigned_minmax` | generated comparator plus two generated bus mux banks |

Generation is deterministic and stdlib-only. Widths 2–4 use exhaustive
physical-vector verification; larger widths use boundary values, transitions
around every power of two, equality rows, and a deterministic pseudorandom
set. `check_all()` always runs regardless of width.

To add another family:

1. Add its name to `SUPPORTED_TEMPLATES` and validate its parameters.
2. Emit a scalar `.logic` oracle before any parent that references it.
3. Build its layout only with `schematic.Schematic` primitives.
4. Add independent integer semantics to `verify()`.
5. Add a JSON example and a deterministic build/physical-evaluation test.

Do not add a coordinate patcher. Layout decisions belong to reusable rules
such as bit-slice pitch, bus boundary placement, and channel allocation.

File ownership is intentionally separated:

| File | May know about circuit semantics? | May emit geometry? |
|---|---:|---:|
| `schematic.py` | No | Yes, only generic checked primitives |
| `compact_arithmetic.py` | Arithmetic only | Yes, through `Schematic` |
| `datapath_templates.py` | Registered template families | Yes, through `Schematic` |
| `datapathc.py` | No | No; validates CLI I/O and reports JSON |

Generated `.logic` and `.circ` examples are reproducibility fixtures. Their
editable source is JSON or Python template code, never the emitted XML.

## Adding a new gate type

1. Add an entry to `_CIRC_NAME` mapping your keyword to Logisim's component name.
2. Add to `_BONUS` and `_NEG_OUT` if it needs extra width or output bubble.
3. Map it in `compile_netlist()` (in the gate-kind dispatch).
4. If it has non-standard port geometry, update `gate_axis()` and `input_dys()`.

## Adding a new language construct

1. Add the keyword/regex to `parse_logic()`.
2. Define a dataclass if needed (`Fet`, `TGate`, `Inst` style).
3. Add fields to `CircuitDef` and `Netlist`.
4. Handle it in `compile_netlist()`.
5. Handle it in both layout engines (or add a guard if only gate-level matters).
6. Update `eval_circuit()` if it has a boolean semantics.

## Evaluator

`eval_circuit(c: CircuitDef, input_values, defs)` is the golden model
evaluator. It recursively evaluates hierarchical circuits:

- Processes assignments: `env[target] = eval_expr(ast, env)`.
- Processes `use` blocks: recursively evaluates the subcircuit with mapped
  inputs, then writes results back.
- Processes `spec` lines: evaluates the expression as a fallback for
  switch-driven outputs.
- Raises `ValueError` if an output is uncomputable (missing subcircuit
  definition, no spec for a switch-driven net).

## Test suite

`test_circuitc.py` is organised by subsystem:

| Section | Tests |
|---|---|
| Geometry | `input_dys`, `gate_axis` for all gate types and fan-in counts |
| Expression parser | Simple exprs, precedence, parens, constants, chained ops, variable extraction |
| Circuit parser | Single/multi circuits, FETs, tgates, pulls, mixed gate+switch, hierarchy, error rejection |
| Compiler | Basic gates, high-fanin tree decomposition, constants, multi-output, fanout |
| Evaluator | Gate-level, hierarchical, multi-output, specs, missing-defs error |
| XML emission | Valid XML, structural check across 25+ circuit types, port map correctness |
| CLI | `build` (single/multi/CMOS), `check`, `describe`, `merge` workflow |
| Error detection | Sabotage, width mismatch, and invalid-source rejection |
| Bus conversion | Real splitters, endpoint wiring, hierarchy-safe wrappers |
| Layout | Runaway-coordinate bounds |
| Behavioral | Exhaustive Logisim sim (requires jar) |
| Layout bounds | No runaway coordinates for representative circuits |

Run: `python3 test_circuitc.py`. Structural tests always run; behavioral
tests require a Logisim jar (`$LOGISIM_JAR` or `--jar`).

## Known limitations

- **No native bus expressions.** `.logic` stays scalar, but numbered public
  pins can be converted to bus pins and real splitters after verification.
- **No sequential logic.** Flip-flops, registers, latches, cross-coupled
  gates need `--test-circuit` benches instead of truth table vectors.
- **Instances inside switch-level circuits** are not supported. Keep `use`
  blocks in gate-level circuits; CMOS is for leaf circuits only.
- **Maximum ~11 inputs per circuit** for exhaustive simulation (2048 random
  rows beyond that).
- **Python ≥ 3.10 required** (uses `dataclasses` with `field`, `match` not used).
- **Logisim Evolution 4.1.0** file skeleton; newer versions may shift attributes.

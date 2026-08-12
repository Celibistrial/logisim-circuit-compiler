# circuitc — agent instructions

You are using `circuitc.py` to produce Logisim Evolution `.circ` circuit files.

## The one rule

**Never write or edit `.circ` XML directly, and never place coordinates.**
Logisim connectivity is defined by pixel geometry; hand-placed coordinates
create shorted or floating nets that fail silently. Write `.logic` source and
let the tool compile and verify. If a `.circ` needs to change, change the
`.logic` and rebuild.

## The loop

1. Write `design.logic` (format below).
2. Run:
   ```bash
   python3 circuitc.py build design.logic -o design.circ
   ```
3. Read the JSON on stdout.
   - `"ok": true` with `behavioral.ok` entries and `load.ok` → done. The
     `.circ` passed exhaustive/random vectors in Logisim itself.
   - `"ok": true` with a missing-jar warning or `--skip-sim` → structurally
     verified only; report that simulation was skipped.
   - `"ok": false` → fix the `.logic` using the report (see below), rebuild.
4. Deliver the `.circ` file.

Exit code mirrors `ok`. All errors are JSON on stdout — you never need to
parse tracebacks.

## Multi-bit pins with splitters

The `.logic` format stays 1-bit so exhaustive verification remains simple — a
64-bit adder is written with `A0..A63`. **For delivered circuits, have the
compiler replace public numbered pin groups with real multi-bit pins and
Splitters:**

```bash
python3 circuitc.py build design.logic -o design.circ --busify

# Or convert an already-built file (all safe public circuits, or one circuit):
python3 circuitc.py busify design.circ
python3 circuitc.py busify design.circ adder64
```

- `busify` recognizes contiguous zero-based groups (`A0,A1,...`), emits one
  pin labelled `A`, inserts and wires a correctly oriented Splitter, then runs
  grid, proximity, collision, width, loop, and hierarchy checks.
- Reusable subcircuit interfaces stay scalar because changing their pin count
  would move ports on every existing instance. Create a public wrapper with
  the same numbered scalar pins, instance the scalar core inside it, and let
  `--busify` convert that unreferenced wrapper.
- **Never widen a pin with `set-property` and delete the other pins.** That old
  workflow disconnected the bit lanes. `check-widths` now detects the width
  mismatch, but only `busify` performs the complete conversion.
- Use `describe` to confirm that public interfaces are compact and contain
  `Splitter` components before delivery.
- For large arithmetic datapaths, boundary `busify` alone is not enough: the
  scalar router will still create long global bit lanes. Use a structure-aware
  backend built on `schematic.Schematic` so buses remain wide between
  blocks and are split only beside bit-level logic.

## Declarative datapath templates (no LLM at generation time)

For supported regular datapaths, do not write topology or layout code. Write a
JSON spec and invoke the template compiler:

```bash
python3 datapathc.py templates
python3 datapathc.py build examples/compare_minmax.json \
  -o examples/compare_minmax.circ
```

Version-1 requests have only `name`, `template`, and `width`. The current
families are `unsigned_comparator` and `unsigned_minmax`, for widths 2–16.
The compiler deterministically emits the scalar `.logic` oracle and structural
`.circ`, runs grid/proximity/collision/width/loop/hierarchy checks, then tests
the emitted physical nets. Never edit either generated file to improve its
layout; change the JSON or shared template implementation and rebuild.

This is the preferred workflow for repeated generation. A new circuit family
requires one reviewed template implementation; subsequent widths, names, and
projects are data-only and require no LLM call.

This workflow is additive. Continue using `circuitc.py build` for arbitrary
`.logic`, CMOS, and unsupported circuit families. Do not describe `datapathc`
as general synthesis: it only accepts registered templates. `schematic.py`
provides generic checked emission/evaluation primitives;
`datapath_templates.py` owns family-specific semantics and layout;
`datapathc.py` is only the JSON CLI.

## .logic format

```
# comment
circuit full_adder:                  # first circuit = Logisim main
  inputs A, B, Cin
  outputs S, Cout
  AxB = A ^ B                        # non-output target = named internal net
  S = AxB ^ Cin
  Cout = (A & B) | (Cin & AxB)
```

- Operators: `~` not, `&` and, `^` xor, `|` or, parens, constants `0` `1`.
  Precedence high→low: `~ & ^ |`.
- Assignments strictly after their operands. No cycles, no forward refs.
- 1-bit signals only. **No buses. No sequential logic** (no flip-flops,
  latches, clocks) — do not attempt them; the tool will reject or miscompile
  nothing, it simply has no syntax for them.
- Several `circuit` blocks per file are fine.

### Transistor level (CMOS)

```
circuit cmos_nand:
  inputs A, B
  outputs Y
  spec Y = ~(A & B)     # REQUIRED golden model for switch-driven outputs
  pmos A: VDD -> Y      # gate: source -> drain
  pmos B: VDD -> Y
  nmos A: GND -> n1     # series stack through internal net n1
  nmos B: n1 -> Y

tgate CH, CL: X -> Y    # transmission gate: active-high ctrl, active-low ctrl
pullup Y                # pull resistor to 1 (pseudo-NMOS etc.)
pulldown Y              # pull resistor to 0
```

- `VDD` / `GND` are reserved nets; Power/Ground components appear automatically.
- **FETs are unidirectional: source passes to drain when the gate is active**
  (N: gate=1, P: gate=0); otherwise the drain floats. N pull-down networks
  therefore read `nmos G: GND -> out` — source at GND, drain at the output.
  Getting the arrow backwards is the #1 mistake.
- Every output that depends on a FET/pull-driven net needs a `spec` line, or
  behavioral verification is skipped.
- Gates and FETs mix freely (e.g. `Sn = ~S` to derive tgate controls).

## Reading failure reports

`build` output per circuit:

```json
"structural": {"ok": false, "errors": ["FLOATING: OR@(340,240).in0 ..."]}
"behavioral": {"ok": false, "passed": 6, "failed": 2, "failures": [
    {"signal": "Y", "got": "U", "expected": "1", "row": 2, "inputs": {"A": 0, "B": 1}}
]}
```

- `behavioral.failures[].inputs` tells you the exact input assignment that
  fails — reason about your logic at those values.
- `got: "U"` = the net floats there (a switch network has no path driving it).
- `got: "E"` = contention (two paths drive opposing values — e.g. pull-up and
  pull-down both conducting, or complementary tgate controls wired equal).
- `structural.errors` (`SHORT`/`FLOATING`/`MULTIDRIVE`/`CONTENTION`/`UNDRIVEN`)
  mean the emitted geometry disagrees with the netlist — normally impossible
  from valid `.logic`; if you see one, simplify the design and rebuild rather
  than patching the `.circ`.
- `behavioral: {"skipped": ...}` → add the missing `spec` line it names.

### Hierarchy

`use CIRC label(In=sig, ...) -> (Out=sig, ...)` instantiates another circuit
as a block. CIRC must be defined earlier in the same file, or already exist
in the `--merge` target. Wire every input pin (constants 0/1 allowed).

## Other commands

```bash
python3 circuitc.py describe file.circ       # JSON netlist of any .circ (pins, comps, nets)
python3 circuitc.py check file.circ          # structural + load check of an existing .circ
python3 circuitc.py verify file.circ --spec spec.logic   # test ANY .circ against golden models
python3 circuitc.py build new.logic -o existing.circ --merge [--lib old.logic]
python3 circuitc.py set-property file.circ circuit --label X --set width=64   # edit pin props
```

- `verify`: spec block names must match circuit names in the file, and pin
  labels must match the spec's inputs/outputs. Works on hand-drawn homework
  files — use it when asked to check a circuit you didn't build.
- `--merge`: modifies an existing .circ in place — other circuits in the file
  are preserved, same-named ones are replaced, and your new circuits may
  `use` the file's existing circuits. Pass `--lib old.logic` so vectors can
  be generated for instanced circuits defined outside your source file.
- Use `describe` to inspect a `.circ` you didn't build before reasoning about it.

## Environment

- Python ≥ 3.10, stdlib only.
- Behavioral + load layers for Logisim Evolution 4.1 need Java ≥21 and the jar:
  auto-found in `/Applications/Logisim-evolution.app`, else set `$LOGISIM_JAR`
  or pass `--jar`. Without Java, `build --skip-sim` still gives structural
  verification — say so explicitly if you deliver an unsimulated circuit.
- A hung simulation returns `stderr: "timeout: ..."` in JSON (default 120 s);
  it usually means a combinational loop in the source.

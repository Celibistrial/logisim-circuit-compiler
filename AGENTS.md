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
   - `"ok": true` → done. The `.circ` is proven correct by exhaustive
     simulation in Logisim itself — do not second-guess it.
   - `"ok": false` → fix the `.logic` using the report (see below), rebuild.
4. Deliver the `.circ` file.

Exit code mirrors `ok`. All errors are JSON on stdout — you never need to
parse tracebacks.

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
python3 circuitc.py describe file.circ   # JSON netlist of any .circ (pins, comps, nets)
python3 circuitc.py check file.circ      # structural + load check of an existing .circ
python3 circuitc.py verify file.circ --spec spec.logic   # test ANY .circ against golden models
python3 circuitc.py build new.logic -o existing.circ --merge [--lib old.logic]
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
- Behavioral + load layers need `java` (≥16) and the Logisim Evolution jar:
  auto-found in `/Applications/Logisim-evolution.app`, else set `$LOGISIM_JAR`
  or pass `--jar`. Without Java, `build --skip-sim` still gives structural
  verification — say so explicitly if you deliver an unsimulated circuit.
- A hung simulation returns `stderr: "timeout: ..."` in JSON (default 120 s);
  it usually means a combinational loop in the source.

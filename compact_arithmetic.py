"""Compact, bus-aware structural layouts for the arithmetic example project.

The boolean compiler deliberately lowers everything to scalar signals.  That
is ideal for verification, but a poor presentation for datapaths: every bit is
routed through the global lane band.  This module is a small schematic backend
for arithmetic structures.  It keeps buses wide between blocks, breaks them
out only at bit-level logic, and uses named local nets where a direct carry or
partial-product wire would otherwise wrap around a subcircuit symbol.

It never patches an existing circuit's coordinates.  Each target circuit is
regenerated from structural rules and then checked by circuitc's geometry,
width, and hierarchy analyzers.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import circuitc
from schematic import Schematic, evaluate_circuit as _evaluate_circuit
from schematic import reverse_bits as _reverse_bits
from schematic import short_tunnel as _short_tunnel


def build_adder8() -> ET.Element:
    """Eight full adders as a vertical bit slice with local named carries."""
    s = Schematic("adder8")
    s.text("8-bit ripple-carry adder", (300, 80))

    s.pin("A", (60, 540), width=8)
    s.pin("B", (60, 620), width=8)
    s.pin("Cin", (60, 700))
    s.pin("S", (760, 560), width=8, output=True)
    s.pin("Cout", (760, 640), output=True)

    amap = _reverse_bits(8)
    a = s.splitter((120, 560), 8, 8, "east", spacing=8, bit_map=amap)
    b = s.splitter((180, 580), 8, 8, "east", spacing=8, bit_map=amap)
    sout = s.splitter((660, 560), 8, 8, "west", spacing=8, bit_map=amap)
    s.wire((60, 540), (100, 540), (100, 560), a["combined"])
    s.wire((60, 620), (160, 620), (160, 580), b["combined"])
    s.wire(sout["combined"], (760, 560))

    anchors = [(500, 800 - 80 * bit) for bit in range(8)]
    for bit, anchor in enumerate(anchors):
        x, y = anchor
        s.instance("full_adder", f"bit{bit}", anchor)
        # full_adder west ports: A, B, Cin; east ports: S, Cout
        s.wire(a[f"end{7 - bit}"], (280, y))
        s.wire(b[f"end{7 - bit}"], (280, y + 20))
        s.wire((x, y), sout[f"end{7 - bit}"])

        if bit == 0:
            s.wire((60, 700), (220, 700), (220, y + 40), (280, y + 40))
        else:
            label = f"c{bit}"
            prev_x, prev_y = anchors[bit - 1]
            s.tunnel(label, (540, prev_y + 20), facing="west")
            s.wire((prev_x, prev_y + 20), (540, prev_y + 20))
            s.tunnel(label, (240, y + 40))
            s.wire((240, y + 40), (280, y + 40))

    # MSB carry-out to the public scalar output.
    msb_x, msb_y = anchors[7]
    s.wire((msb_x, msb_y + 20), (700, msb_y + 20),
           (700, 640), (760, 640))
    return s.circuit


def build_addsub8() -> ET.Element:
    """Bus datapath with shared control rails and a local flag tree."""
    s = Schematic("addsub8")
    s.text("8-bit adder / subtractor", (560, 60))
    s.text("conditional B inversion", (210, 120))
    s.text("result flags", (1160, 410))

    s.pin("A", (60, 180), width=8)
    s.pin("B", (60, 500), width=8)
    s.pin("sub", (60, 800))
    s.pin("S", (1540, 360), width=8, output=True)
    s.pin("zero", (1540, 590), output=True)
    s.pin("overflow", (1540, 820), output=True)

    # Keep datapath buses as visible wires.  They are short enough that tunnel
    # aliases only obscure the flow and consume more room than the bus itself.
    s.wire((60, 180), (620, 180), (620, 360), (680, 360))

    reverse = _reverse_bits(8)
    bsplit = s.splitter((120, 500), 8, 8, "east", spacing=6, bit_map=reverse)
    s.wire((60, 500), bsplit["combined"])
    bjoin = s.splitter((520, 520), 8, 8, "west", spacing=6, bit_map=reverse)
    s.wire(bjoin["combined"], (600, 520), (600, 380), (680, 380))

    # One shared SUB rail replaces eight repeated tunnel boxes.  Every XOR has
    # a short horizontal tap, which reads like an ordinary bit slice.
    s.wire((60, 800), (240, 800), (240, 300))
    for bit in range(8):
        branch = bsplit[f"end{7 - bit}"]
        y = branch[1] + 20
        anchor = (340, y)
        s.gate("XOR", anchor)
        s.wire(branch, (280, branch[1]))
        s.wire((240, y + 20), (280, y + 20))
        if bit == 7:
            s.wire(anchor, (410, y), bjoin[f"end{7 - bit}"])
            s.tunnel("BMOD7", (410, y), facing="west")
        else:
            s.wire(anchor, bjoin[f"end{7 - bit}"])

    # One compact bus adder. Its west ports are A/B/Cin; east ports S/Cout.
    s.instance("adder8", "sum", (900, 360))
    s.wire((240, 800), (650, 800), (650, 400), (680, 400))
    s.wire((900, 360), (1540, 360))
    s.wire((900, 380), (940, 380), (940, 420), (980, 420))
    s.comp("0", "Probe", (980, 420), facing="west")

    # One result tap feeds a balanced zero detector with direct local wires.
    # The 40 px branch pitch is deliberate: labels are unnecessary and no
    # four-input gate terminals are stacked on top of each other.
    rsplit = s.splitter((1040, 600), 8, 8, "east", spacing=4)
    s.wire((1040, 360), rsplit["combined"])
    pairs = [(1140, 460), (1140, 540), (1140, 620), (1140, 700)]
    for pair, bits in zip(pairs, ((0, 1), (2, 3), (4, 5), (6, 7))):
        s.gate("OR", pair)
        in_x = pair[0] - circuitc.gate_axis("OR")
        for bit, dy in zip(bits, circuitc.input_dys(2)):
            s.wire(rsplit[f"end{bit}"], (in_x, pair[1] + dy))

    groups = [(1240, 500), (1240, 660)]
    s.gate("OR", groups[0])
    s.wire(pairs[0], (1170, 460), (1170, 480), (1190, 480))
    s.wire(pairs[1], (1160, 540), (1160, 520), (1190, 520))
    s.gate("OR", groups[1])
    s.wire(pairs[2], (1170, 620), (1170, 640), (1190, 640))
    s.wire(pairs[3], (1160, 700), (1160, 680), (1190, 680))
    s.gate("OR", (1370, 590))
    s.wire(groups[0], (1310, 500), (1310, 570), (1320, 570))
    s.wire(groups[1], (1330, 660), (1330, 610), (1320, 610))
    s.gate("NOT", (1450, 590))
    s.wire((1370, 590), (1420, 590))
    s.wire((1450, 590), (1540, 590))

    # Extract A7 with a one-end splitter; bits 0..6 are intentionally
    # unassigned, so the bus itself remains intact.
    tap_map = {bit: (0 if bit == 7 else None) for bit in range(8)}
    atap = s.splitter((620, 240), 8, 1, "east", bit_map=tap_map)
    _short_tunnel(s, "A7", atap["end0"], (700, atap["end0"][1]))

    # S7 is the only scalar result bit needed outside the local reduction
    # tree, so it alone receives a tunnel name.
    s.wire(rsplit["end7"], (1060, 760))
    s.tunnel("S7", (1060, 760), facing="north")

    # overflow = ~(A7 ^ Bmod7) & (A7 ^ S7)
    s.gate("XOR", (1250, 780))
    _short_tunnel(s, "A7", (1190, 760), (1140, 760))
    _short_tunnel(s, "BMOD7", (1190, 800), (1140, 800))
    s.gate("NOT", (1330, 780))
    s.wire((1250, 780), (1300, 780))
    s.gate("XOR", (1250, 860))
    _short_tunnel(s, "A7", (1190, 840), (1140, 840))
    _short_tunnel(s, "S7", (1190, 880), (1140, 880))
    s.gate("AND", (1450, 820))
    s.wire((1330, 780), (1380, 780), (1380, 800), (1400, 800))
    s.wire((1250, 860), (1380, 860), (1380, 840), (1400, 840))
    s.wire((1450, 820), (1540, 820))
    return s.circuit


def build_mult4() -> ET.Element:
    """4x4 bit-array multiplier built from AND, HA, and FA cells."""
    s = Schematic("mult4")
    s.text("4 × 4 unsigned array multiplier", (980, 50))
    s.text("partial products", (1120, 80))
    s.text("row 0 + row 1", (60, 500))
    s.text("+ shifted row 2", (60, 740))
    s.text("+ shifted row 3", (60, 980))

    s.pin("A", (60, 260), width=4)
    s.pin("B", (60, 560), width=4)
    s.pin("P", (3250, 800), width=8, output=True)

    # Split the input buses once.  Repeated A<n>/B<n> tunnel labels at the
    # individual AND gates mirror the signal labels in a textbook array while
    # avoiding a full-height rail grid through every gate input.
    amap = _reverse_bits(4)
    asplit = s.splitter((180, 260), 4, 4, "east", spacing=6, bit_map=amap)
    bsplit = s.splitter((180, 560), 4, 4, "east", spacing=6, bit_map=amap)
    s.wire((60, 260), asplit["combined"])
    s.wire((60, 560), bsplit["combined"])
    for bit in range(4):
        _short_tunnel(s, f"A{bit}", asplit[f"end{3 - bit}"],
                      (250, asplit[f"end{3 - bit}"][1]))
        _short_tunnel(s, f"B{bit}", bsplit[f"end{3 - bit}"],
                      (250, bsplit[f"end{3 - bit}"][1]))

    # Product weights run right-to-left, as in the usual paper schematic:
    # P0 is at the right edge and carries move one column to the left.
    column_x = {weight: 2750 - 380 * weight for weight in range(7)}
    by_weight: dict[int, list[tuple[int, int]]] = {w: [] for w in range(7)}
    for i in range(4):
        for j in range(4):
            by_weight[i + j].append((i, j))

    for weight, products in by_weight.items():
        for slot, (i, j) in enumerate(products):
            anchor = (column_x[weight], 140 + 90 * slot)
            in_x = anchor[0] - circuitc.gate_axis("AND")
            _short_tunnel(s, f"A{j}", (in_x, anchor[1] - 20),
                          (in_x - 60, anchor[1] - 20))
            _short_tunnel(s, f"B{i}", (in_x, anchor[1] + 20),
                          (in_x - 60, anchor[1] + 20))
            s.gate("AND", anchor)
            product = "P0" if (i, j) == (0, 0) else f"pp{i}{j}"
            _short_tunnel(s, product, anchor, (anchor[0] + 70, anchor[1]))

    signal_sources: dict[str, tuple[tuple[int, int], int]] = {}
    route_counts: dict[tuple[str, int, int], int] = {}
    stage_rows = (520, 760, 1000)

    def route_signal(source_info: tuple[tuple[int, int], int],
                     port: tuple[int, int], input_index: int):
        """Route a cell result through whitespace to one later cell input."""
        source, output_index = source_info
        sx, sy = source
        px, py = port
        source_row = min(stage_rows, key=lambda row: abs(row - sy))
        target_row = min(stage_rows, key=lambda row: abs(row - py))
        if source_row == target_row:
            # A carry moving left within one reduction row.
            key = ("row", source_row, target_row)
            lane = route_counts.get(key, 0)
            route_counts[key] = lane + 1
            band_y = source_row + 100 + 20 * lane
        else:
            # A sum/carry descending to the next reduction row.
            key = ("drop", source_row, target_row)
            lane = route_counts.get(key, 0)
            route_counts[key] = lane + 1
            band_y = source_row + 160 + 20 * lane
        source_x = sx + 40 + 20 * output_index
        target_x = px - 40 - 20 * input_index
        s.wire(source, (source_x, sy), (source_x, band_y),
               (target_x, band_y), (target_x, py), port)

    def product_tunnel(signal: str, port: tuple[int, int], rank: int,
                       count: int):
        """Fan sparse partial products into a cell without stacked labels."""
        px, py = port
        offset = 0
        if count == 2:
            offset = (-30, 30)[rank]
        elif count == 3:
            offset = (-50, 0, 50)[rank]
        ty = py + offset
        tunnel = (px - 90, ty)
        s.tunnel(signal, tunnel, facing="east")
        s.wire(port, (px - 30, py), (px - 30, ty), tunnel)

    def publish_or_store(signal: str, port: tuple[int, int], output_index: int):
        if signal.startswith("P"):
            if signal == "P7":
                tunnel = (port[0] + 70, port[1] + 60)
                s.tunnel(signal, tunnel, facing="west")
                s.wire(port, (port[0] + 30, port[1]),
                       (port[0] + 30, tunnel[1]), tunnel)
            else:
                _short_tunnel(s, signal, port, (port[0] + 70, port[1]))
        else:
            signal_sources[signal] = (port, output_index)

    def cell(kind: str, label: str, weight: int, y: int,
             inputs: tuple[str, ...], sum_out: str, carry_out: str):
        """Place one reduction cell and directly route intermediate results."""
        expected = 2 if kind == "half_adder" else 3
        if len(inputs) != expected:
            raise ValueError(f"{kind} needs {expected} inputs, got {inputs}")
        x = column_x[weight]
        s.instance(kind, label, (x, y))
        port_x = x - circuitc.INST_W
        product_inputs = [signal for signal in inputs if signal.startswith("pp")]
        product_rank = {signal: rank for rank, signal in enumerate(product_inputs)}
        for index, signal in enumerate(inputs):
            port = (port_x, y + 20 * index)
            if signal.startswith("pp"):
                product_tunnel(signal, port, product_rank[signal],
                               len(product_inputs))
            else:
                route_signal(signal_sources[signal], port, index)
        publish_or_store(sum_out, (x, y), 0)
        publish_or_store(carry_out, (x, y + 20), 1)

    # First two partial-product rows.  The sum stays in its weight column;
    # each carry becomes the third input of the cell immediately to the left.
    cell("half_adder", "ha_1", 1, 520, ("pp01", "pp10"), "P1", "c12")
    cell("full_adder", "fa_2", 2, 520,
         ("pp02", "pp11", "c12"), "s12", "c13")
    cell("full_adder", "fa_3", 3, 520,
         ("pp03", "pp12", "c13"), "s13", "c14")
    cell("half_adder", "ha_4", 4, 520,
         ("pp13", "c14"), "s14", "c15")

    # Add the third shifted row.
    cell("half_adder", "ha_2", 2, 760, ("s12", "pp20"), "P2", "c23")
    cell("full_adder", "fa_3b", 3, 760,
         ("s13", "pp21", "c23"), "t3", "c24")
    cell("full_adder", "fa_4", 4, 760,
         ("s14", "pp22", "c24"), "t4", "c25")
    cell("full_adder", "fa_5", 5, 760,
         ("c15", "pp23", "c25"), "t5", "c26")

    # Add the final shifted row.  The last carry is product bit P7.
    cell("half_adder", "ha_3", 3, 1000, ("t3", "pp30"), "P3", "c34")
    cell("full_adder", "fa_4b", 4, 1000,
         ("t4", "pp31", "c34"), "P4", "c35")
    cell("full_adder", "fa_5b", 5, 1000,
         ("t5", "pp32", "c35"), "P5", "c36")
    cell("full_adder", "fa_6", 6, 1000,
         ("c26", "pp33", "c36"), "P6", "P7")

    # Collect only the public product bits at the boundary.  A generous 60 px
    # pitch keeps P0..P7 labels readable beside the splitter.
    pjoin = s.splitter((3150, 800), 8, 8, "west", spacing=6,
                       bit_map=_reverse_bits(8))
    s.wire(pjoin["combined"], (3250, 800))
    for bit in range(8):
        end = pjoin[f"end{7 - bit}"]
        _short_tunnel(s, f"P{bit}", end, (3060, end[1]))
    return s.circuit


def install_compact_arithmetic(circ_path: str | Path) -> dict:
    """Replace scalar arithmetic targets in *circ_path* with compact layouts."""
    circ_path = str(circ_path)
    tree, root = circuitc._load(circ_path)
    replacements = {
        "adder8": build_adder8(),
        "addsub8": build_addsub8(),
        "mult4": build_mult4(),
    }

    for name, replacement in replacements.items():
        old = circuitc._circ(root, name)
        if old is None:
            raise ValueError(f"compact layout target {name!r} not found")
        index = list(root).index(old)
        root.remove(old)
        root.insert(index, replacement)

    # The scalar core exists only to prove the source model before the compact
    # structural backend runs. The final adder directly contains full adders.
    scalar_core = circuitc._circ(root, "adder8_core")
    if scalar_core is not None:
        root.remove(scalar_core)
    main = root.find("main")
    if main is not None:
        main.set("name", "adder8")
    circuitc._rewrite(tree, circ_path)
    report = circuitc.check_all(circ_path)
    return circuitc._check_summary(report)


def verify_compact_arithmetic(circ_path: str | Path) -> dict:
    """Compare emitted compact geometry with independent integer arithmetic."""
    root = ET.parse(circ_path).getroot()
    rows = 0
    edges8 = [0, 1, 2, 3, 7, 15, 31, 63, 64, 127, 128, 129, 254, 255]
    pairs = [(a, b) for a in edges8 for b in edges8]
    pairs += [((37 * i + 11) & 0xFF, (149 * i + 23) & 0xFF) for i in range(64)]
    for a, b in pairs:
        for cin in (0, 1):
            out = _evaluate_circuit(root, "adder8", {"A": a, "B": b, "Cin": cin})
            total = a + b + cin
            if out != {"S": total & 0xFF, "Cout": (total >> 8) & 1}:
                raise ValueError(f"compact adder mismatch A={a} B={b} Cin={cin}: {out}")
            rows += 1
        for sub in (0, 1):
            out = _evaluate_circuit(root, "addsub8", {"A": a, "B": b, "sub": sub})
            result = (a - b if sub else a + b) & 0xFF
            overflow = (((a ^ b) & 0x80) != 0 and ((a ^ result) & 0x80) != 0) \
                if sub else (((a ^ b) & 0x80) == 0 and ((a ^ result) & 0x80) != 0)
            expected = {"S": result, "zero": int(result == 0),
                        "overflow": int(overflow)}
            if out != expected:
                raise ValueError(f"compact addsub mismatch A={a} B={b} sub={sub}: {out} != {expected}")
            rows += 1
    for a in range(16):
        for b in range(16):
            out = _evaluate_circuit(root, "mult4", {"A": a, "B": b})
            if out != {"P": a * b}:
                raise ValueError(f"compact multiplier mismatch A={a} B={b}: {out}")
            rows += 1
    return {"ok": True, "vectors": rows}

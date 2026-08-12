"""Reusable deterministic primitives for structure-aware Logisim layouts.

This module is deliberately circuit-family agnostic.  Template backends use
``Schematic`` to emit grid-aligned XML and ``evaluate_circuit`` to prove the
connectivity of the emitted wires, tunnels, splitters, gates, and hierarchy.
No generated coordinate is inferred by an LLM or patched after emission.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import circuitc


def reverse_bits(width: int) -> dict[int, int]:
    """Map logical bit numbers to visually top-to-bottom splitter ends."""
    return {bit: width - 1 - bit for bit in range(width)}


class Schematic:
    """Small grid-checked XML emitter shared by datapath templates."""

    def __init__(self, name: str):
        self.circuit = ET.Element("circuit", {"name": name})
        for key, value in (("appearance", "logisim_evolution"),
                           ("circuit", name),
                           ("circuitnamedboxfixedsize", "true"),
                           ("simulationFrequency", "1.0")):
            ET.SubElement(self.circuit, "a", {"name": key, "val": value})

    @staticmethod
    def _loc(loc: tuple[int, int]) -> str:
        x, y = loc
        if x % circuitc.GRID or y % circuitc.GRID:
            raise ValueError(f"off-grid template coordinate: {loc}")
        return f"({x},{y})"

    def comp(self, lib: str | None, name: str, loc: tuple[int, int], **attrs):
        data = {"loc": self._loc(loc), "name": name}
        if lib is not None:
            data["lib"] = lib
        elem = ET.SubElement(self.circuit, "comp", data)
        for key, value in attrs.items():
            if value is not None:
                ET.SubElement(elem, "a", {"name": key, "val": str(value)})
        return elem

    def wire(self, *points: tuple[int, int]):
        for start, end in zip(points, points[1:]):
            if start == end:
                continue
            if start[0] != end[0] and start[1] != end[1]:
                raise ValueError(f"diagonal template wire: {start}->{end}")
            ET.SubElement(self.circuit, "wire", {
                "from": self._loc(start), "to": self._loc(end),
            })

    def pin(self, label: str, loc: tuple[int, int], width: int = 1,
            output: bool = False):
        attrs = {"appearance": "classic", "label": label,
                 "width": width if width > 1 else None}
        if output:
            attrs.update({"facing": "west", "type": "output"})
        return self.comp("0", "Pin", loc, **attrs)

    def tunnel(self, label: str, loc: tuple[int, int], width: int = 1,
               facing: str = "east"):
        return self.comp("0", "Tunnel", loc, label=label, facing=facing,
                         width=width if width > 1 else None)

    def gate(self, kind: str, loc: tuple[int, int], inputs: int = 2,
             width: int = 1):
        attrs = {"size": 30} if kind == "NOT" else {
            "size": 50, "inputs": inputs,
        }
        if width > 1:
            attrs["width"] = width
        return self.comp("1", circuitc._CIRC_NAME[kind], loc, **attrs)

    def instance(self, circuit: str, label: str, loc: tuple[int, int]):
        return self.comp(None, circuit, loc, label=label)

    def splitter(self, loc: tuple[int, int], width: int, fanout: int,
                 facing: str, appear: str = "center", spacing: int = 2,
                 bit_map: dict[int, int | None] | None = None):
        elem = self.comp("0", "Splitter", loc, facing=facing, fanout=fanout,
                         incoming=width, appear=appear, spacing=spacing)
        if bit_map is not None:
            for bit in range(width):
                end = bit_map.get(bit)
                ET.SubElement(elem, "a", {
                    "name": f"bit{bit}",
                    "val": "none" if end is None else str(end),
                })
        return {name: ploc for name, ploc, _ in circuitc._splitter_ports(elem)}

    def text(self, text: str, loc: tuple[int, int], size: int = 16):
        return self.comp("9", "Text", loc, text=text,
                         font=f"SansSerif bold {size}")


def short_tunnel(schematic: Schematic, label: str, port: tuple[int, int],
                 tunnel: tuple[int, int], width: int = 1):
    """Connect a labelled tunnel with a single orthogonal stub."""
    schematic.tunnel(label, tunnel, width=width,
                     facing="west" if tunnel[0] > port[0] else "east")
    schematic.wire(port, tunnel)


def _splitter_map(comp) -> tuple[list[int | None], list[int]]:
    attrs = circuitc._comp_attrs(comp)
    incoming = int(attrs.get("incoming", "2"))
    fanout = int(attrs.get("fanout", "2"))
    explicit = {int(k[3:]): v for k, v in attrs.items()
                if k.startswith("bit") and k[3:].isdigit()}
    if explicit:
        mapping = [None if explicit.get(bit) == "none"
                   else int(explicit[bit]) if bit in explicit
                   else min(bit, fanout - 1)
                   for bit in range(incoming)]
    elif fanout >= incoming:
        mapping = list(range(incoming))
    else:
        q, r = divmod(incoming, fanout)
        mapping = []
        for end in range(fanout):
            mapping.extend([end] * (q + (1 if end < r else 0)))
    widths = [sum(mapped == end for mapped in mapping)
              for end in range(fanout)]
    return mapping, widths


def evaluate_circuit(root, name: str, inputs: dict[str, int], stack=()) -> dict[str, int]:
    """Evaluate emitted XML by following its physical connectivity."""
    if name in stack:
        raise ValueError(f"recursive template circuit: {' -> '.join(stack + (name,))}")
    celem = circuitc._circ(root, name)
    if celem is None:
        raise ValueError(f"physical evaluator cannot find circuit {name!r}")
    nm = circuitc._net_map(celem, root)
    values: dict[object, tuple[int, int]] = {}

    def net(loc):
        return nm["netid"](loc)

    def drive(net_id, value: int, width: int) -> bool:
        value &= (1 << width) - 1
        old = values.get(net_id)
        if old is not None:
            if old != (value, width):
                raise ValueError(
                    f"template contention in {name} net {nm['name'](net_id)}: "
                    f"{old} vs {(value, width)}")
            return False
        values[net_id] = (value, width)
        return True

    outputs = {}
    gates, splitters, instances = [], [], []
    registry = {c.get("name"): circuitc._pin_defs(c)
                for c in root.findall("circuit")}
    for comp in celem.findall("comp"):
        kind = comp.get("name")
        loc = circuitc._parse_loc(comp.get("loc"))
        attrs = circuitc._comp_attrs(comp)
        if kind == "Pin":
            width = int(attrs.get("width", "1"))
            label = attrs.get("label", "?")
            is_output = attrs.get("type") == "output" or attrs.get("output") == "true"
            if is_output:
                outputs[label] = (net(loc), width)
            else:
                if label not in inputs:
                    raise ValueError(f"missing physical input {name}.{label}")
                drive(net(loc), inputs[label], width)
        elif kind == "Constant":
            width = int(attrs.get("width", "1"))
            drive(net(loc), int(attrs.get("value", "0x1"), 0), width)
        elif kind in circuitc._GATE_KINDS:
            gate_kind = circuitc._GATE_KINDS[kind]
            count = 1 if gate_kind == "NOT" else int(attrs.get("inputs", "5"))
            width = int(attrs.get("width", "1"))
            ins, out = circuitc.gate_ports(gate_kind, count)
            in_nets = [net((loc[0] + dx, loc[1] + dy)) for dx, dy in ins]
            out_net = net((loc[0] + out[0], loc[1] + out[1]))
            gates.append((gate_kind, width, in_nets, out_net))
        elif kind == "Splitter":
            ports = circuitc._splitter_ports(comp)
            mapping, widths = _splitter_map(comp)
            splitters.append((net(ports[0][1]),
                              [net(port[1]) for port in ports[1:]],
                              mapping, widths))
        elif comp.get("lib") is None and kind in registry:
            ins, outs = registry[kind]
            input_nets = [(label, width, net((loc[0] - circuitc.INST_W,
                                              loc[1] + 20 * i)))
                          for i, (label, width) in enumerate(ins)]
            output_nets = [(label, width, net((loc[0], loc[1] + 20 * i)))
                           for i, (label, width) in enumerate(outs)]
            instances.append((kind, input_nets, output_nets))

    limit = max(20, 4 * (len(gates) + len(splitters) + len(instances)))
    for _ in range(limit):
        changed = False
        for gate_kind, width, in_nets, out_net in gates:
            if out_net in values or any(n not in values for n in in_nets):
                continue
            vals = [values[n][0] for n in in_nets]
            mask = (1 << width) - 1
            if gate_kind == "NOT":
                result = (~vals[0]) & mask
            elif gate_kind in ("AND", "NAND"):
                result = vals[0]
                for value in vals[1:]:
                    result &= value
                if gate_kind == "NAND":
                    result = (~result) & mask
            elif gate_kind in ("OR", "NOR"):
                result = vals[0]
                for value in vals[1:]:
                    result |= value
                if gate_kind == "NOR":
                    result = (~result) & mask
            else:
                result = 0
                for value in vals:
                    result ^= value
                if gate_kind == "XNOR":
                    result = (~result) & mask
            changed |= drive(out_net, result, width)

        for combined, ends, mapping, widths in splitters:
            if combined in values:
                whole = values[combined][0]
                for end, end_net in enumerate(ends):
                    if not widths[end]:
                        continue
                    branch = 0
                    thread = 0
                    for bit, mapped in enumerate(mapping):
                        if mapped == end:
                            branch |= ((whole >> bit) & 1) << thread
                            thread += 1
                    changed |= drive(end_net, branch, widths[end])
            used = [end for end, width in enumerate(widths) if width]
            if combined not in values and all(ends[end] in values for end in used):
                whole = 0
                threads = [0] * len(ends)
                for bit, mapped in enumerate(mapping):
                    if mapped is None:
                        continue
                    branch = values[ends[mapped]][0]
                    whole |= ((branch >> threads[mapped]) & 1) << bit
                    threads[mapped] += 1
                changed |= drive(combined, whole, len(mapping))

        for child, input_nets, output_nets in instances:
            if any(n not in values for _, _, n in input_nets):
                continue
            if all(n in values for _, _, n in output_nets):
                continue
            child_inputs = {label: values[n][0] for label, _, n in input_nets}
            child_outputs = evaluate_circuit(root, child, child_inputs, stack + (name,))
            for label, width, output_net in output_nets:
                changed |= drive(output_net, child_outputs[label], width)
        if not changed:
            break

    unresolved = [label for label, (n, _) in outputs.items() if n not in values]
    if unresolved:
        raise ValueError(f"unresolved physical outputs in {name}: {unresolved}")
    return {label: values[n][0] for label, (n, _) in outputs.items()}

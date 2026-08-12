#!/usr/bin/env python3
"""Build reproducible bus-aware Logisim datapaths from a JSON specification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import datapath_templates


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("templates", help="list built-in structural templates")
    build_parser = sub.add_parser("build", help="build and verify a JSON spec")
    build_parser.add_argument("spec", type=Path)
    build_parser.add_argument("-o", "--output", required=True, type=Path)
    build_parser.add_argument("--logic", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "templates":
            result = {"ok": True,
                      "templates": list(datapath_templates.SUPPORTED_TEMPLATES)}
        else:
            spec = json.loads(args.spec.read_text())
            result = datapath_templates.build(spec, args.output, args.logic)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())

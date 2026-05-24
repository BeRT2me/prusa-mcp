#!/usr/bin/env python3
"""
Extract PrusaSlicer config option definitions from PrintConfig.cpp and help
URLs from Tab.cpp into a structured JSON file for use by the MCP server.

Usage:
    uv run scripts/extract_config_schema.py <path/to/src/libslic3r/PrintConfig.cpp>

Tab.cpp is expected alongside PrintConfig.cpp at:
    <src_root>/slic3r/GUI/Tab.cpp

Output:
    src/prusa_mcp/config_schema.json

Re-run after updating PrusaSlicer to keep the schema current.
"""

import json
import re
import sys
from pathlib import Path

OUTPUT = Path(__file__).parent.parent / "src/prusa_mcp/config_schema.json"

_TYPE_MAP = {
    "coFloat": "float",
    "coFloats": "floats",
    "coInt": "int",
    "coInts": "ints",
    "coString": "string",
    "coStrings": "strings",
    "coPercent": "percent",
    "coPercents": "percents",
    "coFloatOrPercent": "float_or_percent",
    "coFloatsOrPercents": "floats_or_percents",
    "coPoint": "point",
    "coPoints": "points",
    "coBool": "bool",
    "coBools": "bools",
    "coEnum": "enum",
    "coEnums": "enums",
}

_MODE_MAP = {
    "comSimple": "simple",
    "comAdvanced": "advanced",
    "comExpert": "expert",
}

# Base retract keys that PrusaSlicer dynamically generates as nullable
# filament_* overrides via a loop at the end of PrintConfigDef::PrintConfigDef()
_FILAMENT_RETRACT_BASES = [
    "retract_length", "retract_lift", "retract_lift_above", "retract_lift_below",
    "retract_speed", "travel_max_lift", "deretract_speed", "retract_restart_extra",
    "retract_before_travel", "retract_length_toolchange", "retract_restart_extra_toolchange",
    "retract_layer_change", "wipe", "travel_lift_before_obstacle", "travel_ramping_lift",
    "retract_before_wipe", "travel_slope", "seam_gap_distance",
]


def _collect_until(lines: list[str], start: int, end: str) -> tuple[str, int]:
    """Collect lines until a line ending with `end`, return (joined_text, next_i)."""
    buf = lines[start].strip()
    i = start
    while not buf.rstrip().endswith(end) and i + 1 < len(lines):
        i += 1
        buf += " " + lines[i].strip()
    return buf, i


def _extract_strings(s: str) -> list[str]:
    """Pull all double-quoted string contents from a C++ expression."""
    return [
        p.replace("\\n", "\n").replace('\\"', '"')
        for p in re.findall(r'"((?:[^"\\]|\\.)*)"', s)
    ]


def _parse_enum_block(block: str) -> tuple[list[str], list[str]]:
    """
    Parse an enum block into (values, labels).
    Handles both plain lists  "a", "b", "c"
    and paired lists           { "a", L("A") }, { "b", L("B") }
    """
    strings = _extract_strings(block)
    # Pairs are indicated by inner braces: { "val", "label" }
    if re.search(r'\{\s*"[^"]*"\s*,\s*(?:L\()?"[^"]*"', block):
        # Interleaved value/label — even indices are values, odd are labels
        values = strings[0::2]
        labels = strings[1::2]
        return values, labels
    return strings, []


def parse(cpp_path: Path) -> dict[str, dict]:
    lines = cpp_path.read_text(encoding="utf-8", errors="replace").splitlines()
    options: dict[str, dict] = {}
    current: dict | None = None
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # New option definition
        m = re.match(
            r"def\s*=\s*this->add(_nullable)?\s*\(\s*\"([^\"]+)\"\s*,\s*(\w+)\s*\)", line
        )
        if m:
            current = {
                "type": _TYPE_MAP.get(m.group(3), m.group(3)),
                "nullable": m.group(1) is not None,
            }
            options[m.group(2)] = current
            i += 1
            continue

        if current is None:
            i += 1
            continue

        # Simple field assignments: def->field = value;
        m = re.match(r"def->(\w+)\s*=\s*(.*)", line)
        if m:
            field = m.group(1)
            text, i = _collect_until(lines, i, ";")
            rest = text.split("=", 1)[1].strip().rstrip(";").strip()

            match field:
                case "label" | "full_label" | "tooltip" | "sidetext" | "category" | "ratio_over":
                    v = "".join(_extract_strings(rest))
                    if v:
                        current[field] = v
                case "min" | "max":
                    try:
                        current[field] = float(rest)
                    except ValueError:
                        pass
                case "mode":
                    current["mode"] = _MODE_MAP.get(rest, rest)

            i += 1
            continue

        # set_enum_values(...) and set_enum<T>(...) — both use the same inner structure
        if re.search(r"->set_enum\b", line) and "->set_enum_labels" not in line:
            block, i = _collect_until(lines, i, ");")
            values, labels = _parse_enum_block(block)
            if values:
                current["enum_values"] = values
            if labels:
                current["enum_labels"] = labels

        # set_enum_labels(...) — label-only override (e.g. open enum with implied int values)
        elif "->set_enum_labels(" in line:
            block, i = _collect_until(lines, i, ");")
            labels = _extract_strings(block)
            if labels:
                current["enum_labels"] = labels

        i += 1

    # Synthesise filament_* nullable overrides that PrintConfig generates dynamically.
    # Each is a nullable copy of its base retract option, meaning nil = "use printer default".
    for base_key in _FILAMENT_RETRACT_BASES:
        if base_key not in options:
            continue
        base = options[base_key]
        filament_key = f"filament_{base_key}"
        if filament_key in options:
            continue  # already parsed (shouldn't happen, but be safe)
        entry: dict = {"type": base["type"], "nullable": True}
        for field in ("label", "full_label", "tooltip", "sidetext", "category", "mode"):
            if field in base:
                entry[field] = base[field]
        entry["nil_means"] = f"Inherit printer default ({base_key})"
        options[filament_key] = entry

    return options


_HELP_BASE = "https://help.prusa3d.com/en/article/"


def _resolve_str_expr(expr: str, variables: dict[str, str]) -> str | None:
    """Resolve a simple C++ string expression into a Python string.

    Handles:  "literal"
              variable
              variable + "literal"
    """
    expr = expr.strip().rstrip(";").strip()
    if m := re.fullmatch(r'"([^"]*)"', expr):
        return m.group(1)
    if m := re.fullmatch(r"(\w+)", expr):
        return variables.get(m.group(1))
    if m := re.fullmatch(r'(\w+)\s*\+\s*"([^"]*)"', expr):
        base = variables.get(m.group(1), "")
        return base + m.group(2)
    return None


def parse_help_urls(tab_cpp_path: Path) -> dict[str, str]:
    """Extract per-option help URLs from the GUI Tab.cpp registration code."""
    lines = tab_cpp_path.read_text(encoding="utf-8", errors="replace").splitlines()
    variables: dict[str, str] = {}
    urls: dict[str, str] = {}

    for line in lines:
        line = line.strip()

        # String variable assignment:  [std::string] name = "value";
        if m := re.match(r"(?:std::string\s+)?(\w+)\s*=\s*(.+?)\s*;$", line):
            resolved = _resolve_str_expr(m.group(2), variables)
            if resolved is not None:
                variables[m.group(1)] = resolved

        # append_single_option_line("key")  — no URL, skip
        # append_single_option_line("key", path_expr)  — has URL
        if m := re.match(
            r'optgroup->append_single_option_line\(\s*"([^"]+)"\s*,\s*(.+?)\s*\)\s*;', line
        ):
            path_end = _resolve_str_expr(m.group(2), variables)
            if path_end:
                urls[m.group(1)] = _HELP_BASE + path_end

    return urls


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path/to/PrintConfig.cpp>", file=sys.stderr)
        sys.exit(1)

    cpp_path = Path(sys.argv[1])
    if not cpp_path.exists():
        print(f"File not found: {cpp_path}", file=sys.stderr)
        sys.exit(1)

    # Tab.cpp lives at ../slic3r/GUI/Tab.cpp relative to PrintConfig.cpp's directory
    tab_cpp_path = cpp_path.parent.parent / "slic3r/GUI/Tab.cpp"

    print(f"Parsing {cpp_path} ...")
    options = parse(cpp_path)
    print(f"Extracted {len(options)} options")

    if tab_cpp_path.exists():
        print(f"Parsing {tab_cpp_path} ...")
        urls = parse_help_urls(tab_cpp_path)
        print(f"Extracted {len(urls)} help URLs")
        for key, url in urls.items():
            if key in options:
                options[key]["help_url"] = url
    else:
        print(f"Tab.cpp not found at {tab_cpp_path}, skipping help URLs", file=sys.stderr)

    OUTPUT.write_text(json.dumps(options, indent=2, ensure_ascii=False) + "\n")
    print(f"Written to {OUTPUT}")


if __name__ == "__main__":
    main()

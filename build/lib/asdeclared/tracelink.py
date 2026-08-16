"""asdeclared → TraceLink: violations become register entries.

TraceLink (github.com/emmepi86/TraceLink) turns an append-only findings
register into one note per finding, linked to code file:line in both
directions. asdeclared produces findings that are *born* anchored — every
violation already carries paths and lines — so the two compose without
either absorbing the other.

This adapter reads an asdeclared status JSON and emits a **register
fragment**: markdown sections in the exact shape TraceLink's `split.py`
consumes (``## ID — title [SEVERITY]``). Symbols and paths appear in
backticks so `symbols.py` can link them.

Two deliberate choices:

* **The adapter never touches an existing register.** It emits a
  fragment to stdout or ``--out``; appending is the operator's gesture.
  A tool that appends to your findings register on its own initiative
  is a tool you stop trusting.
* **IDs are stable across runs** — derived from the config item id, not
  from a counter — so a violation that persists across runs maps to the
  same note, and its history accumulates where TraceLink expects it:
  in follow-up sections, not in duplicates.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, List, Optional

#: fail on a strict-only rule is a review queue, not an emergency.
SEVERITY_BY_BLOCKING = {True: "HIGH", False: "MEDIUM"}


def _finding_id(prefix: str, result: Dict) -> str:
    stem = result["id"].upper().replace("_", "-")
    return f"{prefix}-{stem}"


def _title(result: Dict) -> str:
    reason = result.get("reason") or "violation"
    return f"{result['rule']}: {reason}"


def _body(result: Dict) -> List[str]:
    lines: List[str] = []
    path = result.get("path")
    if path:
        lines.append(f"Module: `{path}`")
    table = result.get("table")
    if table:
        lines.append(f"Surface: `{table}` — declared writers: "
                     + ", ".join(f"`{w}`" for w in
                                 result.get("declared_writers", [])))
    for w in result.get("undeclared_writers", []):
        lines.append(f"- undeclared write at `{w['path']}`:L{w['line']} "
                     f"(`{w['match'].strip()}`)")
    for e in result.get("evidence", []) if isinstance(
            result.get("evidence"), list) else []:
        kind = e.get("kind", "call")
        lines.append(f"- {kind} `{e['call']}` at `{path}`:L{e['line']}")
    if isinstance(result.get("evidence"), dict):
        for role, verdict in result["evidence"].items():
            lines.append(f"- {role}: {verdict} "
                         f"(authoritative: "
                         f"`{result.get('authoritative_callee', '?')}`)")
    for key in ("repo_only", "ledger_only", "digest_mismatch"):
        for f in result.get(key, []):
            lines.append(f"- {key}: `{f}`")
    return lines


def to_register_fragment(status: Dict, prefix: str = "ARCH") -> str:
    """The markdown fragment for every failing rule, deterministic."""
    sections: List[str] = []
    for result in status.get("results", []):
        if result["status"] != "fail":
            continue
        blocking = not result.get("blocking_in_strict_only", False)
        severity = SEVERITY_BY_BLOCKING[blocking]
        sections.append(
            f"## {_finding_id(prefix, result)} — {_title(result)} "
            f"[{severity}]")
        sections.extend(_body(result))
        commit = status.get("repo_commit")
        sections.append(
            f"Detected by `{status.get('tool_version', 'asdeclared')}`"
            + (f" at commit `{commit[:12]}`" if commit else "")
            + ".")
        sections.append("")
    return "\n".join(sections)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="asdeclared-tracelink",
        description="emit asdeclared violations as a TraceLink register "
                    "fragment (stdout or --out; never appends for you)")
    ap.add_argument("--status", required=True,
                    help="asdeclared output JSON")
    ap.add_argument("--prefix", default="ARCH")
    ap.add_argument("--out", help="write the fragment here instead of stdout")
    args = ap.parse_args(argv)

    try:
        with open(args.status, encoding="utf-8") as fh:
            status = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"status unreadable: {exc}", file=sys.stderr)
        return 1

    fragment = to_register_fragment(status, args.prefix)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(fragment)
        print(f"fragment written to {args.out} "
              f"({fragment.count(chr(10) + '## ') + bool(fragment)} findings)",
              file=sys.stderr)
    else:
        sys.stdout.write(fragment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

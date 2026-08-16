"""asdeclared — a config-driven, read-only guard against architecture drift.

Five checks, one CLI, no dependencies, no network:

1. **Critical deployed bytes** — the files you deploy are the files you
   reviewed. A presence check is not enough: a stale copy *exists* and
   still betrays you. Only a byte comparison tells the truth.
2. **Transaction ownership** — a module declared caller-transactional
   must not `commit()`, `rollback()` or acquire its own connections. If
   it does, every "rollback-only" test built on top of it attests a
   guarantee the code does not give.
3. **Primary/retry entrypoint parity** — the retry path is exercised
   precisely when the primary just failed. If it calls a different,
   older entrypoint, your fallback silently bypasses whatever the
   primary was fixed to do.
4. **Competing writer discovery** — a table with a declared owner and
   an undeclared second writer has two sources of truth. The second
   writer is usually added in good faith by someone who did not find
   the first.
5. **Migration inventory parity** — the repository and the migration
   ledger must tell the same story: a migration applied but never
   recorded, or edited after application, means your repo no longer
   describes the schema that actually ran.

Engine rules:

* **No project names in the engine.** Symbols, tables, paths and
  conventions live in JSON configuration; the engine is reusable as-is.
* `ast` — never regex — for Python call detection: a comment that
  mentions ``commit()`` must not trigger.
* SQL writer discovery scans string literals conservatively, but every
  match reports the exact file and line.
* Omitted optional inputs yield ``not_checked``, never ``pass``: not
  having looked is not the same as having verified.
* Deterministic output: stable ordering, no wall clock unless the
  caller supplies ``--collected-at``. Two identical runs are
  byte-identical.
* Read-only by construction: the only write is ``--out``.
* Unparsable Python is reported, never silently skipped.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

TOOL_VERSION = "asdeclared/0.1.0-draft"

RULE_STATUSES = ("pass", "fail", "not_checked")

DEFAULT_MIGRATION_PATTERN = r"^.*\.sql$"
DEFAULT_LEDGER_FIELDS = {"filename": "filename", "digest": "sha256"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sha256_file(path: str) -> Optional[str]:
    """Streamed digest: critical files can be large."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def repo_commit(repo: str) -> Optional[str]:
    """The commit, when the repo is git. Absence is not an error."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _parse_python(path: str) -> Tuple[Optional[ast.AST], Optional[str]]:
    """``(tree, error)``. A source that does not parse is REPORTED:
    skipping it silently would say ``pass`` about a file never read."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return ast.parse(fh.read(), filename=path), None
    except (OSError, SyntaxError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _dotted_name(node: ast.AST) -> str:
    """``a.b.c`` from an attribute chain; '' when it is not a name."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


# ---------------------------------------------------------------------------
# check 1 — critical deployed bytes
# ---------------------------------------------------------------------------

def check_critical_files(cfg: Dict, repo: str,
                         deployment_root: Optional[str]) -> List[Dict]:
    results = []
    for item in cfg.get("critical_files", []):
        rel = item["path"]
        if deployment_root is None:
            results.append({"id": item["id"],
                            "rule": "critical_deployed_bytes",
                            "status": "not_checked", "path": rel,
                            "reason": "deployment_root not supplied"})
            continue
        d_repo = sha256_file(os.path.join(repo, rel))
        d_dep = sha256_file(os.path.join(deployment_root, rel))
        if d_repo is None:
            status, reason = "fail", "missing from repository"
        elif d_dep is None:
            status, reason = "fail", "missing from deployment"
        elif d_repo != d_dep:
            status, reason = "fail", "bytes differ"
        else:
            status, reason = "pass", None
        results.append({"id": item["id"], "rule": "critical_deployed_bytes",
                        "status": status, "path": rel, "reason": reason,
                        "repo_sha256": d_repo, "deployed_sha256": d_dep})
    return results


# ---------------------------------------------------------------------------
# check 2 — transaction ownership
# ---------------------------------------------------------------------------

def check_transaction_ownership(cfg: Dict, repo: str) -> List[Dict]:
    results = []
    for item in cfg.get("caller_transactional_modules", []):
        path = os.path.join(repo, item["path"])
        tree, error = _parse_python(path)
        if tree is None:
            results.append({"id": item["id"],
                            "rule": "forbidden_transaction_ownership",
                            "status": "fail", "path": item["path"],
                            "reason": f"unparsable: {error}",
                            "evidence": []})
            continue

        forbidden_conn = set(item.get("forbidden_connection_calls", []))
        evidence = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _dotted_name(node.func)
            method = name.rsplit(".", 1)[-1] if name else ""
            if method in ("commit", "rollback"):
                evidence.append({"line": node.lineno, "call": name})
            elif name in forbidden_conn or method in forbidden_conn:
                evidence.append({"line": node.lineno, "call": name,
                                 "kind": "connection_acquisition"})
        evidence.sort(key=lambda e: (e["line"], e["call"]))
        results.append({"id": item["id"],
                        "rule": "forbidden_transaction_ownership",
                        "status": "fail" if evidence else "pass",
                        "path": item["path"], "evidence": evidence,
                        "reason": ("owns the transaction it claims to "
                                   "receive" if evidence else None)})
    return results


# ---------------------------------------------------------------------------
# check 3 — primary/retry entrypoint parity
# ---------------------------------------------------------------------------

def _calls_inside_symbol(tree: ast.AST, symbol: str) -> Optional[set]:
    """Names called inside function ``symbol``. ``None`` when the symbol
    does not exist — which is not the same as an empty set."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == symbol:
            calls = set()
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    name = _dotted_name(child.func)
                    if name:
                        calls.add(name)
                        calls.add(name.rsplit(".", 1)[-1])
            return calls
    return None


def check_entrypoint_parity(cfg: Dict, repo: str) -> List[Dict]:
    results = []
    for item in cfg.get("paired_entrypoints", []):
        path = os.path.join(repo, item["caller_module"])
        tree, error = _parse_python(path)
        if tree is None:
            results.append({"id": item["id"],
                            "rule": "authoritative_entrypoint_parity",
                            "status": "fail",
                            "path": item["caller_module"],
                            "reason": f"unparsable: {error}"})
            continue

        callee = item["authoritative_callee"]
        detail = {}
        for role in ("primary_symbol", "retry_symbol"):
            calls = _calls_inside_symbol(tree, item[role])
            if calls is None:
                detail[role] = "symbol_missing"
            else:
                detail[role] = ("calls_authoritative" if callee in calls
                                else "does_NOT_call_authoritative")
        ok = all(v == "calls_authoritative" for v in detail.values())
        results.append({"id": item["id"],
                        "rule": "authoritative_entrypoint_parity",
                        "status": "pass" if ok else "fail",
                        "path": item["caller_module"],
                        "authoritative_callee": callee,
                        "evidence": dict(sorted(detail.items())),
                        "reason": None if ok else
                        "primary and retry do not converge on the "
                        "authoritative callee"})
    return results


# ---------------------------------------------------------------------------
# check 4 — competing writer discovery
# ---------------------------------------------------------------------------

def _python_files(repo: str) -> List[str]:
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(
            d for d in dirs
            if d not in (".git", "__pycache__", "node_modules",
                         ".venv", "venv"))
        for f in sorted(files):
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(root, f), repo))
    return out


def _write_pattern(table: str) -> "re.Pattern":
    t = re.escape(table)
    return re.compile(
        r"(INSERT\s+INTO\s+" + t + r"\b"
        r"|UPDATE\s+" + t + r"\b"
        r"|DELETE\s+FROM\s+" + t + r"\b)",
        re.IGNORECASE)


def check_competing_writers(cfg: Dict, repo: str) -> List[Dict]:
    items = cfg.get("authoritative_writers", [])
    if not items:
        return []
    patterns = {i["table"]: _write_pattern(i["table"]) for i in items}
    found: Dict[str, List[Dict]] = {i["table"]: [] for i in items}

    for rel in _python_files(repo):
        try:
            with open(os.path.join(repo, rel), "r", encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for table, pat in patterns.items():
            for m in pat.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                found[table].append({"path": rel, "line": line,
                                     "match": m.group(0)[:60]})

    results = []
    for item in items:
        declared = set(item.get("declared_writers", []))
        matches = sorted(found[item["table"]],
                         key=lambda x: (x["path"], x["line"]))
        undeclared = [m for m in matches if m["path"] not in declared]
        results.append({"id": item["id"],
                        "rule": "competing_writer_discovery",
                        "status": "fail" if undeclared else "pass",
                        "table": item["table"],
                        "declared_writers": sorted(declared),
                        "undeclared_writers": undeclared,
                        "all_matches": matches,
                        "reason": (f"{len(undeclared)} undeclared write "
                                   f"site(s)" if undeclared else None),
                        # Discovery is a review queue, not an automatic
                        # breakage: it gates the exit code only under
                        # --strict, but the failure stays visible.
                        "blocking_in_strict_only": True})
    return results


# ---------------------------------------------------------------------------
# check 5 — migration inventory parity
# ---------------------------------------------------------------------------

def check_migration_parity(cfg: Dict, repo: str,
                           ledger_json: Optional[str]) -> List[Dict]:
    directory = cfg.get("migration_directory")
    if not directory:
        return []
    if ledger_json is None:
        return [{"id": "migrations", "rule": "migration_inventory_parity",
                 "status": "not_checked",
                 "reason": "migration_ledger_json not supplied"}]

    fields = dict(DEFAULT_LEDGER_FIELDS)
    fields.update(cfg.get("migration_ledger_fields", {}))
    try:
        with open(ledger_json, encoding="utf-8") as fh:
            ledger = {r[fields["filename"]]: r.get(fields["digest"])
                      for r in json.load(fh)}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [{"id": "migrations", "rule": "migration_inventory_parity",
                 "status": "fail",
                 "reason": f"ledger unreadable: {type(exc).__name__}"}]

    pattern = re.compile(cfg.get("migration_filename_pattern",
                                 DEFAULT_MIGRATION_PATTERN))
    absolute = os.path.join(repo, directory)
    in_repo = {}
    try:
        for f in sorted(os.listdir(absolute)):
            if pattern.match(f):
                in_repo[f] = sha256_file(os.path.join(absolute, f))
    except OSError as exc:
        return [{"id": "migrations", "rule": "migration_inventory_parity",
                 "status": "fail",
                 "reason": f"migration directory unreadable: {exc}"}]

    repo_only = sorted(set(in_repo) - set(ledger))
    ledger_only = sorted(set(ledger) - set(in_repo))
    mismatched = sorted(
        f for f in set(in_repo) & set(ledger)
        if ledger[f] and in_repo[f] and ledger[f] != in_repo[f])
    ok = not (repo_only or ledger_only or mismatched)
    return [{"id": "migrations", "rule": "migration_inventory_parity",
             "status": "pass" if ok else "fail",
             "repo_only": repo_only, "ledger_only": ledger_only,
             "digest_mismatch": mismatched,
             "repo_count": len(in_repo), "ledger_count": len(ledger),
             "reason": None if ok else
             "repository and ledger do not tell the same schema story"}]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

REQUIRED_SECTIONS = ("critical_files", "caller_transactional_modules",
                     "paired_entrypoints", "authoritative_writers",
                     "migration_directory")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="asdeclared",
        description="five read-only architecture drift checks")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--deployment-root")
    ap.add_argument("--migration-ledger-json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--collected-at",
                    help="caller-supplied timestamp: without it the "
                         "output carries no clock and two identical "
                         "runs are byte-identical")
    args = ap.parse_args(argv)

    try:
        with open(args.config, encoding="utf-8") as fh:
            config_text = fh.read()
        cfg = json.loads(config_text)
    except (OSError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    missing = [k for k in REQUIRED_SECTIONS if k not in cfg]
    if missing:
        print(f"incomplete config, missing sections: {missing}",
              file=sys.stderr)
        return 1
    if not os.path.isdir(args.repo):
        print(f"repo is not a directory: {args.repo}", file=sys.stderr)
        return 1

    results: List[Dict] = []
    results += check_critical_files(cfg, args.repo, args.deployment_root)
    results += check_transaction_ownership(cfg, args.repo)
    results += check_entrypoint_parity(cfg, args.repo)
    results += check_competing_writers(cfg, args.repo)
    results += check_migration_parity(cfg, args.repo,
                                      args.migration_ledger_json)
    results.sort(key=lambda r: (r["rule"], r["id"]))

    counts = {s: sum(1 for r in results if r["status"] == s)
              for s in RULE_STATUSES}
    blocking = [r for r in results if r["status"] == "fail"
                and (args.strict or not r.get("blocking_in_strict_only"))]

    input_digests = {"config": hashlib.sha256(
        config_text.encode("utf-8")).hexdigest()}
    if args.migration_ledger_json:
        input_digests["migration_ledger"] = sha256_file(
            args.migration_ledger_json)

    output = {
        "tool_version": TOOL_VERSION,
        "repo_commit": repo_commit(args.repo),
        "strict": bool(args.strict),
        "input_digests": dict(sorted(input_digests.items())),
        "summary": dict(sorted(counts.items())),
        "blocking_failures": len(blocking),
        "results": results,
    }
    if args.collected_at:
        output["collected_at"] = args.collected_at

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")

    print(f"pass={counts['pass']}  fail={counts['fail']}  "
          f"not_checked={counts['not_checked']}  "
          f"blocking={len(blocking)}")
    print(f"report written to {args.out}")
    return 2 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())

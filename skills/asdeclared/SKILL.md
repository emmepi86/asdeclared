---
name: asdeclared
description: Use when code changes might violate declared architecture — before a deploy, after a refactor, when touching a guarded table, transaction boundary or entrypoint — or when asked "who writes this table" or "is production running what we reviewed". Runs the asdeclared drift checks and reports violations with file and line.
---

# asdeclared

Check that the system is still *as declared*: who writes each table, who
owns each transaction, which entrypoints the retry path uses, which
bytes run in production, and whether the migration ledger still matches
the repository.

## Why an agent should care

The failure modes this tool catches are precisely the ones AI-assisted
development produces at speed: a second writer added because the first
was never found, a retry left on the entrypoint the primary was migrated
away from, a "rollback-only" harness built over a module that commits
internally. They are invisible to unit tests and code review because
each lives in the gap between things that are individually correct.

## When this applies

- the project contains a `declared.json` (or any asdeclared config) —
  **consult it before editing** anything it names: those surfaces are
  guarded for a reason, and each entry carries the rationale
- you just refactored, moved, or added code that writes to a database,
  opens connections, or wraps another entrypoint
- a deploy is about to happen and a deployment root is available
- someone asks "who writes this table", "is the retry on the same path
  as the primary", "does production match the repo"

## Run it

Paths must be absolute. When the plugin is invoked the working directory
is the user's project, not the plugin — if the package is not installed,
run it from the plugin checkout with `PYTHONPATH=<plugin>/src python3 -m
asdeclared`.

```console
asdeclared --repo /abs/path/to/repo \
  --config /abs/path/to/declared.json \
  --out /tmp/asdeclared-status.json \
  [--deployment-root /abs/deployed/source] \
  [--migration-ledger-json /abs/ledger.json] \
  [--strict]
```

Exit codes: `0` clean · `1` config or input error · `2` violations.

## Read the result honestly

- `not_checked` is **not** a pass: it means an optional input was not
  supplied. Say "not checked", never "ok".
- Every `fail` carries evidence with exact file and line — report those,
  not a summary of them.
- `blocking_in_strict_only` failures (undeclared writers) are a review
  queue: report them as candidates for the human to declare or remove,
  not as automatic errors.

## Rules for the agent

1. **Never silence a finding by editing the config.** Adding a writer to
   `declared_writers`, relaxing a pattern or deleting an entry is an
   architecture decision — it belongs to a human, with the rationale
   updated to say why.
2. **Self-check after your own edits.** If you changed code in a repo
   that has an asdeclared config, run the tool before declaring the work
   done. The drift it catches is most often drift *you* just introduced.
3. **Report `unparsable` files** — a file the checker cannot read is not
   a clean file.
4. When findings should be remembered, hand them to TraceLink:

```console
asdeclared-tracelink --status /tmp/asdeclared-status.json >> FINDINGS.md
```

then let the TraceLink skill turn the register into linked notes. Never
append to a register without showing the fragment first.

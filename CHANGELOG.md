# Changelog

## 0.1.1

- Writer discovery now scans string literals via `tokenize` instead of
  raw text: a Python comment mentioning `INSERT INTO ...` no longer
  counts as a write. Unparsable files stay conservative (whole text).
  Found in production: a file flagged for a comment.

## 0.1.0

- Five checks: deployed bytes, transaction ownership, entrypoint parity,
  competing writers, migration ledger parity.
- TraceLink adapter: violations as append-ready register fragments,
  stable finding ids, never appends on its own.
- Runnable thirty-second demo project, guarded by its own test.
- Zero dependencies, zero network, deterministic output.
- Extracted from an internal incident-response tool; the five checks are
  the five failure classes that actually happened.

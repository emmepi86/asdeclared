"""asdeclared test suite: miniature repos, public CLI, determinism.

Ported from the internal incident-response tool's suite; every scenario
reproduces the shape of a failure that actually happened somewhere.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from asdeclared import cli as engine            # noqa: E402
from asdeclared import tracelink as adapter     # noqa: E402


def _config(**extra):
    base = {"critical_files": [], "caller_transactional_modules": [],
            "paired_entrypoints": [], "authoritative_writers": [],
            "migration_directory": "migrations"}
    base.update(extra)
    return base


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    return r


def _run_cli(repo, config, out, *argv):
    cfg = repo.parent / "cfg.json"
    cfg.write_text(json.dumps(config), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SRC / "asdeclared" / "cli.py"),
         "--repo", str(repo), "--config", str(cfg),
         "--out", str(out), *argv],
        capture_output=True, text=True, timeout=120)
    data = json.loads(out.read_text()) if out.exists() else None
    return proc.returncode, data


# -- check 1 ------------------------------------------------------------

class TestDeployedBytes:
    def test_missing_deployed_file_fails(self, repo, tmp_path):
        _write(repo / "a.py", "x = 1\n")
        deploy = tmp_path / "deploy"
        deploy.mkdir()
        cfg = _config(critical_files=[
            {"id": "cf-a", "path": "a.py", "rationale": "t"}])
        r = engine.check_critical_files(cfg, str(repo), str(deploy))[0]
        assert r["status"] == "fail" and "missing from deployment" in r["reason"]

    def test_byte_different_file_fails(self, repo, tmp_path):
        _write(repo / "a.py", "x = 1\n")
        _write(tmp_path / "deploy" / "a.py", "x = 2\n")
        cfg = _config(critical_files=[
            {"id": "cf-a", "path": "a.py", "rationale": "t"}])
        r = engine.check_critical_files(cfg, str(repo),
                                        str(tmp_path / "deploy"))[0]
        assert (r["status"], r["reason"]) == ("fail", "bytes differ")

    def test_identical_file_passes(self, repo, tmp_path):
        _write(repo / "a.py", "x = 1\n")
        _write(tmp_path / "deploy" / "a.py", "x = 1\n")
        cfg = _config(critical_files=[
            {"id": "cf-a", "path": "a.py", "rationale": "t"}])
        assert engine.check_critical_files(
            cfg, str(repo), str(tmp_path / "deploy"))[0]["status"] == "pass"


# -- check 2 ------------------------------------------------------------

_COMMITTING = ("def w(conn):\n    cur = conn.cursor()\n"
               "    cur.execute('INSERT INTO t VALUES (1)')\n"
               "    conn.commit()\n")
_CLEAN = ("def w(cur):\n    cur.execute('INSERT INTO t VALUES (1)')\n"
          "    # commit(): naming it in a comment must NOT trigger\n")


class TestTransactionOwnership:
    def _cfg(self, **kw):
        return _config(caller_transactional_modules=[
            {"id": "tx-w", "path": "w.py", "rationale": "t", **kw}])

    def test_internal_commit_fails_with_line(self, repo):
        _write(repo / "w.py", _COMMITTING)
        r = engine.check_transaction_ownership(self._cfg(), str(repo))[0]
        assert r["status"] == "fail"
        assert r["evidence"][0]["line"] == 4
        assert r["evidence"][0]["call"].endswith("commit")

    def test_clean_module_passes_despite_comment(self, repo):
        _write(repo / "w.py", _CLEAN)
        assert engine.check_transaction_ownership(
            self._cfg(), str(repo))[0]["status"] == "pass"

    def test_forbidden_connection_acquisition_fails(self, repo):
        _write(repo / "w.py",
               "import psycopg2\n\ndef f():\n"
               "    return psycopg2.connect(dsn='x')\n")
        r = engine.check_transaction_ownership(
            self._cfg(forbidden_connection_calls=["psycopg2.connect"]),
            str(repo))[0]
        assert r["status"] == "fail"
        assert r["evidence"][0]["kind"] == "connection_acquisition"

    def test_unparsable_source_is_reported(self, repo):
        _write(repo / "w.py", "def broken(:\n")
        r = engine.check_transaction_ownership(self._cfg(), str(repo))[0]
        assert r["status"] == "fail" and "unparsable" in r["reason"]


# -- check 3 ------------------------------------------------------------

_DIVERGENT = ("def primary(p, t):\n    return p.entry_v2(t)\n\n"
              "def retry(p, t):\n    return p.entry_v1(t)\n")
_ALIGNED = ("def primary(p, t):\n    return p.entry_v2(t)\n\n"
            "def retry(p, t):\n    return p.entry_v2(t)\n")


class TestEntrypointParity:
    def _cfg(self):
        return _config(paired_entrypoints=[
            {"id": "ep-c", "caller_module": "c.py",
             "primary_symbol": "primary", "retry_symbol": "retry",
             "authoritative_callee": "entry_v2", "rationale": "t"}])

    def test_retry_on_different_callee_fails(self, repo):
        _write(repo / "c.py", _DIVERGENT)
        r = engine.check_entrypoint_parity(self._cfg(), str(repo))[0]
        assert r["status"] == "fail"
        assert r["evidence"]["retry_symbol"] == "does_NOT_call_authoritative"
        assert r["evidence"]["primary_symbol"] == "calls_authoritative"

    def test_both_on_authoritative_pass(self, repo):
        _write(repo / "c.py", _ALIGNED)
        assert engine.check_entrypoint_parity(
            self._cfg(), str(repo))[0]["status"] == "pass"

    def test_missing_symbol_fails_not_passes(self, repo):
        _write(repo / "c.py", "def primary():\n    pass\n")
        assert engine.check_entrypoint_parity(
            self._cfg(), str(repo))[0]["status"] == "fail"


# -- check 4 ------------------------------------------------------------

class TestCompetingWriters:
    def _cfg(self):
        return _config(authoritative_writers=[
            {"id": "aw-t", "table": "orders_v1",
             "declared_writers": ["owner.py"], "rationale": "t"}])

    def test_undeclared_writer_fails(self, repo):
        _write(repo / "owner.py", 'SQL = "INSERT INTO orders_v1 VALUES (1)"\n')
        _write(repo / "intruder.py", 'Q = """UPDATE orders_v1 SET x = 1"""\n')
        r = engine.check_competing_writers(self._cfg(), str(repo))[0]
        assert r["status"] == "fail"
        assert r["undeclared_writers"][0]["path"] == "intruder.py"
        assert r["undeclared_writers"][0]["line"] == 1

    def test_only_declared_writer_passes(self, repo):
        _write(repo / "owner.py", 'SQL = "INSERT INTO orders_v1 VALUES (1)"\n')
        assert engine.check_competing_writers(
            self._cfg(), str(repo))[0]["status"] == "pass"

    def test_select_is_not_a_write(self, repo):
        _write(repo / "owner.py", 'SQL = "INSERT INTO orders_v1 VALUES (1)"\n')
        _write(repo / "reader.py", 'Q = "SELECT * FROM orders_v1"\n')
        assert engine.check_competing_writers(
            self._cfg(), str(repo))[0]["status"] == "pass"


# -- check 5 ------------------------------------------------------------

class TestMigrationParity:
    def _prepare(self, repo, tmp_path, in_repo, in_ledger):
        for name, body in in_repo.items():
            _write(repo / "migrations" / name, body)
        (repo / "migrations").mkdir(exist_ok=True)
        ledger = tmp_path / "ledger.json"
        rows = [{"filename": n,
                 "sha256": (engine.hashlib.sha256(b.encode()).hexdigest()
                            if b is not None else None)}
                for n, b in in_ledger.items()]
        ledger.write_text(json.dumps(rows), encoding="utf-8")
        return str(ledger)

    def test_repo_only_migration_fails(self, repo, tmp_path):
        ledger = self._prepare(repo, tmp_path, {"001_a.sql": "SELECT 1;"}, {})
        r = engine.check_migration_parity(_config(), str(repo), ledger)[0]
        assert r["status"] == "fail" and r["repo_only"] == ["001_a.sql"]

    def test_ledger_only_migration_fails(self, repo, tmp_path):
        ledger = self._prepare(repo, tmp_path, {}, {"001_a.sql": "SELECT 1;"})
        r = engine.check_migration_parity(_config(), str(repo), ledger)[0]
        assert r["status"] == "fail" and r["ledger_only"] == ["001_a.sql"]

    def test_digest_mismatch_fails(self, repo, tmp_path):
        ledger = self._prepare(repo, tmp_path, {"001_a.sql": "SELECT 1;"},
                               {"001_a.sql": "SELECT 2;"})
        r = engine.check_migration_parity(_config(), str(repo), ledger)[0]
        assert r["status"] == "fail" and r["digest_mismatch"] == ["001_a.sql"]

    def test_pattern_and_ledger_fields_are_configurable(self, repo, tmp_path):
        """The two conventions that were hardcoded in the internal tool."""
        _write(repo / "migrations" / "V42__init.sql", "SELECT 1;")
        ledger = tmp_path / "ledger.json"
        digest = engine.hashlib.sha256(b"SELECT 1;").hexdigest()
        ledger.write_text(json.dumps(
            [{"name": "V42__init.sql", "hash": digest}]), encoding="utf-8")
        cfg = _config(migration_filename_pattern=r"^V\d+__.*\.sql$",
                      migration_ledger_fields={"filename": "name",
                                               "digest": "hash"})
        assert engine.check_migration_parity(
            cfg, str(repo), str(ledger))[0]["status"] == "pass"


# -- public CLI ----------------------------------------------------------

class TestPublicCli:
    def test_omitted_optional_inputs_yield_not_checked_never_pass(
            self, repo, tmp_path):
        _write(repo / "w.py", _CLEAN)
        config = _config(
            critical_files=[{"id": "cf-a", "path": "w.py",
                             "rationale": "t"}],
            caller_transactional_modules=[
                {"id": "tx-w", "path": "w.py", "rationale": "t"}])
        code, data = _run_cli(repo, config, tmp_path / "out.json")
        assert code == 0
        by_rule = {r["rule"]: r["status"] for r in data["results"]}
        assert by_rule["critical_deployed_bytes"] == "not_checked"
        assert by_rule["migration_inventory_parity"] == "not_checked"
        assert data["summary"]["not_checked"] == 2

    def test_two_identical_runs_are_byte_identical(self, repo, tmp_path):
        _write(repo / "w.py", _COMMITTING)
        config = _config(caller_transactional_modules=[
            {"id": "tx-w", "path": "w.py", "rationale": "t"}])
        _run_cli(repo, config, tmp_path / "o1.json")
        _run_cli(repo, config, tmp_path / "o2.json")
        assert (tmp_path / "o1.json").read_bytes() == \
            (tmp_path / "o2.json").read_bytes()

    def test_violation_exits_2_and_broken_config_exits_1(
            self, repo, tmp_path):
        _write(repo / "w.py", _COMMITTING)
        config = _config(caller_transactional_modules=[
            {"id": "tx-w", "path": "w.py", "rationale": "t"}])
        code, _ = _run_cli(repo, config, tmp_path / "o.json")
        assert code == 2
        code2, _ = _run_cli(repo, {"critical_files": []},
                            tmp_path / "o2.json")
        assert code2 == 1

    def test_undeclared_writer_blocks_only_under_strict(
            self, repo, tmp_path):
        _write(repo / "intruder.py",
               'Q = "INSERT INTO orders_v1 VALUES (1)"\n')
        config = _config(authoritative_writers=[
            {"id": "aw-t", "table": "orders_v1",
             "declared_writers": ["owner.py"], "rationale": "t"}])
        code, data = _run_cli(repo, config, tmp_path / "o1.json")
        assert code == 0 and data["summary"]["fail"] == 1
        code_strict, _ = _run_cli(repo, config, tmp_path / "o2.json",
                                  "--strict")
        assert code_strict == 2


# -- TraceLink adapter -----------------------------------------------------

class TestTracelinkAdapter:
    STATUS = {
        "tool_version": "asdeclared/test",
        "repo_commit": "abcdef1234567890",
        "results": [
            {"id": "aw-orders", "rule": "competing_writer_discovery",
             "status": "fail", "table": "orders_v1",
             "declared_writers": ["owner.py"],
             "undeclared_writers": [
                 {"path": "intruder.py", "line": 7,
                  "match": "UPDATE orders_v1"}],
             "reason": "1 undeclared write site(s)",
             "blocking_in_strict_only": True},
            {"id": "tx-w", "rule": "forbidden_transaction_ownership",
             "status": "fail", "path": "w.py",
             "evidence": [{"line": 4, "call": "conn.commit"}],
             "reason": "owns the transaction it claims to receive"},
            {"id": "ok", "rule": "critical_deployed_bytes",
             "status": "pass", "path": "a.py"},
        ],
    }

    def test_only_failures_become_findings(self):
        frag = adapter.to_register_fragment(self.STATUS)
        assert frag.count("## ARCH-") == 2
        assert "a.py" not in frag

    def test_register_shape_and_anchors(self):
        """The exact shape TraceLink's split.py consumes, with file:line
        anchors in backticks for symbols.py to link."""
        frag = adapter.to_register_fragment(self.STATUS)
        assert "## ARCH-AW-ORDERS — competing_writer_discovery" in frag
        assert "[MEDIUM]" in frag          # strict-only rule → review queue
        assert "[HIGH]" in frag            # transaction rule → blocking
        assert "`intruder.py`:L7" in frag
        assert "`w.py`:L4" in frag
        assert "`abcdef123456`" in frag

    def test_ids_are_stable_across_runs(self):
        a = adapter.to_register_fragment(self.STATUS)
        b = adapter.to_register_fragment(self.STATUS)
        assert a == b


class TestShippedExample:
    """The thirty-second demo must not rot: it is the first thing a
    visitor runs, and a broken first run is the last run."""

    def test_demo_project_shows_exactly_the_advertised_drift(self, tmp_path):
        base = Path(__file__).resolve().parents[1]
        demo = base / "examples" / "demo-project"
        out = tmp_path / "status.json"
        proc = subprocess.run(
            [sys.executable, "-m", "asdeclared",
             "--repo", str(demo),
             "--config", str(demo / "declared.json"),
             "--migration-ledger-json", str(demo / "ledger.json"),
             "--out", str(out)],
            capture_output=True, text=True, timeout=120,
            env={**__import__("os").environ,
                 "PYTHONPATH": str(base / "src")})
        assert proc.returncode == 2
        data = json.loads(out.read_text())
        by_id = {r["id"]: r for r in data["results"]}
        assert by_id["tx-writer"]["status"] == "fail"
        assert by_id["ep-ingest"]["evidence"]["retry_symbol"] == \
            "does_NOT_call_authoritative"
        assert by_id["aw-orders"]["undeclared_writers"][0]["path"] == \
            "services/reporting/export.py"
        assert by_id["migrations"]["digest_mismatch"] == ["001_init.sql"]
        assert by_id["migrations"]["repo_only"] == ["002_region.sql"]


class TestCommentsDoNotWrite:
    """The real false positive: `# In production: INSERT INTO audit_logs`.

    A comment writes nothing. The scan reads STRING LITERALS via
    tokenize; unparsable files stay conservative on raw text.
    """

    def _cfg(self):
        return _config(authoritative_writers=[
            {"id": "aw-t", "table": "orders_v1",
             "declared_writers": ["owner.py"], "rationale": "t"}])

    def test_a_comment_does_NOT_trigger(self, repo):
        _write(repo / "owner.py", 'SQL = "INSERT INTO orders_v1 VALUES (1)"\n')
        _write(repo / "innocent.py",
               "def f():\n"
               "    # In production: INSERT INTO orders_v1\n"
               "    return None\n")
        assert engine.check_competing_writers(
            self._cfg(), str(repo))[0]["status"] == "pass"

    def test_line_numbers_inside_multiline_strings_stay_exact(self, repo):
        _write(repo / "owner.py", 'SQL = "INSERT INTO orders_v1 VALUES (1)"\n')
        _write(repo / "multi.py",
               'Q = """\n-- header\nDELETE FROM orders_v1 WHERE id=1\n"""\n')
        r = engine.check_competing_writers(self._cfg(), str(repo))[0]
        assert r["undeclared_writers"][0]["line"] == 3

    def test_unparsable_files_stay_conservative(self, repo):
        _write(repo / "owner.py", 'SQL = "INSERT INTO orders_v1 VALUES (1)"\n')
        _write(repo / "broken.py",
               "def broken(:\n"
               "    x = 'INSERT INTO orders_v1 (id) VALUES (1)'\n")
        assert engine.check_competing_writers(
            self._cfg(), str(repo))[0]["status"] == "fail"

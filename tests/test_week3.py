"""Week 3 tests — restore checker. No Docker daemon required (all docker
interactions are mocked); the one real-docker probe is skipped when no
daemon is reachable."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.error

import pytest

import restore.check as rc


# ---------------------------------------------------------------- fakes
class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *a, **k):
        self._sql = sql
        if self._conn.fail_on and self._conn.fail_on in sql:
            raise RuntimeError("boom")

    def fetchone(self):
        m = re.search(r'FROM "((?:[^"]|"")+)"', self._sql)
        table = m.group(1).replace('""', '"') if m else None
        return (self._conn.counts.get(table, 0),)

    def fetchall(self):
        return self._conn.query_rows if self._conn.query_rows is not None else []


class FakeConn:
    def __init__(self, counts=None, query_rows=None, fail_on=None):
        self.counts = counts or {}
        self.query_rows = query_rows
        self.fail_on = fail_on

    def cursor(self):
        return FakeCursor(self)


# ---------------------------------------------------------------- format detection
def test_detect_format_custom(tmp_path):
    p = tmp_path / "b.dump"
    p.write_bytes(b"PGDMP\x0d\x0a" + b"\x00" * 100)
    assert rc.detect_format(p) == "custom"


def test_detect_format_plain(tmp_path):
    p = tmp_path / "b.sql"
    p.write_text("CREATE TABLE t (id int);\n")
    assert rc.detect_format(p) == "plain"


# ---------------------------------------------------------------- checks runner
def test_table_row_minimums():
    conn = FakeConn(counts={"users": 150, "orders": 0})
    res = rc.run_checks(conn, {"tables": {"users": 100, "orders": 1}, "queries": []})
    by_name = {r["name"]: r for r in res}
    assert by_name["table users rows"]["passed"] is True
    assert "150 rows" in by_name["table users rows"]["detail"]
    assert by_name["table orders rows"]["passed"] is False
    assert "0 rows" in by_name["table orders rows"]["detail"]


def test_custom_query_needs_one_row():
    ok = rc.run_checks(FakeConn(query_rows=[(1,)]),
                       {"tables": {}, "queries": [{"name": "q", "sql": "SELECT 1"}]})
    assert ok[0]["passed"] is True
    bad = rc.run_checks(FakeConn(query_rows=[]),
                        {"tables": {}, "queries": [{"name": "q", "sql": "SELECT 1"}]})
    assert bad[0]["passed"] is False


def test_failing_check_is_recorded_not_raised():
    conn = FakeConn(counts={"users": 5}, fail_on="missing_table")
    res = rc.run_checks(conn, {"tables": {"users": 1, "missing_table": 1}, "queries": []})
    assert res[0]["passed"] is True  # the good check still ran
    assert res[1]["passed"] is False
    assert res[1]["detail"].startswith("error:")


# ---------------------------------------------------------------- checks file loading
def test_load_checks_yaml(tmp_path):
    p = tmp_path / "checks.yaml"
    p.write_text("tables:\n  users: 10\nqueries:\n  - name: q\n    sql: SELECT 1\n")
    data = rc.load_checks(p)
    assert data == {"tables": {"users": 10},
                    "queries": [{"name": "q", "sql": "SELECT 1"}]}


def test_load_checks_json(tmp_path):
    p = tmp_path / "checks.json"
    p.write_text(json.dumps({"tables": {"users": 2}, "queries": []}))
    assert rc.load_checks(p)["tables"] == {"users": 2}


def test_load_checks_yaml_without_pyyaml(tmp_path, monkeypatch):
    p = tmp_path / "checks.yaml"
    p.write_text("tables: {users: 1}\n")
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(rc.InfraError, match="pyyaml"):
        rc.load_checks(p)


def test_load_checks_rejects_bad_shape(tmp_path):
    p = tmp_path / "checks.yaml"
    p.write_text("tables:\n  - not-a-mapping\n")
    with pytest.raises(rc.InfraError, match="mapping"):
        rc.load_checks(p)


# ---------------------------------------------------------------- telegram
class Bomb:
    def __call__(self, *a, **k):
        raise AssertionError("HTTP must not be called")


def test_telegram_skipped_when_unset(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr("urllib.request.urlopen", Bomb())
    assert rc.maybe_notify_telegram("nightly", {"passed": True, "checks": []}) == "skipped"


def test_telegram_send_shape(monkeypatch):
    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert rc.send_telegram("tok123", "42", "hello") is True
    assert seen["url"] == "https://api.telegram.org/bottok123/sendMessage"
    assert seen["body"] == {"chat_id": "42", "text": "hello"}


def test_telegram_failure_never_raises(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("nope")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert rc.send_telegram("tok", "1", "x") is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    assert rc.maybe_notify_telegram("nightly", {"passed": False, "checks": []}) == "failed"


def test_telegram_summary_text():
    result = {"passed": True, "duration_s": 38.2, "checks": [{"passed": True}] * 3,
              "backup": {"size_bytes": 1_200_000_000, "age_s": 18_000}}
    s = rc.telegram_summary("nightly", result)
    assert s.startswith("✅") and "3/3" in s and "nightly" in s
    bad = dict(result, passed=False, failed_reason="restore failed: exit 1")
    s2 = rc.telegram_summary("nightly", bad)
    assert s2.startswith("❌") and "restore failed" in s2


# ---------------------------------------------------------------- event payload
def test_event_payload_pass_shape():
    result = {"label": "nightly", "passed": True, "duration_s": 42.0,
              "restore_ok": True, "restore_error": None, "failed_reason": None,
              "backup": {"size_bytes": 10}, "checks": [{"passed": True}, {"passed": True}]}
    p = rc.build_event_payload("nightly", result)
    assert p["source"] == "restore" and p["type"] == "restore_check"
    assert p["title"] == "Restore check (nightly): PASS (2/2 checks, 42.0s)"
    assert p["payload"] is result and p["url"] is None


def test_event_payload_fail_shape():
    result = {"label": "nightly", "passed": False, "duration_s": 5.0,
              "failed_reason": "1/2 checks failed: table orders rows",
              "backup": {}, "checks": []}
    p = rc.build_event_payload("nightly", result)
    assert "FAIL" in p["title"] and "table orders rows" in p["title"]


# ---------------------------------------------------------------- orchestration / cleanup
def test_container_cleaned_up_on_failure(tmp_path, monkeypatch):
    bp = tmp_path / "b.dump"
    bp.write_bytes(b"PGDMP" + b"\x00" * 10)
    calls = []
    monkeypatch.setattr(rc, "docker_available", lambda: True)
    monkeypatch.setattr(rc, "start_container",
                        lambda image, password, name: (calls.append(("start", name)), ("127.0.0.1", 5433))[1])
    monkeypatch.setattr(rc, "wait_ready",
                        lambda name, timeout_s=120: (_ for _ in ()).throw(rc.CheckError("not ready")))
    monkeypatch.setattr(rc, "stop_container", lambda name: calls.append(("stop", name)))
    monkeypatch.setattr(rc, "post_event", lambda *a, **k: False)
    monkeypatch.setattr(rc, "maybe_notify_telegram", lambda *a, **k: "skipped")

    args = rc.parse_args(["--backup-file", str(bp), "--api-url", "http://127.0.0.1:9"])
    result = rc.run_check(args)

    assert result["passed"] is False
    assert "not ready" in result["failed_reason"]
    kinds = [c[0] for c in calls]
    assert kinds == ["start", "stop"]  # cleanup ran even though wait_ready blew up
    assert calls[1][1].startswith("copilot-restore-")


def test_no_docker_is_infra_error(tmp_path, monkeypatch):
    bp = tmp_path / "b.dump"
    bp.write_bytes(b"PGDMP")
    monkeypatch.setattr(rc, "docker_available", lambda: False)
    args = rc.parse_args(["--backup-file", str(bp)])
    with pytest.raises(rc.InfraError, match="Docker"):
        rc.run_check(args)


def test_missing_backup_file_is_infra_error(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "docker_available", lambda: True)
    args = rc.parse_args(["--backup-file", str(tmp_path / "nope.dump")])
    with pytest.raises(rc.InfraError, match="not found"):
        rc.run_check(args)


def test_redact_url():
    assert rc.redact_url("postgresql://bob:s3cret@db:5432/app") == \
        "postgresql://bob:***@db:5432/app"
    assert "s3cret" not in rc.redact_url("postgresql://bob:s3cret@db:5432/app")


def test_main_exit_codes(monkeypatch):
    monkeypatch.setattr(rc, "run_check",
                        lambda args: {"passed": True, "checks": [{"passed": True}],
                                      "duration_s": 1.0, "failed_reason": None, "telegram": "skipped"})
    assert rc.main(["--backup-file", "x"]) == 0
    monkeypatch.setattr(rc, "run_check",
                        lambda args: {"passed": False, "checks": [], "duration_s": 1.0,
                                      "failed_reason": "bad", "telegram": "skipped"})
    assert rc.main(["--backup-file", "x"]) == 1

    def boom(args):
        raise rc.InfraError("no docker")

    monkeypatch.setattr(rc, "run_check", boom)
    assert rc.main(["--backup-file", "x"]) == 2


# ---------------------------------------------------------------- real docker (skipped without a daemon)
def _daemon_reachable() -> bool:
    try:
        if not shutil.which("docker"):
            return False
        p = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return p.returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(not _daemon_reachable(), reason="needs a Docker daemon")
def test_docker_probe_real():
    assert rc.docker_available() is True

"""Nightly Postgres restore checker (Week 3).

Restores a backup into an ephemeral Postgres container, runs sanity checks,
records the result as an event (source="restore") and optionally notifies via
Telegram.

Usage:
    python -m restore.check --backup-file /backups/latest.dump --checks restore/checks.yaml
    python -m restore.check --source-url postgresql://user:pw@host/db --checks checks.yaml

Exit codes: 0 = all checks passed, 1 = restore or a check failed, 2 = infra/usage error.

Host prerequisites: docker, postgresql-client (pg_dump/pg_restore/psql),
python 3.12 + requirements.txt. Never commit real credentials — pass them via
env vars (.env is gitignored).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import psycopg

log = logging.getLogger("restore-check")

POSTGRES_IMAGE = "postgres:16.4"
READY_TIMEOUT_S = 120
RESTORE_TIMEOUT_S = 900
DUMP_TIMEOUT_S = 900
STATEMENT_TIMEOUT_MS = 30_000

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INFRA = 2


class CheckError(Exception):
    """A restore/check failure (exit 1)."""


class InfraError(Exception):
    """Missing tooling or bad usage (exit 2)."""


# ---------------------------------------------------------------- helpers
def redact_url(url: str) -> str:
    """Hide the password in a Postgres URL for logs."""
    try:
        parts = urllib.parse.urlsplit(url)
        if "@" in parts.netloc:
            userinfo, _, host = parts.netloc.partition("@")
            user, _, _ = userinfo.partition(":")
            netloc = f"{user}:***@{host}"
            return urllib.parse.urlunsplit(
                (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
            )
    except Exception:
        pass
    return "<redacted-url>"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, capturing output. Never logs secrets (callers redact)."""
    log.debug("exec: %s", " ".join(cmd[:2]) + " ...")
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def docker_available() -> bool:
    try:
        p = run(["docker", "info"], timeout=15)
        return p.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def detect_format(path: Path) -> str:
    """'custom' for pg_dump custom format (PGDMP magic), else 'plain' SQL."""
    with open(path, "rb") as f:
        magic = f.read(5)
    return "custom" if magic == b"PGDMP" else "plain"


# ---------------------------------------------------------------- container
def start_container(image: str, password: str, name: str) -> tuple[str, int]:
    """Start an ephemeral Postgres; return (host, port)."""
    p = run(
        [
            "docker", "run", "-d", "--rm",
            "--name", name,
            "-e", f"POSTGRES_PASSWORD={password}",
            "-p", "127.0.0.1::5432",
            image,
        ],
        timeout=180,
    )
    if p.returncode != 0:
        raise InfraError(f"docker run failed: {p.stderr.strip()[-500:]}")
    p = run(["docker", "port", name, "5432"], timeout=15)
    if p.returncode != 0:
        raise InfraError(f"docker port failed: {p.stderr.strip()[-200:]}")
    # e.g. "127.0.0.1:49153" (or "0.0.0.0:49153")
    mapping = p.stdout.strip().splitlines()[-1]
    host, _, port = mapping.rpartition(":")
    return host or "127.0.0.1", int(port)


def wait_ready(name: str, timeout_s: int = READY_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        p = run(["docker", "exec", name, "pg_isready", "-U", "postgres"], timeout=10)
        if p.returncode == 0:
            return
        time.sleep(2)
    raise InfraError(f"postgres container {name} not ready after {timeout_s}s")


def stop_container(name: str) -> None:
    """Best-effort removal. Never raises — called from finally blocks."""
    try:
        run(["docker", "rm", "-f", name], timeout=30)
    except Exception as e:  # noqa: BLE001 - cleanup must not mask the real error
        log.warning("container cleanup failed for %s: %s", name, e)


def restore_backup(
    fmt: str,
    path: Path,
    host: str,
    port: int,
    password: str,
    timeout_s: int = RESTORE_TIMEOUT_S,
) -> None:
    """Restore into the temp container. Raises CheckError on failure."""
    env = {**os.environ, "PGPASSWORD": password}
    base = ["--host", host, "--port", str(port), "--username", "postgres",
            "--dbname", "postgres"]
    if fmt == "custom":
        cmd = ["pg_restore", *base, "--no-owner", "--clean", "--if-exists", str(path)]
    else:
        cmd = ["psql", *base, "-v", "ON_ERROR_STOP=1", "-f", str(path)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout_s)
    except FileNotFoundError:
        tool = "pg_restore" if fmt == "custom" else "psql"
        raise InfraError(f"{tool} not found — install postgresql-client") from None
    except subprocess.TimeoutExpired:
        raise CheckError(f"restore timed out after {timeout_s}s") from None
    if p.returncode != 0:
        raise CheckError(f"restore failed (exit {p.returncode}): {p.stderr.strip()[-800:]}")


def dump_source(url: str, dest: Path, timeout_s: int = DUMP_TIMEOUT_S) -> None:
    """pg_dump a live source to a custom-format file. URL is never logged."""
    try:
        p = subprocess.run(
            ["pg_dump", "--format=custom", "--file", str(dest), url],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except FileNotFoundError:
        raise InfraError("pg_dump not found — install postgresql-client") from None
    except subprocess.TimeoutExpired:
        raise CheckError(f"pg_dump timed out after {timeout_s}s") from None
    if p.returncode != 0:
        raise CheckError(f"pg_dump failed (exit {p.returncode}): {p.stderr.strip()[-800:]}")


# ---------------------------------------------------------------- checks
def load_checks(path: Path) -> dict:
    """Load the checks file (YAML preferred, JSON accepted)."""
    text = path.read_text()
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise InfraError(
                "checks file is YAML but pyyaml is not installed — "
                "pip install pyyaml or use a .json checks file"
            ) from None
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise InfraError(f"checks file {path} must be a mapping")
    tables = data.get("tables") or {}
    queries = data.get("queries") or []
    if not isinstance(tables, dict) or not isinstance(queries, list):
        raise InfraError(f"checks file {path}: 'tables' must be a mapping, 'queries' a list")
    return {"tables": tables, "queries": queries}


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def run_checks(conn, checks: dict) -> list[dict]:
    """Run sanity checks against an open DBAPI connection.

    Returns [{name, type, passed, detail}]. A check that raises is recorded as
    failed (with the error in detail) — it never aborts the remaining checks.
    """
    results: list[dict] = []
    for table, minimum in (checks.get("tables") or {}).items():
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}")
                count = cur.fetchone()[0]
            passed = count >= int(minimum)
            detail = f"{count} rows (minimum {minimum})"
        except Exception as e:  # noqa: BLE001 - recorded, not raised
            passed, detail = False, f"error: {e}"
        results.append({"name": f"table {table} rows", "type": "table_rows",
                        "passed": passed, "detail": detail})
    for q in checks.get("queries") or []:
        name, sql = q.get("name", "query"), q.get("sql", "")
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                n = len(cur.fetchall())
            passed = n >= 1
            detail = f"{n} row(s) returned (need >= 1)"
        except Exception as e:  # noqa: BLE001 - recorded, not raised
            passed, detail = False, f"error: {e}"
        results.append({"name": name, "type": "query", "passed": passed, "detail": detail})
    return results


# ---------------------------------------------------------------- reporting
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_event_payload(label: str, result: dict) -> dict:
    """Shape the POST /api/events body for a restore-check result."""
    checks = result.get("checks", [])
    passed_n = sum(1 for c in checks if c.get("passed"))
    total_n = len(checks)
    dur = result.get("duration_s", 0)
    if result.get("passed"):
        title = f"Restore check ({label}): PASS ({passed_n}/{total_n} checks, {dur}s)"
    else:
        reason = result.get("failed_reason") or "checks failed"
        title = f"Restore check ({label}): FAIL — {reason}"
    return {
        "source": "restore",
        "type": "restore_check",
        "occurred_at": utcnow_iso(),
        "title": title,
        "url": None,
        "payload": result,
    }


def post_event(api_url: str, payload: dict, timeout: int = 15) -> bool:
    """POST the result to the Copilot API. Returns False (never raises) on failure."""
    try:
        req = urllib.request.Request(
            api_url.rstrip("/") + "/api/events",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # noqa: BLE001 - reporting must not fail the check
        log.warning("could not POST result event to %s: %s", api_url, e)
        return False


def telegram_summary(label: str, result: dict) -> str:
    checks = result.get("checks", [])
    passed_n = sum(1 for c in checks if c.get("passed"))
    total_n = len(checks)
    dur = result.get("duration_s", 0)
    backup = result.get("backup", {})
    size_mb = (backup.get("size_bytes") or 0) / 1_000_000
    age_h = (backup.get("age_s") or 0) / 3600
    if result.get("passed"):
        return (f"✅ Restore check ({label}): PASS — {passed_n}/{total_n} checks in {dur}s. "
                f"Backup {size_mb:.0f} MB, {age_h:.1f}h old.")
    reason = result.get("failed_reason") or "checks failed"
    return (f"❌ Restore check ({label}): FAIL — {reason}. "
            f"Backup {size_mb:.0f} MB, {age_h:.1f}h old.")


def send_telegram(token: str, chat_id: str, text: str, timeout: int = 15) -> bool:
    """Send via the Bot API. Returns False (never raises) on failure."""
    try:
        body = json.dumps({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # noqa: BLE001 - notify failures are warnings
        log.warning("telegram send failed: %s", e)
        return False


def maybe_notify_telegram(label: str, result: dict) -> str:
    """Send the summary if configured; returns 'sent' | 'failed' | 'skipped'."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.info("Telegram not configured — skipping")
        return "skipped"
    ok = send_telegram(token, chat_id, telegram_summary(label, result))
    return "sent" if ok else "failed"


# ---------------------------------------------------------------- orchestration
def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Incident Copilot nightly restore checker")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--backup-file", type=Path, help="pg_dump backup file (.dump custom or .sql plain)")
    src.add_argument("--source-url", help="live Postgres URL to pg_dump first (password stays out of logs)")
    ap.add_argument("--checks", type=Path, default=Path(__file__).parent / "checks.example.yaml",
                    help="checks file (YAML or JSON)")
    ap.add_argument("--api-url", default=os.environ.get("COPILOT_API_URL", "http://localhost:8000"),
                    help="Copilot API base URL (default: $COPILOT_API_URL or http://localhost:8000)")
    ap.add_argument("--label", default="nightly", help="run label used in titles")
    ap.add_argument("--postgres-image", default=POSTGRES_IMAGE, help="pinned postgres image for the temp container")
    ap.add_argument("--ready-timeout", type=int, default=READY_TIMEOUT_S)
    ap.add_argument("--restore-timeout", type=int, default=RESTORE_TIMEOUT_S)
    ap.add_argument("--workdir", type=Path, help="working dir (default: fresh temp dir)")
    ap.add_argument("--keep-workdir", action="store_true", help="debug: don't delete the temp dir")
    ap.add_argument("--no-telegram", action="store_true", help="skip Telegram even if configured")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def run_check(args: argparse.Namespace) -> dict:
    """Execute one restore check. Always cleans up the container. Returns the result dict."""
    started = time.monotonic()
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="copilot-restore-"))
    workdir.mkdir(parents=True, exist_ok=True)
    container: str | None = None
    result: dict = {
        "label": args.label,
        "passed": False,
        "duration_s": 0,
        "restore_ok": False,
        "restore_error": None,
        "failed_reason": None,
        "backup": {},
        "checks": [],
    }
    try:
        if not docker_available():
            raise InfraError("no Docker daemon reachable (is dockerd running?)")

        # --- acquire backup
        if args.backup_file:
            bp = args.backup_file
            if not bp.is_file():
                raise InfraError(f"backup file not found: {bp}")
            st = bp.stat()
            result["backup"] = {
                "kind": "file", "path": str(bp),
                "size_bytes": st.st_size, "age_s": int(time.time() - st.st_mtime),
            }
            backup_path = bp
        else:
            backup_path = workdir / "source.dump"
            log.info("dumping live source %s ...", redact_url(args.source_url))
            dump_source(args.source_url, backup_path)
            result["backup"] = {
                "kind": "dump", "source": redact_url(args.source_url),
                "size_bytes": backup_path.stat().st_size, "age_s": 0,
            }
        result["backup"]["format"] = detect_format(backup_path)

        # --- temp postgres
        password = secrets.token_hex(16)
        cname = f"copilot-restore-{secrets.token_hex(4)}"
        container = cname
        host, port = start_container(args.postgres_image, password, cname)
        log.info("temp postgres at %s:%d", host, port)
        wait_ready(cname, args.ready_timeout)

        # --- restore
        log.info("restoring (%s format) ...", result["backup"]["format"])
        restore_backup(result["backup"]["format"], backup_path, host, port,
                       password, args.restore_timeout)
        result["restore_ok"] = True

        # --- sanity checks
        checks = load_checks(args.checks)
        conn = psycopg.connect(host=host, port=port, user="postgres",
                               password=password, dbname="postgres",
                               connect_timeout=10,
                               options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
        try:
            result["checks"] = run_checks(conn, checks)
        finally:
            conn.close()

        failed = [c for c in result["checks"] if not c["passed"]]
        if failed:
            result["failed_reason"] = f"{len(failed)}/{len(result['checks'])} checks failed: " + \
                ", ".join(c["name"] for c in failed[:3])
        else:
            result["passed"] = True
    except CheckError as e:
        result["failed_reason"] = str(e)
        log.error("check failed: %s", e)
    finally:
        result["duration_s"] = round(time.monotonic() - started, 1)
        if container:
            stop_container(container)
        if not args.keep_workdir and not args.workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    # --- report (never lets reporting break the result)
    post_event(args.api_url, build_event_payload(args.label, result))
    result["telegram"] = "skipped" if args.no_telegram else maybe_notify_telegram(args.label, result)
    return result


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    try:
        result = run_check(args)
    except InfraError as e:
        log.error("infra error: %s", e)
        return EXIT_INFRA
    except KeyboardInterrupt:
        log.error("interrupted")
        return EXIT_INFRA
    if result["passed"]:
        log.info("PASS: %d/%d checks in %ss (telegram: %s)",
                 sum(1 for c in result["checks"] if c["passed"]),
                 len(result["checks"]), result["duration_s"], result["telegram"])
        return EXIT_PASS
    log.error("FAIL: %s (telegram: %s)", result["failed_reason"], result["telegram"])
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())

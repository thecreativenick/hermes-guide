#!/usr/bin/env python3
"""Behavioral coverage for checks.check_cron_health.

The "my job never runs" signatures in $HERMES_HOME/cron/jobs.json: an enabled,
scheduled job whose next_run_at is more than the late grace
(constants.CRON_LATE_GRACE_SECONDS) in the past is broken, and so is a job
stuck in state "running" past the stuck threshold (measured from the file
mtime). Failed last runs and unreadable cron schedules are informational;
non-cron schedules (intervals, one-shot dates) are skipped. A missing file is
healthy; an unreadable or malformed one is unknown, and so is a file or broken
symlink where the `cron` folder should be.

Also pins the read-only contract: a run leaves the fixture home byte-for-byte
and mtime-for-mtime unchanged, and starts no subprocess (no `hermes cron`).

No Hermes binary: checks.py is shim-loaded and _hermes_home is pointed at a
fixture directory.

Run: python3 tools/test_cron_health.py
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
UTC = datetime.UTC
MIN = 60
HOUR = 60 * MIN


def _load_checks():
    td = Path(tempfile.mkdtemp())
    pkg = td / "hermes_guide"
    pkg.mkdir()
    for name in ("__init__.py", "checks.py", "constants.py"):
        shutil.copy(REPO / name, pkg / name)
    sys.path.insert(0, str(td))
    import hermes_guide.checks as checks  # noqa: E402

    return checks


def iso(offset_seconds: float, style: str = "offset") -> str:
    """A UTC timestamp *offset_seconds* from now (negative = past)."""
    dt = datetime.datetime.now(UTC) + datetime.timedelta(seconds=offset_seconds)
    if style == "z":
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if style == "naive":
        return dt.replace(tzinfo=None).isoformat()
    return dt.isoformat()


def job(**fields) -> dict:
    """A healthy enabled cron job; keyword arguments override fields."""
    base = {
        "id": "job1",
        "name": "daily-report",
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "enabled": True,
        "state": "scheduled",
        "next_run_at": iso(HOUR),
        "last_run_at": None,
        "last_status": "ok",
    }
    base.update(fields)
    return base


def main() -> int:
    checks = _load_checks()
    failures: list[str] = []
    root = Path(tempfile.mkdtemp(prefix="cron-health-"))
    counter = iter(range(10_000))

    def new_home() -> Path:
        home = root / f"home{next(counter)}"
        home.mkdir()
        return home

    def jobs_file(home: Path) -> Path:
        return home / "cron" / "jobs.json"

    def home_with(document, *, raw: bytes | None = None, mtime_age: float | None = None) -> Path:
        """A home whose cron/jobs.json holds *document* (JSON) or *raw* bytes."""
        home = new_home()
        path = jobs_file(home)
        path.parent.mkdir()
        path.write_bytes(raw if raw is not None else json.dumps(document).encode("utf-8"))
        if mtime_age is not None:
            stamp = time.time() - mtime_age
            os.utime(path, (stamp, stamp))
        return home

    def run_home(home: Path | None) -> dict:
        with patch.object(checks, "_hermes_home", return_value=None if home is None else str(home)):
            return checks.check_cron_health()

    def run(jobs, **kwargs) -> dict:
        return run_home(home_with({"jobs": jobs}, **kwargs))

    def expect(name, result, status, finding_id=None, contains=(), absent=()):
        if result.get("status") != status:
            failures.append(f"{name}: expected {status}, got {result}")
            return
        if finding_id is not None and result.get("id") != finding_id:
            failures.append(f"{name}: expected id {finding_id}, got {result.get('id')}")
        if status in ("healthy", "informational") and "id" in result:
            failures.append(f"{name}: {status} envelope must not carry an id: {result}")
        text = json.dumps(result.get("detail"))
        for needle in contains:
            if needle not in text:
                failures.append(f"{name}: detail missing {needle!r}: {result.get('detail')}")
        for needle in absent:
            if needle in text:
                failures.append(f"{name}: detail must not contain {needle!r}: {result.get('detail')}")

    # --- file-level states ---------------------------------------------------

    expect("home unresolved", run_home(None), "unknown", "HOME_UNRESOLVED")
    expect("no cron dir", run_home(new_home()), "healthy")
    cron_dir_only = new_home()
    (cron_dir_only / "cron").mkdir()
    expect("cron dir, no jobs.json", run_home(cron_dir_only), "healthy")
    expect("empty jobs list", run([]), "healthy")
    expect("healthy job", run([job()]), "healthy")

    expect("bad json", run_home(home_with(None, raw=b'{"jobs": [')), "unknown", "CRON_JOBS_MALFORMED")
    expect("empty file", run_home(home_with(None, raw=b"")), "unknown", "CRON_JOBS_MALFORMED")
    expect("not utf-8", run_home(home_with(None, raw=b'{"jobs": ["\xff"]}')), "unknown",
           "CRON_JOBS_MALFORMED")
    expect("top-level list", run_home(home_with([job()])), "unknown", "CRON_JOBS_MALFORMED")
    expect("jobs not a list", run_home(home_with({"jobs": {"a": job()}})), "unknown",
           "CRON_JOBS_MALFORMED")
    expect("no jobs key", run_home(home_with({"version": 1})), "unknown", "CRON_JOBS_MALFORMED")
    expect("deeply nested json", run_home(home_with(None, raw=b"[" * 100_000 + b"]" * 100_000)),
           "unknown", "CRON_JOBS_MALFORMED")

    # A regular FILE named `cron` where the folder should be is not "no jobs":
    # POSIX raises NotADirectoryError for cron/jobs.json, Windows raises
    # FileNotFoundError. Not skipped on any OS — the check must handle both.
    cron_file = new_home()
    (cron_file / "cron").write_text('{"jobs": []}', encoding="utf-8")
    expect("cron is a file", run_home(cron_file), "unknown", "CRON_JOBS_UNREADABLE")
    if "not a directory" not in str(run_home(cron_file).get("reason")):
        failures.append(f"cron is a file: reason should say not a directory, got {run_home(cron_file)}")
    empty_cron_file = new_home()
    (empty_cron_file / "cron").write_bytes(b"")
    expect("cron is an empty file", run_home(empty_cron_file), "unknown", "CRON_JOBS_UNREADABLE")

    # Same fixture with the Windows errno forced, so every OS (not only the
    # windows-latest leg) exercises the FileNotFoundError path.
    real_lstat = os.lstat
    forced = os.fspath(jobs_file(cron_file))

    def windows_lstat(path, *args, **kwargs):
        if os.fspath(path) == forced:
            raise FileNotFoundError(2, "The system cannot find the path specified", forced)
        return real_lstat(path, *args, **kwargs)

    with patch.object(os, "lstat", windows_lstat):
        windows_style = run_home(cron_file)
    expect("cron is a file (Windows errno)", windows_style, "unknown", "CRON_JOBS_UNREADABLE")
    if "not a directory" not in str(windows_style.get("reason")):
        failures.append(f"cron is a file (Windows errno): reason should match POSIX, got {windows_style}")

    dir_home = new_home()
    jobs_file(dir_home).mkdir(parents=True)
    expect("jobs.json is a directory", run_home(dir_home), "unknown", "CRON_JOBS_UNREADABLE")

    if os.name != "nt":
        dangling = new_home()
        (dangling / "cron").mkdir()
        os.symlink(dangling / "cron" / "missing.json", jobs_file(dangling))
        expect("dangling symlink", run_home(dangling), "unknown", "CRON_JOBS_UNREADABLE")

        # Symlink creation needs privileges on Windows, so these stay POSIX.
        real_dir = new_home()
        linked_cron = new_home()
        os.symlink(real_dir, linked_cron / "cron")
        expect("cron symlinked to an empty dir", run_home(linked_cron), "healthy")
        broken_cron = new_home()
        os.symlink(broken_cron / "nowhere", broken_cron / "cron")
        expect("cron is a dangling symlink", run_home(broken_cron), "unknown", "CRON_JOBS_UNREADABLE")

        linked = home_with({"jobs": [job(next_run_at=iso(-HOUR))]})
        target = jobs_file(linked).with_name("real.json")
        jobs_file(linked).rename(target)
        os.symlink(target, jobs_file(linked))
        expect("symlink to real file is followed", run_home(linked), "broken", "CRON_JOBS_STALLED")

    if hasattr(os, "mkfifo"):
        fifo_home = new_home()
        (fifo_home / "cron").mkdir()
        os.mkfifo(jobs_file(fifo_home))
        outcome: dict = {}
        worker = threading.Thread(target=lambda: outcome.update(run_home(fifo_home)), daemon=True)
        worker.start()
        worker.join(10)
        if worker.is_alive():
            failures.append("fifo: check blocked opening a FIFO")
            # Release the blocked reader (a writer opening then closing gives
            # it EOF) so the thread ends and the report still prints.
            os.close(os.open(jobs_file(fifo_home), os.O_WRONLY | os.O_NONBLOCK))
            worker.join(10)
        else:
            expect("fifo", outcome, "unknown", "CRON_JOBS_UNREADABLE")

    # chmod 000 is ignored by Windows ACLs and bypassed by root: POSIX non-root only.
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0:
        locked = home_with({"jobs": [job()]})
        os.chmod(jobs_file(locked), 0o000)
        try:
            expect("unreadable file", run_home(locked), "unknown", "CRON_JOBS_UNREADABLE")
        finally:
            os.chmod(jobs_file(locked), 0o644)

    # --- late jobs -----------------------------------------------------------

    expect("late 20 min", run([job(next_run_at=iso(-20 * MIN))]), "broken", "CRON_JOBS_STALLED",
           contains=("'daily-report' (id job1)", "late", "15m"))
    expect("late 14 min (inside grace)", run([job(next_run_at=iso(-14 * MIN))]), "healthy")
    expect("late 16 min (past grace)", run([job(next_run_at=iso(-16 * MIN))]), "broken",
           "CRON_JOBS_STALLED")
    expect("late, Z suffix", run([job(next_run_at=iso(-HOUR, "z"))]), "broken", "CRON_JOBS_STALLED")
    # Naive timestamps are UTC, never local time. Run these under a local zone
    # far from UTC (POSIX tzset) so reading them as local would be caught.
    saved_tz = os.environ.get("TZ")
    if hasattr(time, "tzset"):
        os.environ["TZ"] = "Etc/GMT+10"  # POSIX sign: UTC-10
        time.tzset()
    try:
        expect("late, naive (taken as UTC)", run([job(next_run_at=iso(-HOUR, "naive"))]),
               "broken", "CRON_JOBS_STALLED")
        expect("future, naive (taken as UTC)", run([job(next_run_at=iso(HOUR, "naive"))]),
               "healthy")
    finally:
        if saved_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved_tz
        if hasattr(time, "tzset"):
            time.tzset()
    # The offset must be honored, not dropped. 30 min late written as +05:00
    # shows a wall clock 4h30m in the future; 30 min ahead written as -05:00
    # shows one 4h30m in the past. Dropping the offset flips both results.
    def in_zone(offset_seconds: float, hours: int) -> str:
        moment = datetime.datetime.now(UTC) + datetime.timedelta(seconds=offset_seconds)
        return moment.astimezone(datetime.timezone(datetime.timedelta(hours=hours))).isoformat()

    expect("late, +05:00 offset", run([job(next_run_at=in_zone(-30 * MIN, 5))]), "broken",
           "CRON_JOBS_STALLED")
    expect("future, -05:00 offset", run([job(next_run_at=in_zone(30 * MIN, -5))]), "healthy")
    expect("late, no state field", run([{k: v for k, v in job(next_run_at=iso(-HOUR)).items()
                                         if k != "state"}]), "broken", "CRON_JOBS_STALLED")
    expect("late, no enabled field", run([{k: v for k, v in job(next_run_at=iso(-HOUR)).items()
                                           if k != "enabled"}]), "broken", "CRON_JOBS_STALLED")
    expect("late but disabled", run([job(enabled=False, next_run_at=iso(-HOUR))]), "healthy")
    expect("late but paused", run([job(state="paused", next_run_at=iso(-HOUR))]), "healthy")
    expect("late one of several", run([
        job(id="ok1", name="fine"),
        job(id="late1", name="stale", next_run_at=iso(-3 * HOUR)),
    ]), "broken", "CRON_JOBS_STALLED", contains=("'stale' (id late1)",), absent=("'fine'",))

    with patch.object(checks.constants, "CRON_LATE_GRACE_SECONDS", HOUR):
        expect("grace constant honored", run([job(next_run_at=iso(-20 * MIN))]), "healthy")
        expect("grace constant, past it", run([job(next_run_at=iso(-2 * HOUR))]), "broken",
               "CRON_JOBS_STALLED", contains=("1h 0m late",))

    expect("enabled, no next_run_at", run([job(next_run_at=None)]), "informational",
           contains=("no next_run_at",))
    expect("enabled, garbage next_run_at", run([job(next_run_at="tomorrow-ish")]), "informational",
           contains=("could not be read",))
    expect("enabled, numeric next_run_at", run([job(next_run_at=12345)]), "informational")
    expect("disabled, garbage next_run_at", run([job(enabled=False, next_run_at="??")]), "healthy")

    # --- stuck running -------------------------------------------------------

    expect("running 3h", run([job(state="running")], mtime_age=3 * HOUR), "broken",
           "CRON_JOBS_STALLED", contains=("stuck", "3h"))
    expect("running 1h", run([job(state="running")], mtime_age=HOUR), "healthy")
    expect("running, fresh file", run([job(state="running")]), "healthy")
    # A running job is not also reported late: its next_run_at is not the problem.
    fresh_running_late = run([job(state="running", next_run_at=iso(-HOUR))])
    expect("running + past next_run_at, fresh file", fresh_running_late, "healthy")
    stuck_and_late = run([job(state="running", next_run_at=iso(-5 * HOUR))], mtime_age=3 * HOUR)
    expect("running + past next_run_at, old file", stuck_and_late, "broken", "CRON_JOBS_STALLED",
           contains=("stuck",), absent=("late",))
    expect("running + disabled, old file", run([job(state="running", enabled=False)],
                                                mtime_age=3 * HOUR), "broken", "CRON_JOBS_STALLED")
    with patch.object(checks, "_CRON_STUCK_RUNNING_SECONDS", 30 * MIN):
        expect("stuck constant honored", run([job(state="running")], mtime_age=HOUR), "broken",
               "CRON_JOBS_STALLED")

    # --- informational items -------------------------------------------------

    expect("last_status error", run([job(last_status="error", last_run_at=iso(-HOUR, "z"))]),
           "informational", contains=("status 'error'",))
    expect("last_status ok / null", run([job(last_status="ok"), job(last_status=None)]), "healthy")
    expect("bad cron string", run([job(schedule="61 * * * *")]), "informational",
           contains=("61 * * * *", "outside 0-59"))
    expect("bad cron expr object", run([job(schedule={"kind": "cron", "expr": "0 9 * *"})]),
           "informational", contains=("expected 5 fields, found 4",))
    expect("expr object without kind", run([job(schedule={"expr": "0 25 * * *"})]),
           "informational", contains=("hour value 25",))
    expect("cron kind, no expr", run([job(schedule={"kind": "cron"})]), "informational",
           contains=("no expression",))
    expect("late + error + bad schedule", run([
        job(id="a", name="late", next_run_at=iso(-HOUR)),
        job(id="b", name="fails", last_status="error"),
        job(id="c", name="odd", schedule="0 0 L * *"),
    ]), "broken", "CRON_JOBS_STALLED", contains=("'late' (id a)", "'fails' (id b)", "'odd' (id c)"))

    # Non-cron schedules are skipped, never called unreadable.
    for schedule in (
        "30m", "every 2h", "every 2 hours", "2026-12-01T09:00:00Z", "2026-12-01 09:00",
        {"kind": "interval", "minutes": 30, "display": "every 30m"},
        {"kind": "once", "run_at": "2026-12-01T09:00:00Z"},
        {"kind": "interval", "expr": "not cron at all"},
        {"minutes": 30}, None, 30,
    ):
        expect(f"non-cron schedule {schedule!r}", run([job(schedule=schedule)]), "healthy")

    for schedule in ("0 9 * * *", "*/15 * * * *", "0 9 * * MON-FRI", "0 0 1 jan,jul *",
                     "@daily", "@HOURLY", "0 22 * * 7", "0-30/10 8-18 * * 1-5", "5/15 * * * *",
                     {"kind": "cron", "expr": "30 6 1 * *", "display": "monthly"}):
        expect(f"valid cron {schedule!r}", run([job(schedule=schedule)]), "healthy")

    # --- entries and labels --------------------------------------------------

    expect("non-object entries", run([7, "x", None, job()]), "informational",
           contains=("job #1", "job #2", "job #3"))
    expect("label falls back to id", run([job(name=None, next_run_at=iso(-HOUR))]), "broken",
           contains=("id job1",))
    expect("label falls back to position",
           run([job(), job(name="", id=None, next_run_at=iso(-HOUR))]), "broken", contains=("job #2",))
    long_name = run([job(name="x" * 500, next_run_at=iso(-HOUR))])
    if any(len(line) > 400 for line in long_name.get("detail") or []):
        failures.append("long job name is not truncated in the report")

    # --- validator table -----------------------------------------------------

    invalid = {
        "61 * * * *": "outside 0-59",
        "* 24 * * *": "outside 0-23",
        "* * 0 * *": "outside 1-31",
        "* * * 13 *": "outside 1-12",
        "* * * * 8": "outside 0-7",
        "* * * * FOO": "not a number or name",
        "* * * JAN-FOO *": "not a number or name",
        "5-1 * * * *": "backwards",
        "*/0 * * * *": "not a positive number",
        "*/x * * * *": "not a positive number",
        "0 9,,10 * * *": "empty list item",
        "0 9 * *": "found 4",
        "0 0 9 * * *": "found 6",
        "@reboot": "unknown cron macro",
        "0 0 ? * MON": "not a number or name",
    }
    for expr, reason in invalid.items():
        problem = checks._cron_expr_problem(expr)
        if not problem or reason not in problem:
            failures.append(f"validator: {expr!r} expected problem with {reason!r}, got {problem!r}")
    for expr in ("* * * * *", "0 0 * * sun", "0 0 * * SUN-SAT", "59 23 31 12 7", "0 0 1-31/2 * *",
                 "@yearly", "@annually", "@monthly", "@weekly", "@midnight", "  0 9 * * *  "):
        problem = checks._cron_expr_problem(expr)
        if problem:
            failures.append(f"validator: {expr!r} should be valid, got {problem!r}")

    # --- read-only contract ----------------------------------------------------

    def snapshot(path: Path) -> dict:
        state = {}
        for dirpath, dirnames, filenames in os.walk(path):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                st = os.lstat(full)
                content = Path(full).read_bytes() if os.path.isfile(full) else None
                state[full] = (st.st_mode, st.st_mtime_ns, st.st_size, content)
        return state

    rich = home_with({"jobs": [
        job(id="a", next_run_at=iso(-HOUR)),
        job(id="b", state="running"),
        job(id="c", last_status="error", schedule="bad cron * * *"),
    ]}, mtime_age=3 * HOUR)
    before = snapshot(rich)
    spawned: list[str] = []

    def forbid(name):
        def _raise(*args, **kwargs):
            spawned.append(name)
            raise AssertionError(f"{name} called by a read-only check")
        return _raise

    with patch.object(subprocess, "run", forbid("subprocess.run")), \
         patch.object(subprocess, "Popen", forbid("subprocess.Popen")), \
         patch.object(os, "system", forbid("os.system")), \
         patch.object(checks, "_run_hermes", forbid("_run_hermes")), \
         patch.object(checks, "_hermes_home_from_library", return_value=str(rich)):
        readonly = checks.run_all(scope="cron")["cron"]
        fast = checks.run_all(scope="cron", fast=True)["cron"]
    if spawned:
        failures.append(f"read-only: subprocess started: {spawned}")
    if snapshot(rich) != before:
        failures.append("read-only: the check changed files under $HERMES_HOME")
    for name, env in (("run_all", readonly), ("run_all fast", fast)):
        if env.get("status") != "broken" or env.get("id") != "CRON_JOBS_STALLED":
            failures.append(f"{name}: expected broken CRON_JOBS_STALLED, got {env}")
        elif not (env.get("impact") and env.get("fix")):
            failures.append(f"{name}: impact/fix not attached: {env}")

    # --- registration ------------------------------------------------------------

    if "cron" not in checks.labels():
        failures.append("cron is not a registered scope")
    if checks.constants.SKILL_FOR_SCOPE.get("cron") != "diagnosing-cron":
        failures.append("SKILL_FOR_SCOPE['cron'] must be 'diagnosing-cron'")
    for finding_id in ("CRON_JOBS_UNREADABLE", "CRON_JOBS_MALFORMED", "CRON_JOBS_STALLED"):
        if finding_id not in checks.FINDING_IDS:
            failures.append(f"{finding_id} missing from FINDING_IDS")
    if checks.constants.CRON_LATE_GRACE_SECONDS != 15 * MIN:
        failures.append("CRON_LATE_GRACE_SECONDS default should be 15 minutes")

    shutil.rmtree(root, ignore_errors=True)
    if failures:
        print("FAIL:")
        for failure in failures:
            print(f" - {failure}")
        return 1
    print("OK: cron health behavioral tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

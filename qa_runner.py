"""Run pytest in a fresh process and publish measured JSON + standalone HTML."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import perf_counter
import uuid

ROOT = Path(__file__).resolve().parent
LAYERS = {"test_1_unit_math.py": "Unit", "test_2_api_endpoints.py": "API", "test_3_ui_dom.py": "DOM"}


class RunBusyError(RuntimeError):
    pass


class RunnerError(RuntimeError):
    pass


@contextmanager
def run_lock(reports_dir):
    """An OS lock is released even if a runner crashes; works across API workers."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    with (reports_dir / ".qa.lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunBusyError("A QA run is already in progress") from exc
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def defect_log(tests):
    """Do not invent legacy findings or claim a CSS fix without a frontend."""
    verified = {test["name"] for test in tests if test["status"] == "PASSED"}
    definitions = [
        ("BUG-001", "CSV Excel thiếu UTF-8 BOM", "Backend",
         ["test_csv_has_utf8_bom_and_unicode"],
         "CSV được mã hóa utf-8-sig; xác minh ba byte EF BB BF và round-trip tiếng Việt."),
        ("BUG-002", "Tương phản CSS Dark Mode", "Frontend", [],
         "Chưa có template/CSS thực tế; chưa tái hiện hoặc sửa. Đội Frontend cần xác minh."),
        ("BUG-003", "Khoảng trắng STK và dấu tiếng Việt trong VietQR", "Backend",
         ["test_vietqr_normalizes_and_encodes"],
         "Xóa whitespace STK, chuẩn hóa tên Latin viết hoa và URL-encode query."),
        ("BUG-004", "Chia cho 0 và lệch tiền khi làm tròn", "Backend",
         ["test_zero_bill", "test_no_selection_uses_explicit_fallback", "test_weighted_tax_voucher_exact_vnd"],
         "Dùng Fraction và largest remainder; chính sách món chưa chọn là chia đều."),
    ]
    return [{"id": bug_id, "title": title, "owner": owner,
             "status": ("PENDING_FRONTEND" if owner == "Frontend" else
                        "VERIFIED_PREVENTION" if all(t in verified for t in evidence) else "UNVERIFIED"),
             "origin": "user_requested_regression_scenario",
             "legacy_defect_reproduced": False, "description": description,
             "regression_tests": evidence}
            for bug_id, title, owner, evidence, description in definitions]


class ResultCollector:
    def __init__(self):
        self.tests = {}
        self.errors = []
        self.sla_samples = []

    def pytest_collection_finish(self, session):
        for item in session.items:
            self.tests[item.nodeid] = {
                "id": item.nodeid, "name": item.name,
                "layer": LAYERS.get(Path(str(item.path)).name, "Other"),
                "status": "NOT_RUN", "duration_ms": 0.0,
                "description": (getattr(item.obj, "__doc__", None) or item.name).strip(),
                "errors": [],
            }

    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.append(str(report.longrepr))

    def pytest_internalerror(self, excrepr, excinfo):
        self.errors.append(str(excrepr))

    def pytest_runtest_logreport(self, report):
        test = self.tests.get(report.nodeid)
        if test is None:
            return
        test["duration_ms"] += report.duration * 1000
        if report.failed:
            test["status"] = "FAILED"
            test["errors"].append({"phase": report.when, "message": str(report.longrepr)})
        elif report.skipped and test["status"] != "FAILED":
            test["status"] = "SKIPPED"
        elif report.when == "call" and test["status"] == "NOT_RUN":
            test["status"] = "PASSED"
        if report.when == "teardown":
            for key, value in report.user_properties:
                if key == "sla_samples":
                    self.sla_samples.extend(value)


def sla_summary(samples):
    durations = sorted(sample["duration_ms"] for sample in samples)
    endpoints = sorted({sample["endpoint"] for sample in samples})
    return {"threshold_ms": 500, "comparison": "strictly_less_than",
            "scope": "local FastAPI TestClient; sequential business API and qa-summary requests",
            "exclusions": ["POST /api/qa-run (executes the complete suite)", "external VietQR service"],
            "sample_count": len(samples), "max_ms": max(durations, default=None),
            "p95_ms": durations[math.ceil(len(durations) * 0.95) - 1] if durations else None,
            "mean_ms": sum(durations) / len(durations) if durations else None,
            "passed": bool(durations) and all(d < 500 for d in durations),
            "endpoints": [{"endpoint": endpoint,
                           "sample_count": sum(s["endpoint"] == endpoint for s in samples),
                           "max_ms": max(s["duration_ms"] for s in samples if s["endpoint"] == endpoint)}
                          for endpoint in endpoints], "samples": samples}


def worker(output_dir):
    import pytest

    output_dir = Path(output_dir)
    collector = ResultCollector()
    started_at = datetime.now(timezone.utc).isoformat()
    started = perf_counter()
    code = int(pytest.main([str(ROOT / "tests"), "-q", "-p", "pytest_html.plugin", "-p",
                           "pytest_metadata.plugin", "--html", str(output_dir / "autotest_report.html"),
                           "--self-contained-html"], plugins=[collector]))
    tests = list(collector.tests.values())
    for test in tests:
        test["duration_ms"] = round(test["duration_ms"], 3)
    passed = sum(test["status"] == "PASSED" for test in tests)
    result = {"schema_version": "1.0", "run_id": str(uuid.uuid4()),
              "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
              "status": "PASSED" if code == 0 and tests and passed == len(tests) else "FAILED",
              "exit_code": code, "total_tests": len(tests), "passed": passed,
              "failed": sum(test["status"] == "FAILED" for test in tests),
              "skipped": sum(test["status"] == "SKIPPED" for test in tests),
              "not_run": sum(test["status"] == "NOT_RUN" for test in tests),
              "pass_rate_percent": round(100 * passed / len(tests), 2) if tests else 0,
              "total_duration_ms": round((perf_counter() - started) * 1000, 3),
              "test_duration_ms": round(sum(test["duration_ms"] for test in tests), 3),
              "layers": {layer: {"total": sum(t["layer"] == layer for t in tests),
                                  "passed": sum(t["layer"] == layer and t["status"] == "PASSED" for t in tests),
                                  "tests": [t for t in tests if t["layer"] == layer]}
                         for layer in ("Unit", "API", "DOM")},
              "tests": tests, "defects": defect_log(tests),
              "sla": sla_summary(collector.sla_samples), "errors": collector.errors,
              "html_report": "/api/qa-report",
              "limitations": ["DOM tests check backend document/data contracts using a temporary HTML fixture.",
                              "No production frontend or Dark Mode CSS is available in this new workspace.",
                              "Defect log lists required regression scenarios, not four reproduced legacy bugs."]}
    (output_dir / "test_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return code


def run_suite(reports_dir=None, timeout=120):
    if os.environ.get("QA_TEST_RUN") == "1":
        raise RunnerError("Recursive QA runs are disabled inside the test worker")
    reports_dir = Path(reports_dir or ROOT / "reports").resolve()
    with run_lock(reports_dir):
        with tempfile.TemporaryDirectory(prefix=".qa-", dir=reports_dir) as staging:
            environment = os.environ.copy()
            staging_temp = Path(staging) / "tmp"
            staging_temp.mkdir(parents=True, exist_ok=True)
            environment.update(
                QA_TEST_RUN="1",
                PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                PYTHONUTF8="1",
                TEMP=str(staging_temp),
                TMP=str(staging_temp),
            )
            environment.pop("PYTEST_ADDOPTS", None)
            process = subprocess.run(
                [sys.executable, str(ROOT / "qa_runner.py"), "--worker", "--output", staging],
                cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout)
            staged_json = Path(staging) / "test_results.json"
            staged_html = Path(staging) / "autotest_report.html"
            if not staged_json.is_file() or not staged_html.is_file():
                raise RunnerError(f"QA worker could not produce reports (exit {process.returncode}): "
                                  + (process.stderr or process.stdout)[-3000:])
            result = json.loads(staged_json.read_text(encoding="utf-8"))
            if result["exit_code"] != process.returncode:
                raise RunnerError("Worker exit status does not match its report")
            # JSON is the authoritative snapshot and is replaced only after HTML is ready.
            os.replace(staged_html, reports_dir / "autotest_report.html")
            os.replace(staged_json, reports_dir / "test_results.json")
            return result


def main():
    # Windows consoles/pipes may otherwise fail on Vietnamese workspace paths.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args.output)
    try:
        result = run_suite(args.output)
    except (RunBusyError, RunnerError, subprocess.TimeoutExpired) as exc:
        print(f"QA runner error: {exc}", file=sys.stderr)
        return 2
    print(f"{result['passed']}/{result['total_tests']} PASSED "
          f"({result['pass_rate_percent']}%), {result['total_duration_ms']} ms")
    print(args.output / "test_results.json")
    print(args.output / "autotest_report.html")
    return result["exit_code"] or (0 if result["status"] == "PASSED" else 1)


if __name__ == "__main__":
    raise SystemExit(main())

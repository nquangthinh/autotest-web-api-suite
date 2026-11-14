"""Exercise real QA reruns over loopback HTTP; separate from the 26-test suite."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from time import perf_counter, sleep

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qa_runner import RunBusyError, run_lock, run_suite


def verify_result(result):
    assert result["schema_version"] == "1.0"
    assert result["status"] == "PASSED" and result["exit_code"] == 0
    assert result["total_tests"] == result["passed"] == 26
    assert result["pass_rate_percent"] == 100.0
    assert result["failed"] == result["skipped"] == result["not_run"] == 0
    assert {layer: group["total"] for layer, group in result["layers"].items()} == {"Unit": 10, "API": 10, "DOM": 6}
    assert all(t["status"] == "PASSED" and t["duration_ms"] >= 0 and t["description"] for t in result["tests"])
    assert result["sla"]["sample_count"] == 140 and result["sla"]["passed"]
    assert result["sla"]["max_ms"] < 500
    assert len(result["defects"]) == 4
    assert result["defects"][1]["status"] == "PENDING_FRONTEND"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    template = ROOT / "templates" / "index.html"
    before = hashlib.sha256(template.read_bytes()).hexdigest() if template.exists() else None
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    results = {"verified_at": datetime.now(timezone.utc).isoformat(), "transport": "real loopback HTTP + uvicorn",
               "checks": [], "runs": []}
    with tempfile.TemporaryFile(mode="w+b") as log:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "main_api:app", "--host", "127.0.0.1",
                                    "--port", str(port), "--log-level", "warning"], cwd=ROOT, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            with httpx.Client(base_url=base_url, timeout=130, trust_env=False) as client:
                deadline = perf_counter() + 30
                while True:
                    try:
                        assert client.get("/api/health").status_code == 200
                        break
                    except httpx.ConnectError:
                        if process.poll() is not None or perf_counter() >= deadline:
                            log.seek(0)
                            raise RuntimeError(log.read().decode("utf-8", errors="replace"))
                        sleep(0.05)
                initial = client.get("/api/qa-summary")
                assert initial.status_code == 200
                verify_result(initial.json())
                previous_id = initial.json()["run_id"]
                results["checks"].append("GET /api/qa-summary returns the existing 26/26 report")
                for iteration in range(2):
                    started = perf_counter()
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        def rerun():
                            with httpx.Client(base_url=base_url, timeout=130, trust_env=False) as runner_client:
                                return runner_client.post("/api/qa-run")
                        future = pool.submit(rerun)
                        deadline = perf_counter() + 10
                        while True:
                            try:
                                with run_lock(ROOT / "reports"):
                                    pass
                            except RunBusyError:
                                break
                            assert not future.done(), "QA worker finished before lock was observed"
                            assert perf_counter() < deadline, "QA worker did not acquire its lock"
                            sleep(0.01)
                        collision = client.post("/api/qa-run")
                        assert collision.status_code == 409
                        health_start = perf_counter()
                        assert client.get("/api/health").status_code == 200
                        health_ms = (perf_counter() - health_start) * 1000
                        assert health_ms < 500
                        assert client.get("/api/qa-summary").json()["run_id"] == previous_id
                        response = future.result(timeout=130)
                    assert response.status_code == 200, response.text
                    result = response.json()
                    verify_result(result)
                    assert result["run_id"] != previous_id
                    assert client.get("/api/qa-summary").json() == result
                    assert json.loads((ROOT / "reports" / "test_results.json").read_text(encoding="utf-8")) == result
                    html = client.get("/api/qa-report")
                    assert html.status_code == 200
                    assert b"<!DOCTYPE html>" in html.content or b"<!doctype html>" in html.content
                    assert html.content == (ROOT / "reports" / "autotest_report.html").read_bytes()
                    soup = BeautifulSoup(html.text, "html.parser")
                    html_data = json.loads(soup.select_one("#data-container")["data-jsonblob"])
                    html_tests = [test for tests in html_data["tests"].values() for test in tests]
                    assert len(html_tests) == 26 and all(test["result"] == "Passed" for test in html_tests)
                    results["runs"].append({"run_id": result["run_id"], "passed": result["passed"],
                                            "http_run_duration_ms": round((perf_counter() - started) * 1000, 3),
                                            "health_during_run_ms": round(health_ms, 3),
                                            "concurrent_post_status": collision.status_code})
                    previous_id = result["run_id"]
                results["checks"].extend([
                    "Two real POST /api/qa-run calls each execute a new pytest subprocess and return 26/26",
                    "Concurrent QA runs return HTTP 409; health remains below 500ms during testing",
                    "GET summary preserves the previous snapshot while a run is active",
                    "POST result, GET summary and JSON on disk match exactly after each run",
                    "HTML report download matches reports/autotest_report.html",
                ])
                previous_json = (ROOT / "reports" / "test_results.json").read_bytes()
                previous_html = (ROOT / "reports" / "autotest_report.html").read_bytes()
                try:
                    run_suite(timeout=0.001)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    raise AssertionError("A deliberately timed-out subprocess unexpectedly completed")
                assert (ROOT / "reports" / "test_results.json").read_bytes() == previous_json
                assert (ROOT / "reports" / "autotest_report.html").read_bytes() == previous_html
                with run_lock(ROOT / "reports"):
                    pass
                results["checks"].append("Real worker timeout preserves both reports and releases the run lock")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    after = hashlib.sha256(template.read_bytes()).hexdigest() if template.exists() else None
    assert before == after
    results["template_unchanged"] = True
    results["production_template_exists"] = template.exists()
    results["status"] = "PASSED"
    (ROOT / "reports" / "api_verification.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

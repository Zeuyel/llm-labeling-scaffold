import json
import time

from llm_labeling_scaffold.jobs import create_job, get_job, list_jobs, run_job


def _wait_for_job(job) -> None:
    for _ in range(100):
        if job.status in {"succeeded", "failed"}:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_job_never_persists_or_returns_sensitive_values(tmp_path):
    secret = "argilla-secret-value"
    job = create_job(
        "argilla_push",
        {"nested": {"api_key": secret}, "authorization": f"Bearer {secret}"},
        tmp_path,
    )

    def target(current_job):
        current_job.log(f"api_key={secret}")
        return {"token": secret, "echo": f"returned {secret}"}

    run_job(job, target)
    _wait_for_job(job)

    payload = get_job(job.id, tmp_path)
    serialized = json.dumps(payload, ensure_ascii=False)
    persisted = (tmp_path / f"{job.id}.json").read_text(encoding="utf-8")
    assert secret not in serialized
    assert secret not in persisted
    assert payload["params"]["nested"]["api_key"] == "[REDACTED]"
    assert payload["result"]["token"] == "[REDACTED]"


def test_job_redacts_exception_traceback_and_legacy_persisted_payload(tmp_path):
    secret = "legacy-secret-value"
    job = create_job("argilla_pull", {"password": secret}, tmp_path)

    def target(current_job):
        current_job.log(f"authorization=Bearer {secret}")
        raise RuntimeError(f"upstream rejected {secret}")

    run_job(job, target)
    _wait_for_job(job)

    payload = get_job(job.id, tmp_path)
    assert secret not in json.dumps(payload, ensure_ascii=False)

    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(
        json.dumps(
            {
                "id": "legacy",
                "created_at": "2026-01-01T00:00:00+00:00",
                "params": {"api_key": secret},
                "error": f"request failed for {secret}",
                "logs": [f"token={secret}"],
            }
        ),
        encoding="utf-8",
    )

    assert secret not in json.dumps(get_job("legacy", tmp_path), ensure_ascii=False)
    assert secret not in json.dumps(list_jobs(tmp_path), ensure_ascii=False)


def test_job_redacts_sensitive_url_components_from_params_logs_and_errors(tmp_path):
    api_url = (
        "https://argilla-user:argilla-password@argilla.example/api"
        "?access_token=query-secret#refresh_token=fragment-secret"
    )
    job = create_job("argilla_pull", {"api_url": api_url}, tmp_path)

    def target(current_job):
        current_job.log(f"requesting {api_url}")
        raise RuntimeError(f"upstream rejected {api_url}")

    run_job(job, target)
    _wait_for_job(job)

    payload = get_job(job.id, tmp_path)
    serialized = json.dumps(payload, ensure_ascii=False)
    persisted = (tmp_path / f"{job.id}.json").read_text(encoding="utf-8")
    for secret in ("argilla-user", "argilla-password", "query-secret", "fragment-secret"):
        assert secret not in serialized
        assert secret not in persisted
    assert "argilla.example" in serialized
    assert "[REDACTED]" in serialized

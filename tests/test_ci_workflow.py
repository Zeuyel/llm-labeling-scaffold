from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_postgres_ci_uses_one_app_role_for_migrations_and_python_tests():
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    test_job = workflow["jobs"]["test"]
    job_env = test_job["env"]
    steps = {step["name"]: step for step in test_job["steps"]}

    app_user = job_env["SCAFFOLD_POSTGRES_APP_USER"]
    assert app_user == "scaffold_app"

    owner_url = urlsplit(job_env["LLS_TEST_POSTGRES_URL"])
    app_url = urlsplit(job_env["LLS_TEST_POSTGRES_APP_URL"])
    assert owner_url.username == "scaffold_owner"
    assert app_url.username == app_user
    assert owner_url.path == app_url.path == "/scaffold"

    for step_name in (
        "Initialize PostgreSQL runtime role",
        "Verify PostgreSQL runtime role after migration",
        "Run Python tests",
    ):
        effective_env = {**job_env, **steps[step_name].get("env", {})}
        assert effective_env["SCAFFOLD_POSTGRES_APP_USER"] == app_user

    assert "Validate PostgreSQL test configuration" in steps

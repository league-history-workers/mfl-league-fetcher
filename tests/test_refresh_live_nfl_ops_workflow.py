from pathlib import Path

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "refresh_live_nfl_ops.yml"
DEPLOY_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy_nfl_ops_scheduler.yml"
SCHEDULER_DOCKERFILE = Path(__file__).resolve().parents[1] / "nfl_ops_scheduler" / "Dockerfile"


def test_live_nfl_ops_workflow_is_dispatch_only_and_never_uses_github_cron():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow[True]

    assert "workflow_dispatch" in triggers
    assert "repository_dispatch" in triggers
    assert "schedule" not in triggers
    assert "push" not in triggers
    assert "workflow_run" not in triggers
    assert "github.event.client_payload.game_date" in workflow["run-name"]


def test_scheduler_deployment_has_a_pinned_action_and_a_matching_build_context():
    deploy_text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    dockerfile = SCHEDULER_DOCKERFILE.read_text(encoding="utf-8")

    assert "superfly/flyctl-actions/setup-flyctl@ed8efb33836e8b2096c7fd3ba1c8afe303ebbff1" in deploy_text
    assert "setup-flyctl@master" not in deploy_text
    assert "cd nfl_ops_scheduler" in deploy_text
    assert "COPY requirements.txt ./requirements.txt" in dockerfile
    assert "COPY scheduler.py ./scheduler.py" in dockerfile
    assert "COPY nfl_ops_scheduler/" not in dockerfile


def test_live_nfl_ops_workflow_gates_release_and_fly_on_a_ready_scope():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["refresh"]["steps"]
    steps_by_name = {step["name"]: step for step in steps}
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "discover_live_nfl_ops_refresh.py" in text
    assert "github.event.client_payload.game_date" in text
    assert "___leagues" not in text
    assert steps_by_name["Reconstruct and verify the durable baseline"]["if"] == (
        "${{ steps.scope.outputs.should_refresh == 'true' }}"
    )
    assert steps_by_name["Build and verify the local eight-table candidate"]["if"] == (
        "${{ steps.scope.outputs.should_refresh == 'true' }}"
    )
    assert "steps.scope.outputs.should_refresh == 'true'" in steps_by_name[
        "Publish the verified candidate as the next durable baseline"
    ]["if"]
    assert "steps.scope.outputs.should_refresh == 'true'" in steps_by_name[
        "Atomically promote the verified ops artifact"
    ]["if"]
    assert "github.event_name == 'repository_dispatch'" in steps_by_name[
        "Atomically promote the verified ops artifact"
    ]["if"]


def test_durable_baseline_checksum_uses_the_reconstructed_artifact_path() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    # Release manifests retain the original output/ops_nfl.duckdb filename.
    # The verifier must compare that digest with the reconstructed baseline,
    # rather than asking sha256sum to find the old filename locally.
    assert 'expected_hash=$(awk \'{print $1}\' baseline/ops_nfl.duckdb.sha256)' in text
    assert 'actual_hash=$(sha256sum "$BASELINE_PATH" | awk \'{print $1}\')' in text
    assert "durable baseline SHA-256 mismatch" in text
    assert "(cd baseline && sha256sum --check ops_nfl.duckdb.sha256)" not in text

from pathlib import Path

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "refresh_live_nfl_ops.yml"


def test_live_nfl_ops_workflow_uses_an_early_github_schedule_and_no_external_dispatcher():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow[True]

    assert "workflow_dispatch" in triggers
    assert triggers["schedule"] == [
        {"cron": "30 6 * 9,10 *"},
        {"cron": "30 7 * 11,12 *"},
        {"cron": "30 7 * 1,2 *"},
    ]
    assert "repository_dispatch" not in triggers
    assert "push" not in triggers
    assert "workflow_run" not in triggers
    assert "scheduled" in workflow["run-name"]


def test_live_nfl_ops_workflow_gates_release_and_fly_on_a_ready_scope():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["refresh"]["steps"]
    steps_by_name = {step["name"]: step for step in steps}
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "discover_live_nfl_ops_refresh.py" in text
    assert "Hold scheduled refresh until 03:00 Eastern" in text
    assert "DUE_WINDOW_MINUTES = 5" in text
    assert "game_date=$(TZ=America/New_York date --date=yesterday +%F)" in text
    assert "___leagues" not in text
    assert "repository_dispatch" not in text
    assert steps_by_name["Resolve final live refresh scope"]["if"] == (
        "${{ steps.boundary.outputs.should_run == 'true' }}"
    )
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
    assert "github.event_name == 'schedule'" in steps_by_name[
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

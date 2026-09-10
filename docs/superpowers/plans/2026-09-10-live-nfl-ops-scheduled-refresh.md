# Scheduled Live NFL Ops Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically refresh the 2026 NFL ops SuperTable at 3:00 AM America/New_York after a finalized regular-season or postseason game, without touching league data.

**Architecture:** The Yahoo source repository gains a small, read-only schedule discovery command plus exact-date `REG`/`POST` scope support in the existing local artifact builder. The workers repository adds three UTC cron entries and lets its single existing workflow resolve a no-op or one final-game scope before downloading an artifact or calling Fly.

**Tech Stack:** Python 3.12, pandas, NFLverse schedule Parquet, pytest, GitHub Actions YAML, DuckDB/Fly `___ops_nfl` replacement.

**Spec:** `docs/superpowers/specs/2026-09-10-live-nfl-ops-scheduled-refresh-design.md`

## Global Constraints

- Only `___ops_nfl` and explicit `___ops` views may change; never read or write `___leagues`.
- Scheduled discovery admits only season `2026`, game types `REG` or `POST`, a prior America/New_York game date, and final scores for both teams.
- Preserve manual `workflow_dispatch`, `live-nfl-ops-refresh` concurrency, all existing eight-table gates, and exact six-part weekly identity.
- Scheduled no-game executions must succeed before release download, artifact construction, or Fly interaction.
- The worker is the only scheduled workflow; no push, `workflow_run`, fleet, or league trigger is added.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `fantasy_football_data_scripts/multi_league/data_fetchers/live_nfl_ops_refresh.py` | Validates exact completed `REG`/`POST` game scopes and discovers the single safe scheduled work item. |
| `scripts/discover_live_nfl_ops_refresh.py` | Read-only JSON CLI that turns a prior Eastern game date into a no-op or a concrete refresh scope. |
| `scripts/refresh_live_nfl_ops.py` | Accepts optional `season_type` and `game_date` constraints while retaining manual behavior. |
| `fantasy_football_data_scripts/tests/unit/data_fetchers/test_live_nfl_ops_refresh.py` | Unit tests for exact `POST` and date-scoped final game selection. |
| `fantasy_football_data_scripts/tests/unit/scripts/test_discover_live_nfl_ops_refresh.py` | CLI-discovery tests for Sept. 10, no-game, duplicate, and postseason cases. |
| `.github/workflows/refresh_live_nfl_ops.yml` | One scheduled, self-gating ops-only worker with retained manual dispatch. |
| `tests/test_refresh_live_nfl_ops_workflow.py` | YAML contract tests for cron timing, no-op gates, and absence of downstream triggers. |

### Task 1: Add exact-date, postseason-aware source scope selection

**Files:**
- Modify: `fantasy_football_data_scripts/multi_league/data_fetchers/live_nfl_ops_refresh.py:100-134`
- Modify: `fantasy_football_data_scripts/tests/unit/data_fetchers/test_live_nfl_ops_refresh.py:19-103`

**Interfaces:**
- Consumes: NFLverse schedule columns `season`, `week`, `game_type`, `gameday`, `game_id`, `away_score`, and `home_score`.
- Produces: `finalized_game_scope(schedule, *, year: int, week: int, season_types: tuple[str, ...] = ("REG",), game_date: str | None = None) -> pd.DataFrame` and `discover_scheduled_refresh_scope(schedule, *, season: int, game_date: str) -> dict[str, object] | None`.
- Contract: a non-empty discovery result has exactly one `year`, `week`, and `season_type`, plus the requested game date and sorted game IDs; multiple work-item keys raise `RefreshGateError`.

- [ ] **Step 1: Write the failing regular/postseason and date-scope tests**

```python
def test_finalized_game_scope_admits_a_scored_postseason_game_on_the_requested_date():
    schedule = pd.DataFrame([
        {"game_id": "2026_19_NWE_SEA", "season": 2026, "week": 19,
         "game_type": "POST", "gameday": "2027-01-17", "away_team": "NE",
         "away_score": 10, "home_team": "SEA", "home_score": 13},
        {"game_id": "2026_19_KAN_BUF", "season": 2026, "week": 19,
         "game_type": "POST", "gameday": "2027-01-18", "away_team": "KC",
         "away_score": 21, "home_team": "BUF", "home_score": 24},
    ])

    games = finalized_game_scope(
        schedule, year=2026, week=19, season_types=("POST",), game_date="2027-01-17"
    )

    assert games["game_id"].tolist() == ["2026_19_NWE_SEA"]


def test_discover_scheduled_refresh_scope_returns_none_without_a_final_game():
    schedule = pd.DataFrame([
        {"game_id": "2026_01_NE_SEA", "season": 2026, "week": 1,
         "game_type": "REG", "gameday": "2026-09-10", "away_team": "NE",
         "away_score": None, "home_team": "SEA", "home_score": None},
    ])

    assert discover_scheduled_refresh_scope(schedule, season=2026, game_date="2026-09-10") is None
```

- [ ] **Step 2: Run the focused tests and verify they fail because the interfaces do not exist**

Run: `python -m pytest fantasy_football_data_scripts/tests/unit/data_fetchers/test_live_nfl_ops_refresh.py -q`

Expected: FAIL with an unexpected `season_types` argument and an import failure for `discover_scheduled_refresh_scope`.

- [ ] **Step 3: Implement the smallest complete source selector**

Add `VALID_LIVE_GAME_TYPES = frozenset({"REG", "POST"})`. Extend
`finalized_game_scope` with `season_types=("REG",)` and `game_date=None`, reject
unsupported types, and retain the current season/week/final-score/unique-game checks
while filtering `game_type` and, when provided, the exact `gameday`. Add
`discover_scheduled_refresh_scope`, which filters final `REG`/`POST` games for the
requested date, returns `None` when none exist, and raises `RefreshGateError` when
more than one `(season, week, game_type)` work item remains.

Keep the existing default `REG` behavior and output columns unchanged for manual callers.

- [ ] **Step 4: Run the focused source tests and verify they pass**

Run: `python -m pytest fantasy_football_data_scripts/tests/unit/data_fetchers/test_live_nfl_ops_refresh.py -q`

Expected: PASS, including existing Week 1 regular-season coverage and the new exact-date/postseason cases.

- [ ] **Step 5: Commit the source selector change**

```powershell
git add fantasy_football_data_scripts/multi_league/data_fetchers/live_nfl_ops_refresh.py fantasy_football_data_scripts/tests/unit/data_fetchers/test_live_nfl_ops_refresh.py
git commit -m "Add finalized NFL scheduled refresh scope"
```

### Task 2: Add a read-only schedule-discovery CLI and constrained refresh input

**Files:**
- Create: `scripts/discover_live_nfl_ops_refresh.py`
- Modify: `scripts/refresh_live_nfl_ops.py:57-131`
- Create: `fantasy_football_data_scripts/tests/unit/scripts/test_discover_live_nfl_ops_refresh.py`

**Interfaces:**
- Consumes: `--season 2026 --game-date YYYY-MM-DD --output PATH` and the NFLverse schedule Parquet.
- Produces: JSON `{"status": "no-op", "game_date": "2026-09-12", "scope": null}` or `{"status": "ready", "game_date": "2026-09-10", "scope": {"year": 2026, "week": 1, "season_type": "REG", "game_ids": ["2026_01_NE_SEA"]}}`.
- Extends: `fetch_live_refresh_inputs(*, year, week, season_type="REG", game_date=None)` and CLI flags `--season-type {REG,POST}` plus optional `--game-date`.

- [ ] **Step 1: Write failing CLI tests against real discovery functions**

```python
def test_discovery_cli_emits_week_one_scope_for_september_tenth(monkeypatch, tmp_path):
    from scripts import discover_live_nfl_ops_refresh as discovery
    monkeypatch.setattr(discovery.pd, "read_parquet", lambda _url: opener_schedule())
    output = tmp_path / "scope.json"

    assert discovery.main(["--season", "2026", "--game-date", "2026-09-10", "--output", str(output)]) == 0

    assert json.loads(output.read_text()) == {
        "status": "ready", "game_date": "2026-09-10",
        "scope": {"year": 2026, "week": 1, "season_type": "REG", "game_ids": ["2026_01_NE_SEA"]},
    }


def test_discovery_cli_emits_no_op_for_a_date_without_a_final_game(monkeypatch, tmp_path):
    from scripts import discover_live_nfl_ops_refresh as discovery
    monkeypatch.setattr(discovery.pd, "read_parquet", lambda _url: pd.DataFrame([
        {"game_id": "2026_01_NE_SEA", "season": 2026, "week": 1,
         "game_type": "REG", "gameday": "2026-09-10", "away_team": "NE",
         "away_score": None, "home_team": "SEA", "home_score": None},
    ]))
    output = tmp_path / "scope.json"
    assert discovery.main(["--season", "2026", "--game-date", "2026-09-10", "--output", str(output)]) == 0
    assert json.loads(output.read_text())["status"] == "no-op"
    assert json.loads(output.read_text())["status"] == "no-op"
```

- [ ] **Step 2: Run the new CLI tests and verify they fail because the module is absent**

Run: `python -m pytest fantasy_football_data_scripts/tests/unit/scripts/test_discover_live_nfl_ops_refresh.py -q`

Expected: FAIL with `ImportError` for `scripts.discover_live_nfl_ops_refresh`.

- [ ] **Step 3: Implement discovery without any Fly import or credential access**

```python
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    scope = discover_scheduled_refresh_scope(
        pd.read_parquet(SCHEDULE_URL), season=args.season, game_date=args.game_date
    )
    payload = {"status": "no-op", "game_date": args.game_date, "scope": None}
    if scope is not None:
        payload = {"status": "ready", "game_date": args.game_date, "scope": scope}
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0
```

Pass the constrained season type and date through `refresh_live_nfl_ops.py` so a scheduled run cannot replace final games from another date in the same week.  Do not add `--apply` behavior to the discovery command.

- [ ] **Step 4: Run CLI and source suites and verify they pass**

Run: `python -m pytest fantasy_football_data_scripts/tests/unit/data_fetchers/test_live_nfl_ops_refresh.py fantasy_football_data_scripts/tests/unit/scripts/test_discover_live_nfl_ops_refresh.py -q`

Expected: PASS with no network request, Fly target construction, or artifact mutation in the discovery tests.

- [ ] **Step 5: Commit the source CLI change**

```powershell
git add scripts/discover_live_nfl_ops_refresh.py scripts/refresh_live_nfl_ops.py fantasy_football_data_scripts/tests/unit/scripts/test_discover_live_nfl_ops_refresh.py
git commit -m "Add scheduled live NFL refresh discovery"
```

### Task 3: Add self-gating Eastern-time schedule to the workers workflow

**Files:**
- Modify: `.github/workflows/refresh_live_nfl_ops.yml:7-158`
- Create: `tests/test_refresh_live_nfl_ops_workflow.py`

**Interfaces:**
- Consumes: GitHub `schedule` event or current `workflow_dispatch` inputs, plus `refresh_scope.json` produced by Task 2.
- Produces: `should_refresh`, `refresh_year`, `refresh_week`, `refresh_season_type`, and `refresh_game_date` step outputs.
- Contract: schedule uses `0 7 * 9,10 *`, `0 8 * 11,12 *`, and `0 8 * 1,2 *`; a no-op bypasses release reconstruction and Fly promotion; schedule promotion is enabled automatically only when `should_refresh=true`.

- [ ] **Step 1: Write failing workflow-contract tests**

```python
def test_live_nfl_ops_workflow_has_only_eastern_three_am_2026_schedule_crons():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow[True]["schedule"] == [
        {"cron": "0 7 * 9,10 *"},
        {"cron": "0 8 * 11,12 *"},
        {"cron": "0 8 * 1,2 *"},
    ]
    assert "workflow_dispatch" in workflow[True]


def test_live_nfl_ops_workflow_gates_release_and_fly_on_a_ready_scope():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "discover_live_nfl_ops_refresh.py" in text
    assert "should_refresh" in text
    assert "github.event_name == 'schedule'" in text
    assert "___leagues" not in text
```

- [ ] **Step 2: Run workflow-contract tests and verify they fail against the manual-only workflow**

Run: `python -m pytest tests/test_refresh_live_nfl_ops_workflow.py -q`

Expected: FAIL because `schedule` and `should_refresh` are absent.

- [ ] **Step 3: Implement one scheduled self-gating job**

```yaml
on:
  schedule:
    - cron: "0 7 * 9,10 *"
    - cron: "0 8 * 11,12 *"
    - cron: "0 8 * 1,2 *"
  workflow_dispatch:
    inputs:
      year: {default: "2026", required: true, type: string}
      week: {default: "1", required: true, type: string}
      apply: {default: false, required: true, type: boolean}
```

After source checkout, resolve either manual inputs or a prior Eastern date with:

```bash
game_date=$(TZ=America/New_York date --date=yesterday +%F)
python scripts/discover_live_nfl_ops_refresh.py \
  --season 2026 --game-date "$game_date" --output refresh_scope.json
```

Write no-op or ready values to `$GITHUB_OUTPUT`.  Guard artifact reconstruction, local build, durable-release publication, and Fly promotion with `steps.scope.outputs.should_refresh == 'true'`.  For a ready scheduled scope, treat promotion as apply=true; manual dispatch retains its `apply` input.  Upload `refresh_scope.json` as a small receipt for both outcomes.

- [ ] **Step 4: Run workflow and source contract tests**

Run: `python -m pytest tests/test_refresh_live_nfl_ops_workflow.py -q`

Expected: PASS.  Also run:

Run: `python -m pytest fantasy_football_data_scripts/tests/unit/data_fetchers/test_live_nfl_ops_refresh.py fantasy_football_data_scripts/tests/unit/scripts/test_discover_live_nfl_ops_refresh.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the worker workflow and its contract tests**

```powershell
git add .github/workflows/refresh_live_nfl_ops.yml tests/test_refresh_live_nfl_ops_workflow.py
git commit -m "Schedule 2026 live NFL ops refresh"
```

### Task 4: Validate the actual 9/11 scheduled scope and deploy safely

**Files:**
- Modify: no production files beyond Tasks 1–3
- Verify: source and worker commits on their respective `main` branches

**Interfaces:**
- Consumes: official NFLverse schedule data and the live workers workflow.
- Produces: a successful no-Fly local discovery receipt for `2026-09-10`, followed by a scheduled/manual action run only after the deadline policy is confirmed.

- [ ] **Step 1: Run the 9/10 discovery command against the live schedule**

Run: `python scripts/discover_live_nfl_ops_refresh.py --season 2026 --game-date 2026-09-10 --output D:\temp\live_nfl_scope_2026-09-10.json`

Expected: `status=ready`, `year=2026`, `week=1`, `season_type=REG`; this command is read-only and has no Fly credentials.

- [ ] **Step 2: Validate both repository worktrees before publish**

Run: source and worker focused pytest commands from Tasks 2 and 3, `python -m py_compile scripts/discover_live_nfl_ops_refresh.py scripts/refresh_live_nfl_ops.py`, `git diff --check`, and YAML parsing with `yaml.safe_load`.

Expected: all tests pass, source syntax compiles, and cron set matches the three contract values.

- [ ] **Step 3: Publish source then workers workflow with push-trigger suppression**

```powershell
git push origin main
```

For configuration-only commits use `[skip ci]` so legacy push workflows do not run.  Verify the target workers main ref and confirm the only data-worker action started by the post-publish manual test is `Refresh Live NFL Ops`.

- [ ] **Step 4: Confirm the scheduled event will not dispatch a second data worker**

Run: inspect the published `Refresh Live NFL Ops` workflow definition and its Actions
event filters.

Expected: it has only `schedule` and `workflow_dispatch` triggers, and the first
scheduled run will promote automatically only when its discovery receipt says
`status=ready`. Do not dispatch an additional production promotion because the
existing Week 1 refresh receipt already represents the same completed game.

- [ ] **Step 5: Verify live state after the first real scheduled update**

Run read-only Fly queries against `___ops` for the relevant date and `fly_ready` for `databases.___ops.last_replaced` and unchanged `databases.___leagues.last_replaced`.

Expected: final game rows, all eight ops table counts, and the release manifest agree; Fly is serving and `___leagues` has not changed.

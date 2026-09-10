# Scheduled 2026 Live NFL Ops Refresh

## Purpose

Run the existing Track 1 NFL ops refresh after every finalized 2026 NFL game,
including postseason games, at 3:00 AM in `America/New_York`.  A scheduled
execution must touch neither league data nor Fly unless it finds a new,
finalized game to refresh.

## Scope and non-goals

The scheduler owns only `___ops_nfl` and its explicit `___ops` views.  It
does not read, import, write, rebuild, or otherwise contact `___leagues`.
Manual `workflow_dispatch` remains available for recovery and controlled
replays.  This does not introduce a league, fleet, or downstream workflow.

## Trigger contract

GitHub Actions cron is UTC-only.  The 2026 season runs during Eastern daylight
time in September and October, then Eastern standard time from November through
the February 2027 postseason.  The worker therefore has these scheduled
triggers:

| Eastern period | UTC cron | Local run time |
| --- | --- | --- |
| September–October 2026 | `0 7 * 9,10 *` | 3:00 AM EDT |
| November–December 2026 | `0 8 * 11,12 *` | 3:00 AM EST |
| January–February 2027 | `0 8 * 1,2 *` | 3:00 AM EST |

Each scheduled run uses the prior `America/New_York` calendar date as its game
date.  It exits successfully before downloading the durable artifact when that
date has no finalized NFL game.  This preserves flex and postseason scheduling
without encoding a brittle list of game dates.

## Discovery and refresh contract

1. A source-side discovery command reads NFLverse schedule data and selects
   only rows for the requested game date, season `2026`, game type `REG` or
   `POST`, and non-null home and away scores.
2. The command emits distinct `(season, week, season_type)` work items.  It
   returns a no-op result when the date has no matching completed game.
3. The scheduled workflow invokes the existing guarded local artifact refresh
   for each work item.  The game scope remains exact, so a Thursday game does
   not replace Sunday or Monday rows from the same week.
4. A work item may publish only after the local candidate completes the
   existing eight-table shape, schema, identity, rollup, and receipt gates.
   It then replaces only the standalone `___ops_nfl` artifact and recuts the
   explicit ops views.
5. Scheduled promotion is automatic only for discovered finalized games.  A
   missing, unscored, malformed, or ambiguous schedule row fails closed and
   leaves the current Fly ops artifact in place.

## Data-model changes

The current final-game selector is regular-season-only.  It will receive a
validated season-type input and map NFLverse `REG` / `POST` data to the
canonical `season_type` used by the weekly table.  Existing manual Week 1
behavior remains unchanged.  Postseason work must use the same canonical
six-part identity:

`(NFL_player_id, game_date, week, season_type, nfl_team, opponent_nfl_team)`.

## Workflow behavior

The worker keeps its single `live-nfl-ops-refresh` concurrency group.  A
scheduled no-op uploads a small discovery receipt and performs no release or
Fly operation.  A discovered game keeps the existing full-artifact release
publication and atomic promotion path.  The workflow is the only scheduled
job; no `workflow_run`, push, fleet-import, or league trigger is added.

## Verification

Unit and workflow-contract tests cover:

- Sept. 10, 2026 discovery producing the Week 1 regular-season work item for
  the Sept. 11 3:00 AM Eastern run;
- no-game dates returning a success no-op before artifact reconstruction;
- completed postseason (`POST`) selection;
- rejection of unscored, future, wrong-season, and duplicate schedule rows;
- exact Eastern-to-UTC cron entries and manual dispatch retention; and
- the existing local eight-table checks plus read-only Fly verification that
  `___ops` changed while `___leagues` did not.


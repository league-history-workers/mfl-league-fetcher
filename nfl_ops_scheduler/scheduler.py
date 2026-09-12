#!/usr/bin/env python3
"""Dispatch the live NFL Ops worker once, at 03:00 America/New_York.

This process is intentionally not a data worker.  It reads NFLverse's public
schedule, proves that yesterday contains finalized 2026 NFL game data, then
uses GitHub's repository-dispatch API to start the isolated Workers workflow.
No Fly database endpoint or league credential is available to this process.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


EASTERN = ZoneInfo("America/New_York")
SCHEDULE_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.parquet"
REPOSITORY = "league-history-workers/mfl-league-fetcher"
EVENT_TYPE = "refresh-live-nfl-ops"
DUE_WINDOW_MINUTES = 5
REQUIRED_SCHEDULE_COLUMNS = {
    "season",
    "week",
    "game_type",
    "gameday",
    "game_id",
    "away_score",
    "home_score",
}


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(EASTERN)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("--now must include a UTC offset")
    return parsed.astimezone(EASTERN)


def _read_state(path: Path) -> dict[str, set[str]]:
    if not path.exists():
        return {"pending_dates": set(), "dispatched_dates": set()}
    payload = json.loads(path.read_text(encoding="utf-8"))
    state: dict[str, set[str]] = {}
    for key in ("pending_dates", "dispatched_dates"):
        dates = payload.get(key, [])
        if not isinstance(dates, list) or not all(isinstance(date, str) for date in dates):
            raise ValueError(f"scheduler state has an invalid {key} value")
        state[key] = set(dates)
    if state["pending_dates"] & state["dispatched_dates"]:
        raise ValueError("scheduler state contains dates that are both pending and dispatched")
    return state


def _write_state(path: Path, state: dict[str, set[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(
            {
                "pending_dates": sorted(state["pending_dates"]),
                "dispatched_dates": sorted(state["dispatched_dates"]),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _finalized_scope(schedule: pd.DataFrame, *, season: int, game_date: str) -> dict[str, Any] | None:
    missing = sorted(REQUIRED_SCHEDULE_COLUMNS - set(schedule.columns))
    if missing:
        raise ValueError(f"schedule is missing required columns: {', '.join(missing)}")

    scoped = schedule.loc[
        (pd.to_numeric(schedule["season"], errors="coerce") == season)
        & schedule["game_type"].astype(str).str.strip().str.upper().isin({"REG", "POST"})
    ].copy()
    scoped["away_score"] = pd.to_numeric(scoped["away_score"], errors="coerce")
    scoped["home_score"] = pd.to_numeric(scoped["home_score"], errors="coerce")
    scoped["game_date"] = pd.to_datetime(scoped["gameday"], errors="coerce").dt.strftime("%Y-%m-%d")
    scoped = scoped.loc[
        (scoped["game_date"] == game_date)
        & scoped["away_score"].notna()
        & scoped["home_score"].notna()
    ].copy()
    if scoped.empty:
        return None

    work_items = scoped[["week", "game_type"]].drop_duplicates()
    if len(work_items) != 1:
        raise ValueError("finalized game date has multiple refresh work items")
    if scoped["game_id"].isna().any() or (scoped["game_id"].astype(str).str.strip() == "").any():
        raise ValueError("finalized game date has a blank game_id")
    return {"season": season, "game_date": game_date}


def _dispatch(*, dispatch_url: str, token: str, payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        dispatch_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 204:
                raise RuntimeError(f"GitHub dispatch returned HTTP {response.status}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub dispatch failed with HTTP {error.code}: {detail}") from error


def run_once(
    *,
    now: datetime,
    season: int,
    schedule_path: Path | None,
    state_path: Path,
    dispatch_url: str,
    dispatch_token: str,
) -> dict[str, Any]:
    """Dispatch only in the 03:00--03:04 Eastern recovery window.

    A five-minute bound permits a Fly restart immediately after 03:00 without
    ever turning a delayed execution into the unwanted later-morning worker.
    A date is durably marked pending *before* GitHub receives it.  If the
    process is interrupted before it can commit the receipt, retries stop and
    retain that marker instead of risking a duplicate replacement workflow.
    """
    now_eastern = now.astimezone(EASTERN)
    if now_eastern.hour != 3 or now_eastern.minute >= DUE_WINDOW_MINUTES:
        return {"status": "not-due", "at": now_eastern.isoformat()}

    game_date = (now_eastern.date() - timedelta(days=1)).isoformat()
    state = _read_state(state_path)
    if game_date in state["dispatched_dates"]:
        return {"status": "already-dispatched", "game_date": game_date}
    if game_date in state["pending_dates"]:
        return {"status": "delivery-pending", "game_date": game_date}

    schedule = pd.read_parquet(schedule_path or SCHEDULE_URL)
    scope = _finalized_scope(schedule, season=season, game_date=game_date)
    if scope is None:
        return {"status": "no-op", "game_date": game_date}

    payload = {"event_type": EVENT_TYPE, "client_payload": scope}
    _write_state(
        state_path,
        {
            "pending_dates": state["pending_dates"] | {game_date},
            "dispatched_dates": state["dispatched_dates"],
        },
    )
    _dispatch(dispatch_url=dispatch_url, token=dispatch_token, payload=payload)
    _write_state(
        state_path,
        {
            "pending_dates": state["pending_dates"],
            "dispatched_dates": state["dispatched_dates"] | {game_date},
        },
    )
    return {"status": "dispatched", **scope}


def _next_three_am(now: datetime) -> datetime:
    now_eastern = now.astimezone(EASTERN)
    candidate = now_eastern.replace(hour=3, minute=0, second=0, microsecond=0)
    return candidate if now_eastern < candidate else candidate + timedelta(days=1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one eligibility check, then exit")
    parser.add_argument("--now", help="test-only ISO timestamp with an explicit UTC offset")
    parser.add_argument("--season", type=int, default=int(os.getenv("NFL_SEASON", "2026")))
    parser.add_argument("--schedule-path", type=Path, help="local NFLverse schedule Parquet (test use)")
    parser.add_argument("--state-path", type=Path, default=Path(os.getenv("STATE_PATH", "/data/state.json")))
    parser.add_argument(
        "--dispatch-url",
        default=os.getenv("WORKERS_DISPATCH_URL", f"https://api.github.com/repos/{REPOSITORY}/dispatches"),
    )
    parser.add_argument("--dispatch-token", default=os.getenv("WORKERS_DISPATCH_TOKEN", ""))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.dispatch_token:
        raise SystemExit("WORKERS_DISPATCH_TOKEN is required")
    if args.once:
        result = run_once(
            now=_parse_now(args.now),
            season=args.season,
            schedule_path=args.schedule_path,
            state_path=args.state_path,
            dispatch_url=args.dispatch_url,
            dispatch_token=args.dispatch_token,
        )
        print(json.dumps(result, sort_keys=True))
        return 0

    while True:
        now = datetime.now(EASTERN)
        result = run_once(
            now=now,
            season=args.season,
            schedule_path=args.schedule_path,
            state_path=args.state_path,
            dispatch_url=args.dispatch_url,
            dispatch_token=args.dispatch_token,
        )
        print(json.dumps(result, sort_keys=True), flush=True)
        delay = max(1.0, (_next_three_am(datetime.now(EASTERN)) - datetime.now(EASTERN)).total_seconds())
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())

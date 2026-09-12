import json
import importlib.util
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / "nfl_ops_scheduler" / "scheduler.py"


def _scheduler_module():
    spec = importlib.util.spec_from_file_location("nfl_ops_scheduler_under_test", SCHEDULER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _CaptureHandler(BaseHTTPRequestHandler):
    payloads: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers["Content-Length"])
        self.__class__.payloads.append(json.loads(self.rfile.read(length)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args):
        return


def _server():
    _CaptureHandler.payloads = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/dispatch"


def _schedule(path: Path, *, gameday: str, final: bool) -> None:
    score = 21 if final else None
    pd.DataFrame(
        [
            {
                "season": 2026,
                "week": 1,
                "game_type": "REG",
                "gameday": gameday,
                "game_id": "2026_01_SEA_NE",
                "away_score": score,
                "home_score": 17 if final else None,
            }
        ]
    ).to_parquet(path, index=False)


def _run(*, schedule: Path, state: Path, now: str, dispatch_url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCHEDULER),
            "--once",
            "--schedule-path",
            str(schedule),
            "--state-path",
            str(state),
            "--now",
            now,
            "--dispatch-url",
            dispatch_url,
            "--dispatch-token",
            "test-token",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_finalized_prior_day_dispatches_once_at_three_am_eastern(tmp_path: Path):
    """Breaks if a final game is omitted, the date is wrong, or retries duplicate the dispatch."""
    schedule = tmp_path / "games.parquet"
    state = tmp_path / "state.json"
    _schedule(schedule, gameday="2026-09-13", final=True)
    server, dispatch_url = _server()
    try:
        first = _run(
            schedule=schedule,
            state=state,
            now="2026-09-14T03:00:00-04:00",
            dispatch_url=dispatch_url,
        )
        second = _run(
            schedule=schedule,
            state=state,
            now="2026-09-14T03:00:00-04:00",
            dispatch_url=dispatch_url,
        )
    finally:
        server.shutdown()

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert _CaptureHandler.payloads == [
        {
            "event_type": "refresh-live-nfl-ops",
            "client_payload": {"season": 2026, "game_date": "2026-09-13"},
        }
    ]


def test_default_dispatch_url_targets_the_actual_workers_repository():
    scheduler = _scheduler_module()

    assert scheduler.REPOSITORY == "league-history-workers/mfl-league-fetcher"
    assert scheduler.parse_args(["--dispatch-token", "test-token"]).dispatch_url == (
        "https://api.github.com/repos/league-history-workers/mfl-league-fetcher/dispatches"
    )


def test_scheduler_uses_only_a_short_three_am_recovery_window(tmp_path: Path):
    """A late restart may recover near 03:00, but can never become a 07:30 worker."""
    schedule = tmp_path / "games.parquet"
    state = tmp_path / "state.json"
    _schedule(schedule, gameday="2026-09-13", final=True)
    server, dispatch_url = _server()
    try:
        recovery_window = _run(
            schedule=schedule,
            state=state,
            now="2026-09-14T03:04:59-04:00",
            dispatch_url=dispatch_url,
        )
        late = _run(
            schedule=schedule,
            state=state,
            now="2026-09-14T03:05:00-04:00",
            dispatch_url=dispatch_url,
        )
        _schedule(schedule, gameday="2026-09-14", final=False)
        off_day = _run(
            schedule=schedule,
            state=state,
            now="2026-09-15T03:00:00-04:00",
            dispatch_url=dispatch_url,
        )
    finally:
        server.shutdown()

    assert recovery_window.returncode == 0, recovery_window.stderr
    assert late.returncode == 0, late.stderr
    assert off_day.returncode == 0, off_day.stderr
    assert _CaptureHandler.payloads == [
        {
            "event_type": "refresh-live-nfl-ops",
            "client_payload": {"season": 2026, "game_date": "2026-09-13"},
        }
    ]


def test_pending_delivery_is_never_redispatched_after_a_post_dispatch_state_failure(tmp_path: Path, monkeypatch):
    """A crash after GitHub accepts a dispatch must not enqueue a duplicate run."""
    scheduler = _scheduler_module()
    schedule = tmp_path / "games.parquet"
    state = tmp_path / "state.json"
    _schedule(schedule, gameday="2026-09-13", final=True)
    dispatches: list[dict] = []
    original_write = scheduler._write_state
    write_count = 0

    def capture_dispatch(**kwargs):
        dispatches.append(kwargs["payload"])

    def fail_final_write(path, scheduler_state):
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("simulated loss after GitHub accepted the dispatch")
        original_write(path, scheduler_state)

    monkeypatch.setattr(scheduler, "_dispatch", capture_dispatch)
    monkeypatch.setattr(scheduler, "_write_state", fail_final_write)

    try:
        scheduler.run_once(
            now=scheduler._parse_now("2026-09-14T03:00:00-04:00"),
            season=2026,
            schedule_path=schedule,
            state_path=state,
            dispatch_url="http://example.invalid/dispatch",
            dispatch_token="test-token",
        )
    except OSError:
        pass
    else:
        raise AssertionError("the simulated final state write should fail")

    monkeypatch.setattr(scheduler, "_write_state", original_write)
    retry = scheduler.run_once(
        now=scheduler._parse_now("2026-09-14T03:01:00-04:00"),
        season=2026,
        schedule_path=schedule,
        state_path=state,
        dispatch_url="http://example.invalid/dispatch",
        dispatch_token="test-token",
    )

    assert retry == {"status": "delivery-pending", "game_date": "2026-09-13"}
    assert dispatches == [
        {
            "event_type": "refresh-live-nfl-ops",
            "client_payload": {"season": 2026, "game_date": "2026-09-13"},
        }
    ]

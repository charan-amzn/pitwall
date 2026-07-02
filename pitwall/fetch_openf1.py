"""Fetch real Formula 1 data from the OpenF1 API into Pitwall's CSV schema.

OpenF1 (https://openf1.org) is a free, key-less API of real F1 timing data.
This module pulls a season's race sessions and writes the same five CSVs the
rest of Pitwall expects, so swapping real data in for the synthetic sample is a
drop-in (see ``pitwall.data``).

Data source & licence: OpenF1 (https://openf1.org), CC BY 4.0. Pitwall is not
affiliated with OpenF1, Formula 1, or the FIA. "F1" and related marks belong to
their owners; this project is for analysis/education.

Mapping OpenF1 -> Pitwall schema:
  drivers.csv    driverId(=driver_number), code(name_acronym), forename, surname, team
  races.csv      raceId(=session_key), year, round, name, circuit, laps
  results.csv    raceId, driverId, grid, position, points, status
  pit_stops.csv  raceId, driverId, stop, lap, duration_s(=stop_duration)
  lap_times.csv  raceId, driverId, lap, position, lap_time_s(=lap_duration)

Network is required only at fetch time (i.e. when building the MicroVM image).
"""

from __future__ import annotations

import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OPENF1_BASE = os.environ.get("PITWALL_OPENF1_BASE", "https://api.openf1.org/v1")
DEFAULT_SEASON = int(os.environ.get("PITWALL_SEASON", "2026"))
_TIMEOUT = 60
_MAX_RETRIES = int(os.environ.get("PITWALL_OPENF1_RETRIES", "5"))
# OpenF1 is a free, rate-limited API. A small gap between calls plus exponential
# backoff on 429 keeps a full-season fetch under the limit without a key.
_REQUEST_GAP_S = float(os.environ.get("PITWALL_OPENF1_GAP_S", "0.4"))
_VERBOSE = os.environ.get("PITWALL_FETCH_QUIET") != "1"


class RateLimitError(RuntimeError):
    """OpenF1 kept returning 429 after retries (the API is throttling us)."""


def _log(msg: str) -> None:
    if _VERBOSE:
        print(msg, flush=True)


def _get(endpoint: str, **params) -> list[dict]:
    """GET an OpenF1 endpoint with retry/backoff; return JSON list (empty on 404)."""
    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = f"{OPENF1_BASE}/{endpoint}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})

    delay = 1.0
    for attempt in range(_MAX_RETRIES):
        time.sleep(_REQUEST_GAP_S)  # be polite to a free API
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read())
            if isinstance(data, dict):  # {"detail": "..."} when nothing matches
                return []
            return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return []
            if exc.code == 429 or exc.code >= 500:  # rate-limited / transient
                if attempt == _MAX_RETRIES - 1:
                    raise RateLimitError(
                        f"OpenF1 returned {exc.code} after {_MAX_RETRIES} tries "
                        f"({endpoint}). The free API is rate-limiting; wait a few "
                        "minutes and retry, raise PITWALL_OPENF1_GAP_S, or build "
                        "with PITWALL_DATA_SOURCE=synthetic."
                    ) from exc
                retry_after = exc.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                _log(f"   OpenF1 {exc.code} on {endpoint}; retrying in {wait:.0f}s "
                     f"({attempt + 1}/{_MAX_RETRIES})…")
                time.sleep(wait)
                delay = min(delay * 2, 30.0)
                continue
            raise
        except urllib.error.URLError:
            if attempt == _MAX_RETRIES - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            continue
    return []


def _split_name(full_name: str, acronym: str) -> tuple[str, str]:
    """OpenF1 gives 'Lewis HAMILTON'; split into ('Lewis', 'Hamilton')."""
    if not full_name:
        return ("", acronym)
    parts = full_name.split()
    if len(parts) == 1:
        return ("", parts[0].title())
    forename = parts[0].title()
    surname = " ".join(parts[1:]).title()
    return (forename, surname)


def _status(result: dict) -> str:
    if result.get("dsq"):
        return "Disqualified"
    if result.get("dns"):
        return "Did not start"
    if result.get("dnf"):
        return "DNF"
    return "Finished"


def _write_csv(path: Path, header: list[str], rows: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def fetch_season(data_dir: str | os.PathLike, season: int = DEFAULT_SEASON) -> Path:
    """Fetch all completed race sessions for ``season`` into Pitwall CSVs.

    "Completed" = the race session has result rows. Future/ongoing races are
    skipped, so the dataset is current as of fetch time.
    """
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)

    _log(f"Fetching real {season} F1 data from OpenF1 (this takes ~1–2 min)…")
    sessions = _get("sessions", year=season, session_name="Race")
    sessions = [s for s in sessions if not s.get("is_cancelled")]
    sessions.sort(key=lambda s: s.get("date_start", ""))

    drivers_seen: dict[int, tuple] = {}  # driver_number -> (code, forename, surname, team)
    races_rows, results_rows, pit_rows, lap_rows = [], [], [], []
    round_no = 0

    for sess in sessions:
        skey = sess["session_key"]
        results = _get("session_result", session_key=skey)
        if not results:
            continue  # not run yet — skip
        round_no += 1
        race_id = skey
        _log(f"   round {round_no}: {sess.get('country_name', '?')} GP")

        # --- per-session drivers (number -> code/name/team) ---
        sess_drivers = {d["driver_number"]: d for d in _get("drivers", session_key=skey)}
        for num, d in sess_drivers.items():
            if num not in drivers_seen:
                fn, sn = _split_name(d.get("full_name", ""), d.get("name_acronym", ""))
                drivers_seen[num] = (d.get("name_acronym", str(num)), fn, sn, d.get("team_name", ""))

        # --- laps (lap_duration -> lap_times; also derive lap count & grid) ---
        laps = _get("laps", session_key=skey)
        max_lap = 0
        lap1_pos: dict[int, int] = {}
        for lp in laps:
            num = lp.get("driver_number")
            ln = lp.get("lap_number")
            dur = lp.get("lap_duration")
            if num is None or ln is None:
                continue
            max_lap = max(max_lap, ln)
            if dur is not None:
                lap_rows.append([race_id, num, ln, None, round(float(dur), 3)])

        # --- results (position/points/status) ---
        finishers = [r for r in results if r.get("position") is not None]
        finishers.sort(key=lambda r: r["position"])
        for r in finishers:
            num = r.get("driver_number")
            results_rows.append(
                (race_id, num, "", r.get("position"),
                 _round_points(r.get("points")), _status(r))
            )

        # --- pit stops (stop_duration = stationary time) ---
        pit_counter: dict[int, int] = {}  # stop number per driver within this race
        for p in sorted(_get("pit", session_key=skey), key=lambda x: (x.get("driver_number", 0), x.get("lap_number", 0))):
            num = p.get("driver_number")
            ln = p.get("lap_number")
            dur = p.get("stop_duration", p.get("pit_duration"))
            if num is None or ln is None:
                continue
            pit_counter[num] = pit_counter.get(num, 0) + 1
            pit_rows.append((race_id, num, pit_counter[num], ln, _round_or_blank(dur)))

        circuit = sess.get("circuit_short_name", "")
        name = f"{sess.get('country_name', '')} Grand Prix".strip()
        races_rows.append((race_id, season, round_no, name, circuit, max_lap))

    if not races_rows:
        raise RuntimeError(
            f"OpenF1 returned no completed {season} races. "
            "Check connectivity or try a past season via PITWALL_SEASON."
        )

    drivers_rows = [
        (num, code, fn, sn, team)
        for num, (code, fn, sn, team) in sorted(drivers_seen.items())
    ]

    _write_csv(path / "drivers.csv", ["driverId", "code", "forename", "surname", "team"], drivers_rows)
    _write_csv(path / "races.csv", ["raceId", "year", "round", "name", "circuit", "laps"], races_rows)
    _write_csv(path / "results.csv", ["raceId", "driverId", "grid", "position", "points", "status"], results_rows)
    _write_csv(path / "pit_stops.csv", ["raceId", "driverId", "stop", "lap", "duration_s"], pit_rows)
    _write_csv(path / "lap_times.csv", ["raceId", "driverId", "lap", "position", "lap_time_s"], lap_rows)

    (path / "_SOURCE.txt").write_text(
        f"REAL Formula 1 data for the {season} season.\n"
        f"Source: OpenF1 (https://openf1.org), CC BY 4.0. Fetched at build time.\n"
        f"Races included: {len(races_rows)} (completed as of fetch).\n"
        "driverId is the car number; grid is left blank where unavailable.\n",
        encoding="utf-8",
    )
    return path.resolve()


def _round_points(val) -> float | int:
    if val is None:
        return 0
    f = float(val)
    return int(f) if f == int(f) else f


def _round_or_blank(val) -> object:
    if val is None:
        return ""
    try:
        return round(float(val), 2)
    except (TypeError, ValueError):
        return ""


if __name__ == "__main__":
    import sys

    season = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SEASON
    out = fetch_season(os.environ.get("PITWALL_DATA", "pitwall_data"), season)
    print(f"Fetched real {season} F1 data to {out}")

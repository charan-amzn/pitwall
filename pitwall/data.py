"""F1 dataset for the lab.

Two sources, selected by ``PITWALL_DATA_SOURCE`` (default ``openf1``):

  * ``openf1`` — **real** F1 data fetched from the free, key-less OpenF1 API
    (see ``pitwall.fetch_openf1``). This is what gets baked into the MicroVM
    image, so the lab analyses the actual current season. Requires network at
    fetch/build time.

  * ``synthetic`` — a small, deterministic **simulated** dataset (the real 2023
    grid, but all timing figures generated with a fixed seed). Needs no network;
    a useful offline fallback. Clearly labelled so it's never mistaken for
    official timing.

Both write the same five CSVs, so the rest of Pitwall doesn't care which source
produced them. To use your own data, drop CSVs with this schema into the data
directory (``PITWALL_DATA``).

Schema (Ergast-style, simplified):
  drivers.csv    driverId, code, forename, surname, team
  races.csv      raceId, year, round, name, circuit, laps
  results.csv    raceId, driverId, grid, position, points, status
  pit_stops.csv  raceId, driverId, stop, lap, duration_s
  lap_times.csv  raceId, driverId, lap, position, lap_time_s
"""

from __future__ import annotations

import csv
import os
import random
from pathlib import Path

DEFAULT_DATA_DIR = Path("pitwall_data")

# (code, forename, surname, team) — 2023 grid.
DRIVERS = [
    ("VER", "Max", "Verstappen", "Red Bull"),
    ("PER", "Sergio", "Perez", "Red Bull"),
    ("HAM", "Lewis", "Hamilton", "Mercedes"),
    ("RUS", "George", "Russell", "Mercedes"),
    ("LEC", "Charles", "Leclerc", "Ferrari"),
    ("SAI", "Carlos", "Sainz", "Ferrari"),
    ("NOR", "Lando", "Norris", "McLaren"),
    ("PIA", "Oscar", "Piastri", "McLaren"),
    ("ALO", "Fernando", "Alonso", "Aston Martin"),
    ("STR", "Lance", "Stroll", "Aston Martin"),
    ("GAS", "Pierre", "Gasly", "Alpine"),
    ("OCO", "Esteban", "Ocon", "Alpine"),
    ("ALB", "Alexander", "Albon", "Williams"),
    ("SAR", "Logan", "Sargeant", "Williams"),
    ("TSU", "Yuki", "Tsunoda", "AlphaTauri"),
    ("DEV", "Nyck", "de Vries", "AlphaTauri"),
    ("BOT", "Valtteri", "Bottas", "Alfa Romeo"),
    ("ZHO", "Guanyu", "Zhou", "Alfa Romeo"),
    ("MAG", "Kevin", "Magnussen", "Haas"),
    ("HUL", "Nico", "Hulkenberg", "Haas"),
]

# Rough team pace gap (seconds/lap, relative to the quickest car).
TEAM_PACE = {
    "Red Bull": 0.00,
    "Ferrari": 0.45,
    "Mercedes": 0.55,
    "Aston Martin": 0.70,
    "McLaren": 0.85,
    "Alpine": 1.20,
    "Williams": 1.55,
    "AlphaTauri": 1.70,
    "Alfa Romeo": 1.80,
    "Haas": 1.90,
}

# (round, name, circuit, laps, base_lap_s)
RACES = [
    (1, "Bahrain Grand Prix", "Bahrain International Circuit", 57, 95.5),
    (2, "Saudi Arabian Grand Prix", "Jeddah Corniche Circuit", 50, 90.0),
    (3, "Australian Grand Prix", "Albert Park Circuit", 58, 80.5),
    (5, "Miami Grand Prix", "Miami International Autodrome", 57, 90.0),
]

POINTS = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]


def resolve_data_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Pick the data directory: explicit arg > ``PITWALL_DATA`` env > default."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("PITWALL_DATA")
    return Path(env) if env else DEFAULT_DATA_DIR


def ensure_dataset(data_dir: str | os.PathLike | None = None) -> Path:
    """Return a ready data dir, building the dataset if it's empty.

    Source is chosen by ``PITWALL_DATA_SOURCE`` (``openf1`` default, or
    ``synthetic``). Used at MicroVM image-build time (see microvm_image/).
    """
    path = resolve_data_dir(data_dir)
    if (path / "results.csv").exists():
        return path.resolve()
    return build_dataset(path)


def build_dataset(data_dir: str | os.PathLike) -> Path:
    """Build the dataset from the configured source into ``data_dir``.

    Default ``openf1`` (real current-season data). If that fetch fails — e.g.
    the free OpenF1 API is rate-limiting — it falls back to the offline
    ``synthetic`` dataset so the image build still succeeds, unless
    ``PITWALL_REQUIRE_REAL_DATA=1`` is set (then the failure is raised).
    """
    source = os.environ.get("PITWALL_DATA_SOURCE", "openf1").lower()
    if source == "synthetic":
        return generate_sample(data_dir)
    if source == "openf1":
        from pitwall.fetch_openf1 import RateLimitError, fetch_season

        try:
            return fetch_season(data_dir)
        except (RateLimitError, OSError) as exc:
            if os.environ.get("PITWALL_REQUIRE_REAL_DATA") == "1":
                raise
            print(
                f"\n[!] OpenF1 fetch failed ({exc.__class__.__name__}: {exc}).\n"
                "    Falling back to the synthetic dataset so the build can "
                "proceed. Re-run later (or raise PITWALL_OPENF1_GAP_S) for real "
                "data; set PITWALL_REQUIRE_REAL_DATA=1 to fail instead.\n",
                flush=True,
            )
            return generate_sample(data_dir)
    raise SystemExit(
        f"Unknown PITWALL_DATA_SOURCE={source!r}; use 'openf1' or 'synthetic'."
    )


def _write_csv(path: Path, header: list[str], rows: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def generate_sample(data_dir: str | os.PathLike) -> Path:
    """Generate the deterministic synthetic dataset into ``data_dir``."""
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)

    code_to_id = {code: i + 1 for i, (code, *_rest) in enumerate(DRIVERS)}
    id_to_code = {v: k for k, v in code_to_id.items()}
    teams = {code: team for (code, _fn, _sn, team) in DRIVERS}
    # Fixed per-driver pace offset, so a driver is consistently quicker/slower
    # than their car's baseline across all races.
    driver_delta = {code: rng.uniform(-0.15, 0.35) for (code, *_rest) in DRIVERS}

    races_rows, results_rows, pit_rows, lap_rows = [], [], [], []

    for rnd, name, circuit, laps, base in RACES:
        race_id = rnd
        races_rows.append((race_id, 2023, rnd, name, circuit, laps))

        # Qualifying -> grid order.
        quali = sorted(
            (base + TEAM_PACE[teams[c]] + driver_delta[c] + rng.uniform(-0.25, 0.25), c)
            for (c, *_rest) in DRIVERS
        )
        grid = {code: pos + 1 for pos, (_pace, code) in enumerate(quali)}

        # Each driver gets a 1- or 2-stop plan.
        pit_plan: dict[str, list[int]] = {}
        for (code, *_rest) in DRIVERS:
            n_stops = rng.choice([1, 1, 1, 2])
            pit_plan[code] = sorted(rng.sample(range(12, laps - 6), n_stops))

        cum = {code: 0.0 for (code, *_rest) in DRIVERS}
        last_pit = {code: 0 for (code, *_rest) in DRIVERS}
        stop_count = {code: 0 for (code, *_rest) in DRIVERS}

        for lap in range(1, laps + 1):
            for (code, *_rest) in DRIVERS:
                stint_lap = lap - last_pit[code]
                lap_time = base + TEAM_PACE[teams[code]] + driver_delta[code]
                lap_time += 0.045 * stint_lap        # tyre degradation within a stint
                lap_time += 0.030 * (laps - lap)     # fuel load: heavier (slower) early
                lap_time += rng.uniform(-0.15, 0.35)  # noise + traffic
                if lap in pit_plan[code]:
                    stationary = round(rng.uniform(2.0, 3.6), 2)
                    lap_time += 19.0 + stationary    # pit-lane time loss
                    stop_count[code] += 1
                    pit_rows.append((race_id, code_to_id[code], stop_count[code], lap, stationary))
                    last_pit[code] = lap
                cum[code] += lap_time
                lap_rows.append([race_id, code_to_id[code], lap, None, round(lap_time, 3)])

            # Fill in position for this lap by cumulative race time.
            order = sorted((code for (code, *_rest) in DRIVERS), key=lambda c: cum[c])
            pos_map = {code: i + 1 for i, code in enumerate(order)}
            for row in lap_rows[-len(DRIVERS):]:
                row[3] = pos_map[id_to_code[row[1]]]

        final_order = sorted((code for (code, *_rest) in DRIVERS), key=lambda c: cum[c])
        for i, code in enumerate(final_order):
            points = POINTS[i] if i < len(POINTS) else 0
            results_rows.append((race_id, code_to_id[code], grid[code], i + 1, points, "Finished"))

    _write_csv(
        path / "drivers.csv",
        ["driverId", "code", "forename", "surname", "team"],
        [(code_to_id[c], c, fn, sn, team) for (c, fn, sn, team) in DRIVERS],
    )
    _write_csv(path / "races.csv", ["raceId", "year", "round", "name", "circuit", "laps"], races_rows)
    _write_csv(
        path / "results.csv",
        ["raceId", "driverId", "grid", "position", "points", "status"],
        results_rows,
    )
    _write_csv(path / "pit_stops.csv", ["raceId", "driverId", "stop", "lap", "duration_s"], pit_rows)
    _write_csv(path / "lap_times.csv", ["raceId", "driverId", "lap", "position", "lap_time_s"], lap_rows)

    (path / "_SOURCE.txt").write_text(
        "SYNTHETIC sample data generated by pitwall.data (seed=42).\n"
        "Real 2023 drivers/teams/race names; all timing figures are simulated.\n"
        "Replace these CSVs with real data (same schema) for genuine analysis.\n",
        encoding="utf-8",
    )
    return path.resolve()


# Where the dataset is mounted inside the MicroVM image (see microvm_image/).
IN_VM_DATA_DIR = "/opt/pitwall/data"


def dataset_summary(data_dir: str | os.PathLike = IN_VM_DATA_DIR) -> str:
    """One-line-per-file description for the agent's system prompt.

    Defaults to the in-VM dataset path, since analysis runs inside the MicroVM.
    The dataset's own ``_SOURCE.txt`` (written by whichever source built it) is
    included verbatim so the agent knows the season and provenance.
    """
    schema = (
        "Files & columns:\n"
        "  drivers.csv    driverId, code, forename, surname, team\n"
        "  races.csv      raceId, year, round, name, circuit, laps\n"
        "  results.csv    raceId, driverId, grid, position, points, status "
        "(grid may be blank for real data)\n"
        "  pit_stops.csv  raceId, driverId, stop, lap, duration_s\n"
        "  lap_times.csv  raceId, driverId, lap, position, lap_time_s "
        "(position may be blank for real data)"
    )
    note = (
        "Note: driverId is the car number. Join results/laps/pit_stops to "
        "drivers.csv on driverId to get the 3-letter code and team."
    )
    return (
        f"Dataset directory: {data_dir} (read from os.environ['PITWALL_DATA']).\n"
        f"{schema}\n{note}"
    )


if __name__ == "__main__":  # `python -m pitwall.data` regenerates the sample
    out = ensure_dataset()
    print(f"Dataset ready at: {out}")

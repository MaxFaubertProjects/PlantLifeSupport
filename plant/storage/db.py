"""SQLite storage layer.

One table — ``cycles`` — records everything that happened in a single
hourly run: sensor readings, the photo path, what the AI said, the
safety-clamped final decision, and what was actually actuated.

Schema
──────
cycles
  id               INTEGER  PK AUTOINCREMENT
  ts               TEXT     ISO-8601 UTC timestamp for the cycle
  soil_raw         INTEGER  MCP3008 raw ADC (0–1023)
  soil_pct         REAL     Moisture percentage (0–100)
  temp_celsius     REAL     DS18B20 temperature
  photo_path       TEXT     Absolute path to the saved JPEG/PNG

  plant_description TEXT    VLM plain-language description of the plant
  ai_water         INTEGER  AI wanted to water? (0/1)
  ai_water_seconds INTEGER  AI-requested watering duration
  ai_light_on      INTEGER  AI wanted light on? (0/1)
  ai_reasoning     TEXT     AI's natural-language reasoning
  used_fallback    INTEGER  1 if rule-based fallback was used instead

  final_water      INTEGER  After safety clamping — did we water?
  final_water_secs REAL     Actual pulse duration delivered (seconds)
  final_light_on   INTEGER  Actual light state set
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Sequence


# ── Schema ────────────────────────────────────────────────────────────────────

_CREATE_CYCLES = """
CREATE TABLE IF NOT EXISTS cycles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT    NOT NULL,
    soil_raw         INTEGER,
    soil_pct         REAL,
    temp_celsius     REAL,
    photo_path       TEXT,

    plant_description TEXT,
    ai_water         INTEGER,
    ai_water_seconds INTEGER,
    ai_light_on      INTEGER,
    ai_reasoning     TEXT,
    used_fallback    INTEGER DEFAULT 0,

    final_water      INTEGER,
    final_water_secs REAL,
    final_light_on   INTEGER
);
"""

_CREATE_IDX_TS = "CREATE INDEX IF NOT EXISTS idx_cycles_ts ON cycles(ts);"


# ── Public dataclass ──────────────────────────────────────────────────────────

@dataclass
class Cycle:
    """A single hourly cycle record, as read from the database."""
    id: int
    ts: str
    soil_raw: int | None
    soil_pct: float | None
    temp_celsius: float | None
    photo_path: str | None
    plant_description: str | None
    ai_water: bool | None
    ai_water_seconds: int | None
    ai_light_on: bool | None
    ai_reasoning: str | None
    used_fallback: bool
    final_water: bool | None
    final_water_secs: float | None
    final_light_on: bool | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Cycle":
        return cls(
            id=row["id"],
            ts=row["ts"],
            soil_raw=row["soil_raw"],
            soil_pct=row["soil_pct"],
            temp_celsius=row["temp_celsius"],
            photo_path=row["photo_path"],
            plant_description=row["plant_description"],
            ai_water=bool(row["ai_water"]) if row["ai_water"] is not None else None,
            ai_water_seconds=row["ai_water_seconds"],
            ai_light_on=bool(row["ai_light_on"]) if row["ai_light_on"] is not None else None,
            ai_reasoning=row["ai_reasoning"],
            used_fallback=bool(row["used_fallback"]),
            final_water=bool(row["final_water"]) if row["final_water"] is not None else None,
            final_water_secs=row["final_water_secs"],
            final_light_on=bool(row["final_light_on"]) if row["final_light_on"] is not None else None,
        )


# ── Database class ────────────────────────────────────────────────────────────

class Database:
    """Thin wrapper around SQLite providing typed read/write for cycle data."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")  # safe for concurrent reads
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._migrate()

    def _migrate(self) -> None:
        self._conn.execute(_CREATE_CYCLES)
        self._conn.execute(_CREATE_IDX_TS)
        self._conn.commit()

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ── Write ─────────────────────────────────────────────────────────────────

    def insert_cycle(
        self,
        *,
        soil_raw: int | None = None,
        soil_pct: float | None = None,
        temp_celsius: float | None = None,
        photo_path: str | None = None,
        plant_description: str | None = None,
        ai_water: bool | None = None,
        ai_water_seconds: int | None = None,
        ai_light_on: bool | None = None,
        ai_reasoning: str | None = None,
        used_fallback: bool = False,
        final_water: bool | None = None,
        final_water_secs: float | None = None,
        final_light_on: bool | None = None,
    ) -> int:
        """Insert a completed cycle and return its new id."""
        ts = datetime.now(timezone.utc).isoformat()
        with self._tx():
            cur = self._conn.execute(
                """
                INSERT INTO cycles (
                    ts, soil_raw, soil_pct, temp_celsius, photo_path,
                    plant_description,
                    ai_water, ai_water_seconds, ai_light_on, ai_reasoning,
                    used_fallback,
                    final_water, final_water_secs, final_light_on
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?,
                    ?, ?, ?, ?,
                    ?,
                    ?, ?, ?
                )
                """,
                (
                    ts, soil_raw, soil_pct, temp_celsius, photo_path,
                    plant_description,
                    int(ai_water) if ai_water is not None else None,
                    ai_water_seconds,
                    int(ai_light_on) if ai_light_on is not None else None,
                    ai_reasoning,
                    int(used_fallback),
                    int(final_water) if final_water is not None else None,
                    final_water_secs,
                    int(final_light_on) if final_light_on is not None else None,
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    # ── Read ──────────────────────────────────────────────────────────────────

    def latest_cycle(self) -> Cycle | None:
        """Return the most recent cycle, or None if the table is empty."""
        row = self._conn.execute(
            "SELECT * FROM cycles ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return Cycle.from_row(row) if row else None

    def recent_cycles(self, limit: int = 48) -> Sequence[Cycle]:
        """Return up to *limit* most-recent cycles, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM cycles ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Cycle.from_row(r) for r in rows]

    def cycle_count(self) -> int:
        """Total number of recorded cycles."""
        return self._conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]

    def close(self) -> None:
        self._conn.close()


# ── Module-level factory ──────────────────────────────────────────────────────

def open_db(cfg: dict) -> Database:
    """Open (or create) the database at the path configured in config.yaml."""
    path = cfg["storage"]["db_path"]
    return Database(path)

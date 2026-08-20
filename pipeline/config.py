"""Configuration and the replication manifest.

Everything tunable lives here or in tables.yml. There are no constants buried in the
loader, because a number buried in a loader is a number that disagrees with the
write-up six weeks later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

APP_ROOT = Path(__file__).resolve().parent
REPO_ROOT = APP_ROOT.parent
SQL_DIR = REPO_ROOT / "sql"
DOCS_DIR = REPO_ROOT / "docs"
MANIFEST_PATH = APP_ROOT / "tables.yml"


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(
            f"Missing required environment variable {name}.\n"
            f"If you are running this outside docker compose, copy .env.example to .env "
            f"and export the variables it names."
        )
    return value or ""


@dataclass(frozen=True)
class SourceConfig:
    """Connection details for the stand-in production SQL Server."""

    host: str
    port: int
    user: str
    password: str
    database: str

    # How long to wait for SQL Server to accept connections before giving up.
    #
    # This is the pipeline's own readiness wait, and it is deliberately here rather than
    # expressed as a compose healthcheck gate. sqlcmd moved to /opt/mssql-tools18/bin at
    # 2022 CU14 and some published images shipped with no tools directory at all, so a
    # healthcheck that gates startup can fail for reasons unrelated to the database
    # actually being up -- and a gate that cannot open is a hang, which has no error
    # message. Waiting in code I control gives a bounded wait and a real diagnostic.
    #
    # 180s because a cold first start pulls a ~1.6 GB image and then SQL Server runs its
    # own recovery; on a laptop that is slow, not broken.
    connect_timeout_seconds: int = 180
    connect_retry_interval_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> "SourceConfig":
        return cls(
            host=_env("FUNDO_MSSQL_HOST", "mssql"),
            port=int(_env("FUNDO_MSSQL_PORT", "1433")),
            user=_env("FUNDO_MSSQL_USER", "sa"),
            password=_env("FUNDO_MSSQL_PASSWORD", required=True),
            database=_env("FUNDO_MSSQL_DB", "fundo_src"),
        )


@dataclass(frozen=True)
class WarehouseConfig:
    """Where the warehouse stand-in lives.

    DuckDB, not a BigQuery emulator. The available emulator
    (goccy/bigquery-emulator) is SQLite underneath: no partitioning, no clustering, no
    MERGE, no totalBytesBilled and no dry-run. It has the best API fidelity and cannot
    price anything -- and pricing is the one thing this exercise actually grades. Nor
    Postgres: it is row-oriented, so the bytes-scanned half of BigQuery's cost model
    does not transfer at all. DuckDB is columnar, and 1.4 LTS shipped a real MERGE INTO,
    which is the exact statement whose cost SOLUTION.md argues about.
    """

    path: Path

    @classmethod
    def from_env(cls) -> "WarehouseConfig":
        return cls(path=Path(_env("FUNDO_WAREHOUSE_PATH", "/warehouse/fundo_dw.duckdb")))


@dataclass(frozen=True)
class TableSpec:
    """One replicated table, as declared in tables.yml."""

    name: str
    strategy: str                    # "change_tracking" | "high_water"
    primary_key: tuple[str, ...]
    columns: tuple[str, ...]
    watermark_column: str | None = None
    append_only_asserted: bool = False

    def __post_init__(self) -> None:
        if self.strategy not in ("change_tracking", "high_water"):
            raise ValueError(f"{self.name}: unknown strategy {self.strategy!r}")
        if self.strategy == "high_water" and not self.watermark_column:
            raise ValueError(f"{self.name}: high_water strategy needs a watermark_column")
        missing = set(self.primary_key) - set(self.columns)
        if missing:
            # A primary key column absent from the allow-list would produce a warehouse
            # table that cannot be upserted or key-compared. Fail at load time, loudly,
            # rather than producing a table that looks fine and silently duplicates.
            raise ValueError(
                f"{self.name}: primary key column(s) {sorted(missing)} are not in the "
                f"replicated column list"
            )

    @property
    def qualified(self) -> str:
        return f"dbo.{self.name}"

    @property
    def column_list_sql(self) -> str:
        """Bracket-quoted column list. Never SELECT * -- see tables.yml."""
        return ", ".join(f"[{c}]" for c in self.columns)


@dataclass(frozen=True)
class Manifest:
    source_database: str
    tables: tuple[TableSpec, ...]
    excluded_tables: tuple[dict[str, Any], ...]

    @classmethod
    def load(cls, path: Path = MANIFEST_PATH) -> "Manifest":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        tables = tuple(
            TableSpec(
                name=name,
                strategy=spec["strategy"],
                primary_key=tuple(spec["primary_key"]),
                columns=tuple(spec["columns"]),
                watermark_column=spec.get("watermark_column"),
                append_only_asserted=bool(spec.get("append_only_asserted", False)),
            )
            for name, spec in raw["tables"].items()
        )
        return cls(
            source_database=raw["source_database"],
            tables=tables,
            excluded_tables=tuple(raw.get("excluded_tables") or ()),
        )

    def by_strategy(self, strategy: str) -> tuple[TableSpec, ...]:
        return tuple(t for t in self.tables if t.strategy == strategy)

    def get(self, name: str) -> TableSpec:
        for t in self.tables:
            if t.name == name:
                return t
        raise KeyError(f"{name} is not in the replication manifest")

    @property
    def managed_names(self) -> frozenset[str]:
        return frozenset(t.name for t in self.tables)


@dataclass(frozen=True)
class Settings:
    source: SourceConfig
    warehouse: WarehouseConfig
    manifest: Manifest
    seed: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            source=SourceConfig.from_env(),
            warehouse=WarehouseConfig.from_env(),
            manifest=Manifest.load(),
            # The seed is the measuring instrument. Every number in SOLUTION.md is an
            # output of it, so a reviewer's run must reproduce it exactly -- which is
            # why the seed generator uses hash functions over row ordinals and never
            # NEWID() or RAND().
            seed=int(_env("FUNDO_SEED", "20260818")),
        )

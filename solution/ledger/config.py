"""Loads the workflow lead schemas and the disposition catalogue."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalise_header

DEFAULT_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"


@dataclass(frozen=True)
class Field:
    name: str
    type: str                      # string | phone | date | decimal | enum
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: tuple[str, ...] = ()
    values: tuple[str, ...] = ()   # for enum
    default: str | None = None


@dataclass(frozen=True)
class Schema:
    workflow_id: str
    fields: tuple[Field, ...]

    @property
    def phone_field(self) -> Field:
        """The phone field is the lead's identity."""
        return next(f for f in self.fields if f.type == "phone")

    @property
    def reference_field(self) -> Field:
        return next(f for f in self.fields if f.reference)

    def header_lookup(self) -> dict[str, str]:
        """normalised header text -> field name (name + every alias)."""
        out: dict[str, str] = {}
        for f in self.fields:
            for label in (f.name, *f.aliases):
                out.setdefault(normalise_header(label), f.name)
        return out


@dataclass(frozen=True)
class Disposition:
    code: str
    is_terminal: bool
    is_success: bool


@dataclass
class Catalogue:
    schemas: dict[str, Schema] = field(default_factory=dict)
    dispositions: dict[str, dict[str, Disposition]] = field(default_factory=dict)  # wf -> code -> D


def _bool(s: str) -> bool:
    return s.strip().lower() == "true"


def load_catalogue(schema_dir: Path = DEFAULT_SCHEMA_DIR) -> Catalogue:
    cat = Catalogue()
    for p in sorted(schema_dir.glob("*.json")):
        raw = json.loads(p.read_text(encoding="utf-8"))
        fields = tuple(
            Field(
                name=f["name"], type=f["type"], required=bool(f.get("required")),
                pii=bool(f.get("pii")), reference=bool(f.get("reference")),
                aliases=tuple(f.get("aliases", [])), values=tuple(f.get("values", [])),
                default=f.get("default"),
            )
            for f in raw["fields"]
        )
        cat.schemas[raw["workflow_id"]] = Schema(raw["workflow_id"], fields)
    with open(schema_dir / "dispositions.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            cat.dispositions.setdefault(row["workflow_id"], {})[row["code"]] = Disposition(
                row["code"], _bool(row["is_terminal"]), _bool(row["is_success"]))
    return cat

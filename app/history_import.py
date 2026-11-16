"""Explicit, review-first import of historical Excel cultivation records.

The source workbook is immutable. No workbook layout, two-digit year, mixed
variety weight, or duplicate cultivation is silently resolved.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import DateTime, ForeignKey, JSON, String, UniqueConstraint, delete, select, func
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .auth import audit, require_user, require_write
from .db import Base, DATA_DIR, get_db
from .models import (Bay, Cultivation, CultivationRevision, Harvest, PlanningVersion,
                     User, Variety)

router = APIRouter(tags=["Historische Excel-import"])
ROOT = DATA_DIR / "history-imports"
MAX_FILE = 32 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_MEMBERS = 10000
MAX_ROWS = 50000
REQUIRED = {"round_name", "bay_number", "variety", "plant_date", "light_out_date",
            "harvest_end_date", "weight_g"}
ALLOWED = REQUIRED | {"harvest_start_date", "density", "beds", "stem_count", "loss_fraction",
                      "planned_stems", "historical_alar_total", "historical_alar_unit"}
SPECIES = {"a": "antonov", "p": "pjotr", "t": "tutu", "b": "baltazar"}
OLD_INACTIVE = {"g", "k", "w", "altaj"}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def dutch_today():
    return datetime.now(ZoneInfo("Europe/Amsterdam")).date()


class HistoryImport(Base):
    __tablename__ = "history_imports"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(240))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    raw_path: Mapped[str] = mapped_column(String(300))
    profile: Mapped[str] = mapped_column(String(40), default="generic")
    status: Mapped[str] = mapped_column(String(30), default="uploaded")
    mapping: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))


class HistoryPreviewRow(Base):
    __tablename__ = "history_preview_rows"
    __table_args__ = (UniqueConstraint("import_id", "row_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("history_imports.id"), index=True)
    row_number: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(20))
    reasons: Mapped[list] = mapped_column(JSON)
    original: Mapped[dict] = mapped_column(JSON)
    effective: Mapped[dict] = mapped_column(JSON)
    cultivations: Mapped[list] = mapped_column(JSON)
    source: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_ids: Mapped[list] = mapped_column(JSON, default=list)
    payload_hash: Mapped[str] = mapped_column(String(64))


class HistorySourceRow(Base):
    __tablename__ = "history_source_rows"
    __table_args__ = (UniqueConstraint("sha256", "sheet", "row_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    sheet: Mapped[str] = mapped_column(String(200))
    row_number: Mapped[int] = mapped_column()
    payload_hash: Mapped[str] = mapped_column(String(64))
    cultivation_ids: Mapped[list] = mapped_column(JSON)
    whole_cap_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class HistoryReviewVersion(Base):
    """Append-only snapshot of every accepted mapping and human correction."""
    __tablename__ = "history_review_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("history_imports.id"), index=True)
    mapping: Mapped[dict] = mapped_column(JSON)
    summary: Mapped[dict] = mapped_column(JSON)
    reasons: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))


class RowOverride(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    row_number: int = Field(ge=1)
    fields: dict[str, str | int | float | None] = Field(default_factory=dict)
    exclude: bool = False
    accept_anchor_mismatch: bool = False
    include_planning: bool = False
    confirm_program_round_title: bool = False
    reason: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_change(self):
        if (self.fields or self.exclude or self.accept_anchor_mismatch or self.include_planning or
                self.confirm_program_round_title) and len(self.reason.strip()) < 3:
            raise ValueError("correctie of uitsluiting vereist een reden")
        if set(self.fields) - ALLOWED:
            raise ValueError("onbekend correctieveld")
        if len(self.fields) > len(ALLOWED) or any(isinstance(value, str) and len(value) > 2000
                                                  for value in self.fields.values()):
            raise ValueError("correctieveld te groot")
        return self


class MappingInput(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    sheet: str = Field(min_length=1, max_length=200)
    header_row: int = Field(ge=1, le=200)
    column_map: dict[str, str]
    defaults: dict[str, str | int | float | None] = Field(default_factory=dict)
    overrides: list[RowOverride] = Field(default_factory=list, max_length=10000)
    date_year_policy: str = "explicit_only"

    @model_validator(mode="after")
    def validate_mapping(self):
        if self.date_year_policy != "explicit_only":
            raise ValueError("jaartallen moeten expliciet zijn")
        if REQUIRED - set(self.column_map) or set(self.column_map) - ALLOWED:
            raise ValueError("vereiste kolommen ontbreken of onbekende kolommen opgegeven")
        if set(self.defaults) - (ALLOWED - REQUIRED | {"beds"}):
            raise ValueError("ongeldige standaardwaarde")
        if len(self.defaults) > len(ALLOWED) or any(isinstance(value, str) and len(value) > 2000
                                                   for value in self.defaults.values()):
            raise ValueError("standaardwaarde te groot")
        columns = [x.upper().strip() for x in self.column_map.values()]
        if any(not re.fullmatch(r"[A-Z]{1,3}", x) for x in columns) or len(set(columns)) != len(columns):
            raise ValueError("kolomletters ongeldig of dubbel")
        if any(sum((ord(char) - 64) * 26 ** power for power, char in enumerate(reversed(col))) > 256
               for col in columns):
            raise ValueError("kolom buiten ondersteunde eerste 256 kolommen")
        if len({o.row_number for o in self.overrides}) != len(self.overrides):
            raise ValueError("dubbele rijcorrectie")
        return self


class ConfirmInput(BaseModel):
    confirmed: bool


class LegacyPreviewInput(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    program_import_id: int
    round_years: dict[str, int] = Field(default_factory=dict)
    weekday_convention: str
    single_species_beds: int | None = Field(default=None, ge=1, le=6)
    group_rounds: dict[str, str] = Field(default_factory=dict)
    group_round_reason: str = Field(default="", max_length=2000)
    program_record_selection: dict[str, int] = Field(default_factory=dict)
    program_record_selection_reason: str = Field(default="", max_length=2000)
    overrides: list[RowOverride] = Field(default_factory=list, max_length=10000)
    include_unweighted_planning: bool = False
    include_program_only_planning: bool = False
    planning_status: str = "historical"
    exclude_ambiguous: bool = False
    exclusion_reason: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def check(self):
        if self.weekday_convention != "iso_monday_1":
            raise ValueError("bevestig ISO-weekdag Maandag=1")
        if self.planning_status not in ("historical", "planned"):
            raise ValueError("ongeldige status voor planning zonder gewicht")
        if any(not re.fullmatch(r"Ronde\s+\d+", key, re.I) or not 1900 <= year <= 2100
               for key, year in self.round_years.items()):
            raise ValueError("rondejaar vereist expliciete ronde en viercijferig jaar")
        if self.group_rounds and len(self.group_round_reason.strip()) < 3:
            raise ValueError("handmatige rondekoppeling vereist een reden")
        if self.program_record_selection and len(self.program_record_selection_reason.strip()) < 3:
            raise ValueError("keuze tussen dubbele programmarijen vereist een reden")
        if self.exclude_ambiguous and len(self.exclusion_reason.strip()) < 3:
            raise ValueError("groepsuitsluiting vereist een reden")
        if len({o.row_number for o in self.overrides}) != len(self.overrides):
            raise ValueError("dubbele rijcorrectie")
        return self


class BulkMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str | None = None
    reason_contains: str | None = Field(default=None, max_length=200)
    group_index: int | None = Field(default=None, ge=1, le=100)
    source_sheet: str | None = Field(default=None, max_length=200)
    round_name: str | None = Field(default=None, max_length=100)
    variety: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def check(self):
        if self.status not in (None, "valid", "ambiguous", "excluded", "duplicate"):
            raise ValueError("ongeldige rijstatus")
        if not any((self.status, self.reason_contains, self.group_index, self.source_sheet,
                    self.round_name, self.variety)):
            raise ValueError("selecteer minstens één concreet rijkenmerk")
        return self


class BulkReviewInput(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    expected_review_version_id: int = Field(ge=1)
    row_numbers: list[int] | None = Field(default=None, max_length=10000)
    all_matching: BulkMatch | None = None
    fields: dict[str, str | int | float | None] = Field(default_factory=dict)
    overwrite_existing: bool = False
    exclude: bool | None = None
    accept_anchor_mismatch: bool | None = None
    include_planning: bool | None = None
    confirm_program_round_title: bool | None = None
    reason: str = Field(min_length=3, max_length=2000)
    dry_run: bool = False

    @model_validator(mode="after")
    def check(self):
        if (self.row_numbers is None) == (self.all_matching is None):
            raise ValueError("kies rijnummers of één selectie over alle pagina's")
        if self.row_numbers is not None and (not self.row_numbers or
                len(self.row_numbers) != len(set(self.row_numbers)) or any(x < 1 for x in self.row_numbers)):
            raise ValueError("rijnummers ontbreken, zijn dubbel of ongeldig")
        if set(self.fields) - (ALLOWED - {"round_name", "bay_number"}):
            raise ValueError("onbekend of ongeschikt bulkcorrectieveld")
        if any(isinstance(value, str) and len(value) > 2000 for value in self.fields.values()):
            raise ValueError("bulkcorrectieveld te groot")
        if any(value in (None, "") for value in self.fields.values()):
            raise ValueError("bulkcorrectie vereist een ingevulde waarde")
        sensitive = set(self.fields) - {"beds", "density", "planned_stems"}
        scoped = self.row_numbers is not None or (self.all_matching is not None and
            (self.all_matching.group_index is not None or self.all_matching.round_name or
             self.all_matching.variety or
             bool(self.all_matching.source_sheet and
                  re.fullmatch(r"Ronde\s+\d+", self.all_matching.source_sheet, re.I))))
        if sensitive and not scoped:
            raise ValueError("soort, datum of gewicht vereist expliciete rijnummers of één ronde")
        if self.include_planning and self.all_matching and not self.all_matching.reason_contains:
            raise ValueError("planning opnemen over alle pagina's vereist een redenfilter")
        if not self.fields and self.exclude is None and self.accept_anchor_mismatch is None and \
                self.include_planning is None and self.confirm_program_round_title is None:
            raise ValueError("bulkactie ontbreekt")
        if not self.reason.strip() or len(self.reason.strip()) < 3:
            raise ValueError("bulkcontrole vereist een reden")
        return self


def _path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise HTTPException(500, "ongeldig opgeslagen bronpad")
    return DATA_DIR / path


def _begin_write_lock(db: Session):
    if db.info.get("_history_bulk_lock"):
        return
    if db.bind.dialect.name == "sqlite":
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        except OperationalError as exc:
            db.rollback()
            raise HTTPException(409, "import is momenteel in bewerking") from exc


def _validate_xlsx(path: Path):
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) > MAX_MEMBERS or "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise ValueError("geen ondersteund XLSX-werkboek")
            if len({name.casefold() for name in names}) != len(names):
                raise ValueError("dubbele archiefpaden")
            total = 0
            for info in archive.infolist():
                member = PurePosixPath(info.filename)
                lower = info.filename.lower()
                printer_setting = bool(re.fullmatch(r"xl/printersettings/printersettings[0-9]+\.bin", lower))
                if (member.is_absolute() or ".." in member.parts or "\\" in info.filename or
                        info.flag_bits & 1 or "vbaproject" in lower or
                        (lower.endswith(".bin") and not printer_setting) or
                        lower.endswith((".exe", ".dll"))):
                    raise ValueError("onveilig of versleuteld werkboekonderdeel")
                total += info.file_size
                if total > MAX_EXPANDED:
                    raise ValueError("uitgepakte werkboekinhoud te groot")
                if info.filename.lower().endswith((".xml", ".rels")):
                    with archive.open(info) as source:
                        tail = b""
                        while chunk := source.read(65536):
                            probe = (tail + chunk).lower()
                            if b"<!doctype" in probe or b"<!entity" in probe:
                                raise ValueError("XML-entiteiten of DOCTYPE zijn niet toegestaan")
                            tail = probe[-16:]
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise ValueError("ongeldig XLSX-bestand") from exc


def _workbook(path: Path):
    from openpyxl import load_workbook
    try:
        return load_workbook(path, read_only=True, data_only=False, keep_links=False)
    except Exception as exc:
        raise ValueError("werkboek kan niet veilig worden gelezen") from exc


def _display(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _sheet_summaries(path: Path) -> list[dict]:
    from openpyxl.utils import get_column_letter
    wb = _workbook(path)
    try:
        summaries = []
        for ws in wb.worksheets[:100]:
            sample = []
            for row_number, row in enumerate(ws.iter_rows(min_row=1, max_row=min(ws.max_row or 10, 10), max_col=min(ws.max_column or 40, 100)), 1):
                cells = {get_column_letter(c.column): _display(c.value)
                         for c in row[:40] if c.value is not None}
                sample.append({"row_number": row_number, "cells": cells})
            summaries.append({"name": ws.title, "max_row": ws.max_row, "max_column": ws.max_column,
                              "headers": [{"column": col, "value": value} for col, value in
                                          (sample[0]["cells"] if sample else {}).items()],
                              "sample_rows": sample})
        return summaries
    finally:
        wb.close()


def _metadata(item: HistoryImport) -> dict:
    return {"id": item.id, "filename": item.filename, "sha256": item.sha256,
            "profile": item.profile, "status": item.status, "created_at": item.created_at.isoformat() + "Z",
            "confirmed_at": item.confirmed_at.isoformat() + "Z" if item.confirmed_at else None,
            "mapping": item.mapping, "summary": item.summary}


def _row_dict(row: HistoryPreviewRow) -> dict:
    return {"row_number": row.row_number, "status": row.status, "reasons": row.reasons,
            "original": row.original, "effective": row.effective,
            "cultivations": row.cultivations, "source": row.source,
            "created_ids": row.created_ids or []}


def _review_dict(version: HistoryReviewVersion) -> dict:
    return {"id": version.id, "import_id": version.import_id, "mapping": version.mapping,
            "summary": version.summary, "reasons": version.reasons,
            "created_at": version.created_at.isoformat() + "Z",
            "user_id": version.user_id}


def _detail(db: Session, item: HistoryImport, *, offset=0, limit=200) -> dict:
    rows = db.scalars(select(HistoryPreviewRow).where(HistoryPreviewRow.import_id == item.id)
                      .order_by(HistoryPreviewRow.row_number).offset(offset).limit(limit)).all()
    reviews = db.scalars(select(HistoryReviewVersion).where(HistoryReviewVersion.import_id == item.id)
                         .order_by(HistoryReviewVersion.id)).all()
    return {**_metadata(item), "sheets": _sheet_summaries(_path(item.raw_path)),
            "reviews": [_review_dict(v) for v in reviews],
            "legacy_profile": (item.mapping or {}).get("legacy_profile"),
            "preview": {"summary": item.summary, "rows": [_row_dict(r) for r in rows],
                        "offset": offset, "limit": limit, "total": (item.summary or {}).get("total_rows", 0)}}


def _number(value, *, integer=False, minimum=None, maximum=None):
    if isinstance(value, bool) or value is None:
        raise ValueError("getal ontbreekt")
    if isinstance(value, str):
        text = value.strip().replace(" ", "")
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        value = text
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ongeldig getal") from exc
    if not math.isfinite(number) or (integer and not number.is_integer()) or (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        raise ValueError("getal buiten toegestaan bereik")
    return int(number) if integer else number


def _date(value):
    if isinstance(value, datetime):
        value = value.date()
    elif isinstance(value, str):
        text = value.strip()
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                value = date.fromisoformat(text)
            elif re.fullmatch(r"\d{2}-\d{2}-\d{4}", text):
                value = datetime.strptime(text, "%d-%m-%Y").date()
            else:
                raise ValueError("datum vereist expliciet viercijferig jaar")
        except ValueError as exc:
            raise ValueError("ongeldige datum of ambigu jaartal") from exc
    if not isinstance(value, date) or not 1900 <= value.year <= 2100:
        raise ValueError("ongeldige datum of ambigu jaartal")
    return value


def _species(value, beds):
    if value is None or str(value).strip() == "":
        raise ValueError("lege soortcode")
    text = re.sub(r"\s+", "", str(value).lower())
    if text in OLD_INACTIVE:
        raise ValueError("inactieve historische soortcode")
    if text in SPECIES:
        if beds is None:
            raise ValueError("aantal bedden vereist voor enkele soort")
        return [(SPECIES[text], beds)]
    if text in SPECIES.values():
        if beds is None:
            raise ValueError("aantal bedden vereist voor enkele soort")
        return [(text, beds)]
    chunks = re.findall(r"(\d+)([aptb])", text)
    if chunks and "".join(n + code for n, code in chunks) == text:
        parsed = [(SPECIES[code], int(n)) for n, code in chunks]
        if (any(n < 1 for _, n in parsed) or sum(n for _, n in parsed) > 6 or
                len({name for name, _ in parsed}) != len(parsed) or
                (beds is not None and sum(n for _, n in parsed) != beds)):
            raise ValueError("ongeldige verdeling van soortbedden")
        return parsed
    raise ValueError("onbekende of niet-eenduidige soortcode")


def _canonical(db: Session, values: dict) -> list[dict]:
    round_name = str(values.get("round_name") or "").strip()
    if not round_name or len(round_name) > 100:
        raise ValueError("ronde ontbreekt of is te lang")
    bay = _number(values.get("bay_number"), integer=True, minimum=1, maximum=40)
    if not db.get(Bay, bay):
        raise ValueError("kap bestaat niet")
    plant = _date(values.get("plant_date"))
    light_out = _date(values.get("light_out_date"))
    harvest_end = _date(values.get("harvest_end_date"))
    harvest_start = _date(values["harvest_start_date"]) if values.get("harvest_start_date") not in (None, "") else None
    if not (plant <= light_out <= (harvest_start or harvest_end) <= harvest_end):
        raise ValueError("datums staan niet in teeltvolgorde")
    if harvest_end > dutch_today():
        raise ValueError("toekomstige einddatum is geen werkelijk geoogst gewicht")
    beds = _number(values["beds"], integer=True, minimum=1, maximum=6) if values.get("beds") not in (None, "") else None
    raw_species = str(values.get('variety') or '').strip().lower()
    configured = db.scalar(select(Variety).where(func.lower(Variety.name) == raw_species))
    if configured is not None and raw_species not in SPECIES and raw_species not in OLD_INACTIVE:
        if beds is None:
            raise ValueError('aantal bedden vereist voor enkele soort')
        species = [(configured.name, beds)]
    else:
        species = _species(values.get("variety"), beds)
    if {"antonov", "tutu"} <= {name for name, _ in species}:
        raise ValueError("antonov/tutu met één kapgewicht is ambigue")
    raw_weight = values.get("weight_g")
    quality_classes = None
    weight = None
    if isinstance(raw_weight, str) and "/" in raw_weight:
        parts = raw_weight.split("/")
        if len(parts) < 2 or any(not p.strip() for p in parts):
            raise ValueError("ongeldige dubbele gewichtsklassen")
        quality_classes = [{"weight_g": _number(x, minimum=1, maximum=1000)} for x in parts]
    else:
        weight = _number(raw_weight, minimum=1, maximum=1000)
    density = _number(values["density"], minimum=1, maximum=1000) if values.get("density") not in (None, "") else None
    stem_count = _number(values["stem_count"], integer=True, minimum=0) if values.get("stem_count") not in (None, "") else None
    loss = _number(values["loss_fraction"], minimum=0, maximum=1) if values.get("loss_fraction") not in (None, "") else None
    planned_stems = _number(values["planned_stems"], integer=True, minimum=0) if values.get("planned_stems") not in (None, "") else None
    alar_total = (_number(values["historical_alar_total"], minimum=0)
                  if values.get("historical_alar_total") not in (None, "") else None)
    alar_unit = str(values.get("historical_alar_unit") or "").strip()
    if (alar_total is None) != (not alar_unit):
        raise ValueError("historisch alar-totaal vereist een expliciete eenheid")
    if len(alar_unit) > 80:
        raise ValueError("alar-eenheid is te lang")
    result = []
    for name, share in species:
        variety = db.scalar(select(Variety).where(Variety.name == name))
        if not variety:
            raise ValueError(f"soort {name} ontbreekt in soortenbeheer")
        result.append({"round_name": round_name, "bay_number": bay, "variety_id": variety.id,
                       "variety": name, "beds": share, "plant_date": plant.isoformat(),
                       "light_out_date": light_out.isoformat(),
                       "harvest_start_date": harvest_start.isoformat() if harvest_start else None,
                       "harvest_end_date": harvest_end.isoformat(), "weight_g": weight,
                       "quality_classes": quality_classes, "density": density,
                       "stem_count": stem_count if len(species) == 1 else None,
                       "loss_fraction": loss if len(species) == 1 else None,
                       "planned_stems": planned_stems if len(species) == 1 else None,
                       "whole_cap_values": {"stem_count": stem_count, "loss_fraction": loss,
                                            "planned_stems": planned_stems,
                                            "historical_alar_total": alar_total,
                                            "historical_alar_unit": alar_unit or None},
                       "label_quality": "historical_assigned" if len(species) > 1 else "verified"})
    return result


def _planning_canonical(db: Session, values: dict, status: str) -> list[dict]:
    """Archive planning evidence without inventing an end weight or harvest."""
    round_name = str(values.get("round_name") or "").strip()
    if not round_name or len(round_name) > 100:
        raise ValueError("ronde ontbreekt of is te lang")
    bay = _number(values.get("bay_number"), integer=True, minimum=1, maximum=40)
    if not db.get(Bay, bay):
        raise ValueError("kap bestaat niet")
    plant = _date(values.get("plant_date"))
    light_out = _date(values["light_out_date"]) if values.get("light_out_date") else None
    expected = _date(values["expected_harvest_date"]) if values.get("expected_harvest_date") else None
    if (light_out and light_out < plant) or (expected and expected < (light_out or plant)):
        raise ValueError("planningsdatums staan niet in teeltvolgorde")
    if plant > dutch_today() and status == "historical":
        raise ValueError("toekomstige planning vereist status planned")
    beds = _number(values["beds"], integer=True, minimum=1, maximum=6) if values.get("beds") not in (None, "") else None
    raw_species = str(values.get("variety") or "").strip().lower()
    configured = db.scalar(select(Variety).where(func.lower(Variety.name) == raw_species))
    if configured is not None and raw_species not in SPECIES and raw_species not in OLD_INACTIVE:
        if beds is None:
            raise ValueError("aantal bedden vereist voor enkele soort")
        species = [(configured.name, beds)]
    else:
        species = _species(values.get("variety"), beds)
    density = _number(values["density"], minimum=1, maximum=1000) if values.get("density") not in (None, "") else None
    planned_stems = (_number(values["planned_stems"], integer=True, minimum=0)
                     if values.get("planned_stems") not in (None, "") else None)
    alar_total = (_number(values["historical_alar_total"], minimum=0)
                  if values.get("historical_alar_total") not in (None, "") else None)
    alar_unit = str(values.get("historical_alar_unit") or "").strip()
    if (alar_total is None) != (not alar_unit):
        raise ValueError("historisch alar-totaal vereist een expliciete eenheid")
    whole_cap = {**(values.get("whole_cap_values") or {}),
                 "historical_alar_total": alar_total,
                 "historical_alar_unit": alar_unit or None,
                 "planned_stems": planned_stems}
    result = []
    for name, share in species:
        variety = db.scalar(select(Variety).where(Variety.name == name))
        if not variety:
            raise ValueError(f"soort {name} ontbreekt in soortenbeheer")
        result.append({"round_name": round_name, "bay_number": bay, "variety_id": variety.id,
            "variety": name, "beds": share, "plant_date": plant.isoformat(),
            "light_out_date": light_out.isoformat() if light_out else None,
            "expected_harvest_date": expected.isoformat() if expected else None,
            "harvest_start_date": None, "harvest_end_date": None, "weight_g": None,
            "quality_classes": None, "density": density, "stem_count": None,
            "loss_fraction": None,
            "planned_stems": planned_stems if len(species) == 1 else None,
            "whole_cap_values": whole_cap,
            "label_quality": "unknown", "status": status, "create_harvest": False})
    return result


def _hash(payload: list[dict]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _profile(sheets: list[dict]) -> str:
    by_name = {s["name"].lower(): s for s in sheets}
    schema = by_name.get("teeltschema")
    if schema and "data" in by_name:
        cells = (schema.get("sample_rows") or [{}])[0].get("cells", {})
        if all(str(cells.get(col, "")).strip().lower() == label for col, label in
               {"A": "kap", "B": "planten", "G": "licht uit", "P": "leeg", "S": "gewicht"}.items()):
            return "legacy_weight_workbook"
    rounds = [s for s in sheets if re.fullmatch(r"Ronde\s+\d+", s["name"], re.I)]
    if len(rounds) >= 5 and any(
        str((row.get("cells") or {}).get("D", "")).strip().lower() == "schema"
        for row in rounds[0].get("sample_rows", [])):
        return "legacy_program_workbook"
    return "generic"


def _existing_cultivation(db, value: dict):
    candidates = db.scalars(select(Cultivation).where(
        Cultivation.round_name == value["round_name"], Cultivation.bay_number == value["bay_number"],
        Cultivation.variety_id == value["variety_id"])).all()
    for c in candidates:
        plans = db.scalars(select(PlanningVersion).where(PlanningVersion.cultivation_id == c.id)).all()
        if any(p.plant_date and p.plant_date.isoformat() == value["plant_date"] for p in plans):
            return c.id
    return None


@router.post("/api/history-imports")
async def upload_history(file: UploadFile = File(...), db: Session = Depends(get_db),
                         user: User = Depends(require_write)):
    filename = Path(file.filename or "").name
    if not filename.lower().endswith(".xlsx") or not filename or any(ord(c) < 32 for c in filename):
        raise HTTPException(422, "alleen .xlsx zonder macro's wordt ondersteund")
    ROOT.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=ROOT, prefix=".upload-", suffix=".xlsx", delete=False) as tmp:
        temporary = Path(tmp.name)
        size = 0
        try:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE:
                    raise HTTPException(413, "Excel-bestand te groot")
                digest.update(chunk)
                tmp.write(chunk)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        _validate_xlsx(temporary)
        sheets = _sheet_summaries(temporary)
        if not sheets:
            raise ValueError("werkboek heeft geen werkbladen")
        path = ROOT / f"{digest.hexdigest()}.xlsx"
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest.hexdigest():
                raise ValueError("opgeslagen werkboekchecksum wijkt af")
        else:
            os.replace(temporary, path)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        temporary.unlink(missing_ok=True)
    item = HistoryImport(filename=filename, sha256=digest.hexdigest(),
        raw_path=path.relative_to(DATA_DIR).as_posix(), profile=_profile(sheets),
        user_id=user.id, status="uploaded")
    db.add(item); db.flush()
    audit(db, user, "history.upload", item.id, {"sha256": item.sha256, "filename": filename})
    db.commit()
    return _detail(db, item)


@router.get("/api/history-imports")
def list_history(db: Session = Depends(get_db), user: User = Depends(require_user)):
    return {"imports": [_metadata(x) for x in db.scalars(select(HistoryImport)
             .order_by(HistoryImport.id.desc()).limit(200)).all()]}


@router.get("/api/history-imports/{import_id}")
def get_history(import_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    item = db.get(HistoryImport, import_id)
    if not item:
        raise HTTPException(404, "import niet gevonden")
    return _detail(db, item)


@router.get("/api/history-imports/{import_id}/rows")
def history_rows(import_id: int, offset: int = 0, limit: int = 200,
                 db: Session = Depends(get_db), user: User = Depends(require_user)):
    item = db.get(HistoryImport, import_id)
    if not item:
        raise HTTPException(404, "import niet gevonden")
    if offset < 0 or limit < 1 or limit > 500:
        raise HTTPException(422, "ongeldige paginering")
    return _detail(db, item, offset=offset, limit=limit)["preview"]


@router.get("/api/history-imports/{import_id}/sheet-preview")
def sheet_preview(import_id: int, sheet: str, header_row: int = 1,
                  db: Session = Depends(get_db), user: User = Depends(require_user)):
    from openpyxl.utils import get_column_letter
    item = db.get(HistoryImport, import_id)
    if not item:
        raise HTTPException(404, "import niet gevonden")
    if not 1 <= header_row <= 200:
        raise HTTPException(422, "ongeldige kopregel")
    wb = _workbook(_path(item.raw_path))
    try:
        if sheet not in wb.sheetnames:
            raise HTTPException(422, "werkblad niet gevonden")
        ws = wb[sheet]
        if ws.max_row and header_row > ws.max_row:
            raise HTTPException(422, "kopregel buiten werkblad")
        max_col = min(ws.max_column or 1, 100)
        header = next(ws.iter_rows(min_row=header_row, max_row=header_row, max_col=max_col))
        headers = [{"column": get_column_letter(c.column), "value": _display(c.value)}
                   for c in header if c.value is not None]
        sample = []
        for row_number, row in enumerate(ws.iter_rows(min_row=header_row + 1,
                                max_row=min(ws.max_row or header_row + 10, header_row + 10), max_col=max_col), header_row + 1):
            sample.append({"row_number": row_number,
                           "cells": {get_column_letter(c.column): _display(c.value)
                                     for c in row if c.value is not None}})
        return {"sheet": sheet, "header_row": header_row, "max_row": ws.max_row,
                "max_column": ws.max_column, "headers": headers, "sample_rows": sample}
    finally:
        wb.close()


@router.post("/api/history-imports/{import_id}/mapping")
def map_history(import_id: int, data: MappingInput, db: Session = Depends(get_db),
                user: User = Depends(require_write)):
    from openpyxl.utils import column_index_from_string
    _begin_write_lock(db)
    item = db.get(HistoryImport, import_id)
    if not item:
        raise HTTPException(404, "import niet gevonden")
    if item.status == "confirmed":
        raise HTTPException(409, "bevestigde import kan niet worden gewijzigd; upload opnieuw voor correctie")
    wb = _workbook(_path(item.raw_path))
    try:
        if data.sheet not in wb.sheetnames:
            raise HTTPException(422, "werkblad niet gevonden")
        ws = wb[data.sheet]
        if ws.max_row and data.header_row >= ws.max_row:
            raise HTTPException(422, "kopregel heeft geen datarijen")
        if ws.max_row and ws.max_row > MAX_ROWS + data.header_row + 1:
            raise HTTPException(413, "werkblad heeft te veel rijen; gebruik het teeltschema-profiel voor de grote bron")
        columns = {field: column_index_from_string(col.upper().strip()) - 1 for field, col in data.column_map.items()}
        overrides = {o.row_number: o for o in data.overrides}
        previews = []
        seen_keys = set()
        for row_number, row in enumerate(ws.iter_rows(min_row=data.header_row + 1, max_col=max(columns.values()) + 1,
                                max_row=min(ws.max_row or MAX_ROWS + data.header_row + 1,
                                            MAX_ROWS + data.header_row + 1)), data.header_row + 1):
            original = {field: _display(row[index].value) if index < len(row) else None
                        for field, index in columns.items()}
            if all(x in (None, "") for x in original.values()):
                continue
            effective = {**original}
            for field, default in data.defaults.items():
                if effective.get(field) in (None, ""):
                    effective[field] = default
            override = overrides.get(row_number)
            if override:
                effective.update(override.fields)
            reasons = []
            canonical = []
            if override and override.exclude:
                status = "excluded"
                reasons.append(override.reason.strip())
            elif any(index < len(row) and row[index].data_type == "f" and
                     field not in (override.fields if override else {})
                     for field, index in columns.items()):
                status = "ambiguous"
                reasons.append("formulecel: formule en eventuele cache worden niet als bronwaarde gebruikt")
            else:
                try:
                    canonical = _canonical(db, effective)
                    status = "valid"
                except ValueError as exc:
                    status = "ambiguous"
                    reasons.append(str(exc))
            payload_hash = _hash(canonical) if canonical else ""
            if status == "valid":
                keys = {(x["round_name"], x["bay_number"], x["variety_id"], x["plant_date"]) for x in canonical}
                if seen_keys & keys:
                    status = "ambiguous"
                    reasons.append("ronde/kap/soort/plantdatum staat meermaals in dit werkblad")
                seen_keys.update(keys)
            if status == "valid":
                prior = db.scalar(select(HistorySourceRow).where(HistorySourceRow.sha256 == item.sha256,
                    HistorySourceRow.sheet == data.sheet, HistorySourceRow.row_number == row_number))
                if prior:
                    if prior.payload_hash == payload_hash:
                        status = "duplicate"
                        reasons.append("dezelfde bronrij is al bevestigd")
                    else:
                        status = "ambiguous"
                        reasons.append("bronrij heeft een eerder bevestigde, afwijkende koppeling")
                elif any(_existing_cultivation(db, value) for value in canonical):
                    status = "ambiguous"
                    reasons.append("ronde/kap/soort/plantdatum bestaat al; handmatige vergelijking nodig")
            previews.append(HistoryPreviewRow(import_id=item.id, row_number=row_number,
                status=status, reasons=reasons, original=original, effective=effective,
                cultivations=canonical, payload_hash=payload_hash))
            if len(previews) > MAX_ROWS:
                raise HTTPException(413, "te veel historische rijen")
        if any(number <= data.header_row or number > (ws.max_row or 0) for number in overrides):
            raise HTTPException(422, "correctie verwijst naar niet-bestaande datarij")
        db.execute(delete(HistoryPreviewRow).where(HistoryPreviewRow.import_id == item.id))
        db.add_all(previews)
        from collections import Counter
        counts = Counter(p.status for p in previews)
        item.summary = {"total_rows": len(previews), "valid": counts["valid"],
                        "ambiguous": counts["ambiguous"], "excluded": counts["excluded"],
                        "duplicate": counts["duplicate"]}
        item.mapping = data.model_dump(mode="json")
        item.status = "preview_ready"
        db.add(HistoryReviewVersion(import_id=item.id, mapping=item.mapping,
                                    summary=item.summary,
                                    reasons=[{"row_number": p.row_number, "status": p.status,
                                              "reasons": p.reasons} for p in previews if p.reasons],
                                    user_id=user.id))
        audit(db, user, "history.mapping", item.id, {"sha256": item.sha256,
            "sheet": data.sheet, "summary": item.summary})
        db.commit()
        return _detail(db, item)
    finally:
        wb.close()


@router.post("/api/history-imports/{import_id}/legacy-preview")
def legacy_preview(import_id: int, data: LegacyPreviewInput, db: Session = Depends(get_db),
                   user: User = Depends(require_write)):
    """Pair the actual company workbooks through visible source-cell evidence.

    Calendar years and weekday convention come from the reviewer. Literal
    week/day pairs may be converted after that; no formula cache is promoted.
    """
    from collections import Counter
    from .legacy_cultivation import (group_plant_years, iso_date, match_rounds,
                                     next_week_date, parse_weight_workbook)
    from .legacy_program import parse_program

    _begin_write_lock(db)
    item = db.get(HistoryImport, import_id)
    companion = db.get(HistoryImport, data.program_import_id)
    if not item or not companion:
        raise HTTPException(404, "één van de bronwerkboeken ontbreekt")
    if item.status == "confirmed":
        raise HTTPException(409, "bevestigde import kan niet worden gewijzigd")
    if item.profile != "legacy_weight_workbook" or companion.profile != "legacy_program_workbook":
        raise HTTPException(422, "werkboekprofielen passen niet bij de historische koppeling")
    weight = parse_weight_workbook(_path(item.raw_path))
    program = parse_program(_path(companion.raw_path))
    matches = match_rounds(weight["groups"], program["records"])
    by_round = {}
    for record in program["records"]:
        by_round.setdefault(record["round_name"], {}).setdefault(record["bay_number"], []).append(record)
    known_rounds = set(by_round)
    selected_records = {}
    for key, selected_row in data.program_record_selection.items():
        try:
            selected_round, selected_bay_text = key.rsplit(":", 1)
            selected_bay = int(selected_bay_text)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, "programmarijkeuze vereist sleutel 'Ronde N:kap'") from exc
        candidates = by_round.get(selected_round, {}).get(selected_bay, [])
        if len(candidates) < 2 or selected_row not in [r["row_number"] for r in candidates]:
            raise HTTPException(422, "programmarijkeuze verwijst niet naar dubbele bronrij")
        selected_records[(selected_round, selected_bay)] = next(
            r for r in candidates if r["row_number"] == selected_row)
    for group_index, round_name in data.group_rounds.items():
        if not group_index.isdecimal() or not 1 <= int(group_index) <= len(matches) or round_name not in known_rounds:
            raise HTTPException(422, "handmatige rondekoppeling verwijst naar onbekend blok of ronde")
        matches[int(group_index) - 1]["matched_round"] = round_name
        matches[int(group_index) - 1]["confidence"] = "manually_confirmed"
    assigned_rounds = [match["matched_round"] for match in matches if match["matched_round"]]
    if len(assigned_rounds) != len(set(assigned_rounds)):
        raise HTTPException(422, "één programmaronde is aan meerdere gewichtblokken gekoppeld")
    for round_name in data.round_years:
        if round_name not in known_rounds:
            raise HTTPException(422, "rondejaar verwijst naar onbekende ronde")
    overrides = {o.row_number: o for o in data.overrides}
    previews = []
    paired_program_keys = set()
    seen_keys = set()

    def program_record(round_name, bay):
        candidates = by_round.get(round_name, {}).get(bay, [])
        chosen = selected_records.get((round_name, bay))
        return ((chosen or candidates[0]) if len(candidates) == 1 or chosen else None,
                candidates, chosen is not None)

    def append_record(row_number: int, original: dict, values: dict, source: dict,
                      *, planning_only: bool, omit: bool = False, anchor_mismatch: bool = False,
                      formula_weight: bool = False, duplicate_program: bool = False,
                      selected_program: bool = False, round_label_conflict: bool = False,
                      manual_round: bool = False, forced_exclusion_reason: str | None = None):
        override = overrides.get(row_number)
        effective = {**values}
        if override:
            effective.update(override.fields)
        reasons = []
        canonical = []
        if override and override.exclude:
            status = "excluded"
            reasons.append(override.reason.strip())
        elif forced_exclusion_reason:
            status = "excluded"
            reasons.append(forced_exclusion_reason)
        elif formula_weight and not (override and "weight_g" in override.fields):
            status = "ambiguous"
            reasons.append("eindgewicht is formule; bewaarde cache is niet geverifieerd")
        elif omit and not (override and override.include_planning):
            status = "excluded"
            reasons.append("planning-only niet geselecteerd; geen eindgewicht")
        elif duplicate_program and not selected_program:
            status = "ambiguous"
            reasons.append("meerdere programmarijen voor dezelfde ronde en kap; kies bronrij met reden")
        elif round_label_conflict and not manual_round and not (override and override.confirm_program_round_title):
            status = "ambiguous"
            reasons.append("programmarondelabel in bron wijkt af van werkbladnaam")
        elif anchor_mismatch and not (override and override.accept_anchor_mismatch):
            status = "ambiguous"
            reasons.append("plantweek/-dag wijkt af tussen teeltschema en programma")
        else:
            if selected_program:
                reasons.append(f"dubbele programmarij handmatig gekozen: {data.program_record_selection_reason.strip()}")
            if round_label_conflict and manual_round:
                reasons.append(f"afwijkend programmarondelabel handmatig gekoppeld: {data.group_round_reason.strip()}")
            if round_label_conflict and override and override.confirm_program_round_title:
                reasons.append(f"afwijkend programmarondelabel per bronrij gecontroleerd: {override.reason.strip()}")
            if omit and override and override.include_planning:
                reasons.append(f"planning zonder eindgewicht handmatig opgenomen: {override.reason.strip()}")
            if anchor_mismatch:
                reasons.append(f"afwijkende programmakoppeling handmatig geaccepteerd: {override.reason.strip()}")
            if formula_weight:
                reasons.append(f"formulegewicht handmatig vervangen: {override.reason.strip()}")
            try:
                canonical = (_planning_canonical(db, effective, data.planning_status) if planning_only
                             else _canonical(db, effective))
                if not planning_only:
                    for record in canonical:
                        record["whole_cap_values"].update(effective.get("whole_cap_values") or {})
                status = "valid"
            except ValueError as exc:
                status = "ambiguous"
                reasons.append(str(exc))
        payload_hash = _hash(canonical) if canonical else ""
        if status == "valid":
            keys = {(x["round_name"], x["bay_number"], x["variety_id"], x["plant_date"]) for x in canonical}
            if seen_keys & keys:
                status = "ambiguous"
                reasons.append("ronde/kap/soort/plantdatum meermaals in dit voorbeeld")
            seen_keys.update(keys)
        if status == "valid":
            prior = db.scalar(select(HistorySourceRow).where(
                HistorySourceRow.sha256 == source["sha256"],
                HistorySourceRow.sheet == source["sheet"],
                HistorySourceRow.row_number == source["row_number"]))
            if prior:
                if prior.payload_hash == payload_hash:
                    status = "duplicate"
                    reasons.append("dezelfde bronrij is al bevestigd")
                else:
                    status = "ambiguous"
                    reasons.append("bronrij heeft een eerdere afwijkende koppeling")
            elif any(_existing_cultivation(db, x) for x in canonical):
                status = "ambiguous"
                reasons.append("ronde/kap/soort/plantdatum bestaat al")
        if status == "ambiguous" and data.exclude_ambiguous:
            status = "excluded"
            reasons.append(f"door reviewer uitgesloten: {data.exclusion_reason.strip()}")
        previews.append(HistoryPreviewRow(import_id=item.id, row_number=row_number,
            status=status, reasons=reasons, original=original, effective=effective,
            cultivations=canonical, payload_hash=payload_hash, source=source))

    for group, match in zip(weight["groups"], matches):
        round_name = match["matched_round"]
        first_year = data.round_years.get(round_name) if round_name else None
        years = group_plant_years(group, first_year) if first_year else {}
        for row in group["rows"]:
            if round_name:
                paired_program_keys.add((round_name, row["bay_number"]))
            record, candidates, selected_record = program_record(round_name, row["bay_number"])
            duplicate_program = len(candidates) > 1
            label_conflict = any("round_label_conflict" in x.get("review_reasons", []) for x in candidates)
            source = {"sha256": item.sha256, "sheet": "teeltschema",
                      "row_number": row["row_number"], "program_sha256": companion.sha256,
                      "program_record_key": f"{round_name}:{row['bay_number']}" if round_name else None,
                      "program_record_row_number": record["row_number"] if record else None}
            anchor_mismatch = bool(record and row["plant_week"] is not None and row["plant_weekday"] is not None
                and record["plant_week"] is not None and record["plant_weekday"] is not None
                and (row["plant_week"], row["plant_weekday"]) !=
                    (record["plant_week"], record["plant_weekday"]))
            plant = iso_date(years[row["row_number"]], row["plant_week"], row["plant_weekday"]) if first_year else None
            light_out = next_week_date(plant, row["light_out_week"], row["light_out_weekday"], 60)
            harvest_end = next_week_date(light_out, row["harvest_week"], row["harvest_weekday"], 200)
            species = record["schema_code"] if record else None
            species_parts = record["species_beds"] if record else []
            beds = (sum(part["beds"] for part in species_parts) if species_parts and
                    all(part["beds"] is not None for part in species_parts)
                    else data.single_species_beds if len(species_parts) == 1 else None)
            values = {"round_name": round_name, "bay_number": row["bay_number"],
                      "variety": species, "beds": beds,
                      "plant_date": plant.isoformat() if plant else None,
                      "light_out_date": light_out.isoformat() if light_out else None,
                      "harvest_end_date": harvest_end.isoformat() if harvest_end else None,
                      "expected_harvest_date": harvest_end.isoformat() if harvest_end else None,
                      "weight_g": row["weight_raw"], "density": record["density"] if record else None}
            values["whole_cap_values"] = {"source_alar_raw": row["alar_raw"],
                "source_loss_raw": row["loss_raw"], "source_note": row["note"],
                "program_sha256": companion.sha256,
                "program_record_key": source.get("program_record_key"),
                "program_record_row_number": source.get("program_record_row_number")}
            original = {"group_index": group["group_index"], "match": match,
                        "anchor_mismatch": anchor_mismatch,
                        "duplicate_program_bay": duplicate_program,
                        "round_label_conflict": label_conflict,
                        "program_candidates": [{"row_number": x["row_number"],
                            "source_cells": x["source_cells"], "review_reasons": x["review_reasons"]}
                                               for x in candidates] if duplicate_program else [],
                        "date_derivation": {"first_iso_year_confirmed": first_year,
                                            "row_iso_year": years.get(row["row_number"]),
                                            "weekday_convention": data.weekday_convention,
                                            "plant_week_rollover": bool(first_year and years.get(row["row_number"]) != first_year)},
                        "weight_source_cells": row["source_cells"],
                        "program_source_cells": record["source_cells"] if record else None,
                        "alar_raw": row["alar_raw"], "loss_raw": row["loss_raw"],
                        "note": row["note"]}
            planning_only = row["weight_raw"] in (None, "") and not row["weight_formula"]
            append_record(row["row_number"], original, values, source,
                          planning_only=planning_only,
                          omit=planning_only and not data.include_unweighted_planning,
                          anchor_mismatch=anchor_mismatch,
                          formula_weight=bool(row["weight_formula"]),
                          duplicate_program=duplicate_program,
                          selected_program=selected_record,
                          round_label_conflict=label_conflict or match["confidence"] == "round_label_conflict",
                          manual_round=str(group["group_index"]) in data.group_rounds)

    program_only_available = sum(len(records) for name, bays in by_round.items()
                                 for bay, records in bays.items()
                                 if (name, bay) not in paired_program_keys)
    if program_only_available:  # Keep unmatched source records visible even when excluded.
        for round_name, bays in by_round.items():
            year = data.round_years.get(round_name)
            ordered = sorted((record for bay, records in bays.items()
                              if (round_name, bay) not in paired_program_keys for record in records),
                             key=lambda x: (x["bay_number"], x["row_number"]))
            if not ordered:
                continue
            program_group = {"rows": [{"row_number": r["row_number"],
                                       "plant_week": r["plant_week"]} for r in ordered]}
            years = group_plant_years(program_group, year) if year else {}
            for record in ordered:
                row_number = 1_000_000 + int(round_name.split()[-1]) * 1000 + record["row_number"]
                candidates = bays[record["bay_number"]]
                chosen = selected_records.get((round_name, record["bay_number"]))
                duplicate_program = len(candidates) > 1
                label_conflict = "round_label_conflict" in record.get("review_reasons", [])
                plant = iso_date(years[record["row_number"]], record["plant_week"],
                                 record["plant_weekday"]) if year else None
                ld = record["fields"]["vegetative_days"]
                light_out = plant + timedelta(days=ld) if plant and ld is not None else None
                expected = next_week_date(light_out or plant, record["planned_harvest_week"],
                                          record["planned_harvest_weekday"], 200)
                parts = record["species_beds"]
                beds = (sum(part["beds"] for part in parts) if parts and
                        all(part["beds"] is not None for part in parts)
                        else data.single_species_beds if len(parts) == 1 else None)
                values = {"round_name": round_name, "bay_number": record["bay_number"],
                          "variety": record["schema_code"], "beds": beds,
                          "plant_date": plant.isoformat() if plant else None,
                          "light_out_date": light_out.isoformat() if light_out else None,
                          "expected_harvest_date": expected.isoformat() if expected else None,
                          "density": record["density"],
                          "whole_cap_values": {"program_sha256": companion.sha256,
                                               "program_record_key": record["record_key"]}}
                source = {"sha256": companion.sha256, "sheet": round_name,
                          "row_number": record["row_number"],
                          "program_record_key": record["record_key"]}
                append_record(row_number, {"program_source_cells": record["source_cells"],
                                           "program_review_reasons": record["review_reasons"],
                                           "duplicate_program_bay": duplicate_program,
                                           "program_candidates": [{"row_number": x["row_number"],
                                               "source_cells": x["source_cells"],
                                               "review_reasons": x["review_reasons"]} for x in candidates]
                                               if duplicate_program else [],
                                           "date_derivation": {"first_iso_year_confirmed": year,
                                               "row_iso_year": years.get(record["row_number"]),
                                               "weekday_convention": data.weekday_convention,
                                               "light_out_from_literal_ld_days": ld is not None}},
                              values, source, planning_only=True,
                              omit=not data.include_program_only_planning,
                              duplicate_program=duplicate_program,
                              selected_program=chosen is record,
                              round_label_conflict=label_conflict,
                              forced_exclusion_reason=("dubbele programmarij niet gekozen; gekozen bronrij " +
                                                       str(chosen["row_number"]))
                                                       if chosen is not None and chosen is not record else None)
    if any(number not in {r.row_number for r in previews} for number in overrides):
        raise HTTPException(422, "correctie verwijst naar een niet-geselecteerde bronrij")
    db.execute(delete(HistoryPreviewRow).where(HistoryPreviewRow.import_id == item.id))
    db.add_all(previews)
    counts = Counter(r.status for r in previews)
    item.summary = {"total_rows": len(previews), "valid": counts["valid"],
                    "ambiguous": counts["ambiguous"], "excluded": counts["excluded"],
                    "duplicate": counts["duplicate"],
                    "weighted_source_rows": weight["summary"]["weight_rows"],
                    "unweighted_source_rows": weight["summary"]["unweighted_rows"],
                    "program_only_available": program_only_available,
                    "program_only_preview_rows": program_only_available,
                    "paired_weight_preview_rows": weight["summary"]["bay_rows"]}
    row_accounting = {"weight_source_rows": weight["summary"]["bay_rows"],
                      "program_source_rows": len(program["records"]),
                      "paired_program_rows": len(program["records"]) - program_only_available,
                      "program_only_rows": program_only_available,
                      "weighted_rows": weight["summary"]["weight_rows"],
                      "unweighted_rows": weight["summary"]["unweighted_rows"],
                      "formula_weight_rows": weight["summary"]["formula_weight_rows"],
                      "preview_rows": len(previews), "excluded_rows": counts["excluded"],
                      "ambiguous_rows": counts["ambiguous"], "valid_rows": counts["valid"]}
    legacy_profile = {"matches": matches, "program_rounds": sorted(known_rounds,
        key=lambda name: int(name.split()[-1])), "program_summary": program["summary"],
        "weight_summary": weight["summary"], "round_years": data.round_years,
        "weekday_convention": data.weekday_convention,
        "row_accounting": row_accounting}
    item.mapping = {**data.model_dump(mode="json"), "sheet": "teeltschema",
                    "profile": "legacy_paired", "program_sha256": companion.sha256,
                    "legacy_profile": legacy_profile}
    item.status = "preview_ready"
    db.add(HistoryReviewVersion(import_id=item.id, mapping=item.mapping,
        summary=item.summary,
        reasons=[{"row_number": p.row_number, "status": p.status, "reasons": p.reasons}
                 for p in previews if p.reasons], user_id=user.id))
    audit(db, user, "history.legacy_preview", item.id,
          {"weight_sha256": item.sha256, "program_sha256": companion.sha256,
           "summary": item.summary, "round_years": data.round_years})
    db.commit()
    return _detail(db, item)


def _bulk_matches(row: HistoryPreviewRow, match: BulkMatch) -> bool:
    if match.status and row.status != match.status:
        return False
    if match.reason_contains and not any(match.reason_contains.casefold() in str(reason).casefold()
                                         for reason in row.reasons):
        return False
    if match.group_index is not None and (row.original or {}).get("group_index") != match.group_index:
        return False
    if match.source_sheet and (row.source or {}).get("sheet", "").casefold() != match.source_sheet.casefold():
        return False
    if match.round_name and str((row.effective or {}).get("round_name") or "").casefold() != match.round_name.casefold():
        return False
    if match.variety and str((row.effective or {}).get("variety") or "").casefold() != match.variety.casefold():
        return False
    return True


@router.get("/api/history-imports/{import_id}/review-groups")
def history_review_groups(import_id: int, db: Session = Depends(get_db),
                          user: User = Depends(require_user)):
    from collections import Counter
    item = db.get(HistoryImport, import_id)
    if not item:
        raise HTTPException(404, "import niet gevonden")
    rows = db.scalars(select(HistoryPreviewRow).where(HistoryPreviewRow.import_id == import_id)).all()
    latest = db.scalar(select(HistoryReviewVersion.id).where(HistoryReviewVersion.import_id == import_id)
                       .order_by(HistoryReviewVersion.id.desc()).limit(1))
    return {"review_version_id": latest, "total_rows": len(rows),
            "statuses": dict(Counter(row.status for row in rows)),
            "reasons": [{"reason": reason, "count": count} for reason, count in
                        Counter(str(reason) for row in rows for reason in row.reasons).most_common(100)],
            "source_sheets": dict(Counter((row.source or {}).get("sheet") or "onbekend" for row in rows)),
            "groups": dict(Counter(str(row.original["group_index"]) for row in rows
                                   if row.original and row.original.get("group_index") is not None)),
            "rounds": dict(Counter(str(row.effective.get("round_name")) for row in rows
                                   if row.effective and row.effective.get("round_name"))),
            "varieties": dict(Counter(str(row.effective.get("variety")) for row in rows
                                      if row.effective and row.effective.get("variety")))}


@router.post("/api/history-imports/{import_id}/bulk-review")
def bulk_review(import_id: int, data: BulkReviewInput, db: Session = Depends(get_db),
                user: User = Depends(require_write)):
    """Apply one reasoned rule to selected staged rows, then rebuild the preview.

    The current review version prevents applying a selection from an obsolete
    preview. All source evidence and canonical validation run again.
    """
    from collections import Counter
    _begin_write_lock(db)
    item = db.get(HistoryImport, import_id)
    if not item:
        raise HTTPException(404, "import niet gevonden")
    if item.status != "preview_ready" or not item.mapping:
        raise HTTPException(409, "alleen een onbevestigd controlevoorbeeld kan worden bewerkt")
    latest = db.scalar(select(HistoryReviewVersion.id).where(HistoryReviewVersion.import_id == import_id)
                       .order_by(HistoryReviewVersion.id.desc()).limit(1))
    if latest != data.expected_review_version_id:
        raise HTTPException(409, "controlevoorbeeld is gewijzigd; laad de nieuwste versie")
    rows = db.scalars(select(HistoryPreviewRow).where(HistoryPreviewRow.import_id == import_id)
                      .order_by(HistoryPreviewRow.row_number)).all()
    by_number = {row.row_number: row for row in rows}
    if data.row_numbers is not None:
        missing = set(data.row_numbers) - set(by_number)
        if missing:
            raise HTTPException(422, "selectie bevat rijnummers die niet in het controlevoorbeeld staan")
        selected = [by_number[number] for number in data.row_numbers]
    else:
        selected = [row for row in rows if _bulk_matches(row, data.all_matching)]
    if not selected:
        raise HTTPException(422, "geen rijen voldoen aan deze selectie")
    legacy = item.mapping.get("profile") == "legacy_paired"
    if (data.accept_anchor_mismatch is not None or data.include_planning is not None or
            data.confirm_program_round_title is not None) and not legacy:
        raise HTTPException(422, "deze correctie hoort alleen bij gekoppelde historische werkboeken")
    previous = {override["row_number"]: RowOverride.model_validate(override)
                for override in item.mapping.get("overrides", [])}
    changed = {}
    skipped = Counter()
    for row in selected:
        if row.status == "duplicate":
            skipped["already_confirmed"] += 1
            continue
        before = previous.get(row.row_number)
        override = before.model_copy(deep=True) if before else RowOverride(row_number=row.row_number)
        touched = False
        for field, value in data.fields.items():
            current = (row.effective or {}).get(field)
            if current == value:
                continue
            if current not in (None, "") and not data.overwrite_existing:
                skipped["existing_value_preserved"] += 1
                continue
            override.fields[field] = value
            touched = True
        if data.exclude is not None and override.exclude != data.exclude:
            override.exclude = data.exclude
            touched = True
        if data.accept_anchor_mismatch is not None:
            if not (row.original or {}).get("anchor_mismatch"):
                skipped["no_anchor_mismatch"] += 1
            elif override.accept_anchor_mismatch != data.accept_anchor_mismatch:
                override.accept_anchor_mismatch = data.accept_anchor_mismatch
                touched = True
        if data.include_planning is not None:
            if not any("planning-only niet geselecteerd" in str(reason) for reason in row.reasons) and \
                    not (before and before.include_planning):
                skipped["not_planning_only"] += 1
            elif override.include_planning != data.include_planning:
                override.include_planning = data.include_planning
                touched = True
        if data.confirm_program_round_title is not None:
            if not (row.original or {}).get("round_label_conflict") and not any(
                    "round_label_conflict" in str(reason) for reason in
                    (row.original or {}).get("program_review_reasons", [])):
                skipped["no_round_title_conflict"] += 1
            elif override.confirm_program_round_title != data.confirm_program_round_title:
                override.confirm_program_round_title = data.confirm_program_round_title
                touched = True
        if not touched:
            skipped["no_change"] += 1
            continue
        note = data.reason.strip()
        override.reason = (before.reason.strip() + " | " + note) if before and before.reason.strip() else note
        if len(override.reason) > 2000:
            raise HTTPException(422, "samengevoegde correctiereden wordt te lang")
        changed[row.row_number] = RowOverride.model_validate(override.model_dump(mode="json"))
    result = {"matched_rows": len(selected), "changed_rows": len(changed),
              "row_numbers": sorted(changed), "sample_row_numbers": sorted(changed)[:30],
              "skipped": dict(skipped), "review_version_id": latest}
    if data.dry_run:
        db.rollback()
        return {"bulk_review": result}
    if not changed:
        raise HTTPException(409, "geen rijen hoeven te worden gewijzigd")
    previous.update(changed)
    mapping = {name: item.mapping[name] for name in
               (LegacyPreviewInput.model_fields if legacy else MappingInput.model_fields)
               if name in item.mapping}
    mapping["overrides"] = [override.model_dump(mode="json") for override in
                            sorted(previous.values(), key=lambda value: value.row_number)]
    try:
        db.info["_history_bulk_lock"] = True
        if legacy:
            detail = legacy_preview(import_id, LegacyPreviewInput.model_validate(mapping), db, user)
        else:
            detail = map_history(import_id, MappingInput.model_validate(mapping), db, user)
    finally:
        db.info.pop("_history_bulk_lock", None)
    detail["bulk_review"] = {**result, "review_version_id": detail["reviews"][-1]["id"]}
    return detail


@router.post("/api/history-imports/{import_id}/new-review")
def new_history_review(import_id: int, db: Session = Depends(get_db),
                       user: User = Depends(require_write)):
    """Reopen the same immutable evidence in a new review, preserving the old import.

    Previously confirmed source rows are recognized as duplicates by the usual
    preview logic. Previously excluded rows may be reviewed without another
    upload or any duplicate crop creation.
    """
    _begin_write_lock(db)
    original = db.get(HistoryImport, import_id)
    if not original:
        raise HTTPException(404, "import niet gevonden")
    if original.status != "confirmed" or not original.mapping:
        raise HTTPException(409, "alleen een bevestigde import kan een nieuwe controle krijgen")
    legacy = original.mapping.get("profile") == "legacy_paired"
    mapping = {name: original.mapping[name] for name in
               (LegacyPreviewInput.model_fields if legacy else MappingInput.model_fields)
               if name in original.mapping}
    reopened = HistoryImport(filename=original.filename, sha256=original.sha256,
                             raw_path=original.raw_path, profile=original.profile,
                             status="uploaded", user_id=user.id)
    db.add(reopened)
    db.flush()
    try:
        db.info["_history_bulk_lock"] = True
        audit(db, user, "history.new_review", reopened.id,
              {"source_import_id": original.id, "sha256": original.sha256})
        if legacy:
            detail = legacy_preview(reopened.id, LegacyPreviewInput.model_validate(mapping), db, user)
        else:
            detail = map_history(reopened.id, MappingInput.model_validate(mapping), db, user)
    except Exception:
        db.rollback()
        raise
    finally:
        db.info.pop("_history_bulk_lock", None)
    detail["new_review"] = {"cloned_from_import_id": original.id}
    return detail


@router.post("/api/history-imports/{import_id}/confirm")
def confirm_history(import_id: int, data: ConfirmInput, db: Session = Depends(get_db),
                    user: User = Depends(require_write)):
    if not data.confirmed:
        raise HTTPException(422, "expliciete bevestiging vereist")
    _begin_write_lock(db)
    item = db.get(HistoryImport, import_id)
    if not item:
        raise HTTPException(404, "import niet gevonden")
    if item.status == "confirmed":
        rows = db.scalars(select(HistoryPreviewRow).where(HistoryPreviewRow.import_id == item.id)).all()
        return {"id": item.id, "status": "confirmed",
                "created_ids": [cid for row in rows if row.status == "imported" for cid in row.created_ids],
                "skipped_duplicate_rows": [row.row_number for row in rows if row.status == "duplicate"]}
    if item.status != "preview_ready" or not item.mapping:
        raise HTTPException(409, "controlevoorbeeld ontbreekt")
    rows = db.scalars(select(HistoryPreviewRow).where(HistoryPreviewRow.import_id == item.id)
                      .order_by(HistoryPreviewRow.row_number)).all()
    if any(row.status == "ambiguous" for row in rows):
        raise HTTPException(409, "los ambigue rijen eerst op of sluit ze met reden uit")
    created_ids = []
    skipped = []
    varieties = {v.id for v in db.scalars(select(Variety)).all()}
    try:
        for row in rows:
            if row.status == "excluded":
                continue
            row_source = row.source or {"sha256": item.sha256, "sheet": item.mapping["sheet"],
                                        "row_number": row.row_number}
            prior = db.scalar(select(HistorySourceRow).where(
                HistorySourceRow.sha256 == row_source["sha256"],
                HistorySourceRow.sheet == row_source["sheet"],
                HistorySourceRow.row_number == row_source["row_number"]))
            if prior:
                if prior.payload_hash != row.payload_hash:
                    raise HTTPException(409, f"bronrij {row.row_number} heeft andere eerdere koppeling")
                row.status = "duplicate"
                skipped.append(row.row_number)
                continue
            if row.status not in ("valid", "duplicate"):
                raise HTTPException(409, f"bronrij {row.row_number} is niet bevestigd")
            ids = []
            for value in row.cultivations:
                if value["variety_id"] not in varieties or _existing_cultivation(db, value):
                    raise HTTPException(409, f"ronde/kap/soort/plantdatum uit rij {row.row_number} bestaat al of soort ontbreekt")
                if value.get("status") == "planned":
                    variety = db.get(Variety, value["variety_id"])
                    if not variety or not variety.active:
                        raise HTTPException(409, f"niet-actieve soort in geplande rij {row.row_number}")
                    occupied = db.scalar(select(func.coalesce(func.sum(Cultivation.beds), 0)).where(
                        Cultivation.round_name == value["round_name"],
                        Cultivation.bay_number == value["bay_number"],
                        Cultivation.status.in_(("planned", "active"))))
                    if occupied + value["beds"] > 6:
                        raise HTTPException(409, f"meer dan zes geplande bedden in rij {row.row_number}")
                c = Cultivation(round_name=value["round_name"], bay_number=value["bay_number"],
                    variety_id=value["variety_id"], beds=value["beds"], category="standard",
                    status=value.get("status", "historical"), label_quality=value["label_quality"])
                db.add(c); db.flush()
                db.add(CultivationRevision(cultivation_id=c.id, category=c.category,
                    status=c.status, label_quality=c.label_quality))
                db.add(PlanningVersion(cultivation_id=c.id, plant_date=date.fromisoformat(value["plant_date"]),
                    light_out_date=date.fromisoformat(value["light_out_date"]) if value.get("light_out_date") else None,
                    expected_harvest_date=date.fromisoformat(value.get("expected_harvest_date") or value["harvest_end_date"])
                        if value.get("expected_harvest_date") or value.get("harvest_end_date") else None,
                    density=value["density"], planned_stems=value["planned_stems"], author_id=user.id,
                    reason=f"historische Excel-import {item.id}, rij {row.row_number}"))
                if value.get("create_harvest", True):
                    db.add(Harvest(cultivation_id=c.id,
                        start_date=date.fromisoformat(value["harvest_start_date"]) if value["harvest_start_date"] else None,
                        end_date=date.fromisoformat(value["harvest_end_date"]), weight_g=value["weight_g"],
                        quality_classes=value["quality_classes"], stem_count=value["stem_count"],
                        loss_fraction=value["loss_fraction"],
                        notes=f"Historische Excel-bron SHA256 {row_source['sha256']}, {row_source['sheet']} rij {row_source['row_number']}"))
                ids.append(c.id); created_ids.append(c.id)
            db.add(HistorySourceRow(sha256=row_source["sha256"], sheet=row_source["sheet"],
                row_number=row_source["row_number"], payload_hash=row.payload_hash, cultivation_ids=ids,
                whole_cap_values=row.cultivations[0]["whole_cap_values"] if row.cultivations else None))
            row.status = "imported"
            row.created_ids = ids
        item.status = "confirmed"
        item.confirmed_at = utcnow()
        audit(db, user, "history.confirm", item.id, {"sha256": item.sha256,
            "created_ids": created_ids, "skipped_duplicate_rows": skipped})
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "historische bronrij of kapteelt is gelijktijdig aangemaakt") from exc
    except HTTPException:
        db.rollback()
        raise
    return {"id": item.id, "status": "confirmed", "created_ids": created_ids,
            "skipped_duplicate_rows": skipped}

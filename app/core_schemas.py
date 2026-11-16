"""Invoerschema's voor de kernroutes (teelten, planning, metingen, oogst, beheer)."""

import math
from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

NL = ZoneInfo("Europe/Amsterdam")


def contains_nonfinite(value):
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(contains_nonfinite(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_nonfinite(v) for v in value)
    return False


class CoreSchema(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def reject_nonfinite(cls, value):
        if contains_nonfinite(value):
            raise HTTPException(422, "Getallen moeten eindig zijn")
        return value


class VarietyIn(CoreSchema):
    name: str = Field(min_length=1, max_length=100)
    target_weight_g: float = Field(default=105, gt=0, le=1000)
    active: bool = True


class CultivationIn(CoreSchema):
    round_name: str = Field(min_length=1, max_length=100)
    bay_number: int = Field(ge=1, le=40)
    variety_id: int
    beds: int = Field(ge=1, le=6)
    category: str = "standard"
    status: str = "planned"
    label_quality: str = "verified"

    @model_validator(mode="after")
    def validate_values(self):
        if (
            self.category not in ("standard", "adjusted", "control")
            or self.status not in ("planned", "active", "harvested", "historical")
            or self.label_quality not in ("verified", "historical_assigned", "ambiguous", "unknown")
        ):
            raise ValueError("Ongeldige status, categorie of labelkwaliteit")
        if not self.round_name.strip():
            raise ValueError("Ronde is verplicht")
        return self


class PlanningIn(CoreSchema):
    plant_date: date | None = None
    light_out_date: date | None = None
    expected_harvest_date: date | None = None
    density: float | None = Field(default=None, gt=0, le=1000)
    planned_stems: int | None = Field(default=None, ge=0)
    interlight_description: str | None = Field(default=None, max_length=2000)
    lighting_schedule: str | None = Field(default=None, max_length=2000)
    planned_lamp_hours: float | None = Field(default=None, ge=0, le=24)
    reason: str | None = None

    @model_validator(mode="after")
    def order(self):
        if self.plant_date and self.light_out_date and self.light_out_date < self.plant_date:
            raise ValueError("Licht uit ligt vóór planten")
        if self.light_out_date and self.expected_harvest_date and self.expected_harvest_date < self.light_out_date:
            raise ValueError("Oogst ligt vóór licht uit")
        return self


class StemIn(CoreSchema):
    weight_g: float = Field(gt=0, le=2000)
    length_cm: float = Field(gt=0, le=500)
    position: str = Field(min_length=1, max_length=100)
    excluded: bool = False
    exclusion_reason: str | None = None

    @model_validator(mode="after")
    def reason_required(self):
        if self.excluded and not (self.exclusion_reason or "").strip():
            raise ValueError("Reden voor uitsluiting is verplicht")
        return self


class MeasurementIn(CoreSchema):
    client_id: UUID
    cultivation_id: int
    kind: str
    measured_at: datetime
    location: str | None = None
    notes: str | None = None
    stems: list[StemIn] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def valid(self):
        if self.kind not in ("KD10", "KD30", "pilot"):
            raise ValueError("Onbekend meettype")
        if len({s.position.strip().casefold() for s in self.stems}) != 5:
            raise ValueError("Geef vijf verschillende meetplekken op")
        return self


class HarvestIn(CoreSchema):
    start_date: date | None = None
    end_date: date | None = None
    weight_g: float | None = Field(default=None, gt=0, le=2000)
    stem_count: int | None = Field(default=None, ge=0)
    loss_fraction: float | None = Field(default=None, ge=0, le=1)
    quality_classes: list[dict] | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def valid(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("Einddatum ligt vóór begindatum")
        today = datetime.now(NL).date()
        if (self.start_date and self.start_date > today) or (self.end_date and self.end_date > today):
            raise ValueError("Werkelijke oogstdata mogen niet in de toekomst liggen")
        if self.quality_classes and len(self.quality_classes) > 1 and self.weight_g is not None:
            raise ValueError("Bewaar meerdere gewichtsklassen afzonderlijk, zonder gemiddeld eindgewicht")
        return self


class HarvestCorrection(CoreSchema):
    """Alleen opgegeven velden wijzigen; reden is verplicht."""

    start_date: date | None = None
    end_date: date | None = None
    weight_g: float | None = Field(default=None, gt=0, le=2000)
    stem_count: int | None = Field(default=None, ge=0)
    loss_fraction: float | None = Field(default=None, ge=0, le=1)
    quality_classes: list[dict] | None = None
    notes: str | None = None
    reason: str = Field(min_length=3, max_length=2000)

    @model_validator(mode="after")
    def reason_required(self):
        if not self.reason.strip():
            raise ValueError("Reden voor correctie is verplicht")
        return self


class EventIn(CoreSchema):
    occurred_at: datetime
    kind: str = Field(min_length=1, max_length=100)
    amount: float | None = Field(default=None, allow_inf_nan=False)
    unit: str | None = Field(default=None, max_length=50)
    scope: str | None = Field(default=None, max_length=100)
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def valid(self):
        if not self.kind.strip():
            raise ValueError("Gebeurtenis is verplicht")
        if self.amount is not None and not (self.unit or "").strip():
            raise ValueError("Eenheid is verplicht bij hoeveelheid")
        return self


class MappingIn(CoreSchema):
    bay_number: int = Field(ge=1, le=40)
    object_type: str
    object_number: int = Field(ge=1)
    valid_from: date
    valid_until: date | None = None
    reason: str = Field(min_length=1)
    replaces_id: int | None = None

    @model_validator(mode="after")
    def valid(self):
        if (
            self.object_type not in ("department", "valve", "light")
            or self.valid_until
            and self.valid_until < self.valid_from
        ):
            raise ValueError("Ongeldige koppeling of periode")
        if self.object_type == "department" and self.object_number not in (1, 2, 3, 4):
            raise ValueError("Afdeling moet 1 t/m 4 zijn")
        return self


class UserIn(CoreSchema):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=256)
    role: str

    @model_validator(mode="after")
    def valid(self):
        if self.role not in ("admin", "medewerker", "lezer") or not self.username.strip():
            raise ValueError("Ongeldige rol of naam")
        return self


class UserUpdate(CoreSchema):
    role: str | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)
    mfa_reset: bool = False

    @model_validator(mode="after")
    def valid(self):
        if self.role is not None and self.role not in ("admin", "medewerker", "lezer"):
            raise ValueError("Ongeldige rol")
        return self


class VarietyUpdate(CoreSchema):
    target_weight_g: float | None = Field(default=None, gt=0, le=1000)
    active: bool | None = None


class BayUpdate(CoreSchema):
    area_m2: float | None = Field(default=None, gt=0)


class CultivationUpdate(CoreSchema):
    category: str | None = None
    status: str | None = None
    label_quality: str | None = None

    @model_validator(mode="after")
    def valid(self):
        if self.category is not None and self.category not in ("standard", "adjusted", "control"):
            raise ValueError("Ongeldige categorie")
        if self.status is not None and self.status not in ("planned", "active", "harvested", "historical"):
            raise ValueError("Ongeldige status")
        if self.label_quality is not None and self.label_quality not in (
            "verified",
            "historical_assigned",
            "ambiguous",
            "unknown",
        ):
            raise ValueError("Ongeldige labelkwaliteit")
        return self

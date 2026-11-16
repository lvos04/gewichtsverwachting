"""Small, unit-explicit cultivation calculations.

Unknown radiation units and unidentified screens deliberately have no DLI value.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from math import isfinite, acos, cos, pi, radians, sin, tan
from statistics import mean, stdev
from zoneinfo import ZoneInfo

NL = ZoneInfo("Europe/Amsterdam")
UTC = ZoneInfo("UTC")


def _date(value: date | datetime) -> date:
    return local_date(value)


def local_date(value: date | datetime) -> date:
    if not isinstance(value, datetime):
        return value
    return value.replace(tzinfo=UTC).astimezone(NL).date() if value.tzinfo is None else value.astimezone(NL).date()


def ld_days(plant_date: date, light_out_date: date) -> int:
    days = (_date(light_out_date) - _date(plant_date)).days
    if days < 0:
        raise ValueError("licht uit kan niet voor de plantdatum liggen")
    return days


def kd_day(light_out_date: date, measured_at: date | datetime) -> int:
    return (_date(measured_at) - _date(light_out_date)).days


def reaction_days(light_out_date: date, harvest_start: date) -> int:
    return (_date(harvest_start) - _date(light_out_date)).days


def stem_summary(stems: list[object]) -> dict:
    included = [s for s in stems if not s.excluded]
    if not included:
        raise ValueError("geen bruikbare takken")
    weights = [float(s.weight_g) for s in included]
    lengths = [float(s.length_cm) for s in included]
    return {"n": len(included), "weight_g": mean(weights),
            "length_cm": mean(lengths), "weight_sd_g": stdev(weights) if len(weights) > 1 else 0.0,
            "length_sd_cm": stdev(lengths) if len(lengths) > 1 else 0.0}


def growth_g_per_kd_day(first_weight_g: float, second_weight_g: float,
                        first_kd_day: int, second_kd_day: int) -> float:
    elapsed = second_kd_day - first_kd_day
    if elapsed <= 0:
        raise ValueError("tweede meting moet na de eerste liggen")
    return (second_weight_g - first_weight_g) / elapsed


def lamp_dli(groups: list[dict], *, configuration_validated: bool) -> dict:
    """Each group is an independently verified photon contribution.

    `ppfd_umol_m2_s` must be actual output; `hours` and `dim_fraction` are
    independently validated. A report PAR that already combines groups must
    not be passed once per module.
    """
    if not configuration_validated or not groups:
        return {"mol_m2_day": None, "quality": "unknown", "reason": "lampconfiguratie niet gevalideerd"}
    total = 0.0
    ids = set()
    for group in groups:
        ident = group.get("id")
        if not ident or ident in ids or group.get("source_validated") is not True:
            return {"mol_m2_day": None, "quality": "unknown", "reason": "lampbron of kanaal niet gevalideerd"}
        ids.add(ident)
        ppfd, hours, dim = (group.get(k) for k in ("ppfd_umol_m2_s", "hours", "dim_fraction"))
        if any(x is None or not isfinite(float(x)) for x in (ppfd, hours, dim)) or not (0 <= float(hours) <= 24 and 0 <= float(dim) <= 1 and float(ppfd) >= 0):
            raise ValueError("ongeldige lampwaarden")
        total += float(ppfd) * float(hours) * 3600 * float(dim) / 1_000_000
    return {"mol_m2_day": total, "quality": "verified", "reason": None}


def natural_dli(radiation: float | None, *, unit: str | None,
                transmission: float | None, screen_fraction: float | None = None,
                synchronized_screen: bool = False) -> dict:
    """Daily aggregate is insufficient for screen correction.

    J/cm² conversion is approximate even without a screen. A synchronized
    radiation/screen series should be integrated upstream, not supplied here.
    """
    if radiation is None or unit != "J/cm2" or transmission is None:
        return {"mol_m2_day": None, "quality": "unknown", "reason": "stralingseenheid of transmissie onbekend"}
    if not isfinite(radiation) or radiation < 0 or not (0 <= transmission <= 1):
        raise ValueError("ongeldige straling of transmissie")
    if screen_fraction is not None:
        if not (0 <= screen_fraction <= 1):
            raise ValueError("ongeldige schermfractie")
        return {"mol_m2_day": None, "quality": "unknown", "reason": "dagtotaal en schermuren kunnen niet veilig worden gecombineerd"}
    return {"mol_m2_day": radiation * 0.0215 * transmission,
            "quality": "estimate", "reason": "benadering op dagtotaal zonder schermcorrectie"}


def phase_for_day(day: date, plant: date, light_out: date,
                  first: date | None, second: date | None, harvest: date | None) -> str | None:
    if day < plant or (harvest and day >= harvest):
        return None
    if day < light_out:
        return "LD"
    if first is None or day < first:
        return "KD_tot_M1"
    if second is None or day < second:
        return "M1_tot_M2"
    return "M2_tot_oogst"


def alar_summary(events: list[object]) -> dict:
    field = sum(float(e.amount) for e in events if e.kind == "alar" and e.scope == "volvelds" and e.unit == "g/1000m2" and e.amount is not None)
    selective = [float(e.amount) for e in events if e.kind == "alar" and e.scope == "selectief" and e.unit == "g/10l" and e.amount is not None]
    return {"volvelds_g_per_1000m2": field,
            "selectief_treatments": len(selective), "selectief_concentrations_g_per_10l": selective}


def loss_fraction(harvested_stems: int, planted_stems: int) -> float:
    if planted_stems <= 0 or not 0 <= harvested_stems <= planted_stems:
        raise ValueError("ongeldig aantal takken")
    return 1 - harvested_stems / planted_stems


def astronomical_daylength(day: date, latitude_deg: float = 52.0) -> float:
    """Approximate astronomical daylight hours; never greenhouse DLI.

    The default latitude is an explicit Netherlands assumption, not a site
    coordinate. Calculation uses the date, including leap-year day number.
    """
    if not -66 <= latitude_deg <= 66:
        raise ValueError("breedtegraad buiten ondersteund bereik")
    year_days = 366 if date(day.year, 12, 31).timetuple().tm_yday == 366 else 365
    n = day.timetuple().tm_yday
    declination = radians(23.44) * sin(2 * pi * (n - 80) / year_days)
    cosine = -tan(radians(latitude_deg)) * tan(declination)
    return 24 * acos(max(-1, min(1, cosine))) / pi


def season_context(as_of: date, expected_harvest: date | None = None,
                   latitude_deg: float = 52.0) -> dict:
    today = astronomical_daylength(as_of, latitude_deg)
    trend = (astronomical_daylength(as_of + timedelta(days=1), latitude_deg) -
             astronomical_daylength(as_of - timedelta(days=1), latitude_deg)) / 2
    direction = "longer" if trend > .002 else "shorter" if trend < -.002 else "turning"
    remainder = None
    if expected_harvest and expected_harvest > as_of:
        days = (expected_harvest - as_of).days
        # A planning error must not allocate an unbounded series.
        days = min(days, 366)
        lengths = [astronomical_daylength(as_of + timedelta(days=i), latitude_deg) for i in range(1, days + 1)]
        remainder = {"days": days, "mean_astronomical_daylength_hours": mean(lengths),
                     "end_daylength_hours": lengths[-1], "direction":
                     "longer" if lengths[-1] > today else "shorter" if lengths[-1] < today else "turning",
                     "scenarios": [
                         {"name": "dark", "weather_assumption": "onder seizoensgemiddelde", "forecast_weight_g": None},
                         {"name": "average", "weather_assumption": "seizoensgemiddelde", "forecast_weight_g": None},
                         {"name": "light", "weather_assumption": "boven seizoensgemiddelde", "forecast_weight_g": None}],
                     "note": "Daglengte is astronomie, geen gemeten kas-DLI of gewichteffect"}
    return {"as_of_date": as_of.isoformat(), "latitude_deg": latitude_deg,
            "daylength_hours": today, "trend_hours_per_day": trend,
            "trend": direction, "expected_remainder": remainder,
            "quality": "astronomical_estimate_not_greenhouse_DLI"}

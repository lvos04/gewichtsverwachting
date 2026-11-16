"""Priva Bridge als optionele bron voor klimaat- en lichtdagwaarden (standaard uit).

De gewichtsapp is de eerste afnemer van de Priva Bridge (implementatieplan v1.1, sectie 2
en 8). Deze module haalt dagwaarden server-side op (nooit vanuit de browser), bewaart ze met
herkomst in een eigen cache en levert ze aan de bestaande klimaat- en lichtberekeningen.

Uitgangspunten:

* Standaard uit. Met de schakelaar uit wordt de Bridge nooit aangesproken en veranderen
  klimaat-, licht- en modelberekeningen niet.
* Alleen intern netwerk: de Bridge-URL moet een privé-IP of interne hostnaam zijn en moet bij
  elk verzoek naar een privé-adres wijzen. Geen omleidingen, geen proxy uit de omgeving.
* Het API-token wordt versleuteld opgeslagen en nooit naar de webinterface teruggestuurd.
* Ontbreekt is nooit 0: een regel met ``quality = ontbreekt`` of zonder waarde wordt als
  ontbrekend bewaard en nooit als meting gebruikt.
* Elke ontvangen afwijking wordt als nieuwe cacheregel bewaard (append-only), zodat een
  berekening op een eerder moment reproduceerbaar blijft (``fetched_at <= peildatum``).
* De bestaande CSV-import blijft als terugval werken; voorrang is instelbaar.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import secrets
import socket
import ssl
import threading
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from . import __version__
from .auth import audit, require_admin, require_user
from .db import DATA_DIR, Base, get_db
from .models import Bay, User, utcnow

log = logging.getLogger("chrysant.bridge")
router = APIRouter(tags=["Priva Bridge"])
NL = ZoneInfo("Europe/Amsterdam")

API = "/api/v1"
QUALITIES = ("ok", "verdacht", "onvolledig", "ontbreekt")
VOORRANG = ("bridge_ok_verdacht", "bridge_alleen_ok", "eigen_import_eerst")
VOORRANG_LABELS = {
    "bridge_ok_verdacht": "Bridge wint bij kwaliteit ok of verdacht; anders de eigen import",
    "bridge_alleen_ok": "Bridge wint alleen bij kwaliteit ok; anders de eigen import",
    "eigen_import_eerst": "Eigen import wint; de Bridge vult alleen ontbrekende dagen aan (ok of verdacht)",
}
# Meetwaarden uit de catalogus (plan sectie 5) die de app gebruikt. De eerste drie voeden
# dezelfde kenmerken als de eigen CSV-import; de rest wordt alleen getoond.
MODEL_METRICS = {
    "lichtsom_lampen_kap": (
        "lamp_light",
        {"mol/m²/dag", "mol/m2/dag", "mol/m²/d", "mol/m2/d", "mol/m²", "mol/m2", "mol m-2 d-1"},
        0.0,
        100.0,
    ),
    "temp_etmaal_avg": ("temperature", {"°C", "C", "degC"}, -40.0, 65.0),
    "rv_avg": ("rh", {"%"}, 0.0, 100.0),
}
METRIC_BY_FEATURE = {feature: key for key, (feature, *_rest) in MODEL_METRICS.items()}
DISPLAY_METRICS = (
    "lamp_brandseconden",
    "scherm_seconden",
    "temp_etmaal_min",
    "temp_etmaal_max",
    "vochtdeficit_avg",
    "par_som_lampen",
)
DISPLAY_PREFIXES = ("scherm_seconden",)
WANTED_METRICS = tuple(MODEL_METRICS) + DISPLAY_METRICS
PAGE_LIMIT = 5000
SCOPE_CHUNK = 10
MAX_PAGES = 2000
MAX_RETRIES = 3
MAX_RETRY_WAIT = 60
FAILURE_BACKOFF = timedelta(minutes=30)
MAX_PERIOD_DAYS = 3660
MAX_CACHE_READ = 200_000
INTERNAL_SUFFIXES = (".local", ".lan", ".internal", ".intranet", ".home.arpa", ".localdomain", ".home", ".corp")

# Testhaken: tests vervangen het transport (httpx.MockTransport), de wachtfunctie en de
# naamresolutie. In productie blijven ze None/standaard.
_transport: httpx.BaseTransport | None = None
_sleep = time.sleep
_lock = threading.Lock()
_worker: threading.Thread | None = None


def _now_local() -> datetime:
    return datetime.now(NL)


# --- Opslag ---------------------------------------------------------------------------------


class BridgeSync(Base):
    """Eén synchronisatie of verbindingstest; zichtbaar in Beheer."""

    __tablename__ = "bridge_syncs"
    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="bezig")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_from: Mapped[str | None] = mapped_column(String(10), nullable=True)
    period_to: Mapped[str | None] = mapped_column(String(10), nullable=True)
    pages: Mapped[int] = mapped_column(Integer, default=0)
    rows_received: Mapped[int] = mapped_column(Integer, default=0)
    rows_stored: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class BridgeDailyValue(Base):
    """Append-only cache van /api/v1/daily-regels met herkomst."""

    __tablename__ = "bridge_daily_values"
    __table_args__ = (Index("ix_bridge_daily_lookup", "scope", "local_date", "metric"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sync_id: Mapped[int] = mapped_column(ForeignKey("bridge_syncs.id"), index=True)
    scope: Mapped[str] = mapped_column(String(40))
    local_date: Mapped[str] = mapped_column(String(10))
    metric: Mapped[str] = mapped_column(String(80))
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    aggregation: Mapped[str | None] = mapped_column(String(20), nullable=True)
    coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality: Mapped[str] = mapped_column(String(20))
    resolved_from: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    import_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class BridgeCredential(Base):
    """Het API-token, versleuteld. Wordt nooit via de API teruggegeven."""

    __tablename__ = "bridge_credentials"
    id: Mapped[int] = mapped_column(primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    set_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    set_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


# --- Tokenversleuteling ---------------------------------------------------------------------
# Alleen standaardbibliotheek: HMAC-SHA256 als sleutelstroom (tellermodus) met een willekeurige
# nonce, daarna encrypt-then-MAC. De sleutel komt uit BRIDGE_TOKEN_KEY of uit een sleutelbestand
# in DATA_DIR/secrets. Die map gaat bewust niet mee in back-ups: een back-up bevat dan geen
# bruikbaar token, en na herstel op een andere machine voer je het token opnieuw in.

KEY_DIR = DATA_DIR / "secrets"


def _master_key() -> bytes:
    configured = os.environ.get("BRIDGE_TOKEN_KEY", "")
    if configured:
        return hashlib.sha256(configured.encode()).digest()
    path = KEY_DIR / "bridge-token.key"
    if not path.exists():
        KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(secrets.token_bytes(32))
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("Sleutelbestand voor het Bridge-token is ongeldig")
    return key


def _subkeys(key: bytes) -> tuple[bytes, bytes]:
    return hmac.new(key, b"bridge-token-enc", hashlib.sha256).digest(), hmac.new(
        key, b"bridge-token-mac", hashlib.sha256
    ).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks = (length + 31) // 32
    return b"".join(hmac.new(key, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest() for i in range(blocks))[
        :length
    ]


def encrypt_token(token: str) -> str:
    enc, mac = _subkeys(_master_key())
    nonce = secrets.token_bytes(16)
    data = token.encode()
    cipher = bytes(a ^ b for a, b in zip(data, _keystream(enc, nonce, len(data))))
    tag = hmac.new(mac, b"v1" + nonce + cipher, hashlib.sha256).digest()
    return "v1:" + base64.urlsafe_b64encode(nonce + cipher + tag).decode()


def decrypt_token(stored: str) -> str | None:
    try:
        if not stored.startswith("v1:"):
            return None
        raw = base64.urlsafe_b64decode(stored[3:].encode())
        nonce, cipher, tag = raw[:16], raw[16:-32], raw[-32:]
        enc, mac = _subkeys(_master_key())
        if not hmac.compare_digest(tag, hmac.new(mac, b"v1" + nonce + cipher, hashlib.sha256).digest()):
            return None
        return bytes(a ^ b for a, b in zip(cipher, _keystream(enc, nonce, len(cipher)))).decode()
    except (ValueError, OSError, UnicodeDecodeError):
        return None


def load_token(db: Session) -> str | None:
    row = db.get(BridgeCredential, 1)
    return decrypt_token(row.ciphertext) if row else None


# --- Adrescontrole --------------------------------------------------------------------------


def _internal_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return (address.is_private or address.is_loopback or address.is_link_local) and not (
        address.is_unspecified or address.is_multicast
    )


def validate_url(url: str) -> str:
    """Leeg of een http(s)-adres op het interne netwerk; geeft de genormaliseerde basis-URL."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("Bridge-URL moet met http:// of https:// beginnen")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Bridge-URL zonder gebruikersnaam, wachtwoord, query of #-deel invullen")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("Bridge-URL mist een hostnaam")
    try:
        parts.port
    except ValueError as error:
        raise ValueError("Ongeldige poort in de Bridge-URL") from error
    if not _internal_ip(host):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if "." in host and not host.endswith(INTERNAL_SUFFIXES):
                raise ValueError(
                    "Alleen het interne netwerk: gebruik een privé-IP-adres (bijv. 192.168.1.20) of een interne "
                    "naam (zonder punt, of eindigend op .lan, .local, .internal of .home.arpa)"
                ) from None
        else:
            raise ValueError("Alleen het interne netwerk: dit IP-adres is geen privé-adres") from None
    path = parts.path.rstrip("/")
    if path.endswith(API):
        path = path[: -len(API)]
    netloc = parts.netloc.lower()
    return f"{parts.scheme}://{netloc}{path}"


def validate_ca(pem: str) -> str:
    pem = (pem or "").strip()
    if not pem:
        return ""
    if "-----BEGIN CERTIFICATE-----" not in pem or "PRIVATE KEY" in pem:
        raise ValueError("Eigen CA: plak alleen het openbare CA-certificaat (PEM, BEGIN CERTIFICATE)")
    try:
        ssl.create_default_context(cadata=pem)
    except (ssl.SSLError, ValueError) as error:
        raise ValueError("Eigen CA-certificaat kan niet worden gelezen") from error
    return pem


def _resolve(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def check_resolves_internal(base_url: str) -> None:
    host = urlsplit(base_url).hostname or ""
    if _internal_ip(host):
        return
    try:
        addresses = _resolve(host)
    except OSError as error:
        raise BridgeFout("onbereikbaar", f"Hostnaam {host} van de Bridge is niet te vinden") from error
    if not addresses or not all(_internal_ip(a) for a in addresses):
        raise BridgeFout("geen_intern_adres", f"Hostnaam {host} wijst niet (alleen) naar het interne netwerk")


# --- HTTP-client volgens het contract (sectie 8) --------------------------------------------


class BridgeFout(Exception):
    def __init__(self, code: str, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


STATUS_CODES = {
    400: ("verzoek_afgewezen", "De Bridge wees het verzoek af (400)"),
    401: ("token_ongeldig", "Token geweigerd (401). Controleer of het API-token klopt en niet is ingetrokken"),
    403: ("geen_toegang", "Het token heeft geen leesrecht (scope read:data ontbreekt, 403)"),
    404: ("niet_gevonden", "Endpoint niet gevonden (404). Controleer de Bridge-URL en de API-versie"),
    422: ("verzoek_afgewezen", "De Bridge wees de parameters af (422)"),
    429: ("te_veel_verzoeken", "De Bridge beperkt het aantal verzoeken (429)"),
}


def _clean(text, limit=300) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(text or ""))
    return text[:limit]


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())


def _problem(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    parts = [_clean(body.get(k), 200) for k in ("title", "detail") if body.get(k)]
    return " — ".join(parts)


def _client(base_url: str, token: str, ca_pem: str = "") -> httpx.Client:
    verify: ssl.SSLContext | bool = True
    if ca_pem:
        verify = ssl.create_default_context(cadata=ca_pem)
    return httpx.Client(
        base_url=base_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": f"gewichtsverwachting/{__version__}",
        },
        timeout=httpx.Timeout(30.0, connect=5.0),
        verify=verify,
        follow_redirects=False,
        trust_env=False,
        transport=_transport,
    )


def _get(client: httpx.Client, path: str, params=None) -> dict:
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.get(API + path, params=params)
        except httpx.TimeoutException as error:
            raise BridgeFout("timeout", "De Bridge reageerde niet binnen de tijdslimiet") from error
        except httpx.HTTPError as error:
            raise BridgeFout("onbereikbaar", f"Bridge niet bereikbaar ({type(error).__name__})") from error
        status = response.status_code
        if status == 429:
            wait = parse_retry_after(response.headers.get("Retry-After"))
            if attempt < MAX_RETRIES and wait is not None and wait <= MAX_RETRY_WAIT:
                _sleep(wait)
                continue
            code, text = STATUS_CODES[429]
            suffix = f"; opnieuw na {int(wait)} s" if wait is not None else ""
            raise BridgeFout(code, text + suffix, status, wait)
        if 300 <= status < 400:
            raise BridgeFout("doorverwijzing", f"De Bridge stuurt door ({status}); vul de juiste URL in", status)
        if status >= 400:
            code, text = STATUS_CODES.get(status, ("bridge_fout", f"De Bridge gaf een fout ({status})"))
            problem = _problem(response)
            raise BridgeFout(code, text + (f": {problem}" if problem else ""), status)
        try:
            body = response.json()
        except ValueError as error:
            raise BridgeFout("ongeldig_antwoord", "De Bridge gaf geen geldige JSON terug", status) from error
        if not isinstance(body, dict):
            raise BridgeFout("ongeldig_antwoord", "Onverwacht antwoord van de Bridge (geen JSON-object)", status)
        return body
    raise BridgeFout("te_veel_verzoeken", STATUS_CODES[429][1])  # pragma: no cover


def _config(db: Session):
    from .settings import effective_settings

    return effective_settings(db)


def _open(db: Session, config):
    if not config.bridge_url:
        raise BridgeFout("niet_ingesteld", "Vul eerst de Bridge-URL in")
    token = load_token(db)
    if not token:
        raise BridgeFout("geen_token", "Er is geen (leesbaar) API-token opgeslagen; voer het token opnieuw in")
    check_resolves_internal(config.bridge_url)
    return _client(config.bridge_url, token, config.bridge_ca_pem)


def _freshness(body: dict) -> dict:
    source = body.get("freshness") if isinstance(body.get("freshness"), dict) else body
    out = {}
    for key in ("last_import_at", "last_complete_date"):
        value = source.get(key) if isinstance(source, dict) else None
        out[key] = _clean(value, 40) if isinstance(value, str) else None
    return out


def _status_summary(body: dict) -> dict:
    warnings = body.get("warnings") if isinstance(body.get("warnings"), list) else []
    missing = body.get("missing_dates") if isinstance(body.get("missing_dates"), list) else []
    return dict(
        **_freshness(body),
        missing_dates=[_clean(d, 10) for d in missing[:400] if isinstance(d, str)],
        missing_dates_count=len(missing),
        warnings=[
            dict(code=_clean(w.get("code"), 60), detail=_clean(w.get("detail"), 300), since=_clean(w.get("since"), 20))
            for w in warnings[:50]
            if isinstance(w, dict)
        ],
        source_mode=_clean(body.get("source_mode"), 40) or None,
    )


def _catalog_keys(body: dict) -> set[str] | None:
    items = body.get("data", body.get("metrics"))
    if isinstance(items, dict):
        return {str(k) for k in items}
    if not isinstance(items, list):
        return None
    keys = set()
    for item in items:
        if isinstance(item, str):
            keys.add(item)
        elif isinstance(item, dict):
            key = item.get("metric_key") or item.get("metric") or item.get("key")
            if isinstance(key, str):
                keys.add(key)
    return keys


def _record(db: Session, sync: BridgeSync, status: str, *, error: BridgeFout | None = None, details=None):
    sync.status = status
    sync.finished_at = utcnow()
    if error is not None:
        sync.error_code, sync.error = error.code, _clean(error.message, 1000)
        if error.retry_after is not None:
            sync.retry_after = utcnow() + timedelta(seconds=error.retry_after)
    if details is not None:
        sync.details_json = json.dumps(details, ensure_ascii=False)
    db.commit()


def test_connection(db: Session, user: User | None = None) -> dict:
    """Verbinding testen: GET /api/v1/status (en de catalogus) met het opgeslagen token."""
    config = _config(db)
    sync = BridgeSync(trigger="test", user_id=user.id if user else None)
    db.add(sync)
    db.commit()
    try:
        with _open(db, config) as client:
            status = _status_summary(_get(client, "/status"))
            try:
                keys = _catalog_keys(_get(client, "/metrics"))
            except BridgeFout as error:
                if error.code in ("token_ongeldig", "geen_toegang"):
                    raise
                keys = None
    except BridgeFout as error:
        _record(db, sync, "fout", error=error)
        return dict(ok=False, code=error.code, message=error.message, checked_at=_iso(sync.finished_at))
    status["catalog_known"] = keys is not None
    status["missing_metrics"] = sorted(set(MODEL_METRICS) - keys) if keys is not None else []
    _record(db, sync, "ok", details=status)
    return dict(ok=True, checked_at=_iso(sync.finished_at), **status)


# --- Synchronisatie -------------------------------------------------------------------------


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def normalize_row(row) -> dict | None:
    """Contractregel → cacheveld. Geeft None bij een onbruikbare regel."""
    if not isinstance(row, dict):
        return None
    try:
        day = date.fromisoformat(str(row.get("date"))).isoformat()
    except ValueError:
        return None
    scope, metric = row.get("scope"), row.get("metric")
    if not isinstance(scope, str) or not isinstance(metric, str) or not scope or not metric:
        return None
    quality = row.get("quality") if row.get("quality") in QUALITIES else "onbekend"
    value = _number(row.get("value"))
    if row.get("value") is not None and value is None:
        quality = "onbekend"  # geen getal: nooit gebruiken
    if quality == "ontbreekt":
        value = None  # contract: ontbreekt heeft value null, nooit 0
    coverage = _number(row.get("coverage"))
    if coverage is not None and not 0 <= coverage <= 1:
        coverage = None
    revision = row.get("revision")
    revision = revision if isinstance(revision, int) and not isinstance(revision, bool) else None
    sources = row.get("sources") if isinstance(row.get("sources"), list) else []
    return dict(
        scope=scope[:40],
        local_date=day,
        metric=metric[:80],
        value=value,
        unit=_clean(row.get("unit"), 40) or None,
        aggregation=_clean(row.get("aggregation"), 20) or None,
        coverage=coverage,
        quality=quality,
        resolved_from=_clean(row.get("resolved_from"), 40) or None,
        sources_json=json.dumps([_clean(s, 60) for s in sources[:50] if isinstance(s, str)]),
        import_id=_clean(row.get("import_id"), 80) or None,
        revision=revision,
    )


COMPARE = (
    "value",
    "unit",
    "aggregation",
    "coverage",
    "quality",
    "resolved_from",
    "sources_json",
    "import_id",
    "revision",
)


def _scopes(db: Session) -> list[str]:
    bays = db.execute(select(Bay.number, Bay.department).order_by(Bay.number)).all()
    kaps = [f"kap:{n}" for n, _ in bays] or [f"kap:{n}" for n in range(1, 41)]
    departments = sorted({d for _, d in bays if d}) or [1, 2, 3, 4]
    return kaps + [f"afdeling:{d}" for d in departments]


def sync(
    db: Session, *, trigger: str = "handmatig", start: date | None = None, end: date | None = None, user=None
) -> dict:
    """Haal dagwaarden op en bewaar afwijkingen. Eén synchronisatie tegelijk."""
    if not _lock.acquire(blocking=False):
        raise BridgeFout("bezig", "Er loopt al een synchronisatie")
    try:
        return _sync(db, trigger=trigger, start=start, end=end, user=user)
    finally:
        _lock.release()


def _sync(db, *, trigger, start, end, user):
    config = _config(db)
    today = _now_local().date()
    end = end or today - timedelta(days=1)
    start = start or end - timedelta(days=config.bridge_sync_days - 1)
    if start > end or (end - start).days > MAX_PERIOD_DAYS:
        raise BridgeFout("ongeldige_periode", "Ongeldige periode (maximaal tien jaar)")
    record = BridgeSync(
        trigger=trigger,
        period_from=start.isoformat(),
        period_to=end.isoformat(),
        user_id=user.id if user else None,
    )
    db.add(record)
    db.commit()
    details = dict(unexpected_rows=0, invalid_rows=0, qualities={}, freshness={}, metrics=[])
    try:
        if not config.bridge_enabled:
            raise BridgeFout("uitgeschakeld", "Klimaat- en lichtdata uit de Priva Bridge staat uit")
        with _open(db, config) as client:
            status = _status_summary(_get(client, "/status"))
            details["status"] = status
            details["freshness"] = {k: status[k] for k in ("last_import_at", "last_complete_date")}
            keys = _catalog_keys(_get(client, "/metrics"))
            if keys is None:
                metrics = list(WANTED_METRICS)
                details["catalog"] = "onbekend formaat; standaardlijst gebruikt"
            else:
                metrics = [m for m in WANTED_METRICS if m in keys]
                metrics += sorted(k for k in keys if k.startswith(DISPLAY_PREFIXES) and k not in metrics)
                details["missing_metrics"] = sorted(set(MODEL_METRICS) - keys)
            details["metrics"] = metrics
            if not metrics:
                raise BridgeFout("geen_meetwaarden", "De Bridge-catalogus bevat geen van de gebruikte meetwaarden")
            scopes = _scopes(db)
            for index in range(0, len(scopes), SCOPE_CHUNK):
                _sync_chunk(db, client, record, scopes[index : index + SCOPE_CHUNK], metrics, start, end, details)
    except BridgeFout as error:
        db.rollback()
        _record(db, record, "fout", error=error, details=details)
        raise
    except Exception as error:  # onverwacht: vastleggen, niet stil laten verdwijnen
        db.rollback()
        _record(
            db, record, "fout", error=BridgeFout("intern", f"Onverwachte fout: {type(error).__name__}"), details=details
        )
        raise
    _record(db, record, "ok", details=details)
    return sync_dict(record)


def _sync_chunk(db, client, record, scopes, metrics, start, end, details):
    wanted = set(scopes)
    latest = {}
    query = (
        select(BridgeDailyValue)
        .where(
            BridgeDailyValue.scope.in_(scopes),
            BridgeDailyValue.local_date >= start.isoformat(),
            BridgeDailyValue.local_date <= end.isoformat(),
        )
        .order_by(BridgeDailyValue.fetched_at, BridgeDailyValue.id)
    )
    for row in db.scalars(query).yield_per(1000):
        latest[(row.scope, row.local_date, row.metric)] = row
    params = [("scope", s) for s in scopes] + [("metric", m) for m in metrics]
    params += [("from", start.isoformat()), ("to", end.isoformat()), ("inherit", "true"), ("limit", str(PAGE_LIMIT))]
    cursor, seen = None, set()
    for _ in range(MAX_PAGES):
        body = _get(client, "/daily", params + ([("cursor", cursor)] if cursor else []))
        record.pages += 1
        fresh = _freshness(body)
        if any(fresh.values()):
            details["freshness"] = fresh
        rows = body.get("data")
        if not isinstance(rows, list):
            raise BridgeFout("ongeldig_antwoord", 'Antwoord van /daily mist de lijst "data"')
        now = utcnow()
        for raw in rows:
            record.rows_received += 1
            item = normalize_row(raw)
            if item is None:
                details["invalid_rows"] += 1
                continue
            if item["scope"] not in wanted or not start.isoformat() <= item["local_date"] <= end.isoformat():
                details["unexpected_rows"] += 1
                continue
            details["qualities"][item["quality"]] = details["qualities"].get(item["quality"], 0) + 1
            key = (item["scope"], item["local_date"], item["metric"])
            old = latest.get(key)
            if old is not None and all(getattr(old, f) == item[f] for f in COMPARE):
                continue
            new = BridgeDailyValue(sync_id=record.id, fetched_at=now, **item)
            db.add(new)
            latest[key] = new
            record.rows_stored += 1
        db.commit()
        cursor = body.get("next_cursor")
        if cursor is None or cursor == "":
            return
        if not isinstance(cursor, str) or cursor in seen:
            raise BridgeFout("ongeldig_antwoord", "De Bridge gaf een ongeldige of herhaalde cursor")
        seen.add(cursor)
    raise BridgeFout("te_veel_paginas", "Te veel pagina’s in één synchronisatie; kies een kortere periode")


def last_sync(db: Session, *, include_tests=False, status: str | None = None):
    query = select(BridgeSync)
    if not include_tests:
        query = query.where(BridgeSync.trigger != "test")
    if status:
        query = query.where(BridgeSync.status == status)
    return db.scalar(query.order_by(BridgeSync.id.desc()).limit(1))


def maybe_daily_sync(session_factory=None) -> dict | None:
    """Aangeroepen vanuit de bestaande achtergrondtaak (elke minuut). Doet niets als de Bridge uit staat."""
    from .db import SessionLocal

    with (session_factory or SessionLocal)() as db:
        config = _config(db)
        if not config.bridge_enabled or not config.bridge_url or db.get(BridgeCredential, 1) is None:
            return None
        now = _now_local()
        if now.hour < config.bridge_sync_hour:
            return None
        previous = last_sync(db)
        if previous is not None:
            started_local = previous.started_at.replace(tzinfo=timezone.utc).astimezone(NL)
            if previous.status == "ok" and started_local.date() == now.date() and previous.trigger == "auto":
                return None
            if previous.status == "bezig" and utcnow() - previous.started_at < timedelta(hours=2):
                return None
            if previous.status == "fout":
                wait_until = previous.finished_at + FAILURE_BACKOFF if previous.finished_at else utcnow()
                if previous.retry_after and previous.retry_after > wait_until:
                    wait_until = previous.retry_after
                if utcnow() < wait_until:
                    return None
        auto_today = db.scalar(
            select(func.count())
            .select_from(BridgeSync)
            .where(BridgeSync.trigger == "auto", BridgeSync.status == "ok", BridgeSync.started_at >= _utc_midnight(now))
        )
        if auto_today:
            return None
        try:
            return sync(db, trigger="auto")
        except BridgeFout as error:
            log.warning("Dagelijkse Bridge-synchronisatie mislukt: %s", error.message)
            return None


def _utc_midnight(local: datetime) -> datetime:
    midnight = datetime.combine(local.date(), datetime.min.time(), NL)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


def start_background_sync(session_factory, *, start=None, end=None, user_id=None) -> None:
    global _worker
    if _lock.locked() or (_worker is not None and _worker.is_alive()):
        raise BridgeFout("bezig", "Er loopt al een synchronisatie")

    def run():
        with session_factory() as db:
            user = db.get(User, user_id) if user_id else None
            try:
                sync(db, trigger="handmatig", start=start, end=end, user=user)
            except BridgeFout as error:
                log.warning("Handmatige Bridge-synchronisatie mislukt: %s", error.message)
            except Exception:
                log.exception("Handmatige Bridge-synchronisatie mislukt")

    _worker = threading.Thread(target=run, name="bridge-sync", daemon=True)
    _worker.start()


def wait_for_background(timeout: float = 30) -> None:
    if _worker is not None:
        _worker.join(timeout)


# --- Selectie voor berekeningen -------------------------------------------------------------


def allowed_qualities(voorrang: str) -> set[str]:
    return {"ok"} if voorrang == "bridge_alleen_ok" else {"ok", "verdacht"}


def cached_rows(db: Session, scopes, start: date, end: date, metrics=None, cutoff: datetime | None = None):
    """Laatst bekende regel per (scope, datum, metric) zoals bekend op ``cutoff`` (naïeve UTC)."""
    query = select(BridgeDailyValue).where(
        BridgeDailyValue.scope.in_(list(scopes)),
        BridgeDailyValue.local_date >= start.isoformat(),
        BridgeDailyValue.local_date <= end.isoformat(),
    )
    if metrics is not None:
        query = query.where(BridgeDailyValue.metric.in_(list(metrics)))
    if cutoff is not None:
        query = query.where(BridgeDailyValue.fetched_at <= cutoff)
    found, truncated = {}, False
    rows = db.scalars(query.order_by(BridgeDailyValue.fetched_at, BridgeDailyValue.id).limit(MAX_CACHE_READ + 1))
    for index, row in enumerate(rows):
        if index >= MAX_CACHE_READ:
            truncated = True
            break
        found[(row.scope, row.local_date, row.metric)] = row
    return found, truncated


def usable_model_value(row: BridgeDailyValue | None, voorrang: str) -> float | None:
    """Waarde die als meting mag tellen, of None. Nooit een 0 voor een ontbrekende dag."""
    if row is None or row.value is None or row.quality not in allowed_qualities(voorrang):
        return None
    spec = MODEL_METRICS.get(row.metric)
    if spec is None:
        return row.value
    _, units, low, high = spec
    if row.unit not in units or not math.isfinite(row.value) or not low <= row.value <= high:
        return None
    return row.value


def choose(own_present: bool, bridge_value: float | None, voorrang: str) -> str | None:
    """Welke bron telt voor één dag: 'bridge', 'eigen_import' of None (ontbreekt)."""
    if bridge_value is not None and (voorrang != "eigen_import_eerst" or not own_present):
        return "bridge"
    return "eigen_import" if own_present else None


def row_dict(row: BridgeDailyValue | None) -> dict | None:
    if row is None:
        return None
    return dict(
        value=row.value,
        unit=row.unit,
        coverage=row.coverage,
        quality=row.quality,
        resolved_from=row.resolved_from,
        import_id=row.import_id,
        revision=row.revision,
        fetched_at=_iso(row.fetched_at),
        sources=json.loads(row.sources_json or "[]"),
    )


def apply_to_climate(db: Session, result: dict, *, start: date, end: date, bay_number: int | None, own: set) -> None:
    """Vul /api/climate aan (alleen als de Bridge aan staat). ``own`` = {(datum, feature)} uit de eigen import."""
    config = _config(db)
    if not config.bridge_enabled:
        return
    voorrang = config.bridge_voorrang
    scopes = [f"kap:{bay_number}"] if bay_number else [s for s in _scopes(db) if s.startswith("afdeling:")]
    metrics = list(MODEL_METRICS) + [m for m in DISPLAY_METRICS]
    rows, truncated = cached_rows(db, scopes, start, end)
    derived = result.get("derived") or {}
    daily_derived = {d["date"]: d for d in derived.get("daily", [])}
    days = []
    day = start
    counts = {"bridge": 0, "eigen_import": 0, "ontbreekt": 0}
    while day <= end:
        key = day.isoformat()
        for scope in scopes:
            entry = dict(date=key, scope=scope, metrics={}, sources={})
            for metric in sorted({m for (s, d, m) in rows if s == scope and d == key} | set(metrics)):
                row = rows.get((scope, key, metric))
                if row is None and metric not in MODEL_METRICS:
                    continue
                entry["metrics"][metric] = row_dict(row) or dict(value=None, quality="niet_opgehaald")
            for metric, (feature, *_rest) in MODEL_METRICS.items():
                value = usable_model_value(rows.get((scope, key, metric)), voorrang)
                chosen = (
                    choose((key, feature) in own, value, voorrang)
                    if bay_number
                    else ("bridge" if value is not None else None)
                )
                entry["sources"][feature] = chosen or "ontbreekt"
                counts[chosen or "ontbreekt"] += 1
            days.append(entry)
        if bay_number and key in daily_derived:
            item = daily_derived[key]
            value = usable_model_value(rows.get((scopes[0], key, "lichtsom_lampen_kap")), voorrang)
            chosen = choose(item.get("lamp_dli") is not None, value, voorrang)
            if chosen == "bridge":
                item["lamp_dli"] = value
                item["total_dli"] = value + item["daylight_dli"] if item.get("daylight_dli") is not None else None
            item["lamp_source"] = chosen or "ontbreekt"
        day += timedelta(days=1)
    if bay_number and daily_derived:
        values = list(daily_derived.values())
        for field in ("lamp_dli", "total_dli"):
            derived[field] = (
                sum(d[field] for d in values) if values and all(d[field] is not None for d in values) else None
            )
        derived["lamp_source_policy"] = VOORRANG_LABELS[voorrang]
    previous = last_sync(db, status="ok")
    result["bridge"] = dict(
        enabled=True,
        voorrang=voorrang,
        voorrang_label=VOORRANG_LABELS[voorrang],
        scopes=scopes,
        days=days,
        source_counts=counts,
        truncated=truncated,
        freshness=json.loads(previous.details_json).get("freshness", {}) if previous else {},
        last_sync_at=_iso(previous.finished_at) if previous else None,
    )


# --- Webinterface ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def sync_dict(row: BridgeSync | None) -> dict | None:
    if row is None:
        return None
    try:
        details = json.loads(row.details_json or "{}")
    except ValueError:
        details = {}
    return dict(
        id=row.id,
        trigger=row.trigger,
        status=row.status,
        started_at=_iso(row.started_at),
        finished_at=_iso(row.finished_at),
        period_from=row.period_from,
        period_to=row.period_to,
        pages=row.pages,
        rows_received=row.rows_received,
        rows_stored=row.rows_stored,
        error_code=row.error_code,
        error=row.error,
        retry_after=_iso(row.retry_after),
        details=details,
    )


class BridgeConfigIn(BaseModel):
    enabled: bool
    url: str = Field(default="", max_length=300)
    ca_pem: str = Field(default="", max_length=20000)
    voorrang: Literal["bridge_ok_verdacht", "bridge_alleen_ok", "eigen_import_eerst"] = "bridge_ok_verdacht"
    sync_hour: int = Field(default=6, ge=0, le=23)
    sync_days: int = Field(default=14, ge=1, le=400)
    reason: str = Field(default="", max_length=2000)


class BridgeTokenIn(BaseModel):
    token: str = Field(min_length=8, max_length=4096)


class BridgeSyncIn(BaseModel):
    start: date | None = None
    end: date | None = None


@router.get("/api/bridge/status")
def bridge_status(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    config = _config(db)
    credential = db.get(BridgeCredential, 1)
    readable = load_token(db) is not None if credential else False
    last = last_sync(db)
    last_ok = last_sync(db, status="ok")
    test = db.scalar(select(BridgeSync).where(BridgeSync.trigger == "test").order_by(BridgeSync.id.desc()).limit(1))
    return dict(
        enabled=config.bridge_enabled,
        url=config.bridge_url,
        ca_set=bool(config.bridge_ca_pem),
        ca_pem=config.bridge_ca_pem,  # openbaar certificaat, geen geheim
        voorrang=config.bridge_voorrang,
        voorrang_opties=VOORRANG_LABELS,
        sync_hour=config.bridge_sync_hour,
        sync_days=config.bridge_sync_days,
        token_set=credential is not None,
        token_readable=readable,
        token_set_at=_iso(credential.set_at) if credential else None,
        running=_lock.locked(),
        last_sync=sync_dict(last),
        last_success=sync_dict(last_ok),
        last_test=sync_dict(test),
        freshness=(json.loads(last_ok.details_json).get("freshness", {}) if last_ok else {}),
        cached_rows=db.scalar(select(func.count()).select_from(BridgeDailyValue)),
        metrics=dict(model=list(MODEL_METRICS), display=list(DISPLAY_METRICS)),
    )


@router.put("/api/bridge/config")
def bridge_config(data: BridgeConfigIn, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    from .settings import save_settings

    try:
        url = validate_url(data.url)
        ca = validate_ca(data.ca_pem)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if data.enabled and not url:
        raise HTTPException(422, "Vul een Bridge-URL in voordat je de koppeling aanzet")
    current = _config(db)
    update = dict(
        bridge_enabled=data.enabled,
        bridge_url=url,
        bridge_ca_pem=ca,
        bridge_voorrang=data.voorrang,
        bridge_sync_hour=data.sync_hour,
        bridge_sync_days=data.sync_days,
    )
    if all(getattr(current, k) == v for k, v in update.items()):
        return bridge_status(db, user)
    reason = data.reason.strip() or "Priva Bridge-instellingen gewijzigd via Beheer"
    save_settings(db, user, current.model_copy(update=update), utcnow(), reason[:2000])
    return bridge_status(db, user)


@router.put("/api/bridge/token")
def bridge_token(data: BridgeTokenIn, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    token = data.token.strip()
    if any(ch.isspace() or not ch.isprintable() for ch in token):
        raise HTTPException(422, "Het token mag geen spaties of regeleinden bevatten")
    try:
        ciphertext = encrypt_token(token)
    except (OSError, ValueError) as error:
        raise HTTPException(500, "Token kon niet versleuteld worden opgeslagen") from error
    row = db.get(BridgeCredential, 1)
    if row is None:
        db.add(BridgeCredential(id=1, ciphertext=ciphertext, set_at=utcnow(), set_by=user.id))
    else:
        row.ciphertext, row.set_at, row.set_by = ciphertext, utcnow(), user.id
    audit(db, user, "bridge.token_ingesteld", "bridge", {"lengte": len(token)})
    db.commit()
    return {"token_set": True}


@router.delete("/api/bridge/token")
def bridge_token_delete(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    row = db.get(BridgeCredential, 1)
    if row is not None:
        db.delete(row)
        audit(db, user, "bridge.token_verwijderd", "bridge")
        db.commit()
    return {"token_set": False}


@router.post("/api/bridge/test")
def bridge_test(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return test_connection(db, user)


@router.post("/api/bridge/sync", status_code=202)
def bridge_sync(data: BridgeSyncIn, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    config = _config(db)
    if not config.bridge_enabled:
        raise HTTPException(409, 'Zet eerst "Klimaat- en lichtdata uit Priva Bridge" aan')
    if not config.bridge_url or db.get(BridgeCredential, 1) is None:
        raise HTTPException(409, "Vul eerst de Bridge-URL en het API-token in")
    if data.start and data.end and (data.start > data.end or (data.end - data.start).days > MAX_PERIOD_DAYS):
        raise HTTPException(422, "Ongeldige periode (maximaal tien jaar)")
    if (data.start is None) != (data.end is None):
        raise HTTPException(422, "Vul beide datums in, of geen van beide voor de standaardperiode")
    if data.end and data.end >= _now_local().date():
        raise HTTPException(422, "Alleen afgelopen dagen ophalen; vandaag is nog niet compleet")
    audit(db, user, "bridge.ophalen", "bridge", {"start": str(data.start or ""), "end": str(data.end or "")})
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    try:
        start_background_sync(factory, start=data.start, end=data.end, user_id=user.id)
    except BridgeFout as error:
        raise HTTPException(409, error.message) from error
    return {"gestart": True}


@router.get("/api/bridge/syncs")
def bridge_syncs(limit: int = 20, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    rows = db.scalars(select(BridgeSync).order_by(BridgeSync.id.desc()).limit(max(1, min(limit, 100)))).all()
    return [sync_dict(r) for r in rows]


@router.get("/api/bridge/summary")
def bridge_summary(db: Session = Depends(get_db), user: User = Depends(require_user)):
    """Korte status voor iedereen: aan/uit en actualiteit (geen URL of token)."""
    config = _config(db)
    if not config.bridge_enabled:
        return dict(enabled=False)
    last_ok = last_sync(db, status="ok")
    last = last_sync(db)
    return dict(
        enabled=True,
        voorrang_label=VOORRANG_LABELS[config.bridge_voorrang],
        freshness=(json.loads(last_ok.details_json).get("freshness", {}) if last_ok else {}),
        last_success_at=_iso(last_ok.finished_at) if last_ok else None,
        last_status=last.status if last else None,
        last_error=last.error if last and last.status == "fout" else None,
    )

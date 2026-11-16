from datetime import datetime, timezone
from sqlalchemy import String, Integer, Float, Boolean, Date, DateTime, ForeignKey, JSON, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

def utcnow(): return datetime.now(timezone.utc).replace(tzinfo=None)
class User(Base):
    __tablename__='users'
    id: Mapped[int]=mapped_column(primary_key=True)
    username: Mapped[str]=mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str]=mapped_column(String(300))
    role: Mapped[str]=mapped_column(String(20))
    active: Mapped[bool]=mapped_column(Boolean, default=True)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=utcnow)
class AuthSession(Base):
    __tablename__='auth_sessions'
    token_hash: Mapped[str]=mapped_column(String(64), primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
    expires_at: Mapped[datetime]=mapped_column(DateTime, index=True)
class LoginAttempt(Base):
    __tablename__='login_attempts'
    id: Mapped[int]=mapped_column(primary_key=True)
    key: Mapped[str]=mapped_column(String(200), index=True)
    attempted_at: Mapped[datetime]=mapped_column(DateTime, default=utcnow, index=True)
class Variety(Base):
    __tablename__='varieties'
    id: Mapped[int]=mapped_column(primary_key=True)
    name: Mapped[str]=mapped_column(String(100), unique=True)
    target_weight_g: Mapped[float]=mapped_column(Float, default=105)
class Bay(Base):
    __tablename__='bays'
    number: Mapped[int]=mapped_column(primary_key=True)
    department: Mapped[int]=mapped_column(Integer)
    area_m2: Mapped[float|None]=mapped_column(Float, nullable=True)
    beds: Mapped[int]=mapped_column(Integer, default=6)
class Cultivation(Base):
    __tablename__='cultivations'
    id: Mapped[int]=mapped_column(primary_key=True)
    round_name: Mapped[str]=mapped_column(String(100), index=True)
    bay_number: Mapped[int]=mapped_column(ForeignKey('bays.number'))
    variety_id: Mapped[int]=mapped_column(ForeignKey('varieties.id'))
    beds: Mapped[int]=mapped_column(Integer)
    category: Mapped[str]=mapped_column(String(20), default='standard')
    status: Mapped[str]=mapped_column(String(20), default='planned')
    label_quality: Mapped[str]=mapped_column(String(30), default='verified')
class CultivationRevision(Base):
    __tablename__='cultivation_revisions'
    id: Mapped[int]=mapped_column(primary_key=True)
    cultivation_id: Mapped[int]=mapped_column(ForeignKey('cultivations.id'), index=True)
    category: Mapped[str]=mapped_column(String(20))
    label_quality: Mapped[str]=mapped_column(String(30))
    status: Mapped[str]=mapped_column(String(20))
    created_at: Mapped[datetime]=mapped_column(DateTime, default=utcnow, index=True)
class PlanningVersion(Base):
    __tablename__='planning_versions'
    id: Mapped[int]=mapped_column(primary_key=True)
    cultivation_id: Mapped[int]=mapped_column(ForeignKey('cultivations.id'), index=True)
    plant_date: Mapped[object|None]=mapped_column(Date, nullable=True)
    light_out_date: Mapped[object|None]=mapped_column(Date, nullable=True)
    expected_harvest_date: Mapped[object|None]=mapped_column(Date, nullable=True)
    density: Mapped[float|None]=mapped_column(Float, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=utcnow)
    reason: Mapped[str|None]=mapped_column(Text, nullable=True)
    author_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
class Measurement(Base):
    __tablename__='measurements'
    id: Mapped[int]=mapped_column(primary_key=True)
    client_id: Mapped[str]=mapped_column(String(36), unique=True, index=True)
    cultivation_id: Mapped[int]=mapped_column(ForeignKey('cultivations.id'), index=True)
    kind: Mapped[str]=mapped_column(String(10))
    measured_at: Mapped[datetime]=mapped_column(DateTime)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=utcnow)
    actual_kd_day: Mapped[int|None]=mapped_column(Integer, nullable=True)
    location: Mapped[str|None]=mapped_column(String(200), nullable=True)
    notes: Mapped[str|None]=mapped_column(Text, nullable=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
    payload_hash: Mapped[str]=mapped_column(String(64))
class Stem(Base):
    __tablename__='stems'
    id: Mapped[int]=mapped_column(primary_key=True)
    measurement_id: Mapped[int]=mapped_column(ForeignKey('measurements.id'), index=True)
    position: Mapped[str]=mapped_column(String(100))
    weight_g: Mapped[float]=mapped_column(Float)
    length_cm: Mapped[float]=mapped_column(Float)
    excluded: Mapped[bool]=mapped_column(Boolean, default=False)
    exclusion_reason: Mapped[str|None]=mapped_column(Text, nullable=True)
class Harvest(Base):
    __tablename__='harvests'
    id: Mapped[int]=mapped_column(primary_key=True)
    cultivation_id: Mapped[int]=mapped_column(ForeignKey('cultivations.id'), index=True)
    start_date: Mapped[object|None]=mapped_column(Date, nullable=True)
    end_date: Mapped[object|None]=mapped_column(Date, nullable=True)
    weight_g: Mapped[float|None]=mapped_column(Float, nullable=True)
    stem_count: Mapped[int|None]=mapped_column(Integer, nullable=True)
    loss_fraction: Mapped[float|None]=mapped_column(Float, nullable=True)
    quality_classes: Mapped[object|None]=mapped_column(JSON, nullable=True)
    notes: Mapped[str|None]=mapped_column(Text, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=utcnow)
class Event(Base):
    __tablename__='events'
    id: Mapped[int]=mapped_column(primary_key=True)
    cultivation_id: Mapped[int]=mapped_column(ForeignKey('cultivations.id'), index=True)
    occurred_at: Mapped[datetime]=mapped_column(DateTime)
    kind: Mapped[str]=mapped_column(String(100))
    amount: Mapped[float|None]=mapped_column(Float, nullable=True)
    unit: Mapped[str|None]=mapped_column(String(50), nullable=True)
    scope: Mapped[str|None]=mapped_column(String(100), nullable=True)
    reason: Mapped[str|None]=mapped_column(Text, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=utcnow)
class MappingVersion(Base):
    __tablename__='mapping_versions'
    id: Mapped[int]=mapped_column(primary_key=True)
    bay_number: Mapped[int]=mapped_column(ForeignKey('bays.number'), index=True)
    object_type: Mapped[str]=mapped_column(String(20))
    object_number: Mapped[int]=mapped_column(Integer)
    valid_from: Mapped[object]=mapped_column(Date)
    valid_until: Mapped[object|None]=mapped_column(Date, nullable=True)
    reason: Mapped[str|None]=mapped_column(Text, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=utcnow)
class AuditLog(Base):
    __tablename__='audit_logs'
    id: Mapped[int]=mapped_column(primary_key=True)
    user_id: Mapped[int|None]=mapped_column(ForeignKey('users.id'), nullable=True)
    action: Mapped[str]=mapped_column(String(100))
    entity: Mapped[str]=mapped_column(String(200))
    details: Mapped[object|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=utcnow, index=True)

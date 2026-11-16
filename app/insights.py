"""Bounded, read-only descriptive comparison of intervention and control crops."""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_user
from .core import to_utc
from .db import get_db
from .forecast import ForecastSnapshot, ModelArtifact, harvest_available_at
from .models import Cultivation, CultivationRevision, Event, Harvest, User, Variety

router = APIRouter()
MAX_CULTIVATIONS = 5000
MAX_EVENTS = 20000


def _mean(values):
    return statistics.fmean(values) if values else None


def _sd(values):
    return statistics.stdev(values) if len(values) > 1 else None


def _latest_revision(db: Session, cultivation_id: int, cutoff: datetime):
    return db.scalar(select(CultivationRevision).where(
        CultivationRevision.cultivation_id == cultivation_id,
        CultivationRevision.created_at <= cutoff
    ).order_by(CultivationRevision.created_at.desc(), CultivationRevision.id.desc()).limit(1))


def _harvests(db: Session, cultivation_ids: list[int], cutoff: datetime):
    """Pick latest recorded version first; then evaluate that version as of cutoff."""
    by_id = {}
    if not cultivation_ids:
        return by_id
    ranked = select(Harvest.id, func.row_number().over(partition_by=Harvest.cultivation_id,
        order_by=(Harvest.created_at.desc(), Harvest.id.desc())).label('rank')).where(
        Harvest.cultivation_id.in_(cultivation_ids), Harvest.created_at <= cutoff).subquery()
    rows = db.scalars(select(Harvest).join(ranked, ranked.c.id == Harvest.id).where(ranked.c.rank == 1))
    for row in rows:
        available = harvest_available_at(row)
        if available is not None and available <= cutoff:
            by_id[row.cultivation_id] = row
    return by_id


def _preharvest_forecast(db: Session, cultivation_id: int, harvest: Harvest):
    # The label is knowable no earlier than its recording time or the local
    # start of the actual harvest period. Use the earlier boundary so no
    # forecast made after harvest began can explain the already observed label.
    harvest_day = harvest.start_date or harvest.end_date
    label_boundary = to_utc(datetime.combine(harvest_day, datetime.min.time()))
    preharvest_cutoff = min(harvest.created_at, label_boundary)
    candidates = db.scalars(select(ForecastSnapshot).where(
        ForecastSnapshot.cultivation_id == cultivation_id,
        ForecastSnapshot.as_of <= preharvest_cutoff,
        ForecastSnapshot.created_at <= preharvest_cutoff,
        ForecastSnapshot.status.in_(['operational', 'experimental']),
        ForecastSnapshot.model_id.is_not(None)
    ).order_by(ForecastSnapshot.as_of.desc(), ForecastSnapshot.created_at.desc(),
               ForecastSnapshot.id.desc()).limit(30)).all()
    for row in candidates:
        model = db.get(ModelArtifact, row.model_id)
        if (model and model.created_at <= row.as_of and model.created_at <= row.created_at
                and cultivation_id not in (model.training_ids or []) and row.point_g is not None):
            return row
    return None


def _event_group(kind: str) -> str:
    label = (kind or '').casefold()
    if 'alar' in label:
        return 'alar'
    if 'selectief' in label:
        return 'selectief'
    if 'lamp' in label or 'licht' in label or label == 'light':
        return 'lamp'
    if 'temperatuur' in label or 'temperature' in label or 'temp' in label:
        return 'temperature'
    return 'other'


@router.get('/api/interventions/compare')
def compare_interventions(round_name: str = Query(min_length=1, max_length=100),
                          as_of: datetime | None = None,
                          db: Session = Depends(get_db), user: User = Depends(require_user)):
    cutoff = to_utc(as_of) if as_of else datetime.now(timezone.utc).replace(tzinfo=None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if cutoff > now:
        from fastapi import HTTPException
        raise HTTPException(422, 'as_of mag niet in de toekomst liggen')
    cultivations = db.scalars(select(Cultivation).where(
        Cultivation.round_name == round_name
    ).order_by(Cultivation.id).limit(MAX_CULTIVATIONS + 1)).all()
    cultivation_truncated = len(cultivations) > MAX_CULTIVATIONS
    cultivations = cultivations[:MAX_CULTIVATIONS]
    ids = [c.id for c in cultivations]

    events = db.scalars(select(Event).where(
        Event.cultivation_id.in_(ids), Event.occurred_at <= cutoff, Event.created_at <= cutoff
    ).order_by(Event.occurred_at, Event.id).limit(MAX_EVENTS + 1)).all() if ids else []
    event_truncated = len(events) > MAX_EVENTS
    events = events[:MAX_EVENTS]
    event_by_cultivation = defaultdict(list)
    for event in events:
        event_by_cultivation[event.cultivation_id].append(event)

    harvest_by_id = _harvests(db, ids, cutoff)
    varieties = {v.id: v.name for v in db.scalars(select(Variety)).all()}
    groups = defaultdict(list)
    eligible_meta = {}
    exclusions = []
    for cultivation in cultivations:
        revision = _latest_revision(db, cultivation.id, cutoff)
        if revision is None:
            exclusions.append({'cultivation_id': cultivation.id, 'bay_number': cultivation.bay_number,
                               'reason': 'no_category_revision_known_as_of'})
            continue
        category, quality = revision.category, revision.label_quality
        if category not in ('standard', 'control', 'adjusted'):
            exclusions.append({'cultivation_id': cultivation.id, 'bay_number': cultivation.bay_number,
                               'reason': 'unknown_category', 'category': category})
            continue
        if quality in ('ambiguous', 'historical_assigned', 'multiclass') or quality not in ('verified', 'reliable'):
            exclusions.append({'cultivation_id': cultivation.id, 'bay_number': cultivation.bay_number,
                               'reason': 'ineligible_label_quality', 'label_quality': quality})
            continue
        # Keep logged exposures for validly labelled crops even when an actual
        # harvest is not yet available for the weight comparison.
        eligible_meta[cultivation.id] = (cultivation.variety_id, category)
        harvest = harvest_by_id.get(cultivation.id)
        if harvest is None:
            exclusions.append({'cultivation_id': cultivation.id, 'bay_number': cultivation.bay_number,
                               'reason': 'no_known_actual_harvest'})
            continue
        if harvest.weight_g is None or harvest.weight_g <= 0:
            exclusions.append({'cultivation_id': cultivation.id, 'bay_number': cultivation.bay_number,
                               'reason': 'missing_or_nonpositive_harvest_weight'})
            continue
        if harvest.quality_classes and len(harvest.quality_classes) > 1:
            exclusions.append({'cultivation_id': cultivation.id, 'bay_number': cultivation.bay_number,
                               'reason': 'multiple_harvest_quality_classes'})
            continue
        forecast = _preharvest_forecast(db, cultivation.id, harvest)
        residual = harvest.weight_g - forecast.point_g if forecast else None
        groups[(cultivation.variety_id, category)].append({
            'cultivation_id': cultivation.id, 'bay_number': cultivation.bay_number,
            'weight_g': float(harvest.weight_g), 'residual_g': float(residual) if residual is not None else None,
            'forecast_status': forecast.status if forecast else None,
            'forecast_feature_version': (db.get(ModelArtifact, forecast.model_id).feature_version
                                          if forecast and db.get(ModelArtifact, forecast.model_id) else None),
            'events': event_by_cultivation.get(cultivation.id, []),
        })

    comparisons = []
    variety_ids = sorted({key[0] for key in groups})
    for variety_id in variety_ids:
        standard = groups[(variety_id, 'standard')]
        control = groups[(variety_id, 'control')]
        adjusted = groups[(variety_id, 'adjusted')]
        controls_bays = {x['bay_number'] for x in control}
        adjusted_bays = {x['bay_number'] for x in adjusted}
        overlap_bays = controls_bays & adjusted_bays
        independent_controls = [x for x in control if x['bay_number'] not in overlap_bays]
        enough = len(independent_controls) >= 4 and len({x['bay_number'] for x in independent_controls}) >= 4
        observed_diff = (_mean([x['weight_g'] for x in adjusted]) - _mean([x['weight_g'] for x in independent_controls])
                         if adjusted and enough and not overlap_bays and not cultivation_truncated and not event_truncated else None)
        adjusted_residuals = [x['residual_g'] for x in adjusted if x['residual_g'] is not None]
        control_residuals = [x['residual_g'] for x in independent_controls if x['residual_g'] is not None]
        residual_diff = (_mean(adjusted_residuals) - _mean(control_residuals)
                         if adjusted_residuals and len(control_residuals) >= 4 and
                         len({x['bay_number'] for x in independent_controls if x['residual_g'] is not None}) >= 4
                         and not cultivation_truncated and not event_truncated and not overlap_bays else None)

        def summary(items):
            return {'cultivations': len(items), 'bays': len({x['bay_number'] for x in items}),
                    'mean_final_weight_g': _mean([x['weight_g'] for x in items]),
                    'sd_final_weight_g': _sd([x['weight_g'] for x in items]),
                    'mean_prediction_residual_g': _mean([x['residual_g'] for x in items if x['residual_g'] is not None]),
                    'residual_n': sum(x['residual_g'] is not None for x in items),
                    'residual_forecasts': [{'status': status, 'feature_version': version, 'count': sum(
                        x['forecast_status'] == status and x['forecast_feature_version'] == version for x in items)}
                        for status, version in sorted({(x['forecast_status'], x['forecast_feature_version'])
                                                       for x in items if x['residual_g'] is not None})]}

        comparisons.append({'variety_id': variety_id, 'variety': varieties.get(variety_id),
            'round_name': round_name,
            'groups': {'standard': summary(standard), 'control': summary(control), 'adjusted': summary(adjusted)},
            'adjusted_minus_control_observed_difference_g': observed_diff,
            'average_prediction_residual_difference_g': residual_diff,
            'difference_status': ('truncated_data' if cultivation_truncated or event_truncated else
                                  'overlapping_bays' if overlap_bays else
                                  'descriptive_noncausal' if observed_diff is not None else
                                  'insufficient_controls' if not enough else 'no_adjusted_or_harvest_data'),
            'control_bays': len(controls_bays), 'adjusted_bays': len(adjusted_bays),
            'independent_control_bays': len({x['bay_number'] for x in independent_controls}),
            'overlapping_bays': sorted(overlap_bays)})

    # Event measurements retain their original unit and scope in every bucket.
    event_summaries = defaultdict(lambda: {'events': 0, 'cultivations': set(), 'total_amount': 0.0, 'amount_n': 0})
    if not cultivation_truncated and not event_truncated:
        for cultivation_id, (variety_id, category) in eligible_meta.items():
            for event in event_by_cultivation.get(cultivation_id, []):
                group = _event_group(event.kind)
                key = (variety_id, category, group, event.kind, event.unit or 'unspecified', event.scope or 'unspecified')
                item = event_summaries[key]
                item['events'] += 1
                item['cultivations'].add(cultivation_id)
                if event.amount is not None:
                    item['total_amount'] += float(event.amount)
                    item['amount_n'] += 1
    event_output = [{'variety_id': key[0], 'category': key[1], 'group': key[2], 'kind': key[3], 'unit': key[4], 'scope': key[5],
                     'events': val['events'], 'cultivations': len(val['cultivations']),
                     'total_amount': val['total_amount'] if val['amount_n'] else None,
                     'amount_n': val['amount_n']}
                    for key, val in sorted(event_summaries.items())]

    return {'round_name': round_name, 'as_of': cutoff.isoformat() + 'Z',
        'comparisons': comparisons, 'excluded_cultivations': exclusions,
        'events': event_output,
        'truncation': {'cultivations': cultivation_truncated, 'events': event_truncated,
                       'limits': {'cultivations': MAX_CULTIVATIONS, 'events': MAX_EVENTS}},
        'notices': [
            'Waargenomen verschil tussen aangepaste en controlekappen is beschrijvend en niet causaal.',
            'Teelten in dezelfde kap kunnen gecorreleerd zijn; aantallen onafhankelijke kappen staan apart.',
            'Een verschil is geen bewijs voor effect en geeft geen middelenadvies of automatische kasaansturing.',
            'Gebeurtenissen blijven per oorspronkelijke eenheid en scope gescheiden.',
        ]}

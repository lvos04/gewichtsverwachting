"""Read-only extraction of historical Teeltprogramma round sheets.

Output is evidence for review, never an instruction to create cultivations or
observed light/weather. Week/day fields carry no year. Formula caches are
preserved separately and never used as verified results.
"""
from __future__ import annotations
import json
import math
import re
import zipfile
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path, PurePosixPath
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

MAX_FILE=32*1024*1024
MAX_EXPANDED=128*1024*1024
MAX_MEMBERS=10000
MAX_SHEETS=100
MAX_RECORDS=10000
MAX_CELL_TEXT=16384
SPECIES={'a':'antonov','p':'pjotr','t':'tutu','b':'baltazar',
         'g':None,'k':None,'w':None,'altaj':None}
FIELDS={'bay_number':3,'schema_code':4,'plant_week':5,'plant_weekday':6,
        'vegetative_days':7,'light_out_week':8,'light_out_weekday':9,
        'reaction_days':10,'interlight':11,'interlight_extension_days':12,
        'density':13,'planned_harvest_week':14,'planned_harvest_weekday':15,
        'planned_duration_days':16,'remarks':18}

def _validate_archive(path):
    path=Path(path)
    if not path.is_file() or path.stat().st_size>MAX_FILE: raise ValueError('Werkboek ontbreekt of is te groot')
    try:
        with zipfile.ZipFile(path) as z:
            items=z.infolist(); names=[x.filename for x in items]
            if len(items)>MAX_MEMBERS or '[Content_Types].xml' not in names or 'xl/workbook.xml' not in names: raise ValueError('Geen ondersteund XLSX-werkboek')
            if len({x.casefold() for x in names})!=len(names): raise ValueError('Dubbele archiefpaden')
            total=0
            for item in items:
                name=item.filename; pure=PurePosixPath(name)
                printer=bool(re.fullmatch(r'xl/printerSettings/printerSettings[0-9]+\.bin',name,re.I))
                if pure.is_absolute() or '..' in pure.parts or '\\' in name or item.flag_bits&1 or 'vbaproject' in name.lower() or (name.lower().endswith(('.bin','.exe','.dll')) and not printer):
                    raise ValueError('Onveilig of versleuteld werkboekonderdeel')
                total+=item.file_size
                if total>MAX_EXPANDED: raise ValueError('Uitgepakt werkboek te groot')
                if name.lower().endswith(('.xml','.rels')):
                    with z.open(item) as stream:
                        tail=b''
                        while chunk:=stream.read(65536):
                            probe=(tail+chunk).lower()
                            if b'<!doctype' in probe or b'<!entity' in probe: raise ValueError('XML-entiteiten niet toegestaan')
                            tail=probe[-16:]
    except (zipfile.BadZipFile,OSError,RuntimeError) as exc: raise ValueError('Ongeldig XLSX-werkboek') from exc

def _json_value(value):
    if isinstance(value,(date,datetime,time)): return value.isoformat()
    if isinstance(value,str):
        if len(value)>MAX_CELL_TEXT: raise ValueError('Celinhoud te groot')
        return value
    if value is None or isinstance(value,(int,float,bool)):
        if isinstance(value,float) and not math.isfinite(value): raise ValueError('Niet-eindige celinhoud')
        return value
    return str(value)[:MAX_CELL_TEXT]

def _source(cell,cached,row,column):
    raw=cell.value
    formula=raw if cell.data_type=='f' else None
    return {'cell':f'{get_column_letter(column)}{row}','value':None if formula else _json_value(raw),
            'formula':_json_value(formula),'cached':_json_value(cached.value) if formula else None,
            'confidence':'formula_cache_unverified' if formula else 'literal' if raw is not None else 'missing'}

def _int_literal(source, low, high):
    value=source['value']
    if source['formula'] is not None or isinstance(value,bool) or not isinstance(value,(float,int)): return None
    rounded=round(value)
    if abs(value-rounded)>1e-9 or not low<=rounded<=high: return None
    if value!=rounded: source['normalization']='floating_point_roundoff_to_integer'
    return int(rounded)

def parse_schema(value):
    """Parse explicit bed counts; never infer beds for a lone species code."""
    if value is None or not str(value).strip(): return [],['schema_empty']
    text=re.sub(r'\s+','',str(value).lower())
    if text in SPECIES:
        return [{'code':text,'variety':SPECIES[text],'beds':None,'inactive':SPECIES[text] is None}],['bed_count_unknown']+(['inactive_species'] if SPECIES[text] is None else [])
    parts=re.findall(r'(\d+)(altaj|[aptbgkw])',text)
    if not parts or ''.join(n+c for n,c in parts)!=text: return [],['schema_unrecognized']
    rows=[{'code':code,'variety':SPECIES[code],'beds':int(number),'inactive':SPECIES[code] is None} for number,code in parts]
    counts=[item['beds'] for item in rows]
    if any(n<1 for n in counts) or sum(counts)>6 or len({item['code'] for item in rows})!=len(rows): return [],['schema_invalid_beds']
    reasons=[]
    if sum(counts)<6: reasons.append('partial_bed_allocation')
    if any(item['inactive'] for item in rows): reasons.append('inactive_species')
    if len(rows)>1: reasons.append('mixed_species_review')
    return rows,reasons

def parse_program(path):
    """Extract bounded, review-only program evidence; ``path`` is never written.

    The authoritative original filename/year is not used. Records are keyed by
    sheet + bay, not assumed matched to similarly named rounds elsewhere.
    """
    _validate_archive(path)
    formulas=load_workbook(path,read_only=True,data_only=False,keep_links=False)
    cached=None
    try:
        cached=load_workbook(path,read_only=True,data_only=True,keep_links=False)
        if len(formulas.sheetnames)>MAX_SHEETS: raise ValueError('Te veel werkbladen')
        records=[]; sheets=[]; issues=[]
        for ws in formulas.worksheets:
            match=re.fullmatch(r'Ronde\s+(\d+)',ws.title,re.I)
            if not match: continue
            if ws.max_row is not None and ws.max_row>100000: raise ValueError('Werkblad bevat te veel rijen')
            cs=cached[ws.title]
            raw_iter=ws.iter_rows(min_row=1,max_row=51,min_col=1,max_col=18)
            cache_iter=cs.iter_rows(min_row=1,max_row=51,min_col=1,max_col=18)
            paired=iter(zip(raw_iter,cache_iter))
            headers={}; rows=[]
            for r,(raw,values) in enumerate(paired,1):
                if r in (8,10,11): headers[r]=[_json_value(x.value) for x in raw]
                if r>=12: rows.append((r,raw,values))
            def norm(value): return str(value or '').strip().lower()
            h10=headers.get(10,[None]*18); h11=headers.get(11,[None]*18)
            valid=(norm(h10[3])=='schema' and norm(h10[4])=='pl datum' and norm(h10[7])=='licht uit' and norm(h10[13])=='bloei' and norm(h11[2])=='vak' and norm(h11[4])=='w/d')
            if not valid:
                issues.append({'sheet':ws.title,'reason':'unrecognized_round_layout'}); continue
            number=int(match.group(1)); expected=f'ronde {number}'
            title=norm(headers.get(8,[None]*18)[2]); title_conflict=bool(title and title!=expected)
            start=len(records); seen=set()
            for row,raw,values in rows:
                evidence={name:_source(raw[col-1],values[col-1],row,col) for name,col in FIELDS.items()}
                bay=_int_literal(evidence['bay_number'],1,40)
                if bay is None: continue
                reasons=['year_missing','planning_not_verified_actual']
                if bay in seen: reasons.append('duplicate_bay_in_round')
                seen.add(bay)
                if title_conflict: reasons.append('round_label_conflict')
                species,species_reasons=parse_schema(evidence['schema_code']['value']); reasons.extend(species_reasons)
                if any(evidence[key]['formula'] is not None for key in ('light_out_week','light_out_weekday','planned_harvest_week','planned_harvest_weekday')): reasons.append('formula_cache_unverified')
                week=_int_literal(evidence['plant_week'],1,53); weekday=_int_literal(evidence['plant_weekday'],1,7)
                if week is None or weekday is None: reasons.append('plant_week_day_unknown')
                numeric={key:_int_literal(evidence[key],*bounds) for key,bounds in {'light_out_week':(1,53),'light_out_weekday':(1,7),'planned_harvest_week':(1,53),'planned_harvest_weekday':(1,7)}.items()}
                density=evidence['density']['value']
                density=density if isinstance(density,(float,int)) and not isinstance(density,bool) and 0<density<=1000 else None
                records.append({'record_key':f'{ws.title}:{bay}','sheet':ws.title,'row_number':row,
                    'round_name':ws.title,'bay_number':bay,'schema_code':evidence['schema_code']['value'],
                    'species_beds':species,'plant_week':week,'plant_weekday':weekday,**numeric,
                    'plant_date':None,'light_out_date':None,'expected_harvest_date':None,
                    'density':density,'fields':{'vegetative_days':_int_literal(evidence['vegetative_days'],0,365),
                        'reaction_days':_int_literal(evidence['reaction_days'],0,365),
                        'interlight_raw':evidence['interlight']['value'],
                        'interlight_extension_days':_int_literal(evidence['interlight_extension_days'],0,365),
                        'planned_duration_days':_int_literal(evidence['planned_duration_days'],0,730),
                        'remarks':evidence['remarks']['value']},
                    'source_cells':evidence,'review_reasons':list(dict.fromkeys(reasons))})
                if len(records)>MAX_RECORDS: raise ValueError('Te veel kapregels')
            duplicate_counts=Counter(item['bay_number'] for item in records[start:])
            for item in records[start:]:
                if duplicate_counts[item['bay_number']]>1 and 'duplicate_bay_in_round' not in item['review_reasons']:
                    item['review_reasons'].append('duplicate_bay_in_round')
            sheets.append({'sheet':ws.title,'records':len(records)-start,'round_label_cell':'C8',
                           'round_label':headers.get(8,[None]*18)[2],'title_conflict':title_conflict})
        counts=Counter(reason for r in records for reason in r['review_reasons'])
        return {'records':records,'summary':{'recognized_rounds':len(sheets),'records':len(records),
                'review_reason_counts':dict(counts),'sheets':sheets,'issues':issues,
                'authority':'planning_evidence_only; Priva precedes on actual measurements'}}
    finally:
        formulas.close()
        if cached is not None: cached.close()

# Explicit alias for callers that prefer the descriptive name.
parse_program_workbook=parse_program

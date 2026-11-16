import zipfile
from openpyxl import Workbook
from app.legacy_program import parse_program, parse_schema


def make_workbook(path):
    wb=Workbook(); ws=wb.active; ws.title='Ronde 3'
    ws['C8']='Ronde 3'; ws['D10']='Schema'; ws['E10']='pl datum'; ws['H10']='licht uit'; ws['N10']='bloei'; ws['C11']='vak'; ws['E11']='w/d'
    ws['C12']=1; ws['D12']='3a3p'; ws['E12']=8; ws['F12']=2; ws['G12']=14; ws['H12']='=E12+2'; ws['I12']='=F12'; ws['N12']='=H12+8'; ws['O12']='=I12'; ws['M12']=58.5
    ws['C13']=2; ws['D13']='P'; ws['E13']=8; ws['F13']=3
    ws['C14']=3; ws['D14']='4b2p'; ws['E14']='=E12'; ws['F14']=4
    ws['C15']=4; ws['D15']=None; ws['E15']=8; ws['F15']=5
    wb.create_sheet('Ronde 4')['C8']='Ronde 4'
    wb.save(path)
    # Synthetic cache: formulas and cached values coexist in normal XLSX XML.
    with zipfile.ZipFile(path) as source:
        members=[(item,source.read(item.filename)) for item in source.infolist()]
    with zipfile.ZipFile(path,'w') as out:
        for info,blob in members:
            if info.filename=='xl/worksheets/sheet1.xml':
                blob=blob.replace(b'<f>E12+2</f><v />',b'<f>E12+2</f><v>10</v>')
            out.writestr(info,blob)


def test_round_evidence_does_not_promote_cache_or_year(tmp_path):
    path=tmp_path/'source-2022.xlsx'; make_workbook(path)
    result=parse_program(path)
    assert result['summary']['recognized_rounds']==1 and len(result['records'])==4
    a=result['records'][0]
    assert a['record_key']=='Ronde 3:1' and a['sheet']=='Ronde 3' and a['row_number']==12
    assert a['species_beds']==[{'code':'a','variety':'antonov','beds':3,'inactive':False},{'code':'p','variety':'pjotr','beds':3,'inactive':False}]
    assert a['plant_week']==8 and a['plant_weekday']==2
    assert a['source_cells']['light_out_week']['formula']=='=E12+2'
    assert a['source_cells']['light_out_week']['cached']==10
    assert a['light_out_week'] is None and a['plant_date'] is None and a['light_out_date'] is None
    assert {'year_missing','mixed_species_review','formula_cache_unverified'}<=set(a['review_reasons'])
    assert result['records'][1]['species_beds'][0]['beds'] is None
    assert result['records'][2]['plant_week'] is None
    assert 'schema_empty' in result['records'][3]['review_reasons']
    assert result['summary']['issues'][0]['reason']=='unrecognized_round_layout'


def test_species_review_and_bed_validation():
    parsed,reasons=parse_schema('4altaj2p')
    assert parsed[0]['inactive'] and parsed[1]['variety']=='pjotr'
    assert {'inactive_species','mixed_species_review'}<=set(reasons)
    assert 'partial_bed_allocation' in parse_schema('3p')[1]
    assert parse_schema('7a')[1]==['schema_invalid_beds']
    assert parse_schema('AP')[1]==['schema_unrecognized']
    assert parse_schema('')[1]==['schema_empty']


def test_block_executable_and_allow_printer_setting(tmp_path):
    path=tmp_path/'source.xlsx'; make_workbook(path)
    with zipfile.ZipFile(path,'a') as archive: archive.writestr('xl/printerSettings/printerSettings1.bin',b'synthetic printer settings')
    assert len(parse_program(path)['records'])==4
    with zipfile.ZipFile(path,'a') as archive: archive.writestr('xl/embeddings/payload.bin',b'other binary')
    import pytest
    with pytest.raises(ValueError): parse_program(path)

def test_weekday_roundoff_preserves_raw_without_truncating(tmp_path):
    path=tmp_path/'roundoff.xlsx'
    wb=Workbook(); ws=wb.active; ws.title='Ronde 3'
    ws['C8']='Ronde 3'; ws['D10']='Schema'; ws['E10']='pl datum'; ws['H10']='licht uit'; ws['N10']='bloei'; ws['C11']='vak'; ws['E11']='w/d'
    ws['C12']=1; ws['E12']=8; ws['F12']=4.000000000000001
    ws['C13']=2; ws['E13']=8; ws['F13']=1.5
    wb.save(path)
    rows=parse_program(path)['records']
    assert rows[0]['plant_weekday']==4
    assert rows[0]['source_cells']['plant_weekday']['value']==4.000000000000001
    assert rows[0]['source_cells']['plant_weekday']['normalization']=='floating_point_roundoff_to_integer'
    assert rows[1]['plant_weekday'] is None

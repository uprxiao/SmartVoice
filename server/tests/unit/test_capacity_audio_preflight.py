"""Synthetic WAVs exercise the offline tool; never human/model/capacity evidence."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import wave

import pytest

TOOL_PATH=Path(__file__).resolve().parents[3]/'scripts/acceptance/capacity_preflight.py'
TOOL=runpy.run_path(str(TOOL_PATH))
inspect_manifest,InputError,main=TOOL['inspect_manifest'],TOOL['InputError'],TOOL['main']


def audio(folder, name, *, rate=16000, channels=1, width=2):
    path=folder/(name+'.wav')
    with wave.open(str(path),'wb') as stream:
        stream.setnchannels(channels);stream.setsampwidth(width);stream.setframerate(rate)
        stream.writeframes(b'\0'*(1600*channels*width))
    return path


def document(tmp_path):
    records=[]
    # IDs are invented. These silence fixtures cannot prove spoken content or
    # speaker identity, which the report always leaves for manual review.
    for i,(kind,code) in enumerate([('A10',None),('A30',None),('A120',None),
        ('A10','青松'),('A30','青松'),('A10','白杨'),('A30','白杨')]):
        p=audio(tmp_path,f'private-fixture-path-{i}')
        records.append(dict(id=f'audio-{i}',speaker_id=f'synthetic-speaker-{i%2}',kind=kind,
            capacity_code=code,file=p.name,sha256=hashlib.sha256(p.read_bytes()).hexdigest(),samples=1600))
    data={'schema':'svi-capacity-audio-inventory-v1','recordings':records,'extra_capacity_codes':[]}
    p=tmp_path/'manifest.json';p.write_text(json.dumps(data,ensure_ascii=False))
    return p,data


def save(path,data):
    path.write_text(json.dumps(data,ensure_ascii=False))


def test_checked_files_cannot_become_capacity_report_or_human_attestation(tmp_path):
    p,data=document(tmp_path);report=inspect_manifest(p,2)
    assert report['status']=='files_checked_manual_review_required' and not report['missing']
    assert report['planned_normal_groups']==4 and report['planned_rounds_per_group']==20
    assert report['planned_normal_sessions']==160
    assert report['capacity_validation']=='unverified' and report['acceptance_pass_claimed'] is False
    assert len(report['still_required'])==4
    assert all(item['samples']==1600 and item['duration_ms']==100 for item in report['checked_audio'])
    assert 'private-fixture-path' not in json.dumps(report)
    assert 'normal_completed' not in report and 'environment_fingerprint' not in report


def test_empty_inventory_lists_real_missing_inputs_without_fabricating_samples(tmp_path):
    p=tmp_path/'empty.json';save(p,{'schema':'svi-capacity-audio-inventory-v1','recordings':[],'extra_capacity_codes':[]})
    r=inspect_manifest(p,20)
    assert r['status']=='incomplete' and r['checked_audio']==[]
    assert len([m for m in r['missing'] if m['requirement']=='CAPACITY_VARIANT'])==40
    assert len([m for m in r['missing'] if m['requirement']=='BASE_AUDIO'])==3
    assert {'requirement':'TWO_SPEAKERS','observed_count':0} in r['missing']
    assert r['acceptance_pass_claimed'] is False


@pytest.mark.parametrize('key,value,code',[
    ('samples',True,'AUDIO_SAMPLES'),('samples',2880001,'AUDIO_SAMPLES'),
    ('kind',[], 'RECORDING_IDENTITY'),('kind','A999','RECORDING_IDENTITY'),
    ('file','../outside.wav','AUDIO_PATH'),('file','/outside.wav','AUDIO_PATH'),
    ('sha256','0'*64,'AUDIO_DIGEST_MISMATCH'),('sha256','not-a-digest','AUDIO_SHA256'),
])
def test_bad_recording_is_not_ready(tmp_path,key,value,code):
    p,data=document(tmp_path);data['recordings'][0][key]=value;save(p,data)
    with pytest.raises(InputError) as error:inspect_manifest(p,2)
    assert error.value.code==code


@pytest.mark.parametrize('options',[{'rate':8000},{'channels':2},{'width':1}])
def test_wrong_audio_format_rejected_using_real_wav_headers(tmp_path,options):
    p,data=document(tmp_path);item=data['recordings'][0];wav=audio(tmp_path,'wrong',**options)
    item.update(file=wav.name,sha256=hashlib.sha256(wav.read_bytes()).hexdigest());save(p,data)
    with pytest.raises(InputError) as error:inspect_manifest(p,2)
    assert error.value.code=='AUDIO_FORMAT'


def test_truncated_wav_not_hidden_by_a_matching_file_digest(tmp_path):
    p,data=document(tmp_path);item=data['recordings'][0];wav=tmp_path/item['file']
    wav.write_bytes(wav.read_bytes()[:-2]);item['sha256']=hashlib.sha256(wav.read_bytes()).hexdigest();save(p,data)
    with pytest.raises(InputError) as error:inspect_manifest(p,2)
    assert error.value.code=='AUDIO_TRUNCATED'


def test_duplicate_recording_ids_rejected(tmp_path):
    p,data=document(tmp_path);data['recordings'].append(dict(data['recordings'][0]));save(p,data)
    with pytest.raises(InputError) as error:inspect_manifest(p,2)
    assert error.value.code=='DUPLICATE_AUDIO_ID'


def test_above_twenty_requires_manually_annotated_extra_codes(tmp_path):
    p,data=document(tmp_path);r=inspect_manifest(p,50)
    assert {'requirement':'MANUALLY_ANNOTATED_EXTRA_CODES','missing_count':30} in r['missing']
    assert r['status']=='incomplete' and r['planned_normal_sessions']==4000


@pytest.mark.parametrize('payload',[b'{', b'['*16000+b'0'+b']'*16000,b' '*1048577])
def test_invalid_or_oversized_manifest_is_bounded(tmp_path,payload):
    p=tmp_path/'bad.json';p.write_bytes(payload)
    with pytest.raises(InputError):inspect_manifest(p,20)


def test_manifest_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
    p=tmp_path/'fifo';os.mkfifo(p)
    with pytest.raises(InputError) as error:inspect_manifest(p,20)
    assert error.value.code=='NOT_REGULAR_FILE'


def test_cli_never_overwrites_an_existing_output(tmp_path,capsys):
    p,data=document(tmp_path);output=tmp_path/'existing.json';output.write_text('owned-existing-result')
    assert main(['--manifest',str(p),'--sessions','2','--out',str(output)])==1
    assert output.read_text()=='owned-existing-result'
    assert json.loads(capsys.readouterr().out)['status']=='output_unavailable'


def test_cli_writes_new_result_but_never_claims_acceptance(tmp_path,capsys):
    p,data=document(tmp_path);output=tmp_path/'new.json'
    assert main(['--manifest',str(p),'--sessions','2','--out',str(output)])==0
    assert json.loads(output.read_text())['acceptance_pass_claimed'] is False
    assert json.loads(capsys.readouterr().out)['capacity_validation']=='unverified'

"""Offline F-CAPACITY audio inventory. Never certifies capacity or human speech.

No sockets, model/DB imports, microphone capture, playback, or file mutation.
Audio stays in its existing operator-owned fixture directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
import re
import wave

CODES = ('青松','白杨','红枫','银杏','翠竹','海棠','玉兰','紫藤','石榴','金桂',
         '山茶','杜鹃','水仙','茉莉','百合','丁香','月季','荷花','腊梅','芙蓉')
KINDS = {'A10','A30','A120'}
MAX_SAMPLES = 2_880_000
MAX_MANIFEST_BYTES = 1_048_576
HEX64 = re.compile(r'[0-9a-f]{64}\Z')


class InputError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _open_regular(path, maximum):
    fd = os.open(path, os.O_RDONLY | getattr(os,'O_NONBLOCK',0) | getattr(os,'O_NOFOLLOW',0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InputError('NOT_REGULAR_FILE')
        if info.st_size > maximum:
            raise InputError('FILE_SIZE')
        return os.fdopen(fd,'rb')
    except BaseException:
        os.close(fd)
        raise


def digest(path):
    h = hashlib.sha256()
    maximum = MAX_SAMPLES * 2 + 1_048_576
    with _open_regular(path,maximum) as stream:
        observed = 0
        for chunk in iter(lambda: stream.read(65536), b''):
            observed += len(chunk)
            if observed > maximum:
                raise InputError('FILE_SIZE')
            h.update(chunk)
    return h.hexdigest()


def _text(value):
    return isinstance(value,str) and 0 < len(value) <= 128 and not any(ord(c)<32 for c in value)


def _audio(root, item):
    if not isinstance(item,dict) or set(item) != {'id','speaker_id','kind','capacity_code','file','sha256','samples'}:
        raise InputError('RECORDING_SCHEMA')
    if not _text(item['id']) or not _text(item['speaker_id']) or not isinstance(item['kind'],str) or item['kind'] not in KINDS:
        raise InputError('RECORDING_IDENTITY')
    if item['capacity_code'] is not None and not _text(item['capacity_code']):
        raise InputError('CAPACITY_CODE')
    if not isinstance(item['sha256'],str) or not HEX64.fullmatch(item['sha256']):
        raise InputError('AUDIO_SHA256')
    if type(item['samples']) is not int or not 1 <= item['samples'] <= MAX_SAMPLES:
        raise InputError('AUDIO_SAMPLES')
    if not isinstance(item['file'],str) or not item['file']:
        raise InputError('AUDIO_PATH')
    relative = Path(item['file'])
    if relative.is_absolute() or '..' in relative.parts:
        raise InputError('AUDIO_PATH')
    try:
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise InputError('AUDIO_PATH')
        # A PCM WAV of at most 180s plus limited headers. No unbounded read of
        # an unrelated large file disguised as a fixture.
        if path.stat().st_size > MAX_SAMPLES * 2 + 1_048_576:
            raise InputError('AUDIO_SIZE')
        before = digest(path)
        if before != item['sha256']:
            raise InputError('AUDIO_DIGEST_MISMATCH')
        with wave.open(str(path),'rb') as stream:
            if (stream.getnchannels(),stream.getsampwidth(),stream.getframerate(),stream.getcomptype()) != (1,2,16000,'NONE'):
                raise InputError('AUDIO_FORMAT')
            declared = stream.getnframes()
            if declared != item['samples']:
                raise InputError('AUDIO_FRAME_COUNT')
            observed = 0
            while data := stream.readframes(32768):
                observed += len(data)
                if observed > MAX_SAMPLES * 2:
                    raise InputError('AUDIO_SIZE')
            if observed != declared * 2:
                raise InputError('AUDIO_TRUNCATED')
        if digest(path) != before:
            raise InputError('AUDIO_CHANGED_DURING_CHECK')
    except InputError:
        raise
    except (OSError, ValueError, EOFError, wave.Error):
        raise InputError('AUDIO_UNREADABLE') from None
    return {key:item[key] for key in ('id','speaker_id','kind','capacity_code','sha256','samples')} | {
        'duration_ms':item['samples'] * 1000 / 16000}


def inspect_manifest(path, sessions):
    if type(sessions) is not int or not 1 <= sessions <= 2_147_483_647:
        raise InputError('SESSIONS_RANGE')
    try:
        path = Path(path).resolve()
        with _open_regular(path,MAX_MANIFEST_BYTES) as stream:
            data = stream.read(MAX_MANIFEST_BYTES+1)
    except InputError:
        raise
    except (OSError,ValueError,RuntimeError):
        raise InputError('MANIFEST_UNREADABLE') from None
    if len(data)>MAX_MANIFEST_BYTES:
        raise InputError('MANIFEST_SIZE')
    try:
        document=json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise InputError('MANIFEST_JSON') from None
    if not isinstance(document,dict) or set(document)!={'schema','recordings','extra_capacity_codes'} or document['schema']!='svi-capacity-audio-inventory-v1':
        raise InputError('MANIFEST_SCHEMA')
    if not isinstance(document['recordings'],list) or not isinstance(document['extra_capacity_codes'],list):
        raise InputError('MANIFEST_SCHEMA')
    extras=document['extra_capacity_codes']
    if any(not _text(code) for code in extras) or len(set(extras))!=len(extras) or set(extras)&set(CODES):
        raise InputError('EXTRA_CAPACITY_CODES')
    required_codes=(*CODES,*extras)[:sessions]
    checked=[]
    for item in document['recordings']:
        checked.append(_audio(path.parent,item))
    if len({item['id'] for item in checked})!=len(checked):
        raise InputError('DUPLICATE_AUDIO_ID')
    missing=[]
    if len(required_codes)!=sessions:
        missing.append({'requirement':'MANUALLY_ANNOTATED_EXTRA_CODES','missing_count':sessions-len(required_codes)})
    for kind in sorted(KINDS):
        if not any(item['kind']==kind and item['capacity_code'] is None for item in checked):
            missing.append({'requirement':'BASE_AUDIO','kind':kind})
    speakers={item['speaker_id'] for item in checked}
    if len(speakers)<2:
        missing.append({'requirement':'TWO_SPEAKERS','observed_count':len(speakers)})
    for code in required_codes:
        for kind in ('A10','A30'):
            if not any(item['capacity_code']==code and item['kind']==kind for item in checked):
                missing.append({'requirement':'CAPACITY_VARIANT','capacity_code':code,'kind':kind})
    return {
        'schema':'svi-capacity-audio-preflight-v1',
        'status':'incomplete' if missing else 'files_checked_manual_review_required',
        'manifest_sha256':hashlib.sha256(data).hexdigest(),
        'tool_sha256':digest(Path(__file__)),
        'target_sessions':sessions,
        'planned_normal_groups':4,'planned_rounds_per_group':20,
        'planned_normal_sessions':80*sessions,
        'checked_audio':checked,'missing':missing,
        'acceptance_pass_claimed':False,'capacity_validation':'unverified',
        'still_required':[
            'Human provenance and frozen transcript/anchor review; WAV headers and labels cannot prove speech or speaker identity.',
            'Real-time 250ms streaming, original ACK credit, stable partials and per-account semantic checks.',
            'Four real ASR/LLM load groups, real Desktop insertion/readback, independent timing, and two-hour soak.',
            'Independently collected deployment/resource fingerprint and operator-controlled evidence publication.',
        ],
    }


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('--manifest',required=True)
    parser.add_argument('--sessions',type=int,default=20)
    parser.add_argument('--out',help='Create a new JSON result; never overwrite an existing file.')
    args=parser.parse_args(argv)
    try:
        report=inspect_manifest(args.manifest,args.sessions)
        if args.out:
            with Path(args.out).open('x',encoding='utf-8') as output:
                json.dump(report,output,ensure_ascii=False,indent=2)
                output.write('\n')
        print(json.dumps({'status':report['status'],'checked_audio_count':len(report['checked_audio']),
                          'missing_count':len(report['missing']),'capacity_validation':'unverified',
                          'acceptance_pass_claimed':False},ensure_ascii=False))
        return 2 if report['missing'] else 0
    except InputError as exc:
        print(json.dumps({'status':'invalid_input','code':exc.code,'acceptance_pass_claimed':False}))
        return 1
    except OSError:
        print(json.dumps({'status':'output_unavailable','acceptance_pass_claimed':False}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())

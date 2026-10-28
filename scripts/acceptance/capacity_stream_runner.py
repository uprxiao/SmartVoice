"""Real-time protocol load driver. Never certifies human/native/model capacity."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import stat
import struct
import sys
import time
from urllib.parse import urlsplit
import uuid
import wave

import httpx
from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capacity_preflight import CODES, InputError, MAX_SAMPLES, _open_regular, inspect_manifest

MAX_INPUT = 1_048_576


class LoadError(ValueError):
    """Codes only; never include response bodies or credential values."""


def require(value, code):
    if not value:
        raise LoadError(code)


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'INPUT_NOT_REGULAR')
        raw = stream.read(MAX_INPUT + 1)
    require(len(raw) <= MAX_INPUT, 'INPUT_TOO_LARGE')
    return json.loads(raw)


def base_url(value):
    u = urlsplit(value)
    require(u.scheme in ('http', 'https') and u.hostname and not u.username and not u.password
            and u.path in ('', '/') and not u.query and not u.fragment, 'BASE_URL')
    if u.scheme == 'http':
        try: local = ipaddress.ip_address(u.hostname).is_loopback
        except ValueError: local = False
        require(local, 'REMOTE_HTTPS_REQUIRED')
    require(u.port is None or u.port > 0, 'BASE_PORT')
    return value.rstrip('/')


@dataclass(frozen=True)
class Audio:
    id: str
    code: str
    kind: str
    speaker: str
    sha256: str
    pcm: bytes = field(repr=False)

    @property
    def samples(self): return len(self.pcm) // 2

    @property
    def seconds(self): return self.samples / 16000


def load_inputs(manifest_path, credentials_path, sessions):
    require(type(sessions) is int and sessions > 0, 'SESSION_COUNT')
    manifest_path = Path(manifest_path).resolve(strict=True)
    preflight = inspect_manifest(manifest_path, sessions)
    document = read_regular(manifest_path)
    codes = [*CODES, *document['extra_capacity_codes']][:sessions]
    require(len(codes) == sessions, 'CAPACITY_CODES_MISSING')
    # Base A120 and provenance may remain incomplete for a protocol diagnostic;
    # required normal variants must actually exist and all WAVs passed preflight.
    audio = {}
    for item in document['recordings']:
        if item['capacity_code'] not in codes or item['kind'] not in ('A10', 'A30'):
            continue
        path = (manifest_path.parent / item['file']).resolve(strict=True)
        require(path.is_relative_to(manifest_path.parent), 'AUDIO_PATH')
        with _open_regular(path, MAX_SAMPLES * 2 + MAX_INPUT) as stream:
            raw = stream.read(MAX_SAMPLES * 2 + MAX_INPUT + 1)
        require(len(raw) <= MAX_SAMPLES * 2 + MAX_INPUT, 'AUDIO_CHANGED')
        require(hashlib.sha256(raw).hexdigest() == item['sha256'], 'AUDIO_CHANGED')
        with wave.open(io.BytesIO(raw), 'rb') as stream:
            pcm = stream.readframes(item['samples'] + 1)
        require(len(pcm) == item['samples'] * 2, 'AUDIO_CHANGED')
        value = Audio(item['id'], item['capacity_code'], item['kind'], item['speaker_id'], item['sha256'], pcm)
        audio.setdefault((value.code, value.kind), []).append(value)
    require(all((code, kind) in audio for code in codes for kind in ('A10', 'A30')), 'NORMAL_AUDIO_MISSING')
    credentials = read_regular(credentials_path)
    require(isinstance(credentials, dict) and set(credentials) == {'schema', 'accounts'}
            and credentials['schema'] == 'svi-capacity-run-accounts-v1', 'ACCOUNTS_SCHEMA')
    accounts = credentials['accounts']
    require(isinstance(accounts, list) and len(accounts) == sessions, 'ACCOUNT_COUNT')
    for item in accounts:
        require(isinstance(item, dict) and set(item) == {'lane', 'capacity_code', 'email', 'password', 'device'}, 'ACCOUNT_SCHEMA')
        require(all(isinstance(item[k], str) and item[k] for k in ('lane', 'capacity_code', 'email', 'password')), 'ACCOUNT_VALUE')
        require(isinstance(item['device'], dict), 'DEVICE_SCHEMA')
        device = item['device']
        require(set(device) == {'installation_id','name','platform','client_version'}
                and all(isinstance(v,str) and v for v in device.values()), 'DEVICE_SCHEMA')
        try: uuid.UUID(device['installation_id'])
        except ValueError: raise LoadError('DEVICE_INSTALLATION_ID') from None
        require(len(device['name'])<=64 and len(device['client_version'])<=32, 'DEVICE_SCHEMA')
    require([a['capacity_code'] for a in accounts] == codes, 'ACCOUNT_CODE_ORDER')
    for key in ('lane', 'email', 'capacity_code'):
        require(len({a[key] for a in accounts}) == sessions, 'ACCOUNT_DUPLICATE')
    return accounts, audio, preflight


class Journal:
    def __init__(self, path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.stream = os.fdopen(fd, 'w', encoding='utf-8')

    def write(self, lane, event, **fields):
        # Callers must never pass HTTP headers, authentication input/output.
        self.stream.write(json.dumps({'lane': lane, 'event': event, 'monotonic': time.monotonic(), **fields}, ensure_ascii=False)+'\n')
        self.stream.flush()

    def close(self): self.stream.close()


@dataclass
class Identity:
    lane: str
    code: str
    access: str = field(repr=False)
    refresh: str = field(repr=False)
    renew_at: float


async def authenticate(client, account=None, previous=None):
    path = '/api/auth/refresh' if previous else '/api/auth/login'
    body = {'refresh_token': previous.refresh} if previous else {
        'email': account['email'], 'password': account['password'], 'client_type': 'desktop', 'device': account['device']}
    for attempt in range(4):
        response = await client.post(path, json=body)
        if response.status_code != 429:
            break
        delay = response.headers.get('Retry-After', '')
        require(delay.isdigit() and 1 <= int(delay) <= 600 and attempt < 3, 'AUTH_RATE_LIMITED')
        await asyncio.sleep(int(delay))
    require(response.status_code == 200, 'AUTH_PREPARATION_FAILED')
    value = response.json()
    require(isinstance(value.get('access_token'), str) and isinstance(value.get('refresh_token'), str)
            and type(value.get('expires_in')) is int and value['expires_in'] > 30, 'AUTH_RESPONSE')
    return Identity(previous.lane if previous else account['lane'], previous.code if previous else account['capacity_code'],
                    value['access_token'], value['refresh_token'], time.monotonic()+value['expires_in']-30)


def percentile(values, fraction):
    require(values and 0 < fraction <= 1, 'PERCENTILE_INPUT')
    value = sorted(values)[math.ceil(len(values)*fraction)-1]
    return None if not math.isfinite(value) else math.ceil(value)


def group_summary(rows):
    # A non-ready outcome is +infinity, never silently omitted from percentiles.
    times = [r.get('server_stop_to_result_ms', math.inf) if r['outcome'] == 'ready' else math.inf for r in rows]
    return {'observations': len(rows), 'ready': sum(r['outcome'] == 'ready' for r in rows),
            'failed_or_fallback': sum(r['outcome'] != 'ready' for r in rows),
            'p50_ms': percentile(times, .5) if times else None, 'p95_ms': percentile(times, .95) if times else None,
            'semantic_review': 'pending', 'capacity_validation': 'unverified'}


async def stream_audio(ws, audio, start, journal, identity, session_id):
    sent, acked, seq, stable, revision, end_at = 0, 0, -1, '', 0, None
    unacked = deque()
    changed = asyncio.Event()
    end_ack = False
    final_text = None
    stable_before_end = False

    def observe(message):
        require(isinstance(message, dict) and message.get('session_id') == session_id
                and message.get('attempt_no', 1) == 1, 'CROSS_SESSION_EVENT')

    async def receive():
        nonlocal acked, stable, revision, end_ack, final_text, stable_before_end
        while True:
            deadline = unacked[0][1]+2 if unacked else time.monotonic()+30
            if end_at is not None: deadline = min(deadline, end_at+30)
            async with asyncio.timeout(max(0, deadline-time.monotonic())):
                message = json.loads(await ws.recv())
            observe(message)
            journal.write(identity.lane, 'ws.receive', body=message)
            kind = message.get('type')
            if kind == 'audio.ack':
                total, last = message.get('total_samples'), message.get('seq')
                require(type(total) is int and acked <= total <= sent and type(last) is int and 0 <= last <= seq, 'ACK_RANGE')
                require(total == min((last+1)*4000, audio.samples), 'ACK_SAMPLES')
                acked = total
                while unacked and unacked[0][0] <= acked: unacked.popleft()
                changed.set()
            elif kind == 'asr.partial':
                text, rev = message.get('stable_text'), message.get('revision')
                require(isinstance(text, str) and text.startswith(stable) and type(rev) is int and rev > revision, 'STABLE_PREFIX')
                require(type(message.get('decoded_samples')) is int and type(message.get('received_samples')) is int
                        and 0 <= message['decoded_samples'] <= message['received_samples'] <= sent, 'PARTIAL_SAMPLES')
                stable, revision = text, rev
                if text and end_at is None: stable_before_end = True
            elif kind == 'audio.end_ack':
                require(end_at is not None and message.get('last_seq') == seq and message.get('total_samples') == sent, 'END_ACK')
                end_ack = True
            elif kind == 'asr.final':
                require(end_ack and isinstance(message.get('text'), str) and message['text'].startswith(stable), 'FINAL_PREFIX')
                final_text = message['text']
            elif kind == 'result.ready':
                require(end_ack and final_text is not None, 'RESULT_ORDER')
                result = message.get('result')
                require(isinstance(result, dict) and result.get('attempt_no') == 1 and result.get('attempt_kind') == 'initial'
                        and result.get('raw_text') == final_text, 'RESULT_IDENTITY')
                return result
            elif kind in ('error', 'session.empty', 'cancelled'):
                raise LoadError('SERVER_TERMINAL_'+kind.upper().replace('.', '_'))
            else:
                raise LoadError('UNEXPECTED_EVENT')

    async def watch_ack():
        while True:
            changed.clear()
            if not unacked:
                await changed.wait()
                continue
            delay = unacked[0][1]+2-time.monotonic()
            require(delay > 0, 'ACK_DEADLINE')
            try:
                async with asyncio.timeout(delay): await changed.wait()
            except TimeoutError:
                raise LoadError('ACK_DEADLINE') from None

    async def send_and_finish():
        nonlocal sent, seq, end_at, start
        await asyncio.sleep(max(0, start-time.monotonic()))
        planned_start = start
        start = time.monotonic()
        journal.write(identity.lane, "record_start", planned_start=planned_start)
        previous_send = start
        for offset in range(0, len(audio.pcm), 8000):
            count = min(4000, (len(audio.pcm)-offset)//2)
            target = max(start+(sent+count)/16000, previous_send+count/16000)
            await asyncio.sleep(max(0, target-time.monotonic()))
            if reader.done(): await reader; raise LoadError('EARLY_RESULT')
            while unacked and sent-acked+count > 32000:
                changed.clear()
                async with asyncio.timeout(max(0, unacked[0][1]+2-time.monotonic())):
                    await changed.wait()
            require(not unacked or time.monotonic()-unacked[0][1] < 2, 'ACK_DEADLINE')
            seq += 1
            sent += count
            now = time.monotonic()
            unacked.append((sent, now))
            changed.set()
            journal.write(identity.lane, 'ws.send_frame', seq=seq, samples=count, total=sent,
                          target=target, send_started_at=now)
            await ws.send(struct.pack('<II', seq, count)+audio.pcm[offset:offset+count*2])
            previous_send = now
        end_at = time.monotonic()
        journal.write(identity.lane, 'ws.send_end', seq=seq, samples=sent)
        await ws.send(json.dumps({'type':'audio.end', 'last_seq':seq, 'total_samples':sent}))
        async with asyncio.timeout(30): result = await reader
        result_at = time.monotonic()
        async with asyncio.timeout(2): await ws.wait_closed()
        require(ws.close_code == 1000, 'CLOSE_CODE')
        require(acked == sent == audio.samples, 'ACK_INCOMPLETE')
        require(audio.kind != 'A30' or stable_before_end, 'A30_STABLE_MISSING')
        return result, {'record_start_monotonic': start, 'end_monotonic': end_at,
                        'client_stop_to_result_ms': (result_at-end_at)*1000,
                        'sent_samples': sent, 'acked_samples': acked, 'stable_before_end': stable_before_end}
    reader = asyncio.create_task(receive())
    sender = asyncio.create_task(send_and_finish())
    watchdog = asyncio.create_task(watch_ack())
    try:
        done, _ = await asyncio.wait((sender, watchdog), return_when=asyncio.FIRST_COMPLETED)
        if watchdog in done: await watchdog
        return await sender
    finally:
        for task in (reader, sender, watchdog):
            if not task.done(): task.cancel()
        await asyncio.gather(reader, sender, watchdog, return_exceptions=True)


async def lane(client, url, identity, audio, ready, gate, journal, other_codes=()):
    row = {'lane': identity.lane, 'capacity_code': identity.code, 'recording_id': audio.id, 'audio_sha256': audio.sha256,
           'samples': audio.samples, 'outcome': 'error', 'semantic_review':'pending'}
    headers = {'Authorization': 'Bearer '+identity.access}
    session_id = None
    try:
        key = str(uuid.uuid4())
        response = await client.post('/api/v1/sessions', json={'client_request_id':key, 'mode':'dictation', 'learning_enabled':False},
                                     headers={**headers, 'Idempotency-Key':key})
        row['create_status'] = response.status_code
        require(response.status_code == 201, 'SESSION_NOT_CREATED')
        session_id = str(uuid.UUID(response.json()['session_id']));row['session_id'] = session_id
        ws_url = ('wss' if url.startswith('https:') else 'ws')+url[url.index(':'):]+f'/ws/v1/sessions/{session_id}/audio'
        async with connect(ws_url, additional_headers=headers, proxy=None, open_timeout=10, max_size=1_048_576) as ws:
            async with asyncio.timeout(10): first = json.loads(await ws.recv())
            require(first.get('type') == 'ready' and first.get('session_id') == session_id and first.get('attempt_no') == 1, 'WS_NOT_READY')
            ready.set_result(True)
            starts = await gate
            result, observed = await stream_audio(ws, audio, starts[identity.lane], journal, identity, session_id)
            row.update(observed)
            fetched = await client.get(f'/api/v1/sessions/{session_id}', headers=headers)
            require(fetched.status_code == 200, 'RESULT_READBACK_FAILED')
            body = fetched.json()
            require(body.get('state') == 'ready' and body.get('current_attempt_no') == 1 and body.get('received_samples') == audio.samples
                    and body.get('audio_complete') is True and body.get('result') == result, 'RESULT_READBACK_MISMATCH')
            require(type(result.get('stop_to_result_ms')) is int and result['stop_to_result_ms'] >= 0, 'SERVER_LATENCY_MISSING')
            require(type(result.get('review_required')) is bool and all(isinstance(result.get(k), str) for k in ('raw_text','refined_text')), 'RESULT_SCHEMA')
            require(hashlib.sha256(result['refined_text'].encode()).hexdigest() == result.get('text_hash'), 'RESULT_TEXT_HASH')
            row['server_stop_to_result_ms'] = result['stop_to_result_ms']
            row['result_id'] = result['result_id']
            row['outcome'] = 'fallback' if result.get('review_required') is True else 'ready'
            row['raw_sha256'] = hashlib.sha256(result['raw_text'].encode()).hexdigest()
            row['refined_sha256'] = hashlib.sha256(result['refined_text'].encode()).hexdigest()
            row['own_code_in_both_texts'] = all(identity.code in result[k] for k in ('raw_text', 'refined_text'))
            row['foreign_code_found'] = any(code in result[k] for code in other_codes for k in ('raw_text','refined_text'))
            if not row['own_code_in_both_texts'] or row['foreign_code_found']:
                row['outcome'], row['reason'] = 'content_mismatch', 'CAPACITY_CODE_MISMATCH'
    except asyncio.CancelledError:
        row['reason'] = 'CLIENT_CANCELLED'
        raise
    except Exception as exc:
        row['reason'] = str(exc) if isinstance(exc, LoadError) else type(exc).__name__
    finally:
        if not ready.done(): ready.set_result(False)
        if session_id:
            # Only sessions created by this lane; failed cleanup stays explicit.
            try:
                fetched = await client.get(f'/api/v1/sessions/{session_id}', headers=headers)
                body = fetched.json() if fetched.status_code == 200 else {}
                if body.get('state') in ('created', 'streaming', 'finalizing', 'refining'):
                    cancelled = await client.post(f'/api/v1/sessions/{session_id}/cancel', headers=headers)
                    body = cancelled.json() if cancelled.status_code == 200 else {}
                row['session_terminal_confirmed'] = body.get('state') in ('ready','failed','empty','cancelled')
            except Exception:
                row['session_terminal_confirmed'] = False
            if not row['session_terminal_confirmed'] and row['outcome'] in ('ready', 'fallback'):
                row['outcome'], row['reason'] = 'error', 'SESSION_TERMINAL_UNCONFIRMED'
    return row


async def run_round(client, url, identities, clips, load, journal):
    require(load in ('staggered', 'burst'), 'LOAD_KIND')
    require(len(identities) == len(clips) > 0 and len({i.lane for i in identities}) == len(identities), 'ROUND_INPUTS')
    loop = asyncio.get_running_loop();gate = loop.create_future()
    ready = [loop.create_future() for _ in identities]
    tasks = [asyncio.create_task(lane(client,url,identity,clip,event,gate,journal,tuple(i.code for i in identities if i is not identity)))
             for identity,clip,event in zip(identities,clips,ready)]
    try:
        prepared = await asyncio.gather(*ready)
        origin = time.monotonic()+.1
        if load == 'burst':
            end = origin+max(c.seconds for c in clips)
            starts = {i.lane:end-c.seconds for i,c in zip(identities,clips)}
        else:
            step = min(.25, math.floor(min(c.seconds for c in clips)*1000/(2*len(clips)))/1000)
            starts = {i.lane:origin+n*step for n,i in enumerate(identities)}
        gate.set_result(starts)
        rows = await asyncio.gather(*tasks)
        stops = [r['end_monotonic'] for r in rows if 'end_monotonic' in r]
        spread = (max(stops)-min(stops))*1000 if len(stops)==len(rows) else None
        actual_starts = [r['record_start_monotonic'] for r in rows if 'record_start_monotonic' in r]
        all_overlap = len(actual_starts) == len(stops) == len(rows) and max(actual_starts) < min(stops)
        return {'load':load, 'all_prepared':all(prepared), 'all_recordings_overlap':all_overlap,
                'stop_spread_ms':spread, 'burst_window_valid':load!='burst' or spread is not None and spread<=250,
                'lanes':rows, 'summary':group_summary(rows)}
    finally:
        if not gate.done(): gate.cancel()
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def execute(args):
    url = base_url(args.base_url)
    accounts, audio, preflight = load_inputs(args.manifest,args.credentials,args.sessions)
    output = Path(args.out_dir);output.mkdir(mode=0o700, parents=False, exist_ok=False)
    journal = Journal(output/'trace.private.jsonl')
    result = {'schema':'svi-capacity-protocol-run-v1','target_sessions':args.sessions,'rounds_per_group':args.rounds,
              'clock':'time.monotonic','groups':[], 'audio_manifest_sha256':preflight['manifest_sha256'],
              'capacity_validation':'unverified','acceptance_pass_claimed':False,
              'not_verified':['human provenance/complete semantic review','N-1 protocol + real Desktop readback',
                              'trusted environment identity/quotas','two-hour mixed soak and resource closure']}
    try:
        async with httpx.AsyncClient(base_url=url,timeout=10,trust_env=False,follow_redirects=False) as client:
            identities = [await authenticate(client,account=a) for a in accounts]
            accounts = None  # Tokens remain only in process memory.
            for load in ('staggered','burst'):
                for kind in ('A10','A30'):
                    group = {'load':load,'kind':kind,'rounds':[]}
                    result['groups'].append(group)
                    for round_number in range(1,args.rounds+1):
                        identities = [await authenticate(client,previous=i) if time.monotonic()>=i.renew_at else i for i in identities]
                        clips = [audio[(i.code,kind)][(round_number-1)%len(audio[(i.code,kind)])] for i in identities]
                        observed = await run_round(client,url,identities,clips,load,journal)
                        observed['round'] = round_number;group['rounds'].append(observed)
                        print(json.dumps({'load':load,'kind':kind,'round':round_number,**observed['summary']}),flush=True)
                    group['summary'] = group_summary([r for batch in group['rounds'] for r in batch['lanes']])
            result['completed'] = True
            result['protocol_run_passed'] = all(
                batch['all_prepared'] and batch['all_recordings_overlap'] and batch['burst_window_valid']
                and batch['summary']['failed_or_fallback'] == 0
                for group in result['groups'] for batch in group['rounds'])
    except Exception as exc:
        result['completed'] = False
        result['error_code'] = str(exc) if isinstance(exc,(LoadError,InputError)) else type(exc).__name__
    finally:
        journal.close()
        with (output/'result.json').open('x',encoding='utf-8') as stream:
            json.dump(result,stream,ensure_ascii=False,indent=2);stream.write('\n')
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('--base-url',required=True);parser.add_argument('--manifest',required=True)
    parser.add_argument('--credentials',required=True);parser.add_argument('--out-dir',required=True)
    parser.add_argument('--sessions',type=int,default=20);parser.add_argument('--rounds',type=int,default=20)
    args=parser.parse_args(argv)
    try:
        require(args.rounds>0,'ROUND_COUNT')
        result=asyncio.run(execute(args))
        print(json.dumps({'completed':result['completed'],'capacity_validation':'unverified','acceptance_pass_claimed':False}))
        return (0 if result['protocol_run_passed'] else 2) if result['completed'] else 1
    except Exception as exc:
        print(json.dumps({'completed':False,'error_code':str(exc) if isinstance(exc,(LoadError,InputError)) else type(exc).__name__,
                          'capacity_validation':'unverified','acceptance_pass_claimed':False}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())

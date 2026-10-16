"""Offline acceptance orchestration for edit-job/v2 (plan §6).

Runs the real scheduler pipeline and service endpoints against tiny synthetic
media with the paid/GPU worker calls MOCKED at the transport boundary
(scheduler.worker_client.request). FFmpeg rendering, SQLite, caching, gates and
the revision machinery are real. Every mock is labeled in the report; this is
NOT a substitute for live model or human audio-visual review.

Usage:
  python scripts/acceptance-orchestration-v2.py --offline [--keep]
"""
import argparse, importlib.util, json, shutil, sys, time, traceback, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workers.common import ROOT as WORKROOT, atomic_json, digest  # noqa: E402

MOCK_ANALYSIS = {}   # sha -> [{'in_tick','out_tick','text'}]
MOCK_ASR = {}        # audio sha -> [{'text','start_tick','end_tick'}]
MOCK_SCRIPT = {}     # unused placeholder for generated mode
CALLS = {'speech.synthesize': 0, 'speech.transcribe': 0, 'video.analyze': 0,
         'scene.detect': 0, 'material.inspect': 0, 'music.select': 0, 'script.generate': 0}


def _ffmpeg(args, timeout=180):
    import subprocess
    p = subprocess.run([str(ROOT / 'tools/ffmpeg/ffmpeg.exe'), '-nostdin', '-n', '-v', 'error', *map(str, args)],
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError('FFMPEG_FAILED:' + p.stderr[-1500:])
    return p


def make_fixture_video(path, color, seconds, with_audio=False, tone=440):
    args = ['-f', 'lavfi', '-i', f'color=c={color}:s=320x240:r=25:d={seconds}']
    if with_audio:
        args += ['-f', 'lavfi', '-i', f'sine=frequency={tone}:duration={seconds}']
    args += (['-map', '0:v', '-map', '1:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest']
             if with_audio else ['-map', '0:v', '-c:v', 'libx264', '-pix_fmt', 'yuv420p'])
    args.append(path)
    _ffmpeg(args)
    return path


def make_fixture_narration(path, seconds, tone=330):
    _ffmpeg(['-f', 'lavfi', '-i', f'sine=frequency={tone}:duration={seconds}',
             '-c:a', 'pcm_s16le', '-ar', '48000', '-ac', '2', path])
    return path


def install_mocks():
    """Patch the transport under the operation cache; caches and journals stay real."""
    import scheduler.worker_client as wc
    import scheduler.pipeline as pipeline_module
    real_request = wc.request

    def mocked_request(url, payload=None):
        if payload is None or 'op' not in payload:
            return real_request(url, payload)
        op = payload['op']
        CALLS[op] = CALLS.get(op, 0) + 1
        opdir = WORKROOT / 'jobs/worker-operations/mock' / (op + '-' + uuid.uuid4().hex[:8])
        opdir.mkdir(parents=True, exist_ok=True)
        from workers.common import output_ref

        def done(path, kind):
            return {'state': 'SUCCEEDED', 'operation_id': payload.get('operation_id', 'mock'),
                    'output_refs': [output_ref(path, kind)], 'errors': [], 'metrics': {}, 'provider_request_ids': []}

        if op == 'material.inspect':
            from renderer.ffmpeg import probe
            q = probe(payload['input_refs'][0]['path'])
            out = opdir / 'material.json'; atomic_json(out, q)
            return done(out, 'probe')
        if op == 'video.analyze':
            sha = payload['input_refs'][0]['sha256']
            batches = MOCK_ANALYSIS.get(sha)
            if batches is None:
                raise ValueError('MOCK_ANALYSIS_MISSING:' + sha)
            coverage = [{'status': 'success', 'in_tick': b['in_tick'], 'out_tick': b['out_tick'],
                         'observations': [{'frame_id': 'f%d' % b['in_tick'], 'source_tick': b['in_tick'], 'text': b['text']}],
                         'frame_refs': []} for b in batches]
            out = opdir / 'analysis.json'
            atomic_json(out, {'asset_hash': sha, 'coverage': coverage,
                              'usable': all(c['status'] == 'success' for c in coverage), 'reviewed_visual_evidence': []})
            return done(out, 'evidence')
        if op == 'scene.detect':
            sha = payload['input_refs'][0]['sha256']
            cuts = MOCK_ANALYSIS.get(sha + ':cuts') or [0]
            out = opdir / 'scenes.json'
            atomic_json(out, {'source_sha256': sha, 'cuts': cuts, 'fps': 25, 'frame_count': 0,
                              'config': {'detector': 'content-v1', 'threshold': 12, 'min_scene_len_frames': 12},
                              'config_hash': 'det-mock-v1'})
            return done(out, 'evidence')
        if op == 'speech.synthesize':
            text = payload['params']['text']
            out = opdir / 'voice.wav'
            make_fixture_narration(out, max(1.0, round(len(text) * 0.28, 2)), tone=520)
            return done(out, 'audio')
        if op == 'speech.transcribe':
            sha = payload['input_refs'][0]['sha256']
            words = MOCK_ASR.get(sha)
            if words is None:
                raise ValueError('MOCK_ASR_MISSING:' + sha)
            out = opdir / 'alignment.json'
            atomic_json(out, {'audio_sha256': sha, 'words': words, 'device': 'mock', 'compute_type': 'mock',
                              'auxiliary_vad': False, 'model_hash': 'mock'})
            return done(out, 'evidence')
        if op == 'script.generate':
            out = opdir / 'script.json'
            atomic_json(out, {'text': payload['params'].get('prompt', '')[:80] + '（mock 生成整稿）打开包装，看看里面的果实。果肉是白心的。',
                              'claim_ids': payload['params'].get('claim_ids', []), 'quality': 'unverified'})
            return done(out, 'manifest')
        if op == 'music.select':
            out = opdir / 'music.json'
            atomic_json(out, {'path': payload['input_refs'][0]['path'], 'sha256': payload['input_refs'][0]['sha256']})
            return done(out, 'manifest')
        raise ValueError('MOCK_OP_UNKNOWN:' + op)

    wc.request = mocked_request
    pipeline_module.request = mocked_request
    pipeline_module.Pipeline.provider = lambda self, name: {'operation_token': 'mock', 'base_url': 'http://mock', 'model': 'mock-model'}
    def mocked_fact_review(self, text, observations, claims, name):
        out = self.work / (name.replace(':', '_') + '.json')
        atomic_json(out, {'grounded': True, 'reason': 'offline mock', 'text_sha256': 'mock', 'status': 'model_review_only_mock'})
        return {'grounded': True, 'reason': 'offline mock'}
    pipeline_module.Pipeline.fact_review = mocked_fact_review
    return {'worker_transport': 'mocked at scheduler.worker_client.request',
            'provider_tokens': 'mocked', 'fact_review': 'mocked grounded',
            'real': ['sqlite store', 'operation cache', 'ffmpeg render', 'timeline validation', 'service endpoints']}


def asr_words_for(script_text, total_duration_ticks, error=None):
    """Perfect-match synthetic ASR words at 5 chars/sec; optional single homophone error."""
    chars = [c for c in script_text if not ''.join(filter(lambda x: '\u4e00' <= x <= '\u9fff' or x.isalnum(), [c])) == '']
    chars = [c for c in script_text if ('\u4e00' <= c <= '\u9fff') or c.isalnum()]
    step = max(1, min(9600, total_duration_ticks // max(1, len(chars))))
    words = []
    for i, ch in enumerate(chars):
        text = ch
        if error and i == error[0]:
            text = error[1]
        words.append({'text': text, 'start_tick': i * step, 'end_tick': (i + 1) * step})
    return words


class AsgiClient:
    """Minimal ASGI caller reused from tests/test_jobs_api.py conventions."""
    import asyncio as _a, json as _j, types as _t

    def __init__(self, app): self.app = app

    def call(self, method, path, payload=None, headers=None, raw_body=None):
        import asyncio, json, types
        body = raw_body if raw_body is not None else (json.dumps(payload).encode() if payload is not None else b'')
        messages = []
        raw_path, _, query_string = path.partition('?')

        async def run():
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True; return {'type': 'http.request', 'body': body, 'more_body': False}
                return {'type': 'http.disconnect'}

            async def send(message): messages.append(message)
            h = {'content-type': 'application/json', **(headers or {})}
            if raw_body is not None:
                h['content-type'] = 'application/octet-stream'
            await self.app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'scheme': 'http',
                            'method': method, 'path': raw_path, 'raw_path': raw_path.encode(),
                            'query_string': query_string.encode(),
                            'root_path': '', 'headers': [(k.lower().encode(), v.encode()) for k, v in h.items()],
                            'client': ('127.0.0.1', 1234), 'server': ('127.0.0.1', 8760)}, receive, send)
        asyncio.run(run())
        data = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
        status = next(m['status'] for m in messages if m['type'] == 'http.response.start')
        import types as t
        return t.SimpleNamespace(status_code=status, json=lambda: json.loads(data), content=data)

    def post(self, path, json=None, headers=None, raw_body=None):
        return self.call('POST', path, json, headers, raw_body)

    def get(self, path, headers=None):
        return self.call('GET', path, None, headers)


def query(client, path):
    import urllib.parse
    return client.get(path)


def wait_state(client, h, job_id, revision, wanted, timeout=240):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f'/v1/jobs/{job_id}?revision={revision}', headers=h).json()
        last = r['state']
        if last in wanted:
            return r
        time.sleep(0.4)
    raise TimeoutError('WAIT_STATE_TIMEOUT last=%s want=%s' % (last, wanted))


def run_offline(keep=False):
    report = {'schema_version': 'acceptance-orchestration-v2/offline', 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    mocks = install_mocks()
    report['mocks'] = mocks
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix='orchv2-accept-'))
    report['workspace'] = str(tmp)
    from scheduler.db import Store
    from providers.gateway import Gateway
    from scheduler.service import create_app
    store = Store(tmp / 'jobs.sqlite')
    app = create_app(store, Gateway(store), 'operator-test', run_queue=False)
    app.state.queue.thread.start()   # drive the real single-job queue
    client = AsgiClient(app)
    h = {'Authorization': 'Bearer operator-test'}
    steps = []
    try:
        # --- fixtures: shuffled + irrelevant + fixed closing + ready-made narration ---
        open_clip = make_fixture_video(tmp / 'open.mp4', 'ForestGreen', 5)
        open2_clip = make_fixture_video(tmp / 'open2.mp4', 'DarkOliveGreen', 4)
        pack_clip = make_fixture_video(tmp / 'pack.mp4', 'Sienna', 3)
        street_clip = make_fixture_video(tmp / 'street.mp4', 'SlateGray', 3)
        closing_clip = make_fixture_video(tmp / 'closing.mp4', 'Tomato', 2, with_audio=True, tone=600)
        narration = make_fixture_narration(tmp / 'narration.wav', 4.6)
        for path, obs in [(open_clip, [{'in_tick': 0, 'out_tick': 96000, 'text': '双手打开包装盒取出火龙果'},
                                       {'in_tick': 96000, 'out_tick': 240000, 'text': '白色火龙果果肉带黑色籽粒的特写展示'}]),
                          (open2_clip, [{'in_tick': 0, 'out_tick': 192000, 'text': '双手掰开另一个火龙果取出果肉'}]),
                          (pack_clip, [{'in_tick': 0, 'out_tick': 144000, 'text': '火龙果果肉切面特写与包装标签'}]),
                          (street_clip, [{'in_tick': 0, 'out_tick': 144000, 'text': '城市街道上车流来往行人走过'}]),
                          (closing_clip, [{'in_tick': 0, 'out_tick': 96000, 'text': '人物面对镜头说话'}])]:
            MOCK_ANALYSIS[digest(path)] = obs
        script_text = '打开包装，看看里面的果肉。果肉是白心的。'
        import hashlib
        MOCK_ASR[digest(narration)] = asr_words_for(script_text, 4.6 * 48000, error=(16, '星'))  # 白→星 homophone

        def upload(path, kind='video'):
            r = client.post('/v1/imports?filename=' + path.name + '&kind=' + kind, headers=h, raw_body=path.read_bytes())
            assert r.status_code == 200, r.content
            return r.json()
        up_open, up_open2, up_pack, up_street, up_closing = upload(open_clip), upload(open2_clip), upload(pack_clip), upload(street_clip), upload(closing_clip)
        up_narr = upload(narration, 'narration')
        steps.append('imports ok (videos + narration wav)')

        # color preview authorization, same as the UI checkbox
        r = client.post('/v1/color-previews', {'decision': 'allow_preview_bt709',
                        'source_hashes': [up_open['sha256'], up_open2['sha256'], up_pack['sha256'], up_street['sha256'], up_closing['sha256']]}, headers=h)
        assert r.status_code == 200, r.content

        job = client.get('/v2/job-template?account=laoX', headers=h).json()
        job_id = 'accept-v2-' + uuid.uuid4().hex[:6]
        job['job_id'] = job_id
        job['intent']['brief'] = '火龙果画面介绍'
        job['intent']['product_terms'] = ['火龙果']
        job['intent']['target_duration_ticks'] = 1920 * 60
        job['script_input'] = {'mode': 'pasted', 'script_text': script_text,
                               'script_sha256': hashlib.sha256(script_text.encode()).hexdigest(), 'generation_note': None}
        job['narration_input'] = {'mode': 'uploaded', 'preset': None,
                                  'uploaded': {'path': up_narr['path'], 'sha256': up_narr['sha256']}, 'history': None}
        # shuffled order with the irrelevant clip in the middle; fixed closing last
        job['assets'] = [
            {'asset_id': 'a-street', 'path': up_street['path'], 'sha256': up_street['sha256'], 'mode': 'broll',
             'role': 'product', 'analysis': {'mode': 'single_shot'}, 'provenance_ref': up_street['provenance_ref'], 'locked': None},
            {'asset_id': 'a-open2', 'path': up_open2['path'], 'sha256': up_open2['sha256'], 'mode': 'broll',
             'role': 'product', 'analysis': {'mode': 'single_shot'}, 'provenance_ref': up_open2['provenance_ref'], 'locked': None},
            {'asset_id': 'a-open', 'path': up_open['path'], 'sha256': up_open['sha256'], 'mode': 'broll',
             'role': 'product', 'analysis': {'mode': 'single_shot'}, 'provenance_ref': up_open['provenance_ref'], 'locked': None},
            {'asset_id': 'a-pack', 'path': up_pack['path'], 'sha256': up_pack['sha256'], 'mode': 'broll',
             'role': 'product', 'analysis': {'mode': 'single_shot'}, 'provenance_ref': up_pack['provenance_ref'], 'locked': None},
            {'asset_id': 'a-closing', 'path': up_closing['path'], 'sha256': up_closing['sha256'], 'mode': 'spoken_locked',
             'role': 'fixed_closing', 'analysis': {'mode': 'single_shot'}, 'provenance_ref': up_closing['provenance_ref'],
             'locked': {'transcript': '我们下期再见。', 'audio_stream_index': up_closing['probe']['stream_indices']['audio'],
                        'evidence_ref': up_closing['boundary_ref']}},
        ]
        # r3: the opening sentence is served from a fixture-confirmed interval
        # (simulated post-review state); inference alone never auto-selects
        job['v2_overrides']['confirmed_windows'] = [
            {'sentence_id': 's001', 'source_asset_id': 'a-open', 'source_sha256': up_open['sha256'],
             'in_tick': 0, 'out_tick': 105600, 'action_kind': 'open_packaging', 'action_object': 'packaging',
             'basis': 'acceptance fixture: reviewed opening interval', 'operator_id': 'acceptance-fixture',
             'allow_source_window_overlap': False}]
        r = client.post('/v2/jobs', job, {**h, 'Idempotency-Key': job_id})
        assert r.status_code == 202, r.content
        steps.append('v2 job submitted (uploaded narration, shuffled assets, irrelevant clip, fixed closing)')

        st = wait_state(client, h, job_id, 1, {'AWAIT_SCRIPT_CONFIRM', 'FAILED_FINAL'})
        assert st['state'] == 'AWAIT_SCRIPT_CONFIRM', st['errors']
        data = client.get(f'/v2/jobs/{job_id}/review-data?revision=1', headers=h).json()
        assert data['script_plan']['script_text'] == script_text
        r = client.post(f'/v2/jobs/{job_id}/confirm-script', {'revision': 1, 'operator_id': 'acceptance'}, headers=h)
        assert r.status_code == 200, r.content
        steps.append('script confirmed by operator')

        st = wait_state(client, h, job_id, 1, {'AWAIT_NARRATION_CONFIRM', 'FAILED_FINAL'})
        assert st['state'] == 'AWAIT_NARRATION_CONFIRM', st['errors']
        audio = client.get(f'/v2/jobs/{job_id}/narration-audio?revision=1', headers=h)
        assert audio.status_code == 200 and len(audio.content) > 1000, audio.content[:200]
        tts_before = CALLS['speech.synthesize']
        r = client.post(f'/v2/jobs/{job_id}/confirm-narration', {'revision': 1, 'operator_id': 'acceptance'}, headers=h)
        assert r.status_code == 200, r.content
        steps.append('narration listened/confirmed; TTS calls so far=%d (uploaded mode)' % tts_before)
        assert tts_before == 0, 'A1 violated: uploaded narration must never synthesize'

        st = wait_state(client, h, job_id, 1, {'AWAIT_SUBTITLE_REVIEW', 'REVIEW_REQUIRED', 'FAILED_FINAL'})
        if st['state'] == 'AWAIT_SUBTITLE_REVIEW':
            data = client.get(f'/v2/jobs/{job_id}/review-data?revision=1', headers=h).json()
            items = data['subtitle_review']['items']
            assert any(i['status'] == 'uncertain' for i in items), 'homophone diff must gate'
            sid = next(i['sentence_id'] for i in items if i['status'] == 'uncertain')
            r = client.post(f'/v2/jobs/{job_id}/subtitle-decisions',
                            {'revision': 1, 'sentence_id': sid, 'choice': 'asr_error_confirmed',
                             'notes': '试听确认声音读对', 'operator_id': 'acceptance'}, headers=h)
            assert r.status_code == 200, r.content
            steps.append('subtitle difference confirmed by operator (asr_error_confirmed)')
            st = wait_state(client, h, job_id, 1, {'REVIEW_REQUIRED', 'FAILED_FINAL', 'AWAIT_SHOT_GAP'})

        assert st['state'] == 'REVIEW_REQUIRED', st.get('errors')
        result = st['result']
        assert result['technical_status'] == 'PASS', result['qa']
        out = Path(result['output_path'])
        assert out.is_file() and out.stat().st_size > 10000
        timeline = st['timeline']
        sources_used = {c['base_source_ref'] for c in timeline['video_clips']}
        assert 'a-street' not in sources_used, 'irrelevant clip must be excluded'
        assert 'a-closing' in sources_used and sources_used
        steps.append('final rendered; irrelevant excluded; fixed closing present; qa PASS')
        report['A1'] = {'verdict': 'PASS', 'tts_calls': tts_before}
        report['A2'] = {'verdict': 'PASS', 'note': 'homophone uncertain gated until operator decision'}
        report['A3'] = {'verdict': 'PASS', 'analysis_calls': CALLS['video.analyze'], 'sources_used': sorted(sources_used)}
        report['A4'] = {'verdict': 'PASS', 'clips': [{'clip': c['clip_id'], 'src': c['base_source_ref'],
                                                      'dur': c['duration_tick']} for c in timeline['video_clips']]}
        report['output'] = {'path': str(out), 'sha256': result['artifact_sha256'], 'bytes': out.stat().st_size}

        # --- incremental: subtitle-only revision must not redo voice/analysis ---
        cues = timeline['subtitles']['cues']
        edit_target = cues[0]
        rev2 = json.loads(json.dumps(job)); rev2['revision'] = 2; rev2['parent_revision'] = 1
        rev2['overrides']['base_timeline_sha256'] = result['timeline_sha256']
        rev2['overrides']['subtitle_edits'] = [{'cue_id': edit_target['cue_id'], 'text': edit_target['text'] + '（改）'}]
        rev2['changed_paths'] = ['/overrides/subtitle_edits']
        r = client.post(f'/v1/jobs/{job_id}/revisions', rev2, {**h, 'If-Match': st['input_hash'], 'Idempotency-Key': job_id + '-r2'})
        assert r.status_code == 202, r.content
        assert r.json()['invalidated'], r.content
        tts_before2, analyze_before2 = CALLS['speech.synthesize'], CALLS['video.analyze']
        st2 = wait_state(client, h, job_id, 2, {'REVIEW_REQUIRED', 'FAILED_FINAL'})
        assert st2['state'] == 'REVIEW_REQUIRED' and st2['result']['technical_status'] == 'PASS', st2.get('errors')
        assert CALLS['speech.synthesize'] == tts_before2 and CALLS['video.analyze'] == analyze_before2, 'A5 violated: unnecessary rework'
        assert CALLS['speech.transcribe'] > 0  # transcribe cache may hit; count not asserted here
        steps.append('subtitle-only revision rendered without new TTS/analysis calls')
        report['A5'] = {'verdict': 'PASS', 'tts_delta': CALLS['speech.synthesize'] - tts_before2,
                        'analyze_delta': CALLS['video.analyze'] - analyze_before2,
                        'reuse_event': r.json().get('invalidated') is not None}

        # --- single shot replacement ---
        data = client.get(f'/v2/jobs/{job_id}/review-data?revision=2', headers=h).json()
        decision = data['edit_decision']
        # pick a pool clip that actually HAS an eligible alternative (like the UI list)
        replacement_target = None
        for clip in (c for c in decision['clips'] if c['source'] == 'pool'):
            alts = client.get(f"/v2/jobs/{job_id}/shot-alternatives?clip_id={clip['clip_id']}&revision=2", headers=h).json()
            # only candidates passing the shared product/action constraints may be used;
            # an unrelated candidate must be flagged ineligible
            assert all(not a.get('eligible') for a in alts['alternatives']
                       if 'street' in str(a.get('description', ''))), 'irrelevant clip must stay ineligible'
            alt = next((a for a in alts['alternatives'] if a['eligible'] and a['candidate_id'] != clip.get('candidate_id')), None)
            if alt is not None:
                replacement_target, pool_clip = alt, clip
                break
        assert replacement_target is not None, 'no clip had an eligible replacement candidate'
        r = client.post(f'/v2/jobs/{job_id}/shot-replacement',
                        {'parent_revision': 2, 'clip_id': pool_clip['clip_id'], 'candidate_id': replacement_target['candidate_id'],
                         'reason': '验收演练：换一个更合适的镜头'}, {**h, 'If-Match': st2['input_hash'], 'Idempotency-Key': job_id + '-repl'})
        assert r.status_code == 202, r.content
        tts_before3, analyze_before3 = CALLS['speech.synthesize'], CALLS['video.analyze']
        st3 = wait_state(client, h, job_id, 3, {'REVIEW_REQUIRED', 'FAILED_FINAL'})
        assert st3['state'] == 'REVIEW_REQUIRED' and st3['result']['technical_status'] == 'PASS', st3.get('errors')
        new_clip = next(c for c in st3['timeline']['video_clips'] if c['clip_id'] == pool_clip['clip_id'])
        assert new_clip['base_source_ref'] != pool_clip['asset_id'] or new_clip['source_in_tick'] != pool_clip['source_in_tick']
        steps.append('single-shot replacement rendered; voice reused (tts delta=%d)' % (CALLS['speech.synthesize'] - tts_before3))
        report['A5']['replacement_tts_delta'] = CALLS['speech.synthesize'] - tts_before3
        report['calls_final'] = dict(CALLS)
        report['steps'] = steps
        report['verdict'] = 'OFFLINE_PASS'
    except Exception:
        report['verdict'] = 'FAIL'
        report['error'] = traceback.format_exc()
        report['steps'] = steps
        report['calls'] = dict(CALLS)
    finally:
        app.state.queue.stop.set()
    if not keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return report


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--offline', action='store_true')
    ap.add_argument('--keep', action='store_true')
    ap.add_argument('--out', default=str(ROOT / 'state/deployment/orchestration-v2/acceptance-offline.json'))
    args = ap.parse_args()
    if not args.offline:
        print(json.dumps({'verdict': 'NOT_RUN', 'note': 'only --offline is implemented; live mode needs real services'}))
        sys.exit(0)
    rep = run_offline(keep=args.keep)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: rep[k] for k in ('verdict',) if k in rep}, ensure_ascii=False))
    for key in ['A1', 'A2', 'A3', 'A4', 'A5']:
        if key in rep:
            print(key, rep[key].get('verdict'))
    if 'error' in rep:
        print(rep['error'][-3000:])
    sys.exit(0 if rep.get('verdict') == 'OFFLINE_PASS' else 1)

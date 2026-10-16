"""Three narration entrances: auto TTS, uploaded audio, prior confirmed narration.

No silent fallback between modes. uploaded/history never create TTS nodes.
"""
import hashlib
from pathlib import Path
from scheduler.contracts import validate_named, canonical_hash


def _digest_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1048576), b''):
            h.update(b)
    return h.hexdigest()


PAUSE_FLOOR_DBFS = -35


def cap_pauses(source, out_dir, max_pause_ms):
    """Shorten every pause (10 ms RMS below PAUSE_FLOOR_DBFS) longer than max_pause_ms to
    that length, keeping half of it on each side so word tails stay intact. Speech frames
    are copied unchanged. Returns the compacted wav (content-addressed, reruns reuse it)."""
    import array
    import math
    import wave
    out = Path(out_dir) / ('voice-cap%d-%s.wav' % (max_pause_ms, _digest_file(source)[:16]))
    if out.is_file():
        return out
    with wave.open(str(source), 'rb') as w:
        params = w.getparams()
        if params.sampwidth != 2:
            raise ValueError('NARRATION_PCM16_REQUIRED')
        data = array.array('h', w.readframes(params.nframes))
    step = params.framerate // 100 * params.nchannels
    floor = 32768 * 10 ** (PAUSE_FLOOR_DBFS / 20)
    quiet = []
    for i in range(0, len(data), step):
        seg = data[i:i + step]
        quiet.append(math.sqrt(sum(x * x for x in seg) / len(seg)) < floor)
    keep = max_pause_ms // 10
    result = array.array('h')
    i = 0
    while i < len(quiet):
        j = i
        while j < len(quiet) and quiet[j] == quiet[i]:
            j += 1
        if quiet[i] and j - i > keep:
            head = keep // 2
            result.extend(data[i * step:(i + head) * step])
            result.extend(data[(j - keep + head) * step:j * step])
        else:
            result.extend(data[i * step:j * step])
        i = j
    tmp = out.with_suffix('.tmp.wav')
    with wave.open(str(tmp), 'wb') as w:
        w.setparams(params)
        w.writeframes(result.tobytes())
    tmp.replace(out)
    return out


def _resolve_audio(job, previous, resolver, probe):
    """Return (audio_path, audio_sha, duration_ticks, origin, config_hash, preset, mode)."""
    ni = job['narration_input']
    mode = ni['mode']
    if mode == 'uploaded':
        ref = ni['uploaded']
        path = Path(ref['path'])
        if not path.is_file():
            raise ValueError('NARRATION_UPLOAD_MISSING:' + str(path))
        actual = _digest_file(path)
        if actual != ref['sha256']:
            raise ValueError('NARRATION_UPLOAD_HASH_MISMATCH')
        q = probe(path)
        if q['stream_indices']['audio'] is None:
            raise ValueError('NARRATION_UPLOAD_AUDIO_STREAM_MISSING')
        return str(path), actual, q['duration_ticks'], {'kind': 'user_imported_audio', 'ref': None}, None, None, mode
    if mode == 'history':
        ref = ni['history']
        prior = None
        if resolver is not None:
            prior = resolver(ref['job_id'], ref['revision'])
        if prior is None and previous is not None and previous.get('job_id') == ref['job_id'] and previous.get('revision') == ref['revision']:
            prior = previous
        if not prior or prior.get('schema_version') != 'narration-plan/v1':
            raise ValueError('NARRATION_HISTORY_SOURCE_NOT_FOUND')
        if job.get('account'):
            if not prior.get('_account'):
                raise ValueError('NARRATION_HISTORY_ACCOUNT_UNKNOWN')
            if prior['_account'] != job['account']:
                raise ValueError('NARRATION_HISTORY_ACCOUNT_MISMATCH')
        confirmation = prior.get('confirmation')
        source = prior.get('source', {})
        prior_sha = source.get('audio_sha256')
        if not confirmation or confirmation.get('confirmed') is not True or not confirmation.get('operator_id'):
            raise ValueError('NARRATION_HISTORY_NOT_CONFIRMED')
        if confirmation.get('audio_sha256_at_confirm') != prior_sha:
            raise ValueError('NARRATION_HISTORY_CONFIRMATION_STALE')
        if prior_sha != ref['narration_sha256']:
            raise ValueError('NARRATION_HISTORY_HASH_MISMATCH')
        path = Path(source['audio_path'])
        if not path.is_file() or _digest_file(path) != prior_sha:
            raise ValueError('NARRATION_HISTORY_FILE_UNREADABLE')
        q = probe(path)
        return str(path), prior_sha, q['duration_ticks'], {'kind': 'prior_confirmed_narration',
               'ref': {'job_id': ref['job_id'], 'revision': ref['revision']}}, None, None, mode
    # auto: exactly one full-script synthesis; no fallback when binding is absent.
    if ni['preset'] is None:
        raise ValueError('NARRATION_PRESET_REQUIRED_FOR_AUTO')
    return None, None, None, {'kind': 'tts_local_cosy', 'ref': None}, None, ni['preset'], mode


def prepare_narration(job, script_plan, previous=None, *, tts=None, resolver=None, probe=None, voice):
    """Build narration-plan/v1 for the job's narration_input.

    voice is the account's TTS voice id (from its profile).
    tts(text, preset) -> {'path','sha256','config'} is required for mode=auto and
    must perform exactly one full-script synthesis; uploaded/history modes never
    invoke it.
    """
    if probe is None:
        from renderer.ffmpeg import probe as real_probe
        probe = real_probe
    path, sha, duration, origin, config_hash, preset, mode = _resolve_audio(job, previous, resolver, probe)
    if mode == 'auto':
        if tts is None:
            raise ValueError('NARRATION_AUTO_TTS_BINDING_MISSING')
        full_text = script_plan['script_text']
        result = tts(full_text, preset)
        path = result['path']
        sha = result['sha256']
        config = result.get('config') or {}
        config_hash = canonical_hash({'voice': voice, 'preset': preset, **{k: v for k, v in config.items() if k not in ('voice', 'preset')}})
        q = probe(path)
        duration = q['duration_ticks']
        origin = {'kind': 'tts_local_cosy', 'ref': None}
    sentences = [{'sentence_id': s['sentence_id'], 'in_tick': None, 'out_tick': None,
                  'alignment_evidence_sha256': None, 'status': 'unknown'} for s in script_plan['sentences']]
    plan = {
        'schema_version': 'narration-plan/v1',
        'job_id': job['job_id'], 'revision': job['revision'],
        'source': {'mode': mode, 'preset': preset if mode == 'auto' else None, 'voice': voice,
                   'audio_path': str(path), 'audio_sha256': sha, 'actual_duration_ticks': duration,
                   'origin': origin, 'config_hash': config_hash},
        'script': {'script_sha256': script_plan['script_sha256'], 'sentence_count': len(sentences)},
        'sentences': sentences,
        'confirmation': None,
    }
    return validate_named(plan, 'narration-plan-v1')


def fill_sentence_windows(plan, windows, alignment_sha256):
    """Attach ASR-backed sentence windows; unmatched sentences stay unknown, never guessed."""
    by_id = {w['sentence_id']: w for w in windows}
    for s in plan['sentences']:
        w = by_id.get(s['sentence_id'])
        if w and w.get('in_tick') is not None and w.get('out_tick') is not None:
            s.update(in_tick=w['in_tick'], out_tick=w['out_tick'],
                     alignment_evidence_sha256=alignment_sha256, status='aligned')
    return plan

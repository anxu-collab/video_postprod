"""Per-account settings supplied by the calling project.

The engine only carries generic editing capabilities. Account identity, voice,
default music, extra import roots and account-specific prompt rules live in a
profile file owned by the calling project and registered in
config/account_profiles.json. New jobs pin the profile by content hash; a pinned
snapshot keeps every later revision on the exact same settings."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'config/account_profiles.json'
SNAPSHOTS = ROOT / 'state/account-profiles'
SCHEMA_VERSION = 'postprod-profile/v1'
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
_SHA = re.compile(r'^[a-f0-9]{64}$')
RULE_KINDS = ('script', 'fact_review', 'shot_review')


def _id(value, what):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError('ACCOUNT_PROFILE_INVALID:' + what)
    return value


def _resolve(base, path):
    p = Path(path)
    return (p if p.is_absolute() else Path(base) / p).resolve()


def _validate(data, account, base):
    if not isinstance(data, dict) or data.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('ACCOUNT_PROFILE_INVALID:schema_version')
    if _id(data.get('account'), 'account') != account:
        raise ValueError('ACCOUNT_PROFILE_ACCOUNT_MISMATCH:' + str(data.get('account')))
    for key in ('display_name', 'persona'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError('ACCOUNT_PROFILE_INVALID:' + key)
    profiles = data.get('profiles')
    if not isinstance(profiles, list) or not profiles:
        raise ValueError('ACCOUNT_PROFILE_INVALID:profiles')
    for p in profiles:
        _id(p, 'profiles')
    voice = data.get('voice') or {}
    # Cosy is the only TTS adapter; the route id names the account's voice binding.
    if not isinstance(voice.get('tts_route'), str) or not voice['tts_route'].startswith('cosy-'):
        raise ValueError('ACCOUNT_PROFILE_INVALID:voice.tts_route')
    _id(voice.get('voice_id'), 'voice.voice_id')
    presets = voice.get('presets')
    if not isinstance(presets, list) or not presets or voice.get('default_preset') not in presets:
        raise ValueError('ACCOUNT_PROFILE_INVALID:voice.presets')
    for p in presets:
        _id(p, 'voice.presets')
    cps = voice.get('chars_per_second')
    if cps is not None and (not isinstance(cps, (int, float)) or not 1 <= cps <= 10):
        raise ValueError('ACCOUNT_PROFILE_INVALID:voice.chars_per_second')
    pause = voice.get('max_pause_ms')
    if pause is not None and (not isinstance(pause, int) or isinstance(pause, bool) or not 120 <= pause <= 2000):
        raise ValueError('ACCOUNT_PROFILE_INVALID:voice.max_pause_ms')
    hold = voice.get('tail_hold_ms')
    if hold is not None and (not isinstance(hold, int) or isinstance(hold, bool) or not 0 <= hold <= 800):
        raise ValueError('ACCOUNT_PROFILE_INVALID:voice.tail_hold_ms')
    music = data.get('music')
    if music is not None:
        template = music.get('template') or {}
        _id(template.get('template_id'), 'music.template')
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', str(template.get('version'))):
            raise ValueError('ACCOUNT_PROFILE_INVALID:music.template.version')
        if not _SHA.fullmatch(str(music.get('sha256'))):
            raise ValueError('ACCOUNT_PROFILE_INVALID:music.sha256')
        gain = music.get('gain_db')
        if not isinstance(gain, (int, float)) or not -96 <= gain <= 0:
            raise ValueError('ACCOUNT_PROFILE_INVALID:music.gain_db')
        music = dict(music, path=str(_resolve(base, music['path'])), license_ref=str(_resolve(base, music['license_ref'])))
    roots = data.get('asset_roots') or []
    if not isinstance(roots, list) or not all(isinstance(r, str) and r for r in roots):
        raise ValueError('ACCOUNT_PROFILE_INVALID:asset_roots')
    rules = data.get('prompt_rules') or {}
    if set(rules) - set(RULE_KINDS):
        raise ValueError('ACCOUNT_PROFILE_INVALID:prompt_rules')
    for kind in RULE_KINDS:
        if not isinstance(rules.get(kind, []), list) or not all(isinstance(r, str) and r.strip() for r in rules.get(kind, [])):
            raise ValueError('ACCOUNT_PROFILE_INVALID:prompt_rules.' + kind)
    return dict(data, music=music, asset_roots=[str(_resolve(base, r)) for r in roots],
                prompt_rules={k: list(rules.get(k, [])) for k in RULE_KINDS})


def registry():
    if not REGISTRY.is_file():
        return {}
    data = json.loads(REGISTRY.read_text(encoding='utf-8'))
    if data.get('schema_version') != 'postprod-account-registry/v1':
        raise ValueError('ACCOUNT_REGISTRY_INVALID')
    return {_id(account, 'registry'): _resolve(ROOT, path) for account, path in (data.get('accounts') or {}).items()}


def _load_current(account):
    path = registry().get(account)
    if path is None:
        raise ValueError('ACCOUNT_NOT_REGISTERED:' + str(account))
    if not path.is_file():
        raise ValueError('ACCOUNT_PROFILE_MISSING:' + str(path))
    raw = path.read_bytes().decode('utf-8-sig').encode('utf-8')
    sha = hashlib.sha256(raw).hexdigest()
    return path, raw, sha, dict(_validate(json.loads(raw), account, path.parent), _sha256=sha, _path=str(path))


def current(account):
    """Registered profile as it is on disk now: (profile, sha256)."""
    _, _, sha, profile = _load_current(account)
    return profile, sha


def pin(account):
    """Snapshot the current profile by content hash and return that hash."""
    path, raw, sha, _ = _load_current(account)
    snapshot = SNAPSHOTS / (sha + '.json')
    if not snapshot.is_file():
        from workers.common import atomic_json
        atomic_json(snapshot, {'account': account, 'source_path': str(path), 'raw': raw.decode('utf-8')})
    return sha


def pinned(account, sha):
    snapshot = SNAPSHOTS / (str(sha) + '.json')
    if not _SHA.fullmatch(str(sha)) or not snapshot.is_file():
        raise ValueError('ACCOUNT_PROFILE_SNAPSHOT_MISSING:' + str(sha))
    data = json.loads(snapshot.read_text(encoding='utf-8'))
    raw = data['raw'].encode('utf-8')
    if data.get('account') != account or hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError('ACCOUNT_PROFILE_SNAPSHOT_CORRUPTED:' + str(sha))
    return dict(_validate(json.loads(data['raw']), account, Path(data['source_path']).parent), _sha256=sha, _path=data['source_path'])


def for_job(job):
    """Pinned jobs replay their snapshot; jobs created before pinning use the registered profile."""
    sha = job.get('account_profile_sha256')
    return pinned(job['account'], sha) if sha else current(job['account'])[0]


def _registered():
    out = []
    for account in registry():
        try:
            out.append(current(account)[0])
        except (ValueError, OSError):
            continue
    return out


def asset_roots():
    """Extra import roots declared by every registered profile."""
    return [Path(r) for p in _registered() for r in p['asset_roots']]


def allowed_voices():
    return {(p['voice']['voice_id'], preset) for p in _registered() for preset in p['voice']['presets']}


def voice_for_preset(preset):
    voices = {v for v, p in allowed_voices() if p == preset}
    if len(voices) != 1:
        raise ValueError('VOICE_PRESET_UNREGISTERED_OR_AMBIGUOUS:' + str(preset))
    return voices.pop()


def rules_text(profile, kind, heading):
    rules = profile['prompt_rules'].get(kind) or []
    return ('\n' + heading + '\n' + '\n'.join('- ' + r for r in rules)) if rules else ''


def summary(profile):
    """Public, path-free description for operator UIs and calling projects."""
    music = profile['music']
    return {'account': profile['account'], 'display_name': profile['display_name'], 'profiles': profile['profiles'],
            'voice_id': profile['voice']['voice_id'], 'presets': profile['voice']['presets'],
            'default_preset': profile['voice']['default_preset'], 'tts_route': profile['voice']['tts_route'],
            'music_template': music['template'] if music else None, 'profile_sha256': profile['_sha256'],
            'prompt_rule_counts': {k: len(v) for k, v in profile['prompt_rules'].items()}}

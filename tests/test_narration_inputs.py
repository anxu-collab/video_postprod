"""Counterexamples for the three narration entrances (S1); written before implementation."""
import json, hashlib, sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def plan_for(job, previous=None, tts=None, resolver=None):
    from scheduler.narration import prepare_narration
    return prepare_narration(job, {'schema_version': 'script-plan/v1', 'script_text': '第一句。第二句。',
                                   'script_sha256': hashlib.sha256('第一句。第二句。'.encode()).hexdigest(),
                                   'sentences': [{'sentence_id': 's001', 'text': '第一句。'},
                                                 {'sentence_id': 's002', 'text': '第二句。'}]},
                             previous, tts=tts, resolver=resolver, probe=fake_probe, voice='laox')


def base_job():
    return {'schema_version': 'edit-job/v2', 'job_id': 'narr-test', 'revision': 1, 'profile': 'warming',
            'narration_input': {'mode': 'auto', 'preset': 'laox_warming_compact', 'uploaded': None, 'history': None}}


class FakeAudio:
    def __init__(self, tmp_path, name='voice.wav', size=1000):
        self.path = tmp_path / name
        self.path.write_bytes(b'\0' * size)
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()


def fake_probe(path):
    return {'duration_ticks': 96000, 'epoch_ticks': 0, 'stream_indices': {'audio': 0}}


def test_uploaded_narration_never_calls_tts(tmp_path):
    audio = FakeAudio(tmp_path)
    job = base_job(); job['narration_input'].update(mode='uploaded', uploaded={'path': str(audio.path), 'sha256': audio.sha256})
    calls = []
    plan = plan_for(job, tts=lambda *a, **k: calls.append(1))
    assert calls == []                                   # A1: TTS count must be zero
    assert plan['source']['audio_sha256'] == audio.sha256
    assert plan['source']['origin']['kind'] == 'user_imported_audio'
    assert plan['source']['mode'] == 'uploaded'


def test_uploaded_hash_mismatch_and_missing_file_rejected(tmp_path):
    audio = FakeAudio(tmp_path)
    job = base_job(); job['narration_input'].update(mode='uploaded', uploaded={'path': str(audio.path), 'sha256': '0' * 64})
    with pytest.raises(ValueError): plan_for(job)
    job['narration_input']['uploaded'] = {'path': str(tmp_path / 'missing.wav'), 'sha256': '0' * 64}
    with pytest.raises(ValueError): plan_for(job)


def test_history_requires_confirmed_readable_source(tmp_path):
    audio = FakeAudio(tmp_path)
    prior = {'schema_version': 'narration-plan/v1', 'job_id': 'other', 'revision': 3,
             'source': {'audio_path': str(audio.path), 'audio_sha256': audio.sha256},
             'confirmation': {'confirmed': True, 'operator_id': 'op', 'audio_sha256_at_confirm': audio.sha256}}
    job = base_job(); job['narration_input'].update(mode='history', history={'job_id': 'other', 'revision': 3, 'narration_sha256': audio.sha256})
    calls = []
    plan = plan_for(job, resolver=lambda j, r: prior, tts=lambda *a, **k: calls.append(1))
    assert calls == [] and plan['source']['origin']['kind'] == 'prior_confirmed_narration'
    # unconfirmed prior narration cannot be reused
    prior['confirmation'] = None
    with pytest.raises(ValueError): plan_for(job, resolver=lambda j, r: prior)
    # hash drift between history ref and actual file rejected
    prior['confirmation'] = {'confirmed': True, 'operator_id': 'op', 'audio_sha256_at_confirm': audio.sha256}
    job['narration_input']['history']['narration_sha256'] = '1' * 64
    with pytest.raises(ValueError): plan_for(job, resolver=lambda j, r: prior)
    # prior confirmation bound to a different audio hash is stale
    job['narration_input']['history']['narration_sha256'] = audio.sha256
    prior['confirmation']['audio_sha256_at_confirm'] = '2' * 64
    with pytest.raises(ValueError): plan_for(job, resolver=lambda j, r: prior)
    # no resolvable prior plan at all
    with pytest.raises(ValueError): plan_for(job)


def test_auto_mode_uses_single_full_script_synthesis_and_caches(tmp_path):
    job = base_job()
    audio = FakeAudio(tmp_path, 'tts.wav')
    made = []
    def fake_tts(text, preset):
        made.append((text, preset))
        return {'path': str(audio.path), 'sha256': audio.sha256, 'config': {'voice': 'laox', 'preset': preset}}
    plan = plan_for(job, tts=fake_tts)
    assert made == [('第一句。第二句。', 'laox_warming_compact')]   # exactly one call, full script
    assert plan['source']['mode'] == 'auto' and plan['source']['origin']['kind'] == 'tts_local_cosy'
    assert plan['source']['actual_duration_ticks'] == 96000
    assert plan['source']['config_hash']
    # auto without any tts binding is a wiring error, not a silent fallback
    with pytest.raises(ValueError): plan_for(job, tts=None)

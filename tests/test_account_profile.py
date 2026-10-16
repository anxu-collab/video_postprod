import hashlib
import json

import pytest


def write_profile(folder, account, **over):
    music = folder / 'theme.mp3'
    music.write_bytes(b'music-' + account.encode())
    (folder / 'license.json').write_text('{}', encoding='utf-8')
    profile = {'schema_version': 'postprod-profile/v1', 'account': account, 'display_name': account + ' 名',
               'persona': account + '老师', 'profiles': ['daily'],
               'voice': {'tts_route': 'cosy-' + account, 'voice_id': account + '_voice',
                         'presets': [account + '_calm'], 'default_preset': account + '_calm'},
               'music': {'template': {'template_id': account + '-theme', 'version': '1.0.0'}, 'path': 'theme.mp3',
                         'sha256': hashlib.sha256(music.read_bytes()).hexdigest(), 'license_ref': 'license.json', 'gain_db': -28},
               'asset_roots': ['media'],
               'prompt_rules': {'script': ['不用网络流行语'], 'fact_review': ['价格必须有资料出处'], 'shot_review': ['宠物镜头必须拍到宠物']}}
    profile.update(over)
    path = folder / 'profile.json'
    path.write_text(json.dumps(profile, ensure_ascii=False), encoding='utf-8')
    return path


@pytest.fixture
def registry(tmp_path, monkeypatch):
    import scheduler.account_profile as ap
    accounts = {}
    reg = tmp_path / 'account_profiles.json'

    def register(account, **over):
        folder = tmp_path / account
        folder.mkdir()
        accounts[account] = str(write_profile(folder, account, **over))
        reg.write_text(json.dumps({'schema_version': 'postprod-account-registry/v1', 'accounts': accounts}), encoding='utf-8')
        return folder
    monkeypatch.setattr(ap, 'REGISTRY', reg)
    monkeypatch.setattr(ap, 'SNAPSHOTS', tmp_path / 'snapshots')
    return register


def test_profile_resolves_paths_and_voices(registry):
    import scheduler.account_profile as ap
    folder = registry('petlab')
    profile, sha = ap.current('petlab')
    assert profile['music']['path'] == str((folder / 'theme.mp3').resolve())
    assert profile['asset_roots'] == [str((folder / 'media').resolve())]
    assert ap.voice_for_preset('petlab_calm') == 'petlab_voice'
    assert ('petlab_voice', 'petlab_calm') in ap.allowed_voices()
    with pytest.raises(ValueError, match='ACCOUNT_NOT_REGISTERED'):
        ap.current('nobody')


def test_pinned_snapshot_survives_profile_edits(registry):
    import scheduler.account_profile as ap
    folder = registry('petlab')
    sha = ap.pin('petlab')
    write_profile(folder, 'petlab', persona='改过的人设')
    assert ap.current('petlab')[0]['persona'] == '改过的人设'
    job = {'account': 'petlab', 'account_profile_sha256': sha}
    assert ap.for_job(job)['persona'] == 'petlab老师'
    assert ap.for_job({'account': 'petlab'})['persona'] == '改过的人设'


def test_profile_rejects_invalid_or_shared_presets(registry):
    import scheduler.account_profile as ap
    registry('a1', voice={'tts_route': 'cosy-a1', 'voice_id': 'v1', 'presets': ['shared'], 'default_preset': 'shared'})
    registry('a2', voice={'tts_route': 'cosy-a2', 'voice_id': 'v2', 'presets': ['shared'], 'default_preset': 'shared'})
    with pytest.raises(ValueError, match='VOICE_PRESET_UNREGISTERED_OR_AMBIGUOUS'):
        ap.voice_for_preset('shared')
    registry('bad', voice={'tts_route': 'edge-tts', 'voice_id': 'v', 'presets': ['p'], 'default_preset': 'p'})
    with pytest.raises(ValueError, match='voice.tts_route'):
        ap.current('bad')


def test_routes_follow_the_account_profile(registry):
    from scheduler.service import pin_account_profile, validate_routes, keep_account_profile
    registry('petlab')
    job = {'schema_version': 'edit-job/v2', 'revision': 1, 'account': 'petlab', 'profile': 'daily',
           'providers': {'text': 'claude-cli', 'vision': 'ark-approved', 'tts': 'cosy-petlab', 'asr': 'mpt-cuda-small', 'lipsync': None},
           'templates': {'subtitle': {'template_id': 'subtitle-bottom', 'version': '1.0.0'},
                         'bgm': {'template_id': 'petlab-theme', 'version': '1.0.0'}},
           'narration_input': {'mode': 'auto', 'preset': 'petlab_calm'}}
    pin_account_profile(job)
    assert job['account_profile_sha256']
    validate_routes(job)
    for change, code in [({'providers': dict(job['providers'], tts='cosy-laox')}, 'UNAPPROVED_PROVIDER_ROUTE'),
                         ({'profile': 'selling'}, 'ACCOUNT_PROFILE_LINE_UNKNOWN'),
                         ({'narration_input': {'mode': 'auto', 'preset': 'laox_warming_compact'}}, 'VOICE_PRESET_NOT_IN_ACCOUNT_PROFILE')]:
        with pytest.raises(ValueError, match=code):
            validate_routes(dict(job, **change))
    revision = {k: v for k, v in job.items() if k != 'account_profile_sha256'}
    assert keep_account_profile(job, revision)['account_profile_sha256'] == job['account_profile_sha256']
    with pytest.raises(ValueError, match='ACCOUNT_PROFILE_CHANGE_REQUIRES_NEW_JOB'):
        keep_account_profile(job, dict(job, account_profile_sha256='0' * 64))


def test_prompts_use_persona_and_layer_account_rules(registry):
    import scheduler.account_profile as ap
    from scheduler.clef_review import build_system, RUBRIC
    from scheduler.script_plan import SCRIPT_AGENT_ROLE, SCRIPT_FORMAT, _generation_prompt, script_system_prompt
    registry('petlab')
    profile = ap.current('petlab')[0]
    system = script_system_prompt(profile, {'id': 'test-explicit', 'version': '1.0.0', 'rules': '按实际动作介绍。'})
    assert SCRIPT_AGENT_ROLE in system and system.startswith(SCRIPT_AGENT_ROLE)
    assert '你为petlab老师写中文旁白。' in system and SCRIPT_FORMAT in system and '不用网络流行语' in system
    assert '不编造' not in system and '口感' not in system
    prompt = _generation_prompt({'brief': '猫粮', 'claims': []}, {'candidates': []}, profile['persona'], profile['prompt_rules']['script'])
    assert prompt.startswith('为petlab老师写一条完整中文口播整稿。') and '不用网络流行语' in prompt
    review = build_system(profile)
    assert review.startswith('你是petlab老师短视频的文字审片。') and RUBRIC in review and '宠物镜头必须拍到宠物' in review
    assert '老X' not in system + prompt + review


def test_laox_prompt_keeps_persona_without_footage_constraints():
    from scheduler.script_plan import _generation_prompt
    prompt = _generation_prompt({'brief': '火龙果', 'claims': []}, {'candidates': []}, '老X', [])
    assert prompt.startswith('为老X写一条完整中文口播整稿。目的：火龙果。')
    assert '不编造' not in prompt
    assert '可用画面概况' not in prompt
    assert prompt.endswith('。整稿为连续口语，按叙事顺序直接输出正文，无标题、无说明、无括号。')


def test_narration_records_account_voice(tmp_path):
    from scheduler.narration import prepare_narration
    audio = tmp_path / 'voice.wav'
    audio.write_bytes(b'x')
    job = {'job_id': 'j', 'revision': 1, 'narration_input': {'mode': 'auto', 'preset': 'petlab_calm', 'uploaded': None, 'history': None}}
    plan = prepare_narration(job, {'script_text': '一句。', 'script_sha256': '0' * 64, 'sentences': [{'sentence_id': 's001'}]},
                             tts=lambda text, preset: {'path': str(audio), 'sha256': '1' * 64, 'config': {}},
                             probe=lambda p: {'duration_ticks': 48000, 'stream_indices': {'audio': 0}}, voice='petlab_voice')
    assert plan['source']['voice'] == 'petlab_voice'

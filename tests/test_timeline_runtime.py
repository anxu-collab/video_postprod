import copy,importlib,json
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
def timeline():return json.loads((ROOT/'contracts/examples/timeline-ab.example.json').read_text(encoding='utf-8'))

def validator():
    assert (ROOT/'scheduler/semantic.py').is_file(), 'runtime semantic validator missing'
    return importlib.import_module('scheduler.semantic').validate_timeline

@pytest.mark.parametrize('case',['cross_source','window','hole','overlap','duplicate','subtitle_hash','unsafe_cut','C_unavailable'])
def test_timeline_runtime_rejects_cross_object_failures(case):
    t=copy.deepcopy(timeline())
    if case=='cross_source':t['audio_events'][1]['audio_ref']='a-source'
    if case=='window':t['audio_events'][1]['source_in_tick']=4800
    if case=='hole':t['video_clips'][1]['timeline_start_tick']+=1920
    if case=='overlap':t['video_clips'][1]['timeline_start_tick']-=1920
    if case=='duplicate':t['resources'][1]['ref_id']=t['resources'][0]['ref_id']
    if case=='subtitle_hash':t['subtitles']['source_audio_sha256']='f'*64
    if case=='unsafe_cut':t['video_clips'][1]['boundary_evidence_ref']=None
    if case=='C_unavailable':t['video_clips'][1].update(mode='avatar_redub',lip_result_ref='b-boundary')
    with pytest.raises(ValueError):validator()(t,verify_files=False)

def test_ffmpeg_executor_never_uses_shell_or_overwrites(tmp_path):
    assert (ROOT/'renderer/ffmpeg.py').is_file(), 'FFmpeg executor missing'
    ff=importlib.import_module('renderer.ffmpeg')
    existing=tmp_path/'occupied.wav';existing.write_bytes(b'protected')
    with pytest.raises(Exception):ff.execute(['-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','0.1',str(existing)],tmp_path,timeout=10)
    assert existing.read_bytes()==b'protected'

def test_locked_render_filters_use_same_epoch_and_no_stretch():
    assert (ROOT/'renderer/compiler.py').is_file(), 'timeline compiler missing'
    from renderer.compiler import source_filters
    video,audio=source_filters(4800,100800,3840)
    assert 'PTS-0.18/TB' in video and 'PTS-0.18/TB' in audio
    assert 'STARTPTS' not in video+audio
    assert 'atempo' not in audio and 'async' not in audio

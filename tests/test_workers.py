import importlib
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]

def test_failed_visual_text_cannot_become_description():
    assert (ROOT/'workers/narrato/analysis.py').is_file(), 'strict Narrato adapter missing'
    normalize=importlib.import_module('workers.narrato.analysis').normalize
    raw={'analysis_artifact':{'batches':[{'status':'failed','time_range':'00:00:00,000-00:00:01,000','frame_paths':[], 'frame_observations':[], 'fallback_summary':'可信的治疗功效','raw_response':'fake','error_message':'timeout'}]},'video_clip_json':[{'picture':'fake'}]}
    result=normalize(raw,48000,'a'*64,'test',{})
    assert result['coverage'][0]['description'] is None
    assert result['coverage'][0]['status']=='failed'
    assert not result['usable']
    assert '治疗功效' not in str(result)

def test_visual_missing_frame_fails_closed():
    assert (ROOT/'workers/narrato/analysis.py').is_file(), 'strict Narrato adapter missing'
    normalize=importlib.import_module('workers.narrato.analysis').normalize
    raw={'analysis_artifact':{'batches':[{'status':'success','time_range':'00:00:00,000-00:00:01,000','frame_paths':['missing.jpg'],'frame_observations':[{'timestamp':'00:00:00,000','observation':'果实'}]}]}}
    assert not normalize(raw,48000,'a'*64,'test',{})['usable']

def test_whisper_facade_disables_auxiliary_vad():
    assert (ROOT/'workers/mpt/components.py').is_file(), 'MPT adapter missing'
    cls=importlib.import_module('workers.mpt.components').NativeWhisperFacade
    class Native:
        def transcribe(self,audio,**kwargs):
            self.kwargs=kwargs
            return iter([]),object()
    native=Native(); facade=cls(native)
    facade.transcribe('actual.wav',vad_filter=True,vad_parameters={'min_silence_duration_ms':500})
    assert native.kwargs['vad_filter'] is False
    assert 'vad_parameters' not in native.kwargs

def test_no_native_full_pipeline_in_worker_operations():
    assert (ROOT/'workers/common.py').is_file(), 'worker operation boundary missing'
    from workers.common import validate_operation
    for op in ['task.start','generate_video','combine_videos','lipsync.redub']:
        with pytest.raises(ValueError):validate_operation({'protocol':'postprod-worker/v1','op':op},'mpt')

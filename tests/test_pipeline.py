import json,copy,importlib
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
def test_cues_are_actual_words_not_script_and_bound_to_duration():
    from scheduler.pipeline import cues_from_words
    cues=cues_from_words([{'start_tick':4800,'end_tick':9600,'text':'实际'},{'start_tick':9600,'end_tick':19200,'text':'录音'}],24000)
    assert ''.join(c['text'] for c in cues)=='实际录音'
    assert cues[0]['start_tick']==4800 and cues[-1]['end_tick']==19200
    with pytest.raises(ValueError):cues_from_words([{'start_tick':0,'end_tick':30000,'text':'越界'}],24000)
def test_subtitle_revision_does_not_synthesize_or_realign():
    from scheduler.pipeline import apply_subtitle_edits
    t={'subtitles':{'cues':[{'cue_id':'c1','text':'识别','start_tick':0,'end_tick':1920,'claim_ids':[]}]}}
    edit={'cue_id':'c1','text':'修订'}
    apply_subtitle_edits(t,[edit]);assert t['subtitles']['cues'][0]['text']=='修订'
    with pytest.raises(ValueError):apply_subtitle_edits(t,[{'cue_id':'absent','text':'test'}])

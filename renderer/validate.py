"""Technical evidence is distinct from authenticated visual/hearing reviews."""
import array,json,math,re,subprocess,wave
from pathlib import Path
from renderer.ffmpeg import FFPROBE,execute,probe,decode
from renderer.compiler import seconds,source_filters
from workers.common import atomic_json,digest

def pcm(path):
    with wave.open(str(path),'rb') as f:
        if f.getframerate()!=48000 or f.getnchannels()!=2 or f.getsampwidth()!=2:raise ValueError('PCM_FORMAT_INVALID')
        a=array.array('h',f.readframes(f.getnframes()));return a

def validate_output(t,final,voice,work):
    work=Path(work);q=probe(final);decode(final,work)
    v=next(s for s in q['streams'] if s['codec_type']=='video');a=next(s for s in q['streams'] if s['codec_type']=='audio')
    checks={'complete_decode':True,'canvas':(v['width'],v['height'])==(768,1376),'fps':v['avg_frame_rate']=='25/1','audio_format':a['codec_name']=='aac' and int(a['sample_rate'])==48000 and a['channels']==2,'duration':abs(q['duration_ticks']-t['duration_ticks'])<=1920,'stream_sync':abs(float(v.get('start_time',0))-float(a.get('start_time',0)))<=.04}
    packet=subprocess.run([str(FFPROBE),'-v','error','-select_streams','v:0','-show_frames','-show_entries','frame=best_effort_timestamp_time','-of','json',str(final)],capture_output=True,text=True,check=True,timeout=60)
    pts=[float(f['best_effort_timestamp_time']) for f in json.loads(packet.stdout)['frames'] if 'best_effort_timestamp_time' in f]
    checks['frame_count']=len(pts)==t['duration_ticks']//1920;checks['monotonic_pts']=all(y>x for x,y in zip(pts,pts[1:]));checks['frame_grid']=all(abs(p-i/25)<.001 for i,p in enumerate(pts))
    r=execute(['-i',final,'-af','loudnorm=I=-16:TP=-1.5:LRA=7:print_format=json','-f','null','-'],work,verbose=True)
    m=re.search(r'\{\s*"input_i".*?\}',r.stderr,re.S)
    if not m:raise ValueError('FINAL_LOUDNESS_MEASUREMENT_MISSING')
    loud=json.loads(m.group());checks['loudness']=abs(float(loud['input_i'])+16)<=1;checks['true_peak']=float(loud['input_tp'])<=-1
    actual=pcm(voice);refs={r['ref_id']:r for r in t['resources']};locked=[]
    for event in t['audio_events']:
        if event['role']!='dialogue':continue
        src=Path(refs[event['audio_ref']]['path']);qsrc=probe(src);_,filt=source_filters(event['source_in_tick'],event['source_out_tick'],qsrc['epoch_ticks'])
        filt+=f',aformat=sample_fmts=s16:channel_layouts=stereo,apad=whole_len={event["duration_tick"]},atrim=end_sample={event["duration_tick"]}'
        ref=work/(event['event_id']+'-reference.wav')
        if not ref.exists():execute(['-copyts','-i',src,'-map','0:'+str(event['stream_index']),'-af',filt,'-c:a','pcm_s16le','-ar','48000','-ac','2',ref],work)
        expected=pcm(ref);offset=event['timeline_start_tick']*2;segment=actual[offset:offset+len(expected)]
        error=max((abs(x-y) for x,y in zip(expected,segment)),default=65536)
        passed=len(segment)==len(expected) and error<=1
        locked.append({'event_id':event['event_id'],'source_sha256':digest(src),'reference_sha256':digest(ref),'gain_db':0,'source_window_preserved':True,'sample_count':len(expected)//2,'max_pcm_difference_lsb':error,'pre_mix_pcm_match':passed,'verified_shift_samples':0 if passed else None,'mouth_quality':'NOT_TESTED','transcript_quality':'REVIEW_REQUIRED'})
    checks['locked_pcm']=all(r['pre_mix_pcm_match'] for r in locked)
    checks['subtitle_hash']=t['subtitles']['source_audio_sha256']==digest(voice);checks['subtitle_bounds']=all(0<=c['start_tick']<c['end_tick']<=t['duration_ticks'] for c in t['subtitles']['cues'])
    result={'passed':all(checks.values()),'checks':checks,'frame_count':len(pts),'duration_ticks':q['duration_ticks'],'expected_ticks':t['duration_ticks'],'loudness':loud,'locked_audio':locked,'final_sha256':digest(final),'subtitle_semantic_quality':'NOT_TESTED','visual_color_lipsync_quality':'REVIEW_REQUIRED','technical_only':True}
    atomic_json(work/'technical-qa.json',result);atomic_json(work/'final-probe.json',q);return result

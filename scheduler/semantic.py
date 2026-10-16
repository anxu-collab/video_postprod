import json
from pathlib import Path
from scheduler.contracts import validate
from workers.common import ROOT,digest

def checked_file(path,sha256,allow_original=False):
    p=Path(path).resolve(strict=True)
    roots=[ROOT]
    if allow_original:
        from scheduler.account_profile import asset_roots
        roots += asset_roots()
    if str(p).startswith('\\\\') or not any(p.is_relative_to(r.resolve()) for r in roots):raise ValueError('INPUT_PATH_OUTSIDE_ALLOWLIST')
    if digest(p)!=sha256:raise ValueError('RESOURCE_HASH_CHANGED')
    return p

def validate_timeline(t,job=None,capabilities=None,verify_files=True):
    validate(t,'timeline')
    def unique(items,key):
        ids=[i[key] for i in items]
        if len(ids)!=len(set(ids)):raise ValueError('DUPLICATE_ID:'+key)
        return {i[key]:i for i in items}
    resources=unique(t['resources'],'ref_id');clips=unique(t['video_clips'],'clip_id');unique(t['audio_events'],'event_id');unique(t['subtitles']['cues'],'cue_id')
    def ref(id):
        if id not in resources:raise ValueError('MISSING_RESOURCE_REFERENCE:'+str(id))
        return resources[id]
    for r in resources.values():
        for id in [r['probe_ref'],r['provenance_ref']]:
            if id is not None:ref(id)
        if verify_files:checked_file(r['path'],r['sha256'])
    cursor=0
    for c in sorted(clips.values(),key=lambda x:x['timeline_start_tick']):
        if c['mode']=='avatar_redub':raise ValueError('CAPABILITY_UNAVAILABLE_LIPSYNC')
        if c['timeline_start_tick']!=cursor:raise ValueError('VIDEO_GAP_OR_OVERLAP')
        if c['source_out_tick']-c['source_in_tick']!=c['duration_tick']:raise ValueError('SOURCE_WINDOW_DURATION_MISMATCH')
        cursor+=c['duration_tick'];r=ref(c['video_ref']);ref(c['base_source_ref'])
        if r['kind']!='video':raise ValueError('VIDEO_RESOURCE_KIND')
        if c['mode']=='spoken_locked' and c['base_source_ref']!=c['video_ref']:raise ValueError('LOCKED_SYNC_VIOLATION')
        for id in [c['boundary_evidence_ref'],c['visual_evidence_ref'],c['lip_evidence_ref']]:
            if id is not None:ref(id)
        if verify_files:
            if not r['probe_ref']:raise ValueError('VIDEO_PROBE_MISSING')
            probe=json.loads(Path(ref(r['probe_ref'])['path']).read_text(encoding='utf-8'))
            if c['source_out_tick']>probe['duration_ticks']+1920:raise ValueError('SOURCE_WINDOW_OUT_OF_RANGE')
            if c['mode']=='spoken_locked':
                e=json.loads(Path(ref(c['boundary_evidence_ref'])['path']).read_text(encoding='utf-8'))
                if e.get('source_sha256')!=r['sha256'] or e.get('validator') not in ['asr-silence/v1','full-source/v1'] or not e.get('validated'):raise ValueError('SCUT_UNVERIFIED')
                if e.get('in_tick')!=c['source_in_tick'] or e.get('out_tick')!=c['source_out_tick']:raise ValueError('SCUT_UNVERIFIED')
                if e['validator']=='full-source/v1' and (c['source_in_tick']!=0 or c['source_out_tick']!=((probe['duration_ticks']+1919)//1920)*1920):raise ValueError('FULL_SOURCE_BOUNDARY_MISMATCH')
    if cursor!=t['duration_ticks']:raise ValueError('TIMELINE_DURATION_INVALID')
    paired=set();voiced=[]
    claims={c['claim_id'] for c in job['claims']} if job else None
    for a in t['audio_events']:
        r=ref(a['audio_ref'])
        if a['source_out_tick']-a['source_in_tick']!=a['duration_tick'] or a['timeline_start_tick']+a['duration_tick']>cursor:raise ValueError('AUDIO_WINDOW_INVALID')
        if a['alignment_ref'] is not None:ref(a['alignment_ref'])
        if claims is not None and not set(a['claim_ids'])<=claims:raise ValueError('UNKNOWN_CLAIM')
        if a['role']=='dialogue':
            c=clips.get(a['sync_clip_id'])
            if c is None or c['mode']!='spoken_locked' or a['audio_ref']!=c['video_ref'] or a['gain_db']!=0:raise ValueError('LOCKED_SYNC_VIOLATION')
            for key in ['source_in_tick','source_out_tick','timeline_start_tick','duration_tick']:
                if a[key]!=c[key]:raise ValueError('LOCKED_SYNC_VIOLATION')
            if c['clip_id'] in paired:raise ValueError('DUPLICATE_LOCKED_AUDIO')
            paired.add(c['clip_id'])
        if a['role'] in ['dialogue','narration']:voiced.append((a['timeline_start_tick'],a['timeline_start_tick']+a['duration_tick']))
        if verify_files:
            if not r['probe_ref']:raise ValueError('AUDIO_PROBE_MISSING')
            probe=json.loads(Path(ref(r['probe_ref'])['path']).read_text(encoding='utf-8'))
            stream=next((s for s in probe['streams'] if s['index']==a['stream_index']),None)
            if not stream or stream['codec_type']!='audio':raise ValueError('AUDIO_STREAM_INVALID')
            if a['role']!='bgm' and a['source_out_tick']>probe['duration_ticks']+1920:raise ValueError('AUDIO_WINDOW_OUT_OF_RANGE')
    if {c['clip_id'] for c in clips.values() if c['mode']=='spoken_locked'}!=paired:raise ValueError('LOCKED_AUDIO_MISSING')
    voiced.sort()
    if any(voiced[i][0]<voiced[i-1][1] for i in range(1,len(voiced))):raise ValueError('VOICE_EVENTS_OVERLAP')
    stem=ref(t['voice_stem_ref']);s=t['subtitles']
    if s['source_audio_ref']!=stem['ref_id'] or s['source_audio_sha256']!=stem['sha256']:raise ValueError('SUBTITLE_AUDIO_HASH_MISMATCH')
    ref(s['alignment_ref'])
    for cue in s['cues']:
        if not 0<=cue['start_tick']<cue['end_tick']<=cursor:raise ValueError('SUBTITLE_TIME_INVALID')
    if t['template_events']:raise ValueError('TEMPLATE_CAPABILITY_UNAVAILABLE')
    return t

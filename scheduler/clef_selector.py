"""Scheduler-only Clef integration, retaining locked audio and narration layout."""
import copy

from scheduler import clef_review
from scheduler.clef_adapter import ClefDecisionAdapter, RULE_VERSION, safe
from scheduler.clef_store import ClefRepository
from scheduler.shot_semantics import parse_action_intent

GRID=1920


def _ceil(tick):return ((int(tick)+GRID-1)//GRID)*GRID


def _sources(library):
    value=library.get('sources') or {}
    return value if isinstance(value,dict) else {s['asset_id']:s for s in value}


def _window(candidate,sentence,script_plan,job,constraints):
    """Necessary source/claim/action gates, with no old preference/reuse filters."""
    if not candidate.get('selection_eligible'):return None,';'.join(candidate.get('gate_reasons') or ['CANDIDATE_GATE_REFUSED'])
    native=candidate.get('native') or {}
    if native.get('speech_locked') or native.get('fixed_role') or candidate.get('audio_policy')=='must_preserve_original':
        return None,'ORIGINAL_AUDIO_MUST_BE_PRESERVED'
    start,end=candidate.get('in_tick'),candidate.get('out_tick')
    if not isinstance(start,int) or not isinstance(end,int) or start<0 or end-start<GRID:
        return None,'SOURCE_BOUNDS_INVALID'
    if native.get('occlusion')=='severe':return None,'SEVERELY_OBSTRUCTED'
    text=' '.join([candidate.get('description') or '',native.get('product_description') or '',
                  ' '.join(native.get('evidence',{}).get('observation_texts') or [])])
    # A job-level product preference affects model recommendations, never admission.
    # An explicit named product claim in THIS sentence still needs source evidence.
    terms=[t for t in (script_plan.get('product_terms') or []) if t in sentence['text']]
    if terms and not native.get('product_identity_verified') and not any(t in text for t in terms):
        return None,'PRODUCT_NOT_SHOWN_IN_CANDIDATE'
    intent=parse_action_intent(sentence['text'])
    if intent['uncertainty']:return None,'SENTENCE_SEMANTICS_UNRESOLVED'
    if intent['action_kind'] and intent['state']=='in_progress':
        intervals=(job.get('v2_overrides',{}).get('confirmed_windows') or [])+(constraints.get('reusable_intervals') or [])
        for interval in intervals:
            if interval.get('source_sha256')!=candidate.get('source_sha256'):continue
            if interval.get('sentence_id') not in (None,sentence['sentence_id']):continue
            if interval.get('action_kind')!=intent['action_kind'] or interval.get('action_object')!=intent['action_object']:continue
            if not interval.get('operator_id') or not interval.get('basis'):continue
            lo,hi=max(start,interval['in_tick']),min(end,interval['out_tick'])
            if hi-lo>=GRID:return (lo,hi),'CONFIRMED_ACTION_INTERVAL'
        return None,'ACTION_CONFIRMATION_REQUIRED'
    return (start,end),None


def _active(job,script_plan,narration_plan,library,snapshot,baseline,constraints,model):
    decision=copy.deepcopy(baseline)
    by_id={c['candidate_id']:c for c in snapshot['candidates']}
    sentences={s['sentence_id']:s for s in script_plan['sentences']}
    rows={r['sentence_id']:r for r in model['sentences']}
    aligned=[w for w in narration_plan['sentences'] if w.get('status')=='aligned']
    if not aligned:raise ValueError('CLEF_NARRATION_WINDOWS_MISSING')
    narration_ids={s['sentence_id'] for s in baseline['narration']['sentences']}
    if any(w['sentence_id'] not in narration_ids for w in aligned):
        raise ValueError('CLEF_BASELINE_NARRATION_INCOMPLETE')
    body_start=baseline['narration']['body_start_tick']
    body_end=body_start+_ceil(max(w['out_tick'] for w in aligned))
    mode=job.get('intent',{}).get('selection_mode','precise')
    fixed=[copy.deepcopy(c) for c in baseline['clips'] if c['source']=='fixed']
    sources=_sources(library)
    sources=dict(sources,**(constraints.get('sources') or {}))
    selected=[];gate_reasons={};cursor=body_start
    spans=[]
    for index,w in enumerate(aligned):
        start=body_start if index==0 else body_start+_ceil(w['in_tick'])
        if spans:start=max(start,spans[-1][1])
        end=body_start+(_ceil(aligned[index+1]['in_tick']) if index+1<len(aligned) else _ceil(w['out_tick']))
        spans.append((start,max(end,start+GRID)))
    for index,w in enumerate(aligned):
        sid=w['sentence_id'];sentence=sentences[sid];row=rows[sid]
        if row['status'] not in ('OK',):raise ValueError('CLEF_SENTENCE_'+row['status']+':'+sid)
        ranked=row['ranked_ids'];gate_reasons[sid]={};windows=[]
        for cid in ranked:
            c=by_id.get(cid)
            if c is None:raise ValueError('CLEF_UNKNOWN_CANDIDATE:'+cid)
            window,reason=_window(c,sentence,script_plan,job,constraints)
            if window:
                src=sources.get(c['asset_id'])
                if not src or src.get('sha256')!=c.get('source_sha256'):
                    window=None;reason='SOURCE_HASH_BINDING_MISSING'
                elif window[1]>src['duration_ticks']:
                    window=None;reason='SOURCE_WINDOW_OUT_OF_RANGE'
            gate_reasons[sid][cid]=reason
            if window:windows.append((c,window,reason))
        if not windows:raise SelectionFailure('CLEF_NO_GATED_MATCH:'+sid,gate_reasons)
        if mode=='whole_clip':
            # A whole file is legal only if the complete source is evidence-gated;
            # a PASS sub-window never authorizes expansion into refused frames.
            chosen=None
            for c,window,proof in windows:
                duration=sources[c['asset_id']]['duration_ticks']
                source_coverage=[]
                if proof!='CONFIRMED_ACTION_INTERVAL':
                    for sibling in snapshot['candidates']:
                        if sibling.get('asset_id')!=c['asset_id'] or sibling.get('source_sha256')!=c['source_sha256']:
                            continue
                        # Coverage union may establish whole-source technical safety;
                        # it never expands a manifest PASS interval over a refused gap.
                        if not sibling.get('selection_eligible') or sibling.get('audio_policy')=='must_preserve_original':continue
                        lo,hi=sibling.get('in_tick'),sibling.get('out_tick')
                        if isinstance(lo,int) and isinstance(hi,int) and 0<=lo<hi<=duration:
                            source_coverage.append((lo,hi))
                covered=0
                for lo,hi in sorted(source_coverage):
                    if lo>covered:break
                    covered=max(covered,hi)
                if (window[0]==0 and window[1]>=duration) or covered>=duration:
                    chosen=(c,(0,_ceil(duration)),proof);break
            if chosen is None:raise SelectionFailure('CLEF_WHOLE_SOURCE_NOT_GATED:'+sid,gate_reasons)
            # One existing full source may already cover multiple real narration windows.
            if selected and cursor>body_start+w['in_tick']:
                previous=selected[-1]
                compatible=any(c['candidate_id']==previous['candidate_id'] for c,_,_ in windows)
                if not compatible:raise SelectionFailure('CLEF_WHOLE_CLIP_OVERLAP_NEEDS_REVIEW:'+sid,gate_reasons)
                if sid not in previous['sentence_ids']:previous['sentence_ids'].append(sid)
                if cursor>=body_start+w['out_tick']:continue
            c,window,proof=chosen
            clip=_clip(c,window,cursor,[sid],len(selected)+1,proof)
            selected.append(clip);cursor+=clip['duration_tick']
        else:
            target_start,target_end=spans[index]
            # Preserve real leading/inter-sentence silence; visual grid alone rounds
            # boundaries. Narration/audio ticks are never quantized or moved.
            cursor=target_start
            for c,window,proof in windows:
                available=((window[1]-window[0])//GRID)*GRID
                duration=min(available,target_end-cursor)
                if duration<GRID:continue
                selected.append(_clip(c,(window[0],window[0]+duration),cursor,[sid],len(selected)+1,proof))
                cursor+=duration
                if cursor>=target_end:break
            if cursor<target_end:raise SelectionFailure('CLEF_SHORT_COVERAGE_REVIEW_REQUIRED:'+sid,gate_reasons)
    if mode=='whole_clip' and cursor<body_end:raise SelectionFailure('CLEF_WHOLE_CLIP_MATERIAL_SHORT',gate_reasons)
    if mode=='whole_clip':
        # Opening stays exact; the original locked tail follows the new complete
        # picture. Audio events are still compiled by the unchanged compiler.
        tail=[f for f in fixed if f.get('fixed_role')!='fixed_opening']
        for f in sorted(tail,key=lambda c:c['timeline_start_tick']):
            f['timeline_start_tick']=cursor;cursor+=f['duration_tick']
    decision.update(clips=sorted(fixed+selected,key=lambda c:c['timeline_start_tick']),missing=[],compiled_timeline_sha256=None)
    cursor=0
    for clip in decision['clips']:
        if clip['timeline_start_tick']!=cursor:raise SelectionFailure('CLEF_LAYOUT_NEEDS_REVIEW',gate_reasons)
        cursor+=clip['duration_tick']
    return decision,gate_reasons


class SelectionFailure(ValueError):
    def __init__(self,message,gates):super().__init__(message);self.gates=gates


def _clip(candidate,window,start,sids,index,proof):
    return {'clip_id':'clip-clef-%04d'%index,'source':'pool','candidate_id':candidate['candidate_id'],
        'asset_id':candidate['asset_id'],'source_sha256':candidate['source_sha256'],
        'source_in_tick':window[0],'source_out_tick':window[1],
        'timeline_start_tick':start,'duration_tick':window[1]-window[0], 'sentence_ids':sids,
        'match_reason':'Clef choice; verified source window (see decision audit)',
        'match_evidence':{'selector_rule_version':RULE_VERSION,'action_evidence':proof},
        'fixed_role':None,'overlap_exception':None}


def select_with_clef(store,job,script_plan,narration_plan,library,snapshot,baseline,constraints,*,adapter=None,reviewer=None,review_system=None):
    """reviewer: optional chat(system,user)->text for the Claude text review layer;
    review_system: that layer's system prompt (account rules included).
    Active mode applies Clef only when that review approves every pick."""
    repository=ClefRepository(store);config=job.get('clef_selector') or {};mode=config.get('mode','shadow')
    if mode not in ('off','shadow','active'):raise ValueError('CLEF_MODE_INVALID')
    windows={w['sentence_id']:w for w in narration_plan['sentences']}
    sentences=[{'sentence_id':s['sentence_id'],'text':s['text'],
        'in_tick':windows.get(s['sentence_id'],{}).get('in_tick'),
        'out_tick':windows.get(s['sentence_id'],{}).get('out_tick'),
        'visual_intent':dict({'product_context':script_plan.get('product_terms') or []},**(s.get('visual') or {}))}
        for s in script_plan['sentences']]
    overrides=job.get('v2_overrides') or {}
    adjustments=(overrides.get('clip_replacements') or [])+(overrides.get('whole_clip_replacements') or [])
    audit={'schema_version':'clef-audit/v1','mode':mode,'rule_version':RULE_VERSION,
           'sentences':sentences,'baseline':copy.deepcopy(baseline),'model':None,
           'fallback_reason':None,'operator_priority':bool(adjustments),'selection_gate_reasons':{}}
    decision=copy.deepcopy(baseline)
    if mode!='off':
        try:
            model=(adapter or ClefDecisionAdapter(config)).decide(sentences,snapshot['candidates'],repository)
            audit['model']=model
            audit['fallback_reason']=model.get('fallback_reason')
            failed_rows=[r for r in model.get('sentences',[]) if r.get('status')!='OK']
            if failed_rows and not audit['fallback_reason']:
                audit['fallback_reason']=';'.join('CLEF_SENTENCE_%s:%s'%(r.get('status','ERROR'),r.get('sentence_id','unknown')) for r in failed_rows)
            if reviewer is not None and not audit['fallback_reason']:
                text_review=clef_review.review(clef_review.build_items(sentences,snapshot['candidates'],model),
                                               reviewer,script_plan.get('product_terms'),system=review_system)
                audit['text_review']=text_review
                if text_review['status']!='APPROVED':audit['fallback_reason']='CLAUDE_TEXT_REVIEW_'+text_review['status']
            if mode=='active' and not adjustments and not audit['fallback_reason']:
                decision,gates=_active(job,script_plan,narration_plan,library,snapshot,baseline,constraints,model)
                audit['selection_gate_reasons']=gates
        except Exception as exc:
            audit['fallback_reason']=safe(type(exc).__name__+':'+str(exc))
            audit['selection_gate_reasons']=getattr(exc,'gates',{})
            decision=copy.deepcopy(baseline)
    # Model output never mutates an explicit operator choice. Each change retains
    # its source reason and immutable revision; this is not usage history.
    recorded_adjustments=[]
    for adjustment in adjustments:
        payload=dict(adjustment,selector_priority='operator')
        candidate=next((c for c in snapshot['candidates'] if adjustment.get('candidate_id') in
            (c['candidate_id'],(c.get('native') or {}).get('candidate_id')) and adjustment.get('candidate_id') is not None),None)
        if candidate:
            payload.update(selected_candidate_id=candidate['candidate_id'],asset_id=candidate['asset_id'],
                           source_sha256=candidate['source_sha256'],global_asset_id='asset-'+candidate['source_sha256'])
        elif adjustment.get('source_asset_id'):
            src=next((a for a in job.get('assets',[]) if a['asset_id']==adjustment['source_asset_id']),None)
            if src:payload.update(asset_id=src['asset_id'],source_sha256=src['sha256'],global_asset_id='asset-'+src['sha256'])
        repository.record_adjustment(job['job_id'],job['revision'],payload)
        recorded_adjustments.append(payload)
    audit['operator_adjustments']=recorded_adjustments
    audit['final_decision']=copy.deepcopy(decision)
    audit['actual_positions']=[{'sentence_id':s['sentence_id'],
        'narration_in_tick':baseline['narration']['body_start_tick']+s.get('in_tick',0),
        'narration_out_tick':baseline['narration']['body_start_tick']+s.get('out_tick',0),
        'clips':[{'clip_id':c['clip_id'],'candidate_id':c.get('candidate_id'),'asset_id':c['asset_id'],
                  'in_tick':max(c['timeline_start_tick'],baseline['narration']['body_start_tick']+s.get('in_tick',0)),
                  'out_tick':min(c['timeline_start_tick']+c['duration_tick'],baseline['narration']['body_start_tick']+s.get('out_tick',0))}
                 for c in decision['clips'] if c['source']=='pool'
                 and c['timeline_start_tick']<baseline['narration']['body_start_tick']+s.get('out_tick',0)
                 and c['timeline_start_tick']+c['duration_tick']>baseline['narration']['body_start_tick']+s.get('in_tick',0)]}
        for s in sentences if s.get('in_tick') is not None and s.get('out_tick') is not None]
    decision_id=repository.record_decision(job['job_id'],job['revision'],snapshot,safe(audit),decision)
    return decision,audit,decision_id

"""Bounded editorial repair on immutable sources; missing evidence never becomes approval."""
from scheduler.contracts import canonical_hash
from scheduler.controlled_assembly import check_polish,output_from_text
from scheduler.script_drafts import text_hash


def validate_repair_review(value,count):
    from scheduler.script_draft_generation import validate_review
    if not isinstance(value,dict) or set(value)!={'status','sentences','repair_targets'}:
        raise ValueError('REVIEW_REPAIR_STRUCTURE_INVALID')
    validate_review({k:value[k] for k in ('status','sentences')},count)
    targets=value['repair_targets']
    if not isinstance(targets,list) or len(targets)>20:raise ValueError('REVIEW_REPAIR_TARGETS_INVALID')
    for t in targets:
        if (not isinstance(t,dict) or set(t)!={'index','issue','instruction'} or
                type(t['index']) is not int or not 0<t['index']<count-1 or
                not isinstance(t['issue'],str) or t['issue'] not in value['sentences'][t['index']]['issues'] or
                not isinstance(t['instruction'],str) or not t['instruction'].strip()):
            raise ValueError('REVIEW_REPAIR_TARGET_INVALID')
    return value


def repair_targets(review,output,policy):
    targets=list(review.get('repair_targets',[]))
    for i,row in enumerate(output['segments'][1:-1],1):
        matches=[x for x in policy.get('editorial_rejections',[]) if x in row['text']]
        if matches:
            targets.append({'index':i,'issue':'仍有用户否定的产品方向式讲解：'+'、'.join(matches),
                            'instruction':'重写这一段的衔接与表达，不再报定位或介绍方向；保留必要词、作用对象和事实边界。'})
    return targets


def run_repair(generator,id,p,result,source):
    store=generator.store;bundle=source['assembly'];policy=bundle['repair_policy']
    max_repairs=policy.get('max_repairs')
    if type(max_repairs) is not int or not 0<=max_repairs<=2:raise ValueError('REPAIR_LIMIT_INVALID')
    context=p.get('repair_context');previous=None;feedback=None;targets=[]
    if context:
        if context['sha256']!=canonical_hash({k:v for k,v in context.items() if k!='sha256'}):
            raise ValueError('REPAIR_CONTEXT_CHANGED')
        previous=context['previous'];feedback=context['review_feedback']
        if text_hash('\n'.join(r['text'] for r in previous['segments']))!=context['script_sha256']:
            raise ValueError('REPAIR_PARENT_TEXT_CHANGED')
        targets=repair_targets(feedback,previous,policy)
        # Historical reviewer output has no typed targets; keep its exact feedback.
        if not targets:
            targets=[{'index':s['index'],'issue':issue,
                      'instruction':'优先修复此问题中可用现有材料解决的中间衔接；证据缺口保留，不编造。'}
                     for s in feedback['sentences'] if 0<s['index']<len(previous['segments'])-1 for issue in s['issues']]
        store.snapshot(id,'repair-context.json',context)
    manual='script_text' in p
    for round in range(1 if manual else max_repairs+1):
        if manual:
            output=output_from_text(bundle,p['script_text'])
        else:
            store.stage(id,'polishing')
            output=generator.chat(id,round,'script-polish',p['skill_snapshot']['rules'],
                                  {'source':source,'previous':previous,'review_feedback':feedback,'repair_targets':targets})
        checks=check_polish(bundle,output)
        store.snapshot(id,f'{round:02d}-polish-checks.json',checks)
        attempt={'round':round,'output':output,'checks':checks,'received_repair_targets':targets}
        result['rounds'].append(attempt)
        result.update(last_attempt=output,last_attempt_checks=checks)
        if not checks['passed']:
            result.update(status='needs_review',issues=checks['issues'],repair_stop_reason='text_guard_failed')
            return store.finish(id,result)
        store.stage(id,'reviewing')
        review=validate_repair_review(generator.chat(id,round,'script-polish-review',bundle['review_prompt'],
                                     {'source':source,'script':output,'manual':manual}),len(output['segments']))
        targets=repair_targets(review,output,policy)
        attempt.update(review=review,repair_targets=targets)
        store.snapshot(id,f'{round:02d}-repair-review.json',attempt)
        text='\n'.join(s['text'] for s in output['segments'])
        roles=['hook']+['middle']*(len(output['segments'])-2)+['cta']
        issues=[f'第{s["index"]+1}段：{issue}' for s in review['sentences'] for issue in s['issues']]
        result.update(raw_polish=output,checks=checks,script_text=text,script_sha256=text_hash(text),
                      sentences=[dict(row,role=role) for row,role in zip(output['segments'],roles)],
                      semantic_review=review,pending_repair_targets=targets,
                      issues=list(dict.fromkeys(bundle['evidence_issues']+issues+[t['issue'] for t in targets])))
        result['status']=('needs_evidence' if bundle['evidence_issues'] or review['status']=='needs_evidence'
                          else 'needs_review' if targets or review['status']=='revise' else 'ready_for_user_review')
        if manual or not targets:
            result['repair_stop_reason']='manual_review_only' if manual else 'no_repairable_issues'
            break
        if round==max_repairs:
            result['repair_stop_reason']='repair_limit_reached'
            break
        previous=output;feedback=review
    return store.finish(id,result)

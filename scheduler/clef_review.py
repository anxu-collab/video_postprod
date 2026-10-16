"""Second layer over Clef-Flash: Claude Sonnet reads the text evidence of each sentence's
Clef pick and approves, rejects or defers it. Text only; it never sees frames, so an
approval is not visual acceptance."""
import json

RULE_VERSION='clef-text-review/v1'
VERDICTS=('approve','reject','needs_review')
RUBRIC=('每条给出一句旁白和选镜模型为它挑中的镜头文字描述。'
        '只依据这些文字判断镜头能否配这句旁白：主体商品对不上、动作或状态对不上（例如旁白在打开而镜头已是打开后的静态）、'
        '镜头里有人讲话需要保留原声，都判 reject；文字信息不足以判断判 needs_review；对得上判 approve。'
        '只返回JSON {"sentences":[{"sentence_id":"…","verdict":"approve|reject|needs_review","reason":"一句理由"}]}，覆盖每一条。')
SYSTEM='你是短视频的文字审片。'+RUBRIC


def build_system(profile):
    """Generic rubric, attributed to the account persona, plus that account's own review rules."""
    from scheduler.account_profile import rules_text
    return '你是'+profile['persona']+'短视频的文字审片。'+RUBRIC+rules_text(profile,'shot_review','账号附加审片规则：')


def build_items(sentences,candidates,model):
    by_id={c['candidate_id']:c for c in candidates}
    texts={s['sentence_id']:s['text'] for s in sentences}
    items=[]
    for row in (model or {}).get('sentences') or []:
        if row.get('status')!='OK' or not row.get('preferred_ids'):continue
        c=by_id.get(row['preferred_ids'][0])
        if c is None:continue
        items.append({'sentence_id':row['sentence_id'],'旁白':texts.get(row['sentence_id'],''),
                      'clef_choice':c['candidate_id'],'镜头描述':str(c.get('description') or '')[:500],
                      '逐帧观察':[str(o.get('text'))[:200] for o in (c.get('observations') or [])][:8],
                      '镜头区间秒':[c.get('start_sec'),c.get('end_sec')]})
    return items


def review(items,chat,product_terms=None,system=None):
    """chat(system,user)->reply text. Returns the audit record; status is APPROVED only
    when every reviewed sentence is approved."""
    record={'rule_version':RULE_VERSION,'items':items,'verdicts':[],'status':'SKIPPED','error':None}
    if not items:return record
    user=json.dumps({'商品关键词':list(product_terms or []),'待审':items},ensure_ascii=False)
    try:
        from scheduler.pipeline import json_object
        answer=json_object(chat(system or SYSTEM,user))
        rows={r.get('sentence_id'):r for r in answer.get('sentences') or [] if isinstance(r,dict)}
        verdicts=[]
        for item in items:
            r=rows.get(item['sentence_id']) or {}
            verdict=r.get('verdict') if r.get('verdict') in VERDICTS else 'needs_review'
            verdicts.append({'sentence_id':item['sentence_id'],'clef_choice':item['clef_choice'],
                             'verdict':verdict,'reason':str(r.get('reason') or '未返回该句结论')[:300]})
        record['verdicts']=verdicts
        if any(v['verdict']=='reject' for v in verdicts):record['status']='REJECTED'
        elif all(v['verdict']=='approve' for v in verdicts):record['status']='APPROVED'
        else:record['status']='NEEDS_REVIEW'
    except Exception as exc:
        record.update(status='ERROR',error=(type(exc).__name__+':'+str(exc))[:300])
    return record

from scheduler.contracts import canonical_hash
def node_key(kind,inputs,params,versions):return canonical_hash({'type':kind,'inputs':inputs,'params':params,'versions':versions,'contract':'edit-job/v1+timeline/v1'})
def revision_diff(old,new):
    dirty=set();a=old.get('overrides',{});b=new.get('overrides',{})
    if old['templates'].get('bgm')!=new['templates'].get('bgm') or a.get('bgm_override')!=b.get('bgm_override'):
        dirty|={'music.select','mix.raw','mix.loudnorm','mux.final','review.audio'}
    if old['templates'].get('subtitle')!=new['templates'].get('subtitle') or a.get('subtitle_edits')!=b.get('subtitle_edits'):
        dirty|={'subtitle.layout','picture.caption','mux.final','review.subtitles'}
    if old['assets']!=new['assets'] or a.get('shot_edits')!=b.get('shot_edits'):
        dirty|={'material.inspect','video.analyze','visual.factcheck','video.chunk','picture.master','picture.caption','mux.final','review.visual'}
    if old.get('intent')!=new.get('intent') or old.get('claims')!=new.get('claims') or old.get('product_documents')!=new.get('product_documents') or a.get('speech_edits')!=b.get('speech_edits'):
        dirty|={'script.generate','claims.check','speech.synthesize','safe.cuts','voice.stem','speech.transcribe','subtitle.layout','mix.raw','mix.loudnorm','picture.caption','mux.final','review.audio','review.subtitles'}
    if old.get('schema_version')=='edit-job/v2' and new.get('schema_version')=='edit-job/v2':
        va,vb=old.get('v2_overrides',{}),new.get('v2_overrides',{})
        if old.get('script_input')!=new.get('script_input'):
            dirty|={'script.build','claims.check','speech.synthesize','speech.transcribe','subtitle.review','edit.plan','video.chunk','mux.final'}
        if old.get('narration_input')!=new.get('narration_input'):
            dirty|={'speech.transcribe','subtitle.review','edit.plan','video.chunk','mux.final'}
            if old['narration_input'].get('preset')!=new['narration_input'].get('preset'):
                dirty|={'speech.synthesize'}
        if va.get('clip_replacements')!=vb.get('clip_replacements'):
            dirty|={'edit.plan','video.chunk','picture.master','picture.caption','mux.final'}
        if va.get('whole_clip_replacements')!=vb.get('whole_clip_replacements'):
            dirty|={'edit.plan','video.chunk','picture.master','picture.caption','mux.final'}
        if va.get('sentence_redubs')!=vb.get('sentence_redubs'):
            dirty|={'speech.synthesize','speech.transcribe','subtitle.review','edit.plan','video.chunk','mux.final'}
    return dirty

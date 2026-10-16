"""Incremental revision planning for edit-job/v2 (S6).

The plan is computed from the full revision semantics, never from UI claims
about "only subtitles changed". Successful voices, analyses and unaffected
renders are reused; only genuinely invalidated nodes re-run.
"""
RENDER_TAIL = ['video.chunk', 'picture.master', 'subtitle.layout', 'picture.caption', 'mix.raw', 'mix.loudnorm', 'mux.final']
AUDIO_CHAIN = ['speech.transcribe', 'subtitle.review', 'edit.plan']


def plan_revision(old, new, artifacts):
    reuse, invalidated, reasons = set(), set(), {}

    def invalidate(nodes, why):
        for n in nodes:
            invalidated.add(n)
        reasons.setdefault(why, []).extend(nodes)

    a, b = old.get('v2_overrides', {}), new.get('v2_overrides', {})
    oa, ob = old.get('overrides', {}), new.get('overrides', {})
    from scheduler.script_generation import source_hash
    script_changed = source_hash(old) != source_hash(new)
    narration_changed = old.get('narration_input') != new.get('narration_input')
    assets_changed = old.get('assets') != new.get('assets')
    subtitle_edits_changed = oa.get('subtitle_edits') != ob.get('subtitle_edits')
    bgm_changed = (old.get('templates', {}).get('bgm') != new.get('templates', {}).get('bgm')
                   or oa.get('bgm_override') != ob.get('bgm_override'))
    clip_edits_changed = a.get('clip_replacements') != b.get('clip_replacements')
    whole_clip_swaps_changed = a.get('whole_clip_replacements') != b.get('whole_clip_replacements')
    selection_mode_changed = (old.get('intent', {}).get('selection_mode')
                              != new.get('intent', {}).get('selection_mode'))
    redubs_changed = a.get('sentence_redubs') != b.get('sentence_redubs')

    if script_changed:
        invalidate(['script.build', 'script.confirm'] + (['speech.synthesize'] if new['narration_input']['mode'] == 'auto' else []) + AUDIO_CHAIN + RENDER_TAIL,
                   'writing inputs changed')
    if narration_changed:
        nodes = AUDIO_CHAIN + RENDER_TAIL
        if old['narration_input']['mode'] == 'auto' and new['narration_input']['mode'] == 'auto':
            if old['narration_input']['preset'] != new['narration_input']['preset']:
                nodes = ['speech.synthesize'] + nodes
                reasons['narration preset changed'] = ['speech.synthesize']
        invalidate(nodes, 'narration_input changed')
    if assets_changed:
        invalidate(['material.inspect', 'video.analyze', 'shot.library', 'edit.plan'] + RENDER_TAIL,
                   'assets changed')
    if clip_edits_changed:
        invalidate(['edit.plan'] + ['video.chunk', 'picture.master', 'picture.caption', 'mix.raw', 'mix.loudnorm', 'mux.final'],
                   'clip replacement only re-renders affected pictures')
    if whole_clip_swaps_changed:
        # a whole-source swap re-plans and re-flows picture/fixed-tail/mix/export;
        # analysis, script and confirmed narration are reused (no speech.synthesize)
        invalidate(['edit.plan'] + ['video.chunk', 'picture.master', 'picture.caption', 'mix.raw', 'mix.loudnorm', 'mux.final'],
                   'whole-clip replacement swaps a whole source file')
    if selection_mode_changed:
        invalidate(['edit.plan'] + RENDER_TAIL, 'selection_mode changed (whole_clip <-> precise)')
    if redubs_changed:
        invalidate(['speech.synthesize'] + AUDIO_CHAIN + RENDER_TAIL, 'sentence redub re-synthesizes that sentence only')
    if subtitle_edits_changed:
        invalidate(['subtitle.layout', 'picture.caption', 'mux.final'], 'subtitle text/timing edit')
    if bgm_changed:
        invalidate(['music.select', 'mix.raw', 'mix.loudnorm', 'mux.final'], 'bgm change')

    every = {'script.build', 'script.confirm', 'speech.synthesize', 'speech.transcribe', 'subtitle.review',
             'edit.plan', 'material.inspect', 'video.analyze', 'shot.library', 'music.select'} | set(RENDER_TAIL)
    reuse = sorted(every - invalidated)
    return {'reuse': reuse, 'invalidated': sorted(invalidated), 'reasons': {k: sorted(set(v)) for k, v in reasons.items()}}

import json,math,os,re
from pathlib import Path
from renderer.ffmpeg import ROOT,execute,probe,render_cached
from workers.common import atomic_json,digest

def seconds(ticks):return format(ticks/48000,'.9f').rstrip('0').rstrip('.') or '0'

def source_filters(start_tick,end_tick,epoch_tick):
    s=seconds(start_tick+epoch_tick);e=seconds(end_tick+epoch_tick)
    return (f'trim=start={s}:end={e},setpts=PTS-{s}/TB',f'atrim=start={s}:end={e},asetpts=PTS-{s}/TB,aresample=48000:first_pts=0')

def graph_file(work,name,text):
    p=Path(work)/(name+'.ffgraph');p.write_text(text,encoding='utf-8');return p

def build_picture(t,work,preview_color_authorization=None):
    resources={r['ref_id']:r for r in t['resources']};chunks=[];metrics=[]
    for c in t['video_clips']:
        src=Path(resources[c['video_ref']]['path']);q=probe(src);v=next(s for s in q['streams'] if s['codec_type']=='video')
        space=v.get('color_space');prim=v.get('color_primaries');trc=v.get('color_transfer')
        color_assumption=None
        if space not in ['bt709','bt470bg','smpte170m'] or prim not in ['bt709','bt470bg','smpte170m'] or trc not in ['bt709','bt470bg','smpte170m']:
            if not preview_color_authorization or preview_color_authorization.get('decision')!='allow_preview_bt709' or digest(src) not in preview_color_authorization['source_hashes'] or t['review']['status']!='pending':raise ValueError('COLOR_QUALIFICATION_REQUIRED')
            color_assumption='bt709'
        filt,_=source_filters(c['source_in_tick'],c['source_out_tick'],q['epoch_ticks'])
        # Near-canvas aspects (e.g. 1088x1920 vs 768x1376, ~1.5%) fill the frame by a
        # thin centre crop instead of leaving letterbox bars; other aspects keep padding.
        aspect=v['width']/v['height'];fill=aspect!=768/1376 and abs(aspect/(768/1376)-1)<=0.03
        fit='scale=768:1376:force_original_aspect_ratio=increase,crop=768:1376' if fill else 'scale=768:1376:force_original_aspect_ratio=decrease,pad=768:1376:(ow-iw)/2:(oh-ih)/2'
        filt+=(','+'colorspace=iall=bt709:all=bt709:fast=0' if color_assumption else ',colorspace=all=bt709:fast=0')+','+fit+',setsar=1,fps=25,format=yuv420p'
        # At most one repeated terminal frame covers the declared frame rounding;
        # it never changes speed, removes dialogue, or extends an arbitrary scene.
        filt+=',tpad=stop_mode=clone:stop_duration=0.04,trim=duration='+seconds(c['duration_tick'])
        def args(out):
            script=graph_file(work,'video_'+c['clip_id'],f'[0:{v["index"]}]{filt}[v]')
            return ['-copyts','-i',src,'-filter_complex_script',script,'-map','[v]','-an','-c:v','libx264','-crf','18','-preset','medium','-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709','-movflags','+faststart',out]
        chunk,cached=render_cached('video.chunk',[src],dict(c,color_assumption=color_assumption,**({'fit':'cover'} if fill else {})),args,'.mp4',work);chunks.append(chunk);metrics.append({'clip_id':c['clip_id'],'cached':cached,'hash':digest(chunk),'color_assumption':color_assumption,'color_qualified':color_assumption is None})
    duration=t['duration_ticks']
    def concat(out):
        args=[]
        for p in chunks:args+=['-i',p]
        graph=''.join(f'[{i}:v]' for i in range(len(chunks)))+f'concat=n={len(chunks)}:v=1:a=0[v]'
        return args+['-filter_complex_script',graph_file(work,'concat',graph),'-map','[v]','-an','-c:v','libx264','-crf','18','-preset','medium','-pix_fmt','yuv420p','-movflags','+faststart',out]
    master,cached=render_cached('picture.master',chunks,{'duration_ticks':duration,'order':[c['clip_id'] for c in t['video_clips']]},concat,'.mp4',work)
    return master,{'chunks':metrics,'master_cached':cached}

def build_voice(t,work):
    refs={r['ref_id']:r for r in t['resources']};events=[a for a in t['audio_events'] if a['role'] in ['dialogue','narration']];paths=[];graphs=[];args=[]
    for i,a in enumerate(events):
        src=Path(refs[a['audio_ref']]['path']);q=probe(src);_,filt=source_filters(a['source_in_tick'],a['source_out_tick'],q['epoch_ticks'])
        args+=['-copyts','-i',src];paths.append(src)
        filt+=f',aformat=sample_fmts=s16:channel_layouts=stereo,apad=whole_len={a["duration_tick"]},atrim=end_sample={a["duration_tick"]},volume={a["gain_db"]}dB,adelay={a["timeline_start_tick"]}S:all=1'
        graphs.append(f'[{i}:{a["stream_index"]}]{filt}[a{i}]')
    duration=t['duration_ticks']
    graph=';'.join(graphs)+';'+''.join(f'[a{i}]' for i in range(len(events)))+f'amix=inputs={len(events)}:duration=longest:dropout_transition=0:normalize=0,apad=whole_len={duration},atrim=end_sample={duration}[voice]'
    def factory(out):return args+['-filter_complex_script',graph_file(work,'voice',graph),'-map','[voice]','-c:a','pcm_s16le','-ar','48000','-ac','2',out]
    return render_cached('voice.stem',paths,{'events':events,'duration_ticks':duration},factory,'.wav',work)

def build_mix(t,voice,work):
    refs={r['ref_id']:r for r in t['resources']};bgm=[a for a in t['audio_events'] if a['role']=='bgm'];sfx=[a for a in t['audio_events'] if a['role']=='sfx']
    if sfx or len(bgm)>1:raise ValueError('AUDIO_TEMPLATE_CAPABILITY_UNAVAILABLE')
    duration=t['duration_ticks'];args=['-i',voice];inputs=[voice];parts=[]
    if bgm:
        a=bgm[0];p=Path(refs[a['audio_ref']]['path']);inputs.append(p);args+=['-stream_loop','-1','-i',p]
        if a['timeline_start_tick']!=0 or a['duration_tick']!=duration:raise ValueError('BGM_TEMPLATE_TIME_INVALID')
        parts=[f'[0:a]asplit=2[vc][sc]',f'[1:{a["stream_index"]}]atrim=start={seconds(a["source_in_tick"])}:duration={seconds(duration)},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,volume={a["gain_db"]}dB,afade=t=in:st=0:d=0.2,afade=t=out:st={seconds(max(0,duration-14400))}:d=0.3[music]', '[music][sc]sidechaincompress=threshold=0.025:ratio=8:attack=15:release=250:makeup=1[duck]',f'[vc][duck]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,atrim=end_sample={duration}[mix]']
    else:parts=[f'[0:a]atrim=end_sample={duration}[mix]']
    def raw_factory(out):return args+['-filter_complex_script',graph_file(work,'mix',';'.join(parts)),'-map','[mix]','-c:a','pcm_s16le','-ar','48000','-ac','2',out]
    raw,cached=render_cached('mix.raw',inputs,{'bgm':bgm,'duration_ticks':duration,'duck':'v1'},raw_factory,'.wav',work)
    measurement=execute(['-i',raw,'-af','loudnorm=I=-16:TP=-1.5:LRA=7:print_format=json','-f','null','-'],work,verbose=True)
    match=re.search(r'\{\s*"input_i".*?\}',measurement.stderr,re.S)
    if not match:raise ValueError('LOUDNESS_MEASUREMENT_FAILED')
    measured=json.loads(match.group());atomic_json(Path(work)/'loudnorm-measured.json',measured)
    if not all(math.isfinite(float(measured[k])) for k in ['input_i','input_tp','input_lra','input_thresh','target_offset']):raise ValueError('SILENT_OR_INVALID_OUTPUT_AUDIO')
    filt='loudnorm=I=-16:TP=-1.5:LRA=7:linear=true:measured_I='+measured['input_i']+':measured_TP='+measured['input_tp']+':measured_LRA='+measured['input_lra']+':measured_thresh='+measured['input_thresh']+':offset='+measured['target_offset']+',aresample=48000'
    def factory(out):return ['-i',raw,'-af',filt,'-c:a','pcm_s16le','-ar','48000','-ac','2',out]
    normalized,reused=render_cached('mix.loudnorm',[raw],{'duration_ticks':duration,'measured':measured},factory,'.wav',work)
    return normalized,{'mix_cached':cached,'loudnorm_cached':reused,'measurement':measured}

def write_ass(t,work):
    def ts(ticks):
        cs=round(ticks/480);return f'{cs//360000}:{cs//6000%60:02d}:{cs//100%60:02d}.{cs%100:02d}'
    lines=['[Script Info]','ScriptType: v4.00+','PlayResX: 768','PlayResY: 1376','WrapStyle: 2','[V4+ Styles]','Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding','Style: Default,Microsoft YaHei,42,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,70,70,170,1','[Events]','Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    for c in t['subtitles']['cues']:
        text=c['text'].replace('\\','＼').replace('{','｛').replace('}','｝').replace('\r','').replace('\n','')
        if len(text)>32:raise ValueError('SUBTITLE_TEMPLATE_TWO_LINES_EXCEEDED')
        if len(text)>16:text=text[:len(text)//2]+'\\N'+text[len(text)//2:]
        lines.append(f'Dialogue: 0,{ts(c["start_tick"])},{ts(c["end_tick"])},Default,,0,0,0,,{text}')
    file=Path(work)/'captions.ass';file.write_text('\n'.join(lines)+'\n',encoding='utf-8');return file

def caption_and_mux(t,picture,mix,work):
    ass=write_ass(t,work)
    def caption(out):return ['-i',picture,'-vf','subtitles=captions.ass','-an','-c:v','libx264','-crf','18','-preset','medium','-pix_fmt','yuv420p','-movflags','+faststart',out]
    captioned,c1=render_cached('picture.caption',[picture,ass],{'duration_ticks':t['duration_ticks'],'template':t['subtitles']['template']},caption,'.mp4',work)
    def mux(out):return ['-i',captioned,'-i',mix,'-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k','-ar','48000','-ac','2','-movflags','+faststart',out]
    final,c2=render_cached('mux.final',[captioned,mix],{'duration_ticks':t['duration_ticks']},mux,'.mp4',work)
    return final,{'caption_cached':c1,'mux_cached':c2}

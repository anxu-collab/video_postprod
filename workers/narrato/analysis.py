"""Narrato-only headless adapter. Failed compatibility summaries are discarded."""
import asyncio
import hashlib
import json
import re
import subprocess
from pathlib import Path

from workers.common import ROOT,atomic_json,digest,input_path,output_ref

# scene.detect carries only detector configuration; script text or import order
# must never leak into its cache key via params.
_SCRIPT_FREE_FORBIDDEN_KEYS={'brief','script','script_text','asset_order','order','text'}

def timestamp_tick(text):
    m=re.fullmatch(r'(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})',text)
    if not m:raise ValueError('FRAME_TIME_INVALID')
    h,mi,s,ms=map(int,m.groups())
    if mi>=60 or s>=60:raise ValueError('FRAME_TIME_INVALID')
    return (h*3600+mi*60+s)*48000+ms*48

def normalize(raw,duration_ticks,asset_hash,model_id,frames):
    coverage=[]
    for batch in raw.get('analysis_artifact',{}).get('batches',[]):
        item={'status':'failed','description':None,'observations':[],'frame_refs':[],'error':'ANALYSIS_UNTRUSTED','in_tick':0,'out_tick':duration_ticks}
        try:
            times=batch['time_range'].split('-');start,end=map(timestamp_tick,times)
            if not 0<=start<=end<=duration_ticks:raise ValueError('FRAME_TIME_INVALID')
            item.update(in_tick=start,out_tick=min(duration_ticks,end+48000))
            if batch.get('status')!='success':raise ValueError('VISION_BATCH_FAILED')
            paths=batch['frame_paths'];obs=batch['frame_observations']
            if not paths or len(obs)!=len(paths):raise ValueError('FRAME_COVERAGE_MISSING')
            for p,o in zip(paths,obs):
                f=frames[str(p)];t=timestamp_tick(o['timestamp'])
                if abs(t-f['source_tick'])>1920 or not start<=t<=end:raise ValueError('FRAME_TIME_INVALID')
                text=o.get('observation','').strip()
                if not text or len(text)>6000:raise ValueError('OBSERVATION_INVALID')
                item['observations'].append({'frame_id':f['frame_id'],'source_tick':f['source_tick'],'text':text,'model_id':model_id,'prompt_version':'strict-v1'})
                item['frame_refs'].append(f)
            item.update(status='success',description='\n'.join(o['text'] for o in item['observations']),error=None)
        except (ValueError,KeyError,TypeError) as e:
            item.update(status='failed',description=None,observations=[],frame_refs=[],error=str(e)[:100])
        coverage.append(item)
    return {'asset_hash':asset_hash,'coverage':coverage,'usable':bool(coverage) and all(c['status']=='success' for c in coverage),'reviewed_visual_evidence':[]}

def scene_params_are_script_free(params):
    keys=set(params or {})
    return not (keys & _SCRIPT_FREE_FORBIDDEN_KEYS)

def detect_scenes(req,work):
    from scenedetect import ContentDetector,detect
    source=input_path(req['input_refs'][0]);p=req['params']
    if not scene_params_are_script_free(p):raise ValueError('SCENE_PARAMS_CARRY_SCRIPT_DATA')
    threshold=float(p.get('threshold',12));min_scene_len=int(p.get('min_scene_len_frames',12))
    scenes=detect(str(source),ContentDetector(threshold=threshold,min_scene_len=min_scene_len),show_progress=False)
    cuts=[];fps=None
    for start,end in scenes:
        fps=fps or start.framerate
        cuts.append(round(start.get_seconds()*48000))
    out=work/'scenes.json';atomic_json(out,{'source_sha256':digest(source),'cuts':sorted(set(cuts)),
        'fps':fps,'frame_count':(scenes[-1][1].frame_num if scenes else 0),
        'config':{'detector':'content-v1','threshold':threshold,'min_scene_len_frames':min_scene_len},
        'config_hash':hashlib.sha256(json.dumps({'detector':'content-v1','threshold':threshold,'min_scene_len_frames':min_scene_len},sort_keys=True).encode()).hexdigest()})
    return {'output_refs':[output_ref(out,'evidence')],'metrics':{'scenes':len(scenes)},'device_evidence':{'neural_inference':0},'provider_request_ids':[]}

def analyze(req,work):
    if req['op']=='scene.detect':return detect_scenes(req,work)
    from PIL import Image
    from app.config import config
    from app.services.documentary.frame_analysis_service import DocumentaryFrameAnalysisService
    from app.services.llm.providers import register_all_providers
    from app.utils.video_processor import VideoProcessor
    register_all_providers();config.app['llm_max_retries']=0
    source=input_path(req['input_refs'][0]);params=req['params'];provider=params['provider']
    q=subprocess.run([str(ROOT/'tools/ffmpeg/ffprobe.exe'),'-v','error','-show_streams','-show_format','-of','json',str(source)],capture_output=True,text=True,check=True,timeout=30)
    probe=json.loads(q.stdout);videos=[s for s in probe['streams'] if s['codec_type']=='video']
    if not videos or videos[0]['width']<=0 or videos[0]['height']<=0:raise ValueError('VIDEO_PROBE_INVALID')
    duration=float(videos[0].get('duration',probe['format'].get('duration',0)))
    if duration<=0:raise ValueError('VIDEO_DURATION_INVALID')
    frames={}
    class Headless(DocumentaryFrameAnalysisService):
        async def _analyze_batches(self,**kwargs):
            native=kwargs['analyzer']
            class Timestamped:
                async def analyze_images(self,**call):
                    stamps=[]
                    for image_path in call['images']:
                        tick=frames[str(image_path)]['source_tick'];ms=round(tick/48)
                        stamps.append(f'{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}')
                    call['prompt']+='\n每张图片按列表顺序对应以下原视频绝对时间戳。逐项原样填写 timestamp，禁止从零重新计时：'+json.dumps(stamps,ensure_ascii=False)
                    return await native.analyze_images(**call)
            kwargs['analyzer']=Timestamped()
            return await super()._analyze_batches(**kwargs)
        def _load_or_extract_keyframes(self,video_path,frame_interval_seconds):
            out=work/'frames';out.mkdir(exist_ok=True)
            paths=VideoProcessor(video_path).extract_frames_by_interval_with_fallback(output_dir=str(out),interval_seconds=frame_interval_seconds)
            paths=[str(p) for p in paths if str(p).endswith('.jpg')]
            # Add the last decodable frame explicitly; batches remain samples, not shots.
            actual=subprocess.run([str(ROOT/'tools/ffmpeg/ffprobe.exe'),'-v','error','-select_streams','v:0','-show_entries','frame=best_effort_timestamp_time','-of','json',video_path],capture_output=True,text=True,check=True,timeout=60)
            pts=[float(f['best_effort_timestamp_time']) for f in json.loads(actual.stdout).get('frames',[]) if 'best_effort_timestamp_time' in f]
            if not pts:raise ValueError('LAST_FRAME_PTS_MISSING')
            last=max(pts);ms=round(last*1000);h=ms//3600000;mi=ms//60000%60;s=ms//1000%60
            p=out/f'keyframe_{len(paths):06d}_{h:02d}{mi:02d}{s:02d}{ms%1000:03d}.jpg'
            subprocess.run([str(ROOT/'tools/ffmpeg/ffmpeg.exe'),'-nostdin','-n','-v','error','-ss',str(last),'-i',video_path,'-frames:v','1',str(p)],check=True,capture_output=True,timeout=120)
            if not p.is_file():raise ValueError('LAST_FRAME_DECODE_FAILED')
            paths.append(str(p));paths=sorted(set(paths),key=lambda name:re.search(r'_(\d{9})\.jpg$',name).group(1))
            for index,p in enumerate(paths):
                with Image.open(p) as image:image.verify()
                token=re.search(r'_(\d{9})\.jpg$',p).group(1);tick=timestamp_tick(token[:2]+':'+token[2:4]+':'+token[4:6]+','+token[6:])
                frames[p]={'frame_id':f'frame-{index}','path':p,'sha256':digest(p),'source_tick':tick}
            return paths
        def _save_analysis_artifact(self,artifact):
            p=work/'raw-analysis.json';atomic_json(p,artifact);return str(p)
    raw=asyncio.run(Headless().analyze_video(video_path=str(source),video_theme=params.get('brief',''),custom_prompt='只陈述可见物体和动作，不推断具体品牌、产品功效、口感或实拍真实性。',frame_interval_input=1,vision_batch_size=4,max_concurrency=1,vision_llm_provider='openai',vision_api_key=provider['operation_token'],vision_model_name=provider['model'],vision_base_url=provider['base_url']))
    result=normalize(raw,round(duration*48000),digest(source),provider['model'],frames)
    result['config_hash']=hashlib.sha256(json.dumps({'model':provider['model'],'interval':1,'batch':4,'prompt':'strict-v1'},sort_keys=True).encode()).hexdigest()
    p=work/'analysis.json';atomic_json(p,result)
    if not result['usable']:raise ValueError('ANALYSIS_UNTRUSTED:'+str(p))
    return {'output_refs':[output_ref(p)],'metrics':{'frames':len(frames)},'device_evidence':{'provider':'cloud','local_neural_inference':0},'provider_request_ids':[]}

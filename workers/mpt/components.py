import gc,json,os,re,threading
from pathlib import Path
from workers.common import ROOT,atomic_json,digest,input_path,output_ref

_asr_lock=threading.Lock()
class NativeWhisperFacade:
    def __init__(self,native):self.native=native;self.segments=[]
    def transcribe(self,audio_file,**kwargs):
        kwargs['vad_filter']=False;kwargs.pop('vad_parameters',None);kwargs.setdefault('language','zh')
        segments,info=self.native.transcribe(audio_file,**kwargs)
        self.segments=list(segments)
        return iter(self.segments),info

def transcribe(source,work,timeout_sec=570):
    from workers.mpt.model_runner import run_model_process
    result=run_model_process('whisper',{'source':str(source),'work':str(work)},work,timeout_sec=timeout_sec)
    lifecycle=result.pop('model_lifecycle')
    result.setdefault('device_evidence',{})['model_lifecycle']=lifecycle
    return result


def _transcribe_in_process(source,work):
    model_dir=ROOT/'upstream/MoneyPrinterTurbo/models/whisper-small'
    if not (model_dir/'model.bin').is_file():raise ValueError('ASR_MODEL_MISSING')
    if any(not (model_dir/name).is_file() for name in ('config.json','tokenizer.json','vocabulary.txt')):
        raise ValueError('ASR_LOCAL_COMPONENT_MISSING')
    import ctranslate2
    if ctranslate2.get_cuda_device_count()<1:raise ValueError('CUDA_UNAVAILABLE')
    handles=[]
    dll=Path('F:/AI/Fun-CosyVoice3/env/Lib/site-packages/torch/lib')
    if dll.exists():handles.append(os.add_dll_directory(str(dll)))
    from app.config import config
    config.whisper.update(model_size='small',device='cuda',compute_type='float16')
    from app.services import subtitle
    if subtitle.device!='cuda' or subtitle.compute_type!='float16':raise ValueError('ASR_CONFIG_IMPORT_ORDER')
    with _asr_lock:
        if not subtitle._ensure_model_loaded():raise ValueError('ASR_LOAD_FAILED')
        native=subtitle.model
        if getattr(native.model,'device',None)!='cuda':raise ValueError('ASR_DEVICE_NOT_CUDA')
        facade=NativeWhisperFacade(native);subtitle.model=facade
        try:
            srt=work/'actual.srt';result=subtitle.create(str(source),str(srt),word_level=True,log_details=False)
            # Pinned native create() writes the SRT and returns None on success.
            if not srt.is_file() or not srt.read_text(encoding='utf-8-sig').strip():raise ValueError('ASR_EMPTY_RESULT')
            words=[{'start_tick':round(w.start*48000),'end_tick':round(w.end*48000),'text':w.word.strip()} for seg in facade.segments for w in (seg.words or []) if w.word.strip()]
            if not words:raise ValueError('ASR_WORD_ALIGNMENT_MISSING')
            p=work/'alignment.json';atomic_json(p,{'audio_sha256':digest(source),'words':words,'device':'cuda','compute_type':'float16','auxiliary_vad':False,'model_hash':digest(model_dir/'model.bin')})
            return {'output_refs':[output_ref(p,'evidence'),output_ref(srt,'subtitle')],'device_evidence':{'asr':'cuda','auxiliary_vad':False},'metrics':{'words':len(words)},'provider_request_ids':[]}
        finally:
            subtitle.model=None;facade.native=None;del native;gc.collect()

def handle(req,work):
    op=req['op'];p=req['params'];source=input_path(req['input_refs'][0]) if req['input_refs'] else None
    if op=='speech.transcribe':
        from datetime import datetime,timezone
        remaining=(datetime.fromisoformat(req['deadline_utc'].replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds()
        if remaining<=0:raise ValueError('OPERATION_DEADLINE_EXPIRED')
        return transcribe(source,work,timeout_sec=min(570,remaining))
    if op=='speech.synthesize':
        from workers.mpt.cosy_provider import synthesize
        return synthesize(p,work)
    if op=='script.generate':
        from app.services import llm
        provider=p['provider'];llm._max_retries=1
        config={'llm_provider':'openai','openai_api_key':provider['operation_token'],'openai_base_url':provider['base_url'],'openai_model_name':provider['model']}
        text=llm.generate_script(video_subject=p['brief'],language='zh',paragraph_number=1,video_script_prompt=p['prompt'],custom_system_prompt='只输出可读旁白，无标题、说明或括号。',app_config=config)
        if not text.strip() or text.startswith('Error:'):raise ValueError('SCRIPT_EMPTY_OR_PROVIDER_FAILED')
        out=work/'script.json';atomic_json(out,{'text':text,'claim_ids':p.get('claim_ids',[]),'quality':'unverified'})
        return {'output_refs':[output_ref(out)],'device_evidence':{'provider':'cloud','local_neural_inference':0},'metrics':{},'provider_request_ids':[]}
    if op=='material.inspect':
        import subprocess
        from app.services.video import is_material_resolution_acceptable
        q=subprocess.run([str(ROOT/'tools/ffmpeg/ffprobe.exe'),'-v','error','-show_streams','-show_format','-of','json',str(source)],check=True,capture_output=True,text=True,timeout=30)
        probe=json.loads(q.stdout);v=next(s for s in probe['streams'] if s['codec_type']=='video')
        if not is_material_resolution_acceptable(v['width'],v['height']):raise ValueError('MATERIAL_RESOLUTION_REJECTED')
        out=work/'material.json';atomic_json(out,probe);return {'output_refs':[output_ref(out,'probe')],'metrics':{},'provider_request_ids':[],'device_evidence':{'neural_inference':0}}
    if op=='music.select':
        from app.services.video import get_bgm_file
        selected=get_bgm_file('specified',str(source))
        if not selected:raise ValueError('BGM_NATIVE_PATH_REJECTED')
        out=work/'music.json';atomic_json(out,{'path':selected,'sha256':digest(selected)});return {'output_refs':[output_ref(out)],'metrics':{},'provider_request_ids':[],'device_evidence':{'neural_inference':0}}
    raise ValueError('OPERATION_NOT_ALLOWED')

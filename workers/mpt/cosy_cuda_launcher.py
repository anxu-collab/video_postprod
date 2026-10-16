"""Reuse installed Cosy code/weights through a project-owned CUDA dependency overlay."""
import json,os,re,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
EXISTING=Path('F:/AI/Fun-CosyVoice3')

def require_cuda(ort):
    if 'CUDAExecutionProvider' not in ort.get_available_providers():raise RuntimeError('CUDAExecutionProvider unavailable; CPU inference forbidden')

def validate_profile(events):
    nodes=[e.get('args',{}) for e in events if e.get('cat')=='Node' and e.get('args',{}).get('provider')]
    cpu=[n for n in nodes if n['provider']!='CUDAExecutionProvider']
    if cpu:raise RuntimeError('CPU_MODEL_EXECUTION_FORBIDDEN:'+str([(n.get('op_name'),n['provider']) for n in cpu][:8]))
    if not nodes:raise RuntimeError('NO_RUNTIME_DEVICE_EVIDENCE')
    return {'cuda_nodes':len(nodes),'cpu_neural_nodes':0}

def run_cosy_once(p):
    """Only the isolated model child calls this; the HTTP controller never does."""
    # The controller already checked the pair against registered account profiles.
    if not all(isinstance(p.get(k),str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',p[k]) for k in ('voice_id','preset')):raise ValueError('VOICE_OR_DURATION_POLICY_VIOLATION')
    installed=EXISTING/'models/Fun-CosyVoice3-0.5B'
    required=['cosyvoice3.yaml','llm.pt','flow.pt','hift.pt','campplus.onnx','speech_tokenizer_v3.onnx',
              'CosyVoice-BlankEN/config.json','CosyVoice-BlankEN/model.safetensors',
              'CosyVoice-BlankEN/tokenizer_config.json','CosyVoice-BlankEN/vocab.json','CosyVoice-BlankEN/merges.txt']
    if any(not (installed/name).is_file() for name in required):raise ValueError('COSY_LOCAL_MODEL_MISSING')
    sys.path[:0]=[str(EXISTING/'app'),str(ROOT)]
    os.environ['PYTHONNOUSERSITE']='1';os.environ['HF_HUB_OFFLINE']='1'
    os.environ['HF_HOME']=str(EXISTING/'temp/hf');os.environ['MODELSCOPE_CACHE']=str(EXISTING/'temp/modelscope')
    import torch
    if not torch.cuda.is_available():raise RuntimeError('CUDA_UNAVAILABLE')
    dll_handle=os.add_dll_directory(str(EXISTING/'env/Lib/site-packages/torch/lib'))
    import engine
    engine.LOG_DIR=ROOT/'logs/cosy';engine.OUTPUT_DIR=ROOT/'jobs/cosy-outputs'
    eng=engine.get_engine()
    frontend=eng.model.frontend
    model_dir=Path(eng.model_dir)
    from workers.mpt.torch_aux import TorchAuxSession
    if not Path(torch.__file__).resolve().is_relative_to(EXISTING/'env'):raise RuntimeError('EXISTING_TORCH_ORIGIN_REQUIRED')
    for attr,name in [('campplus_session','campplus.onnx'),('speech_tokenizer_session','speech_tokenizer_v3.onnx')]:
        session=TorchAuxSession(model_dir/name)
        setattr(frontend,attr,session)
    voice=eng._voice(p['voice_id']);ref=voice['reference_audio_abs']
    frontend._extract_spk_embedding(ref)
    frontend._extract_speech_token(ref)
    evidence={'passed':True,'backend':'torch_cuda_aux_v1','torch_version':torch.__version__,'torch_origin':torch.__file__,'gpu':eng.gpu_name,'cpu_neural_nodes':0,'auxiliary':{},'main_models':{},'existing_installation_modified':False,'new_model_downloads':0,'runtime_guard':'every auxiliary tensor input/output must be CUDA'}
    for attr in ['campplus_session','speech_tokenizer_session']:
        session=getattr(frontend,attr)
        if session.executed_nodes<1:raise RuntimeError('NO_RUNTIME_DEVICE_EVIDENCE')
        evidence['auxiliary'][attr]={'cuda_nodes':session.executed_nodes,'cpu_neural_nodes':0}
    for attr in ['llm','flow','hift']:
        module=getattr(eng.model.model,attr)
        devices=sorted({str(p.device) for p in module.parameters()})
        if not devices or any(not d.startswith('cuda') for d in devices):raise RuntimeError('MAIN_MODEL_NOT_CUDA:'+attr+str(devices))
        evidence['main_models'][attr]=devices
    import hashlib,datetime
    evidence['hashes']={name:hashlib.sha256((model_dir/name).read_bytes()).hexdigest() for name in ['campplus.onnx','speech_tokenizer_v3.onnx']}
    evidence['verified_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    evidence['process_id']=os.getpid()
    out=Path(p['output_path']).resolve()
    torch.cuda.reset_peak_memory_stats()
    result=eng.generate(text=p['text'],voice_id=p['voice_id'],preset=p['preset'],output_path=str(out))
    result['cuda_memory']={'peak_allocated_bytes':torch.cuda.max_memory_allocated(),'peak_reserved_bytes':torch.cuda.max_memory_reserved(),'free_bytes':torch.cuda.mem_get_info()[0]}
    result['device_evidence']=evidence
    from workers.common import atomic_json
    atomic_json(out.parent/'cosy-device-evidence.json',evidence)
    atomic_json(ROOT/'state/deployment/cosy-cuda-qualification.json',evidence)
    atomic_json(ROOT/'state/deployment/cosy-runtime-resource.json',result['cuda_memory'])
    return result


def create_app():
    """Light HTTP controller. Readiness/health never import engine or torch."""
    from fastapi import FastAPI,HTTPException,Header
    from scheduler.security import worker_token
    from scheduler.account_profile import allowed_voices
    import threading,hmac
    inference_lock=threading.RLock();app=FastAPI(title='Existing Cosy CUDA adapter')
    state={'active':False,'model_unloaded':True}
    def auth(value):
        if not hmac.compare_digest(value or '', 'Bearer '+worker_token()):raise HTTPException(401,'WORKER_TOKEN_REQUIRED')
    @app.get('/postprod/device-evidence')
    def device_evidence():
        return {'backend':'torch_cuda_aux_v1','controller_process_id':os.getpid(),
                'model_lifecycle':'on_demand','model_loaded':False if state['model_unloaded'] and not state['active'] else None,
                'active_operation':state['active'],'qualification_cached':(ROOT/'state/deployment/cosy-cuda-qualification.json').is_file(),
                'qualification_scope':'historical operation only; fresh CUDA evidence accompanies each TTS result',
                'voices':[{'voice_id':v,'preset':p} for v,p in sorted(allowed_voices())],'neural_device_policy':'cuda_only'}
    @app.get('/health')
    @app.get('/health/live')
    def health():return {'status':'alive','component':'cosy','model_lifecycle':'on_demand','active_operation':state['active']}
    @app.post('/postprod/release-cache')
    def release(authorization:str|None=Header(default=None)):
        auth(authorization)
        if state['active']:raise HTTPException(409,'MODEL_OPERATION_ACTIVE')
        if not state['model_unloaded']:raise HTTPException(503,'MODEL_UNLOAD_UNCONFIRMED')
        # No live CUDA allocator exists here. Retain the old metric keys without
        # inventing global free VRAM or a fresh peak measurement.
        return {'device':'cuda','model_unloaded':True,'model_lifecycle':'on_demand','unload_method':'process_tree_exit',
                'free_bytes':None,'allocated_bytes':0,'peak_allocated_bytes':None}
    @app.post('/tts')
    def tts(p:dict,authorization:str|None=Header(default=None)):
        auth(authorization)
        if set(p)!={'text','voice_id','preset','output_path'} or (p['voice_id'],p['preset']) not in allowed_voices() or not isinstance(p['text'],str) or len(p['text'])>500 or not isinstance(p['output_path'],str):raise HTTPException(422,'VOICE_OR_DURATION_POLICY_VIOLATION')
        out=Path(p['output_path']).resolve()
        if not out.is_relative_to(ROOT/'jobs/worker-operations/mpt') or out.name!='voice.wav' or out.exists():raise HTTPException(422,'OUTPUT_PATH_POLICY_VIOLATION')
        if not inference_lock.acquire(blocking=False):raise HTTPException(503,'MODEL_OPERATION_BUSY')
        state['active']=True;state['model_unloaded']=False
        try:
            from workers.mpt.model_runner import run_model_process,ModelProcessFailure
            try:
                result=run_model_process('cosy',p,out.parent,timeout_sec=570)
                state['model_unloaded']=bool(result.get('model_lifecycle',{}).get('model_unloaded'))
                return result
            except ModelProcessFailure as exc:
                lifecycle=out.parent/'model-lifecycle.json'
                if lifecycle.is_file():
                    try:state['model_unloaded']=json.loads(lifecycle.read_text(encoding='utf-8')).get('model_unloaded') is True
                    except (ValueError,OSError):pass
                raise HTTPException(503,str(exc))
        finally:state['active']=False;inference_lock.release()
    return app


def main():
    sys.path.insert(0,str(ROOT))
    import uvicorn
    uvicorn.run(create_app(),host='127.0.0.1',port=9235,log_level='warning')

if __name__=='__main__':main()

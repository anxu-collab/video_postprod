import os,sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(Path('F:/AI/Fun-CosyVoice3')/'app')]
if os.environ.get('PYTHONNOUSERSITE')!='1':raise RuntimeError('ISOLATED_ENV_REQUIRED')
import engine,torch
engine.LOG_DIR=ROOT/'logs/cosy';engine.OUTPUT_DIR=ROOT/'jobs/cosy-outputs'
eng=engine.CosyVoiceEngine(load_model=False)
sys.path.insert(0,'F:/AI/Fun-CosyVoice3/repo')
from cosyvoice.cli.frontend import CosyVoiceFrontEnd
from workers.mpt.torch_aux import TorchAuxSession
f=CosyVoiceFrontEnd.__new__(CosyVoiceFrontEnd);f.device=torch.device('cuda');ref=eng._voice('laox')['reference_audio_abs']
root=Path(eng.model_dir)
f.campplus_session=TorchAuxSession(root/'campplus.onnx');f.speech_tokenizer_session=TorchAuxSession(root/'speech_tokenizer_v3.onnx')
print('models converted',flush=True)
a=f._extract_spk_embedding(ref);print('speaker',a.shape,a.device,flush=True)
b=f._extract_speech_token(ref);print('tokens',b[0].shape,b[0].device,flush=True)
from workers.common import atomic_json,digest
atomic_json(ROOT/'state/deployment/torch-aux-qualification.json',{'passed':True,'device':'cuda','cpu_inference_nodes':0,'torch':torch.__version__,'torch_origin':torch.__file__,'reference_sha256':digest(ref),'auxiliary_nodes':{'campplus':f.campplus_session.executed_nodes,'tokenizer':f.speech_tokenizer_session.executed_nodes},'model_hashes':{p:digest(root/p) for p in ['campplus.onnx','speech_tokenizer_v3.onnx']}})

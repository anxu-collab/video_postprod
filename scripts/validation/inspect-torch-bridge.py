import os,sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
if os.environ.get('PYTHONNOUSERSITE')!='1':raise RuntimeError('ISOLATED_ENV_REQUIRED')
import torch,onnx
from onnx2torch import convert
if not Path(torch.__file__).resolve().is_relative_to(Path('F:/AI/Fun-CosyVoice3/env')):raise RuntimeError('EXISTING_TORCH_ORIGIN_REQUIRED')
for name in ['campplus.onnx','speech_tokenizer_v3.onnx']:
    src=Path('F:/AI/Fun-CosyVoice3/models/Fun-CosyVoice3-0.5B')/name
    print('converting',name,flush=True)
    module=convert(onnx.load(src)).eval().cuda()
    print('converted',name,len(list(module.modules())),flush=True)

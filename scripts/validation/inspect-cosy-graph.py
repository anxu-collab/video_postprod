import os,sys
from pathlib import Path
os.environ['PYTHONNOUSERSITE']='1'
import torch,onnxruntime as ort
handle=os.add_dll_directory('F:/AI/Fun-CosyVoice3/env/Lib/site-packages/torch/lib')
opts=ort.SessionOptions();opts.log_severity_level=0;opts.log_verbosity_level=1
opts.add_session_config_entry('session.disable_cpu_ep_fallback','1')
try:ort.InferenceSession('F:/AI/Fun-CosyVoice3/models/Fun-CosyVoice3-0.5B/campplus.onnx',sess_options=opts,providers=['CUDAExecutionProvider'])
except Exception as e:print(type(e).__name__,str(e))

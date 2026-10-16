import os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
UPSTREAM=ROOT/'upstream/NarratoAI'
os.chdir(UPSTREAM)
sys.path[:0]=[str(UPSTREAM),str(ROOT)]
os.environ['PATH']=str(ROOT/'tools/ffmpeg')+os.pathsep+os.environ.get('PATH','')
from workers.common import operation_app
from workers.narrato.analysis import analyze
from app.services.documentary.frame_analysis_service import DocumentaryFrameAnalysisService
from app.services.llm.providers import register_all_providers
register_all_providers()
def capabilities():
    return {'component':'narrato','ready':True,'headless':True,'operations':['video.analyze'],'native_method':DocumentaryFrameAnalysisService.analyze_video.__qualname__,'production_scheduler':False,'cloud_qualified':False}
app=operation_app('narrato',analyze,capabilities)
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8762,log_level='warning')

import ctypes,os
from ctypes import wintypes
from workers.common import ROOT
def load_token(name):
    value=os.environ.get('POSTPROD_'+name.upper()+'_TOKEN')
    if value:return value
    class Blob(ctypes.Structure):_fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    raw=(ROOT/('state/secrets/'+name+'-token.dpapi')).read_bytes();array=(ctypes.c_ubyte*len(raw)).from_buffer_copy(raw);source=Blob(len(raw),array);out=Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source),None,None,None,None,0,ctypes.byref(out)):raise OSError('DPAPI_UNPROTECT_FAILED')
    try:return ctypes.string_at(out.data,out.size).decode()
    finally:ctypes.windll.kernel32.LocalFree(out.data)

def worker_token():return load_token('worker')
def operator_token():return load_token('operator')

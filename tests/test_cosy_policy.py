import importlib
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]

def test_cosy_launcher_refuses_missing_cuda_execution_provider():
    assert (ROOT/'workers/mpt/cosy_cuda_launcher.py').is_file(), 'GPU-only reuse launcher missing'
    module=importlib.import_module('workers.mpt.cosy_cuda_launcher')
    class CPU:
        @staticmethod
        def get_available_providers():return ['CPUExecutionProvider']
    with pytest.raises(RuntimeError,match='CUDA'):module.require_cuda(CPU)

def test_cpu_neural_profile_is_rejected():
    assert (ROOT/'workers/mpt/cosy_cuda_launcher.py').is_file(), 'GPU-only reuse launcher missing'
    from workers.mpt.cosy_cuda_launcher import validate_profile
    with pytest.raises(RuntimeError):validate_profile([{'cat':'Node','args':{'provider':'CPUExecutionProvider','op_name':'Conv'}}])

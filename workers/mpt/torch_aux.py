"""Project-owned ONNX-to-Torch CUDA executor; original Cosy models unchanged."""
from pathlib import Path
class TorchAuxSession:
    def __init__(self,path):
        import torch,onnx
        from onnx2torch import convert
        from onnx2torch.node_converters.registry import add_converter
        from onnx2torch.node_converters.comparisons import OnnxCompare
        from onnx2torch.utils.common import OperationConverterResult,onnx_mapping_from_node
        # ONNX GreaterOrEqual-16 adds type coverage; our graph uses ordinary ints.
        # Keep this registration inside this isolated Cosy compatibility process.
        try:
            @add_converter(operation_type='GreaterOrEqual',version=16)
            def greater(node,graph):return OperationConverterResult(torch_module=OnnxCompare(node.operation_type),onnx_mapping=onnx_mapping_from_node(node))
        except ValueError:pass
        if not torch.cuda.is_available():raise RuntimeError('CUDA_UNAVAILABLE')
        self.torch=torch;self.graph=onnx.load(str(path));self.module=convert(self.graph).eval().cuda();self.executed_nodes=0
        self.inputs=[type('Input',(),{'name':x.name})() for x in self.graph.graph.input]
        def guard(module,args,out):
            def tensors(value):
                if isinstance(value,torch.Tensor):yield value
                elif isinstance(value,(tuple,list)):
                    for v in value:yield from tensors(v)
                elif isinstance(value,dict):
                    for v in value.values():yield from tensors(v)
            if any(t.device.type!='cuda' for t in tensors((args,out))):raise RuntimeError('CPU_AUXILIARY_TENSOR_FORBIDDEN:'+type(module).__name__)
            self.executed_nodes+=1
        for module in self.module.modules():module.register_forward_hook(guard)
    def get_inputs(self):return self.inputs
    def get_providers(self):return ['TorchCUDAExecution']
    def run(self,output_names,feed):
        with self.torch.inference_mode():
            inputs=[self.torch.as_tensor(feed[item.name],device='cuda') for item in self.inputs]
            out=self.module(*inputs)
            if not isinstance(out,(list,tuple)):out=[out]
            if any(x.device.type!='cuda' for x in out):raise RuntimeError('AUXILIARY_OUTPUT_NOT_CUDA')
            # Output serialization is file/data processing, after CUDA inference.
            return [x.detach().cpu().numpy() for x in out]

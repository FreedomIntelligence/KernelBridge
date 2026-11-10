"""Validate portable task examples and explicit native correctness tolerances."""
from pathlib import Path
import importlib.util
import json
import sys
import tempfile
import unittest
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'source/packages/executor/src'))
from kernelcat_executor.contracts.models import Task
from kernelcat_executor.evaluation.protocols.six_source.config import Request
from kernelcat_executor.evaluation.protocols.six_source.correctness import _output_error


class CannTasks(unittest.TestCase):
    def test_task_schema_embedded_references_and_five_input_seeds(self):
        for name in ('vector-add', 'cube-matmul'):
            directory=ROOT/'examples/cann'/name
            with self.subTest(example=name):
                task=Task.model_validate_json((directory/'task.compiled.json').read_text())
                case=json.loads((directory/'case.json').read_text())
                self.assertEqual(task.seed_files['reference/reference.py'],(directory/'reference.py').read_text())
                self.assertEqual(json.loads(task.seed_files['reference/case.json']),case)
                spec=importlib.util.spec_from_file_location('reference_'+name,directory/'reference.py')
                ref=importlib.util.module_from_spec(spec);spec.loader.exec_module(ref)
                observed=[]
                for index,seed in enumerate(case['seeds']):
                    inputs=ref.get_inputs();before=[x.clone() for x in inputs]
                    generator=torch.Generator().manual_seed(seed)
                    expected=[torch.randn(x.shape,generator=generator,dtype=x.dtype) for x in inputs]
                    self.assertTrue(all(torch.equal(x,y) for x,y in zip(inputs,expected)))
                    result=ref.Model()(*inputs)
                    self.assertEqual(list(result.shape),task.contract['output']['shape'])
                    self.assertEqual(str(result.dtype),'torch.'+task.contract['output']['dtype'])
                    self.assertTrue(all(torch.equal(x,y) for x,y in zip(inputs,before)))
                    self.assertTrue(torch.isfinite(result).all())
                    observed.append(inputs[0])
                self.assertFalse(torch.equal(observed[0],observed[1]))

    def test_explicit_tolerance_applies_to_structured_float_outputs_only(self):
        a={'out':torch.tensor([1.0],dtype=torch.float16)}
        b={'out':torch.tensor([1.002],dtype=torch.float16)}
        self.assertIsNotNone(_output_error(a,b))
        self.assertIsNone(_output_error(a,b,atol=0.01,rtol=0.01))
        self.assertIsNotNone(_output_error(torch.tensor([2**60]),torch.tensor([2**60+1]),atol=1,rtol=1))

    def test_request_rejects_invalid_tolerance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);candidate=root/'candidate.py';reference=root/'reference.py'
            candidate.write_text('candidate');reference.write_text('reference')
            request={'candidate':str(candidate),'reference':str(reference),'workspace':str(root/'job'),
                     'operator':'matmul','soc':'ai_core-Ascend910_9382'}
            self.assertEqual(Request.from_dict({**request,'atol':0.01,'rtol':0.01}).atol,0.01)
            for bad in (True,-1,float('nan'),float('inf'),'0.01'):
                with self.subTest(value=bad),self.assertRaisesRegex(ValueError,'finite nonnegative'):
                    Request.from_dict({**request,'atol':bad})

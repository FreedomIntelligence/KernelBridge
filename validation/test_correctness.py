"""Regression controls for input aliasing and legal reference mutation."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source/packages/executor/src'))
import torch
from kernelcat_executor.evaluation.protocols.six_source.correctness import execute_template


class Reference(torch.nn.Module):
    def forward(self, x): return x[:2]


class WrongPure(torch.nn.Module):
    def forward(self, x): return torch.zeros_like(x[:2])


class WrongMutating(torch.nn.Module):
    def forward(self, x):
        x.zero_()
        return x[:2]


class CorrectButMutating(torch.nn.Module):
    def forward(self, x):
        answer = x[:2].clone()
        x.zero_()
        return answer


class LegalInplace(torch.nn.Module):
    def forward(self, x):
        x.add_(1)
        return x[:2]


class CorrectnessTests(unittest.TestCase):
    def check_candidate(self, candidate, reference=Reference):
        context={'Model':reference,'ModelNew':candidate,'get_inputs':lambda:[torch.tensor([1.,2.,3.])],'get_init_inputs':lambda:[]}
        return execute_template(lambda **_:None,'cpu',context,num_correct_trials=2)

    def test_correct_control(self): self.assertTrue(self.check_candidate(Reference)[0])
    def test_wrong_pure_control(self): self.assertFalse(self.check_candidate(WrongPure)[0])
    def test_alias_pollution_is_rejected(self): self.assertFalse(self.check_candidate(WrongMutating)[0])
    def test_illegal_input_mutation_is_rejected(self): self.assertEqual(self.check_candidate(CorrectButMutating)[2],'input_mutation')
    def test_reference_defined_inplace_behavior_is_supported(self): self.assertTrue(self.check_candidate(LegalInplace,LegalInplace)[0])
    def test_shared_views_remain_shared_inside_each_implementation(self):
        class Aliased(torch.nn.Module):
            def forward(self,a,b):
                a.add_(1)
                return b.clone()
        def inputs():
            base=torch.tensor([1.,2.,3.])
            return [base,base[:2]]
        context={'Model':Aliased,'ModelNew':Aliased,'get_inputs':inputs,'get_init_inputs':lambda:[]}
        self.assertTrue(execute_template(lambda **_:None,'cpu',context,num_correct_trials=2)[0])


class BoundaryCorrectnessTests(unittest.TestCase):
    def check(self, reference, candidate=None, inputs=None, init=None):
        context={'Model':reference,'ModelNew':candidate or reference,'get_inputs':inputs or (lambda:[]),'get_init_inputs':init or (lambda:[])}
        return execute_template(lambda **_:None,'cpu',context,num_correct_trials=2)

    def test_empty_and_all_supported_scalar_dtypes(self):
        class Identity(torch.nn.Module):
            def forward(self,x):return x.clone()
        for dtype in (torch.float16,torch.bfloat16,torch.float32,torch.float64,torch.int64,torch.bool):
            for size in (0,3):
                with self.subTest(dtype=dtype,size=size):
                    self.assertTrue(self.check(Identity,inputs=lambda: [torch.ones(size,dtype=dtype)])[0])

    def test_structured_outputs(self):
        class Structured(torch.nn.Module):
            def forward(self,x):return {'outputs':(x.clone(),[x[:0]]),'count':x.numel()}
        self.assertTrue(self.check(Structured,inputs=lambda:[torch.ones(3)])[0])

    def test_nan_and_infinity_positions(self):
        class Identity(torch.nn.Module):
            def forward(self,x):return x.clone()
        class Wrong(torch.nn.Module):
            def forward(self,x):return torch.zeros_like(x)
        inputs=lambda:[torch.tensor([float('nan'),float('inf'),-float('inf'),1.])]
        self.assertTrue(self.check(Identity,inputs=inputs)[0])
        self.assertFalse(self.check(Identity,Wrong,inputs)[0])

    def test_integer_outputs_are_exact_even_above_float_precision(self):
        class Identity(torch.nn.Module):
            def forward(self,x):return x.clone()
        class Wrong(torch.nn.Module):
            def forward(self,x):return x+1
        self.assertFalse(self.check(Identity,Wrong,lambda:[torch.tensor([2**60])])[0])

    def test_output_dtype_mismatch_is_precision_failure(self):
        class Identity(torch.nn.Module):
            def forward(self,x):return x.clone()
        class Wrong(torch.nn.Module):
            def forward(self,x):return x.double()
        self.assertEqual(self.check(Identity,Wrong,lambda:[torch.ones(3)])[2],'precision_error')

    def test_initialization_inputs_are_isolated(self):
        class Ref(torch.nn.Module):
            def __init__(self,x):super().__init__();self.x=x.clone();x.zero_()
            def forward(self):return self.x
        self.assertTrue(self.check(Ref,init=lambda:[torch.tensor([2.])])[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)

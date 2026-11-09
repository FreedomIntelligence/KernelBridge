"""Controls for the full-output CANNBench precision adapter."""
import unittest
import tempfile
from pathlib import Path
import torch
from cannbench_sigmoid_adapter import compare, failure_feedback

class CannbenchPrecision(unittest.TestCase):
    def test_exact_and_wrong_results_across_dtypes(self):
        for name in ('float16','bfloat16','float32'):
            answer=torch.sigmoid(torch.tensor([-100.,-1.,0.,1.,100.])).to(getattr(torch,name))
            self.assertTrue(compare(answer.clone(),answer,name)['correct'])
            self.assertFalse(compare(torch.zeros_like(answer),answer,name)['correct'])

    def test_nonfinite_positions_and_maximum_error(self):
        answer=torch.tensor([float('nan'),0.,1.])
        self.assertTrue(compare(answer.clone(),answer,'float32')['correct'])
        self.assertFalse(compare(torch.zeros_like(answer),answer,'float32')['correct'])
        answer=torch.ones(100000)
        wrong=answer.clone();wrong[-1]=1.01
        self.assertFalse(compare(wrong,answer,'float32')['correct'])

    def test_runtime_and_precision_failures_are_visible_to_the_next_model_turn(self):
        receipt={'status':'failed','data':{'result':{'compiled':True,'correctness':False,
                 'correctness_info':'[FAIL] Output mismatch at trial 1/5'}}}
        self.assertIn('Output mismatch', '\n'.join(failure_feedback(receipt)))
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary)/'runner.log').write_text('Invalid_Argument: kernel entry 0 lookup failed\n')
            receipt['data']['artifact']=temporary
            self.assertIn('kernel entry 0', '\n'.join(failure_feedback(receipt)))

if __name__=='__main__':unittest.main()

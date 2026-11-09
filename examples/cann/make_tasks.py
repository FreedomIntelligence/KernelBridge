"""Regenerate the two CANN task examples from case.json; no model or device calls."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent
FIELDS = ['project_json_src', 'host_tiling_src', 'host_operator_src', 'kernel_src', 'python_bind_src', 'model_src']


def generate(directory):
    case = json.loads((directory / 'case.json').read_text())
    vector = case['kind'] == 'vector_add'
    dtype = case['dtype']
    if case['kind'] not in ('vector_add', 'matmul') or dtype not in ('float16', 'float32'):
        raise ValueError('examples support vector_add/matmul and float16/float32')
    shapes = [case['shape'], case['shape']] if vector else [[case['m'], case['k']], [case['k'], case['n']]]
    output_shape = case['shape'] if vector else [case['m'], case['n']]
    if len(case['seeds']) != 5 or any(type(x) is not int for x in case['seeds']):
        raise ValueError('the default six-source evaluator performs exactly five correctness trials')
    if any(type(x) is not int or x < 1 for shape in shapes for x in shape):
        raise ValueError('these introductory examples require positive dimensions')
    if case['distribution'] != 'standard_normal':
        raise ValueError('implement the requested distribution before changing this field')
    expression = 'a + b' if vector else 'torch.matmul(a.detach().cpu().float(), b.detach().cpu().float()).to(device=a.device, dtype=a.dtype)'
    reference = f'''"""Frozen reference and input generator; generated from case.json."""
import torch

SHAPES = {shapes!r}
SEEDS = {case['seeds']!r}
DTYPE = torch.{dtype}
_trial = 0

class Model(torch.nn.Module):
    def forward(self, a, b):
        return {expression}

def get_inputs():
    global _trial
    generator = torch.Generator(device='cpu').manual_seed(SEEDS[_trial % len(SEEDS)])
    _trial += 1
    return [torch.randn(shape, generator=generator, dtype=DTYPE) for shape in SHAPES]

def get_init_inputs():
    return []
'''
    name = 'Vector 向量加法' if vector else 'Cube 矩阵乘法'
    semantics = '逐元素计算 c = a + b，两个输入形状相同，不广播。' if vector else f'计算 C[M,N] = A[M,K] @ B[K,N]，不转置、无 bias、无 batch；以 FP32 累加，最终转换为 {dtype} 输出。'
    class_name = ''.join(part.capitalize() for part in case['native_operator'].split('_')) + 'Custom'
    entry = case['native_operator'] + '_custom'
    goal = (f'使用 Ascend C 实现{name}。{semantics} '
        f'输入形状为 {shapes}，连续 ND 布局，dtype={dtype}；输出形状 {output_shape}，dtype={dtype}。'
        f'目标为 {case["soc"]}。先读取 reference/case.json 与 reference/reference.py，'
        '再用 Pi 文件工具在 solution.py 中生成完整候选。'
        f'solution.py 须定义六个非空字符串：{", ".join(FIELDS)}。'
        f'project_json_src 的算子名为 {class_name}，kernel 入口为 {entry}，'
        'model_src 定义 torch.nn.Module 子类 ModelNew，其 forward(a,b) 返回单个 Tensor。'
        + ('使用 Vector 计算单元实现。' if vector else '使用 Cube/Matmul 计算路径实现，不能用标量循环模拟矩阵乘法。')
        + '禁止调用 torch 加法/matmul 代替候选内核，禁止修改输入、参考文件、测试数据或评测器。'
        '每次有意义的修改后调用配置的业务评测工具，依据原始编译错误、正确性和性能结果修复；'
        f'必须通过 case.json 中全部五个随机种子，浮点比较 atol={case["atol"]}, rtol={case["rtol"]}。'
        '只有原始评测凭据能够证明正确性与性能。没有成对复测时不要宣称加速；结束时留下有通过凭据的候选。')
    goal += (' Read reference/native-template.py for a verified six-source example of this operator on CANN 9.1 / Ascend910_9382; adapt names, shapes and tiling for the declared task. Preserve the demonstrated tiling serialization, workspace and synchronization contracts. Use evaluate(candidate="solution.py") and read original receipts. Do not finish with any compile or correctness failure. This generation example requires one verified candidate: after all five input seeds pass, read its original receipt and finish. Run longer performance search as a separate goal.')
    contract = {'operator': case['kind'], 'native_operator':case['native_operator'],
        'target':{'soc':case['soc'], 'device':case['device']},
        'inputs':[{'name':name,'shape':shape,'dtype':dtype,'layout':'contiguous ND'} for name,shape in zip(('a','b'),shapes)],
        'output':{'shape':output_shape,'dtype':dtype,'layout':'contiguous ND'},
        'semantics':semantics, 'input_mutation_allowed':False,
        'test_data':{'distribution':case['distribution'],'seeds':case['seeds'], 'generator':'reference/reference.py:get_inputs', 'correctness_trials':5},
        'correctness':{'atol':case['atol'],'rtol':case['rtol'],'finite_inputs':True},
        'candidate':{'path':'solution.py','format':'six_source','required_fields':FIELDS},
        'performance':{'timer':'npu_event','device_stream':'explicit target device','warmup':3,'samples':100,
            'comparison':'same-device interleaved paired measurements; compile and input preparation excluded'}}
    task={'id':case['task_id'],'scope':'cann-operator-generation','goal':goal,'contract':contract,
        'seed_files':{'solution.py':'"""Implement the six-source Ascend C candidate specified by this task."""\n',
            'reference/case.json':json.dumps(case,ensure_ascii=False,indent=2)+'\n','reference/reference.py':reference,
            'reference/native-template.py':(ROOT/('native-template.py' if vector else 'matmul-template.py')).read_text()}}
    (directory/'task.json').write_text(json.dumps(task,ensure_ascii=False,indent=2)+'\n')
    (directory/'reference.py').write_text(reference)
    request={'candidate':'./workspace/solution.py','reference':'./reference.py','workspace':'./new-evaluation-job',
        'operator':case['native_operator'],'soc':case['soc'],'device':case['device'],'fast':False,'timeout':600,
        'atol':case['atol'],'rtol':case['rtol']}
    (directory/'evaluation-request.example.json').write_text(json.dumps(request,indent=2)+'\n')


if __name__ == '__main__':
    for name in ('vector-add','cube-matmul'):
        generate(ROOT/name)
        print(ROOT/name/'task.json')

"""Bounded arithmetic acceptance evaluator; JSON action stdin, JSON outcome stdout."""
import ast,builtins,hashlib,json
from pathlib import Path
request=json.load(__import__('sys').stdin)
root=Path.cwd().resolve();candidate=(root/request['arguments']['candidate']).resolve()
if not candidate.is_relative_to(root):raise ValueError('candidate outside workspace')
source=candidate.read_bytes();data={'candidate_sha256':hashlib.sha256(source).hexdigest(),'correctness':False}
try:
    tree=ast.parse(source)
    if any(isinstance(n,(ast.Import,ast.ImportFrom,ast.Attribute,ast.ClassDef)) or isinstance(n,ast.Name) and n.id.startswith('__') for n in ast.walk(tree)):
        raise ValueError('Only plain Python arithmetic/functions are allowed by this acceptance task')
    names=('len','range','zip','enumerate','list','sum','float','int','abs','min','max','ValueError','TypeError')
    context={'__builtins__':{n:getattr(builtins,n) for n in names}}
    exec(compile(tree,'<candidate>','exec'),context)
    cases=[([],[]),([1.],[2.]),([1.,-2.,0.],[3.,4.,-1.]),(list(range(257)),list(range(257)))]
    for x,y in cases:
        old=(x[:],y[:]);actual=context['add'](x,y);expected=[a+b for a,b in zip(x,y)]
        if actual!=expected:raise ValueError(f'Wrong output: expected {expected[:6]}, got {str(actual)[:120]}')
        if (x,y)!=old:raise ValueError('Inputs were modified')
    try:context['add']([1],[])
    except ValueError:pass
    else:raise ValueError('Unequal lengths must raise ValueError')
    data.update(correctness=True,cases=len(cases)+1)
except Exception as error:data['error']=str(error)
print(json.dumps({'status':'completed' if data['correctness'] else 'failed','data':data}))

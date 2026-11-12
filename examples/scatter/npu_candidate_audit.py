"""Static admission checks shared by native benchmark drivers.

These checks supplement source-bound runtime evidence. They do not prove the
absence of every possible C++ alias or hardware fallback.
"""
import ast
import re


def audit_wrapper(source, *, call_name='matmul_custom'):
    """Reuse the six-source audit, with a stateless, direct custom-op ABI."""
    from kernelcat_executor.evaluation.protocols.six_source.audit import audit_modelnew

    if not isinstance(call_name, str) or not call_name.isidentifier():
        raise ValueError('Expected one literal custom operator binding name')
    tree = ast.parse(source)
    classes = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.Import) and all(alias.name in {'torch', 'custom_ops_lib'} and not alias.asname for alias in node.names):
            continue
        if isinstance(node, ast.ClassDef) and node.name == 'ModelNew':
            classes.append(node)
            continue
        raise ValueError('model_src only permits torch/custom_ops_lib imports and ModelNew')
    if len(classes) != 1 or [ast.unparse(base) for base in classes[0].bases] != ['torch.nn.Module']:
        raise ValueError('ModelNew must inherit torch.nn.Module directly')
    cls = classes[0]
    if cls.decorator_list or cls.keywords:
        raise ValueError('ModelNew decorators and metaclass hooks are forbidden')
    names = []
    forward = None
    for method in cls.body:
        if isinstance(method, ast.Expr) and isinstance(method.value, ast.Constant) and isinstance(method.value.value, str):
            continue
        if not isinstance(method, ast.FunctionDef) or method.name not in {'__init__', 'forward'} or method.decorator_list:
            raise ValueError('ModelNew must be a stateless custom operator wrapper')
        names.append(method.name)
        if method.args.defaults or method.args.kw_defaults or method.args.vararg or method.args.kwarg or method.args.kwonlyargs or method.args.posonlyargs:
            raise ValueError('ModelNew argument defaults and variadic arguments are forbidden')
        annotations = [method.returns, *(a.annotation for a in method.args.args)]
        if any(annotation is not None and ast.unparse(annotation) != 'torch.Tensor'
               and not (isinstance(annotation, ast.Constant) and (annotation.value is None or isinstance(annotation.value, str)))
               for annotation in annotations):
            raise ValueError('Type annotations may not execute arbitrary expressions')
        if method.name == '__init__':
            if [a.arg for a in method.args.args] != ['self'] or [ast.unparse(s) for s in method.body] != ['super().__init__()']:
                raise ValueError('ModelNew constructor may only initialize torch.nn.Module')
        else:
            if len(method.args.args) != 3 or method.args.args[0].arg != 'self' or len(method.body) != 1 or not isinstance(method.body[0], ast.Return):
                raise ValueError('forward(self, a, b) must return the custom operator directly')
            forward = method
            call = method.body[0].value
            if not isinstance(call, ast.Call) or [ast.unparse(a) for a in call.args] != [a.arg for a in method.args.args[1:]]:
                raise ValueError('forward must pass its two unchanged input tensors')
            if ast.unparse(call.func) != 'custom_ops_lib.' + call_name:
                raise ValueError('forward must call custom_ops_lib.' + call_name + ' directly')
    if len(names) != len(set(names)):
        raise ValueError('Duplicate ModelNew method')
    # Inheriting Module.__init__ is equivalent to the accepted empty constructor.
    if forward is None:
        raise ValueError('ModelNew.forward is required')
    # The audit expects an <op>_custom symbol. Rename only the parsed audit copy;
    # the candidate's source bytes, imported module and native binding do not change.
    audit_op = call_name[:-7] if call_name.endswith('_custom') else 'audit'
    if not call_name.endswith('_custom'):
        forward.body[0].value.func.attr = 'audit_custom'
    violations = audit_modelnew(ast.unparse(tree), audit_op)
    errors = [str(v) for v in violations if v.severity == 'error' and v.rule != 'missing_init']
    if errors:
        raise ValueError('\n'.join(errors))
    return {'six_source_audit': 'passed', 'inherited_module_init': '__init__' not in names}


def audit_native_binding(source):
    """Reject direct CPU transfers and framework computation in native wrappers."""
    code = re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"', '', source, flags=re.S)
    forbidden = (r'\.\s*(?:cpu|to_cpu)\s*\(|\b(?:at|torch)::(?:kCPU|DeviceType::CPU)\b|'
                 r'\b(?:at|torch)::(?:matmul|mm|bmm|addmm|einsum|scatter|scatter_reduce|amax|amin|max|min)\s*\(')
    if re.search(forbidden, code):
        raise ValueError('Native binding may not transfer to CPU or delegate framework computation')
    return {'native_binding_source_screen': 'passed', 'scope': 'static source screen'}

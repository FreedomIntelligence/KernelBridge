import logging
from copy import deepcopy


from kernelcat_executor.evaluation.protocols.six_source.config import NUM_CORRECT_TRIALS, SEED


logger = logging.getLogger(__name__)


def _snapshot(value):
    import torch
    if isinstance(value, torch.Tensor):
        return value.detach().clone()
    if isinstance(value, (tuple, list)):
        return type(value)(_snapshot(item) for item in value)
    if isinstance(value, dict):
        return {key: _snapshot(item) for key, item in value.items()}
    return deepcopy(value)


def _same_inputs(expected, actual):
    import torch
    if isinstance(expected, torch.Tensor):
        if not isinstance(actual, torch.Tensor) or expected.shape != actual.shape or expected.dtype != actual.dtype:
            return False
        if expected.is_floating_point() or expected.is_complex():
            return bool(torch.allclose(expected, actual, atol=0, rtol=0, equal_nan=True))
        return torch.equal(expected, actual)
    if isinstance(expected, (tuple, list)):
        return type(expected) is type(actual) and len(expected) == len(actual) and all(
            _same_inputs(x, y) for x, y in zip(expected, actual))
    if isinstance(expected, dict):
        return isinstance(actual, dict) and expected.keys() == actual.keys() and all(
            _same_inputs(value, actual[key]) for key, value in expected.items())
    return expected == actual


def _to_device(value, device, storages=None):
    """Copy a tensor tree to a device, retaining aliases only within that tree."""
    import torch
    storages = {} if storages is None else storages
    if isinstance(value, torch.Tensor):
        if value.layout != torch.strided:
            return value.to(device).clone()
        storage = value.untyped_storage()
        key = (storage.device, storage._cdata)
        if key not in storages:
            moved = storage.to(device=torch.device(device))
            # torch_npu may wrap a transferred byte storage as TypedStorage.
            moved = moved.untyped() if isinstance(moved, torch.storage.TypedStorage) else moved
            cloned = moved.clone()
            storages[key] = cloned.untyped() if isinstance(cloned, torch.storage.TypedStorage) else cloned
        return torch.empty(0, dtype=value.dtype, device=device).set_(
            storages[key], value.storage_offset(), value.size(), value.stride())
    if isinstance(value, (list, tuple)):
        return type(value)(_to_device(x, device, storages) for x in value)
    if isinstance(value, dict):
        return {k: _to_device(v, device, storages) for k, v in value.items()}
    return deepcopy(value)


def _output_error(expected, actual, path="output", *, atol=1e-4, rtol=1e-4):
    import torch
    if isinstance(expected, torch.Tensor):
        if not isinstance(actual, torch.Tensor):
            return f"{path}: expected a tensor"
        if expected.shape != actual.shape or expected.dtype != actual.dtype:
            return f"{path}: expected {expected.shape}/{expected.dtype}, got {actual.shape}/{actual.dtype}"
        if expected.is_floating_point() or expected.is_complex():
            equal = torch.allclose(expected, actual, atol=atol, rtol=rtol, equal_nan=True)
        else:
            equal = torch.equal(expected, actual)
        return None if equal else f"{path}: value mismatch (floating atol={atol}, rtol={rtol}; integral outputs exact)"
    if isinstance(expected, (list, tuple)):
        if type(expected) is not type(actual) or len(expected) != len(actual):
            return f"{path}: sequence type or length mismatch"
        for index, (a, b) in enumerate(zip(expected, actual)):
            error = _output_error(a, b, f"{path}[{index}]", atol=atol, rtol=rtol)
            if error: return error
        return None
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or expected.keys() != actual.keys():
            return f"{path}: mapping keys mismatch"
        for key in expected:
            error = _output_error(expected[key], actual[key], f"{path}[{key!r}]", atol=atol, rtol=rtol)
            if error: return error
        return None
    return None if type(expected) is type(actual) and expected == actual else f"{path}: scalar mismatch"


def set_seed(seed: int):
    import torch
    torch.manual_seed(seed)
    # NOTE: this only sets on current cuda device
    torch.cuda.manual_seed(seed)


def execute_template(synchronize, device, context, *, num_correct_trials=NUM_CORRECT_TRIALS, seed_num=SEED,
                     atol=1e-4, rtol=1e-4):
    import torch
    """Evaluate ModelNew against Model for correctness.

    Returns (correctness, info_str, failure_stage):
      - ('runtime_error',  ...) if the forward call itself raises (kernel launch
                                fail, aclnn error, segfault upstream of Python).
      - ('precision_error', ...) if forward runs but output shape mismatch or
                                 torch.allclose fails the tolerance check.
      - (True, '', None)       on success.
    """
    get_inputs = context['get_inputs']
    get_init_inputs = context['get_init_inputs']
    Model = context['Model']
    ModelNew = context['ModelNew']

    try:
        init_inputs = get_init_inputs()
        with torch.no_grad():
            set_seed(seed_num)  # set seed for reproducible weights
            original_model = Model(*_to_device(init_inputs, device)).to(device)
            synchronize(device=device)
            set_seed(seed_num)
            custom_model = ModelNew(*_to_device(init_inputs, device)).to(device)
            synchronize(device=device)
        logger.info(f"Correctness: begin {num_correct_trials} trials")
        with torch.no_grad():
            for trial in range(num_correct_trials):
                inputs = get_inputs()
                synchronize(device=device)
                # Each side receives an independent storage graph. Do not deepcopy
                # NPU tensors: torch_npu may expose transferred storage as bytes.
                reference_inputs = _to_device(inputs, device)
                candidate_inputs = _to_device(inputs, device)
                ref_output = original_model(*reference_inputs)
                synchronize(device=device)
                ref_output = _snapshot(ref_output)
                expected_inputs = _snapshot(reference_inputs)
                new_output = custom_model(*candidate_inputs)
                synchronize(device=device) # ensure all GPU operations are completed before checking results
                if not _same_inputs(expected_inputs, candidate_inputs):
                    return False, '[FAIL] Candidate input mutation differs from the reference', 'input_mutation'
                error = _output_error(ref_output, new_output, atol=atol, rtol=rtol)
                if error:
                    feedback = f"[FAIL] Output mismatch at trial {trial + 1}/{num_correct_trials}: {error}"
                    logger.error(feedback)
                    return False, feedback, 'precision_error'
                logger.info(f"Correctness: trial {trial + 1}/{num_correct_trials} passed")
        logger.info(f"Correctness: all {num_correct_trials} trials passed")
    except Exception as e:
        logger.error('[FAIL] runtime error when evaluating correctness')
        return False, f"[FAIL] {str(e)}", 'runtime_error'

    return True, '', None

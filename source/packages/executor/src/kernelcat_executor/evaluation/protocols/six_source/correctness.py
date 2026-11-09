import logging


from kernelcat_executor.evaluation.protocols.six_source.config import NUM_CORRECT_TRIALS, SEED


logger = logging.getLogger(__name__)


def set_seed(seed: int):
    import torch
    torch.manual_seed(seed)
    # NOTE: this only sets on current cuda device
    torch.cuda.manual_seed(seed)


def execute_template(synchronize, device, context, *, num_correct_trials=NUM_CORRECT_TRIALS, seed_num=SEED):
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
        init_inputs = [
            x.to(device=device) if isinstance(x, torch.Tensor) else x for x in init_inputs
        ]
        with torch.no_grad():
            set_seed(seed_num)  # set seed for reproducible weights
            original_model = Model(*init_inputs).to(device)
            synchronize(device=device)
            set_seed(seed_num)
            custom_model = ModelNew(*init_inputs).to(device)
            synchronize(device=device)
        logger.info(f"Correctness: begin {num_correct_trials} trials")
        with torch.no_grad():
            for trial in range(num_correct_trials):
                inputs = get_inputs()
                inputs = [
                    x.to(device) if isinstance(x, torch.Tensor) else x
                    for x in inputs
                ]
                synchronize(device=device)
                ref_output = original_model(*inputs)
                synchronize(device=device)
                new_output = custom_model(*inputs)
                synchronize(device=device) # ensure all GPU operations are completed before checking results
                if ref_output.shape != new_output.shape:
                    feedback = f"[FAIL] Output shape mismatch: Expected {ref_output.shape}, got {new_output.shape}"
                    logger.error(feedback)
                    return False, feedback, 'precision_error'
                if not torch.allclose(ref_output, new_output, atol=1e-04, rtol=1e-04):
                    abs_err = (ref_output - new_output).abs().max().item()
                    rel_err_denom = ref_output.abs().max().item() or 1.0
                    rel_err = abs_err / rel_err_denom
                    feedback = (f"[FAIL] Output mismatch at trial {trial + 1}/{num_correct_trials} "
                                f"(max_abs={abs_err:.3e}, max_rel={rel_err:.3e}, atol=rtol=1e-4)")
                    logger.error(feedback)
                    return False, feedback, 'precision_error'
                logger.info(f"Correctness: trial {trial + 1}/{num_correct_trials} passed")
        logger.info(f"Correctness: all {num_correct_trials} trials passed")
    except Exception as e:
        logger.error('[FAIL] runtime error when evaluating correctness')
        return False, f"[FAIL] {str(e)}", 'runtime_error'

    return True, '', None

import logging




logger = logging.getLogger(__name__)

# 每多少个 trial 打一行进度. 100 trials / 10 = 10 行, 既能看到推进又不刷屏
_PROGRESS_EVERY = 10


def time_execution_event_template(context, device, synchronize, event_class, eval_target, *,
                                  num_perf_trials, num_warmup):
    import torch
    get_inputs = context['get_inputs']
    get_init_inputs = context['get_init_inputs']
    generated_elapsed_times = []
    ModelNew = context[eval_target]
    inputs = get_inputs()
    inputs = [
        x.to(device) if isinstance(x, torch.Tensor) else x
        for x in inputs
    ]
    init_inputs = get_init_inputs()
    init_inputs = [
        x.to(device=device) if isinstance(x, torch.Tensor) else x for x in init_inputs
    ]
    with torch.no_grad(), torch.npu.device(device):
        custom_model = ModelNew(*init_inputs).to(device)
        stream = torch.npu.current_stream(device)

        def internel_eval(kernel_fn, elapsed_times):
            logger.info(f"Performance: {num_warmup} warmup + {num_perf_trials} timed trials")
            for _ in range(num_warmup):
                kernel_fn(*inputs)
                synchronize(device=device)
            logger.info("Performance: warmup done, begin timed trials")
            for trail in range(num_perf_trials):
                start_event = event_class(enable_timing=True)
                end_event = event_class(enable_timing=True)
                start_event.record(stream)
                kernel_fn(*inputs)
                end_event.record(stream)
                # Synchronize to ensure the events have completed
                synchronize(device=device)
                # Calculate the elapsed time in milliseconds
                elapsed_time_ms = start_event.elapsed_time(end_event)
                elapsed_times.append(elapsed_time_ms)
                # 进度日志: 每 _PROGRESS_EVERY 次打一行, 让 timeout 时能看到卡在哪
                if (trail + 1) % _PROGRESS_EVERY == 0 or trail + 1 == num_perf_trials:
                    logger.info(f"Performance: trial {trail + 1}/{num_perf_trials} done")
            logger.info(f"Performance: collected {len(elapsed_times)} samples")

        internel_eval(custom_model, generated_elapsed_times)
    return generated_elapsed_times

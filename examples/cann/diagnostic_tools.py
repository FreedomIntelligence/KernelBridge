"""Optional operator diagnostics exposed through the generic business-tool registry."""

def tools(argv, files):
    string = {'type': 'string', 'minLength': 1, 'maxLength': 512}
    reference = {'collection': string, 'path': string}
    specs = [
        ('ir_collect', 'Collect device LLVM IR for the current solution.py, retaining source and compiler identity.', {}, [], 10, 1800),
        ('profiler_collect', 'Collect a native device timeline and PipeUtilization metrics for the current solution.py.', {}, [], 10, 1800),
        ('diagnostics_parse', 'Parse retained LLVM or profiler CSV evidence into a bounded JSON summary.', reference, ['collection', 'path'], 1, 60),
        ('diagnostics_read', 'Read a bounded page of a retained diagnostic artifact. Omit path to list files.',
            {**reference, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 16384}}, ['collection'], 1, 60),
        ('diagnostics_render', 'Render two retained LLVM snapshots through the pinned PyPTO IR viewer.',
            {'before_collection': string, 'before_path': string, 'after_collection': string, 'after_path': string},
            ['before_collection', 'before_path', 'after_collection', 'after_path'], 1, 60),
    ]
    return [{'name': name, 'description': description, 'argv': argv, 'files': files,
             'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False},
             'credits': credits, 'timeout': timeout} for name, description, properties, required, credits, timeout in specs]

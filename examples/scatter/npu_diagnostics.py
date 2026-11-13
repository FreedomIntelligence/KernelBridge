"""Source-bound device IR collection; never execute a kernel or change its mode.

The preserved-mode bitcode recipe is a capability attempt, not a claim that all
CANN releases support it. Retained bitcode is preferred. Only successful,
source/target-checked decoding produces accepted IR; every failed command stays
in the receipt. Device qualification must use the actual pinned compiler.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import time

MAX_ARTIFACT = 64 * 1024 * 1024
DEVICE_TRIPLE = 'hiipu64-hisilicon-cce'


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class IRFileValidationError(ValueError):
    """A file rejection with bounded, original metadata; no compiler was implied."""
    def __init__(self, details, message):
        self.details = details
        observed = details.get('observed', {})
        size = f" (bytes={observed['bytes']})" if 'bytes' in observed else ''
        super().__init__(f"{message}: {details['role']} {details['path']}{size}")


def _regular(path, root, *, allow_empty=False, role='artifact'):
    path, root = Path(path), Path(root)
    within = path.is_absolute() and path.is_relative_to(root)
    details = {'schema_version': 'kernelbridge.ir-file-validation/v1',
               'phase': 'input_validation' if role in {'source', 'forced_include', 'source_input', 'candidate_wrapper', 'build_recipe'} else 'artifact_validation',
               'role': role, 'path': str(path.relative_to(root)) if within else str(path),
               'path_scope': 'bound_root_relative' if within else 'outside_bound_root',
               'expected': {'regular': True, 'no_links': True, 'within_root': True,
                            'min_bytes': 0 if allow_empty else 1, 'max_bytes': MAX_ARTIFACT}}
    def reject(code, message):
        details['code'] = code
        raise IRFileValidationError(details, message)
    if not within:
        reject('outside_root', 'IR file is outside its bound root')
    try:
        resolved = path.resolve(strict=True)
        if resolved != path:
            reject('linked_path', 'IR file must be non-linked; path contains a link or traversal')
        if not path.is_file():
            reject('not_regular', 'IR file is not a regular file')
        size = path.stat().st_size
    except RuntimeError as error:
        details['os_error'] = {'type': type(error).__name__}
        reject('linked_path', 'IR file must be non-linked; path contains a link loop')
    except OSError as error:
        details['os_error'] = {'type': type(error).__name__, 'errno': error.errno}
        reject('unreadable', 'IR file could not be inspected')
    details['observed'] = {'bytes': size}
    if size > MAX_ARTIFACT:
        reject('size_limit', 'IR file exceeds its size bound')
    if not allow_empty and size == 0:
        # Hash only after root, no-link, regular-file and size validation.
        details['observed']['sha256'] = _sha(path)
        reject('empty_file', 'IR file is empty')
    return path


def _identity(path, job, *, allow_empty=False, role='artifact'):
    path = _regular(path, job, allow_empty=allow_empty, role=role)
    return {'path': str(path.relative_to(job)), 'sha256': _sha(path), 'bytes': path.stat().st_size}


def _save(folder, records):
    temporary = folder / 'receipt.tmp'
    with temporary.open('w') as stream:
        json.dump(records, stream, indent=2, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(folder / 'receipt.json')


def _execute(argv, log, folder, records, record, operation):
    step = {'operation': operation, 'argv': argv, 'log': str(log.relative_to(folder.parent)),
            'started_ns': time.time_ns(), 'status': 'started'}
    record['steps'].append(step)
    _save(folder, records)
    try:
        compiler = Path(argv[0]).resolve(strict=True)
        if str(compiler) != record['compiler'] or _sha(compiler) != record['compiler_sha256']:
            raise OSError('Pinned IR compiler identity changed')
        with log.open('xb') as stream:
            process = subprocess.Popen(argv, cwd=log.parent, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            step['pid'] = process.pid; _save(folder, records)
            try:
                step['returncode'] = process.wait(timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
                step.update(returncode=process.returncode, error='IR compiler timeout; process group killed and reaped')
        step['status'] = 'completed' if step['returncode'] == 0 else 'failed'
    except OSError as error:
        step.update(status='failed', error=str(error))
    step['ended_ns'] = time.time_ns()
    if log.is_file(): step['log_sha256'] = _sha(log)
    _save(folder, records)
    return len(record['steps']) - 1


def _recipe(original, job, sdk, record=None):
    compiler = Path(original[0]).resolve(strict=True)
    if not compiler.is_relative_to(sdk): raise ValueError('Compiler is outside the pinned SDK')
    language = None
    for index, arg in enumerate(original):
        if arg == '-x' and index + 1 < len(original): language = original[index + 1]
        elif arg.startswith('-x') and arg != '-x': language = arg[2:]
    targets = [arg.split('=', 1)[1] for arg in original
               if arg.startswith(('--cce-aicore-arch=', '--npu-arch='))]
    if (language not in {'cce', 'asc'} or len(targets) != 1
            or '--cce-aicore-only' not in original or '-o' not in original
            or original.index('-o') + 1 == len(original)):
        raise ValueError('IR requires one explicit device-only cce/asc recipe and target')
    if any(arg.startswith('@') for arg in original):
        raise ValueError('Unexpanded response-file recipe is not qualified for IR replay')
    sources = [Path(arg) for arg in original if arg.endswith(('.cpp', '.cce')) and not arg.startswith('-')]
    if not sources: raise ValueError('IR recipe has no original source file')
    if record is not None:
        record.update(compiler=str(compiler), compiler_sha256=_sha(compiler), language=language,
                      target=targets[0], inputs=[], input_checks=[])
    inputs = []
    def retain_input(path, role):
        identity = _identity(path, job, allow_empty=True, role=role)
        if path not in inputs:
            inputs.append(path)
            if record is not None: record['inputs'].append(identity)
        if record is not None: record['input_checks'].append({'role': role, **identity})
    for path in sources: retain_input(path, 'source')
    for index, arg in enumerate(original):
        included = (original[index + 1] if arg == '-include' and index + 1 < len(original)
                    else arg[len('-include'):] if arg.startswith('-include') else None)
        if included:
            path = Path(included)
            if path.is_absolute() and path.is_relative_to(job):
                retain_input(path, 'forced_include')
    output = Path(original[original.index('-o') + 1])
    if not output.is_absolute() or not output.resolve().is_relative_to(job):
        raise ValueError('Original compiler output is outside the frozen job')
    return compiler, sources, list(dict.fromkeys(inputs)), output, language, targets[0]


def bitcode_command(original, output):
    """Keep -c, language, target, macros/includes and optimization settings.

    Only output/dependency destinations and save-temporary flags are removed.
    In particular, never turn a cce compilation into asc or append generic -S.
    """
    result, index = [], 0
    while index < len(original):
        arg = original[index]; index += 1
        if arg in {'-o', '-MF', '-MT', '-MQ'}:
            if index == len(original): raise ValueError('Missing compiler output/dependency argument')
            index += 1
        elif arg in {'-MD', '-MMD', '-save-temps', '--save-temp-files'} or arg.startswith('-save-temps='):
            continue
        else:
            result.append(arg)
    if '-c' not in result or '-S' in result or '-emit-llvm' in result:
        raise ValueError('IR emission requires the original object-compilation action')
    return [*result, '-emit-llvm', '-o', str(output)]


def _llvm_metadata(path, sources, target):
    text = path.read_text(encoding='utf-8')
    triple = re.search(r'^target triple = "([^"\n]+)"', text, re.M)
    source = re.search(r'^source_filename = "((?:[^"\\]|\\.)*)"', text, re.M)
    if not triple or triple[1] != DEVICE_TRIPLE or not source or not re.search(r'^define\s', text, re.M):
        raise ValueError('Decoded IR has no verified device triple, source or function definition')
    # LLVM quoted identifiers escape bytes, including each UTF-8 byte separately.
    filename = re.sub(rb'\\([0-9A-Fa-f]{2})', lambda match: bytes([int(match[1], 16)]),
                      source[1].encode('utf-8')).decode('utf-8')
    if filename not in {str(path) for path in sources}:
        raise ValueError('Decoded IR source_filename differs from the original recipe')
    cpus = set(re.findall(r'"target-cpu"="([^"\n]+)"', text))
    if cpus != {target}:
        raise ValueError('Decoded IR target-cpu differs from the original recipe')
    return {'triple': triple[1], 'source_filename': filename, 'target_cpu': target}


def _decode(bitcode, directory, folder, records, record, compiler, sources, compile_step=None, retained=None):
    job = folder.parent
    _regular(bitcode, job)
    with bitcode.open('rb') as stream: magic = stream.read(4)
    if magic not in (b'BC\xc0\xde', b'\xde\xc0\x17\x0b'):
        raise ValueError('Compiler output is not LLVM bitcode')
    destination = directory / 'kernel.ll'
    # cc1 is paired with this exact SDK/compiler. The explicit diagnostic makes
    # a host/other-target module an error, rather than silently rewriting it to
    # our expected device triple. Unsupported options remain a failed attempt.
    argv = [str(compiler), '-cc1', '-x', 'ir', '-emit-llvm', '-triple', DEVICE_TRIPLE,
            '-Werror=override-module', '-o', str(destination), str(bitcode)]
    step = _execute(argv, directory / 'decode.log', folder, records, record, 'decode_bitcode')
    if record['steps'][step].get('returncode') != 0:
        destination.unlink(missing_ok=True)
        return False
    try:
        log_text = (directory / 'decode.log').read_text(errors='replace')
        if re.search(r'overriding (?:the )?module target triple', log_text, re.I):
            raise ValueError('IR decoder attempted to rewrite the original module target')
        _regular(destination, job)
        metadata = _llvm_metadata(destination, sources, record['target'])
    except (OSError, UnicodeError, ValueError) as error:
        record['steps'][step]['validation_error'] = str(error)
        if isinstance(error, IRFileValidationError): record['steps'][step]['validation_failure'] = error.details
        destination.unlink(missing_ok=True)
        return False
    accepted = {'bitcode': _identity(bitcode, job), 'llvm': {**_identity(destination, job), **metadata},
                'decode_step': step, 'compile_step': compile_step}
    if retained is not None: accepted['retained_from'] = retained
    record['accepted'].append(accepted)
    return True


def _collect_recipe(original, recipe, index, job, sdk, folder, records):
    record = {'recipe': str(recipe.relative_to(job)), 'recipe_sha256': _sha(recipe),
              'original_argv': original, 'candidate_sha256': _sha(job / 'candidate.py'),
              'started_ns': time.time_ns(), 'status': 'unavailable', 'steps': [], 'accepted': []}
    records.append(record); _save(folder, records)
    directory = folder / f'kernel-{index:03d}'; directory.mkdir()
    try:
        compiler, sources, inputs, output, language, target = _recipe(original, job, sdk, record)
        # Only this recipe's local output/source trees and matching source stems.
        # A .bc elsewhere in the job is not evidence for this compilation unit.
        stems = {path.stem for path in sources} | {output.stem}
        candidates = set()
        for root in {recipe.parent, output.parent, *(path.parent for path in sources)}:
            if not root.is_relative_to(job) or not root.is_dir(): continue
            for path in root.glob('*.bc'):
                if any(path.stem == stem or path.stem.startswith(stem + '-') for stem in stems):
                    candidates.add(path)
        if len(candidates) > 128: raise ValueError('Too many retained bitcode candidates for one recipe')
        for number, path in enumerate(sorted(candidates)):
            retained = _identity(path, job)
            destination = directory / f'retained-{number:03d}'; destination.mkdir()
            copied = destination / 'kernel.bc'; shutil.copyfile(path, copied)
            if _sha(copied) != retained['sha256']: raise ValueError('Retained bitcode changed while copying')
            try:
                if _decode(copied, destination, folder, records, record, compiler, sources, retained=retained):
                    record['strategy'] = 'original_retained_bitcode'
                    break
            except (OSError, UnicodeError, ValueError) as error:
                record.setdefault('retained_errors', []).append({'path': retained['path'], 'error': str(error)})
        if not record['accepted']:
            destination = directory / 'emitted'; destination.mkdir()
            bitcode = destination / 'kernel.bc'
            argv = bitcode_command(original, bitcode)
            argv[0] = str(compiler)
            step = _execute(argv, destination / 'compile.log', folder, records, record, 'preserved_mode_bitcode')
            if record['steps'][step].get('returncode') == 0:
                _decode(bitcode, destination, folder, records, record, compiler, sources, compile_step=step)
            else:
                bitcode.unlink(missing_ok=True)
            record['strategy'] = 'preserved_mode_bitcode_capability_attempt'
        if record['accepted']:
            record['status'] = 'completed'
        else:
            record['reason'] = 'Pinned compiler produced no source- and target-validated IR in the original language mode'
    except (OSError, UnicodeError, ValueError) as error:
        record['reason'] = str(error)
        record['failure'] = {'phase': 'recipe_validation' if not record['steps'] else 'artifact_validation',
                             'exception_type': type(error).__name__,
                             **(error.details if isinstance(error, IRFileValidationError) else {})}
    record['compiler_process_started'] = any(step.get('pid') is not None for step in record['steps'])
    record['ended_ns'] = time.time_ns(); _save(folder, records)


def collect_ir(job):
    job = Path(job).resolve(strict=True)
    _regular(job / 'candidate.py', job, role='candidate_wrapper')
    folder = job / 'ir'; folder.mkdir(exist_ok=False)
    sdk = Path(os.environ['ASCEND_HOME_PATH']).resolve(strict=True)
    records, seen = [], set()
    for recipe in sorted((job / 'build').rglob('*.mk')):
        _regular(recipe, job, role='build_recipe')
        for line in recipe.read_text().splitlines():
            if not line.startswith('\t'): continue
            try: original = shlex.split(line.strip())
            except ValueError: continue
            if (not original or Path(original[0]).name not in {'bisheng', 'ccec'}
                    or '-E' in original or '-c' not in original or tuple(original) in seen):
                continue
            seen.add(tuple(original))
            _collect_recipe(original, recipe, len(records), job, sdk, folder, records)
    _save(folder, records)
    return records


def ir_failure_reason(records):
    """Summarize the first failed recipe, linking only recorded log artifacts."""
    failed = next((record for record in records if record.get('status') != 'completed'), None)
    if failed is None: return None
    phase = failed.get('failure', {}).get('phase')
    if phase is None:
        phase = 'compiler_or_artifact_validation' if failed.get('steps') else 'recipe_validation'
    reason = failed.get('reason', 'No source- and target-validated IR was produced')
    first_step_failure = next((step for step in failed.get('steps', [])
                               if step.get('status') == 'failed' or step.get('validation_error')), None)
    if first_step_failure is not None:
        detail = first_step_failure.get('validation_error') or first_step_failure.get('error')
        reason += '; ' + first_step_failure.get('operation', 'compiler step') + ': ' + (
            str(detail) if detail else 'returncode=' + str(first_step_failure.get('returncode')))
    message = f"IR recipe {failed.get('recipe', '<unknown>')} failed during {phase}: {reason}"
    message = message[:1536] + '; receipt: ir/receipt.json'
    logs = [str(step['log']) for step in failed.get('steps', [])
            if step.get('log') and step.get('log_sha256')]
    if logs: message += '; recorded compiler logs: ' + ', '.join(logs[:3])
    return message[:2048]


def validated_ir_artifacts(job, records):
    """Recheck the collector's accepted records; never promote suffix-only files."""
    job = Path(job).resolve(strict=True)
    result = {}
    for record in records:
        if record.get('status') != 'completed': continue
        try:
            if record['candidate_sha256'] != _sha(job / 'candidate.py'):
                raise ValueError('IR candidate identity changed')
            if _sha(Path(record['compiler'])) != record['compiler_sha256']:
                raise ValueError('IR compiler identity changed')
            recipe = _regular(job / record['recipe'], job)
            if _sha(recipe) != record['recipe_sha256']: raise ValueError('IR recipe changed')
            for item in record['inputs']:
                if _identity(job / item['path'], job, allow_empty=True, role='source_input') != item:
                    raise ValueError('IR source input changed')
            sources = [job / item['path'] for item in record['inputs'] if item['path'].endswith(('.cpp', '.cce'))]
            for accepted in record['accepted']:
                indices = [accepted['decode_step']]
                if accepted['compile_step'] is not None: indices.append(accepted['compile_step'])
                if any(record['steps'][index].get('returncode') != 0 for index in indices):
                    raise ValueError('IR output belongs to a failed command')
                if accepted['compile_step'] is None:
                    retained = accepted['retained_from']
                    if _identity(job / retained['path'], job) != retained: raise ValueError('Original retained bitcode changed')
                for kind in ('bitcode', 'llvm'):
                    item = accepted[kind]; path = _regular(job / item['path'], job / 'ir')
                    if _identity(path, job) != {key: item[key] for key in ('path', 'sha256', 'bytes')}:
                        raise ValueError('IR output identity changed')
                    if kind == 'llvm' and _llvm_metadata(path, sources, record['target']) != {key: item[key] for key in ('triple', 'source_filename', 'target_cpu')}:
                        raise ValueError('IR target/source changed')
                    result[item['path']] = item['sha256']
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, IndexError) as error:
            # A failed recheck must not quietly turn an incomplete collection
            # into a successful subset that satisfies required diagnostics.
            raise ValueError('Accepted IR manifest failed revalidation: ' + str(error)) from error
    return result

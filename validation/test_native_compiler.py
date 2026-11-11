"""Host-side native compiler contracts, independent of NPU provisioning."""
import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'source/packages/executor/src'))
from kernelcat_executor.backends.ascend import compiler
from kernelcat_executor.build.inputs import BuildSettings
from kernelcat_executor.build.outputs import BuildOutputs

class CompilerContracts(unittest.TestCase):
    def test_tiling_location_and_explicit_job_install(self):
        for directory in ('op_host','op_kernel'):
            with self.subTest(directory=directory),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'CppExtension/csrc').mkdir(parents=True)
                project=root/'AuditAddCustom';calls=[]
                def run(argv,cwd,**kwargs):
                    calls.append(argv)
                    if argv[0]=='msopgen':
                        for part in ('op_host','op_kernel','build_out'):(project/part).mkdir(parents=True,exist_ok=True)
                        (project/directory/'audit_add_custom_tiling.h').write_text('generated')
                        (project/'build_out/custom_opp_fixture.run').touch()
                sources={name:'candidate field '+name for name in compiler._FIELDS}
                outputs=BuildOutputs(root,'a'*64,'ai_core-Ascend910_9382',())
                with patch.object(compiler,'_run',side_effect=run),patch.object(compiler,'capture_outputs',return_value=outputs):
                    compiler.compile_sources(sources,BuildSettings('audit_add',root,'ai_core-Ascend910_9382'))
                self.assertEqual((project/directory/'audit_add_custom_tiling.h').read_text(),sources['host_tiling_src'])
                installers=[a for a in calls if a[0].endswith('.run')]
                self.assertEqual(len(installers),1)
                self.assertIn('--install-path='+str(root/'opp'),installers[0])
                self.assertFalse((project/('op_kernel' if directory=='op_host' else 'op_host')/'audit_add_custom_tiling.h').exists())

    def test_kernel_include_api_matches_generated_cmake(self):
        for marker,expected in [('add_kernels_compile()','add_ops_compile_options'),('npu_op_kernel_library(ascendc_kernels)','npu_op_kernel_options')]:
            with self.subTest(marker=marker),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'op_kernel').mkdir();cmake=root/'op_kernel/CMakeLists.txt';cmake.write_text(marker)
                compiler._inject_kernel_include_paths(root,('/tmp/headers with spaces',))
                text=cmake.read_text();self.assertIn(expected,text);self.assertLess(text.index(expected),text.index(marker))
                compiler._inject_kernel_include_paths(root,('/tmp/headers with spaces',));self.assertEqual(cmake.read_text(),text)

    def test_bundle_launcher_is_executable(self):
        self.assertTrue((ROOT/'kernelagent').stat().st_mode&0o111)

    def test_build_diagnostics_survive_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with self.assertRaisesRegex(RuntimeError, 'native failure marker'):
                compiler._run([sys.executable, '-c',
                    "import sys; print('full stdout'); print('native failure marker',file=sys.stderr); sys.exit(7)"], root)
            logs=list(root.glob('.kernelbridge-build-*.log'))
            self.assertEqual(len(logs), 1)
            text=logs[0].read_text()
            self.assertIn('full stdout',text)
            self.assertIn('native failure marker',text)
            self.assertEqual(json.loads(text.splitlines()[0])['argv'][0],sys.executable)


class CompilerFeedback(unittest.TestCase):
    def test_primary_cpp_error_survives_make_cascade(self):
        from kernelcat_executor.backends.ascend.compiler import diagnostic_lines
        text = "kernel.cpp:12:3: error: no matching function for call to Cast\n" + "gmake: *** failed with Error 2\n" * 30
        self.assertEqual(diagnostic_lines(text), ["kernel.cpp:12:3: error: no matching function for call to Cast"])

    def test_cpp_stack_frame_does_not_hide_runtime_cause_or_wrapped_member(self):
        from kernelcat_executor.backends.ascend.compiler import diagnostic_lines
        runtime = 'call aclnn failed: kernel entry 0 is invalid\nframe #0: c10::Error::Error(...)\n'
        self.assertIn('kernel entry 0', diagnostic_lines(runtime)[0])
        wrapped = "error: class TilingContext has no member named\n  'SetTilingDataSize'; did you mean GetRawTilingData?\n"
        self.assertIn('SetTilingDataSize', diagnostic_lines(wrapped)[0])

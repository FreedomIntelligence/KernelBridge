"""Local compiler-interface qualification only; no operator algorithm or NPU."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'source/packages/executor/src'))
from kernelcat_executor.backends.ascend import compiler
from kernelcat_executor.build.inputs import BuildSettings

CC = shutil.which('clang++') or shutil.which('g++')
_TEMP = tempfile.TemporaryDirectory(prefix='kb-native-includes-')
EVIDENCE = Path(_TEMP.name)
NAME = 'audit_add_custom'
HEADER = '#pragma once\nstruct FixtureTiling { int value; };\n'
SOURCES = {
    'project_json_src': '[{"op":"AuditAddCustom"}]',
    'host_tiling_src': HEADER,
    'host_operator_src': '#include "profile_sdk.h"\n#include "audit_add_custom_tiling.h"\nstatic_assert(sizeof(FixtureTiling)==sizeof(int), "layout");\nint fixture_host() { return 0; }\n',
    'kernel_src': '#include "audit_add_custom_tiling.h"\nstatic_assert(sizeof(FixtureTiling)==sizeof(int), "layout");\nint fixture_kernel() { return 0; }\n',
    'python_bind_src': '#include "Python.h"\nstatic_assert(FIXTURE_PYTHON_HEADER == 7, "profile Python headers");\nextern "C" int fixture_extension() { return 0; }\n',
}
# An SDK-shaped subprocess that does not read CMake flags or add project -I.
# Both -E and -c are real compiler invocations; the kernel source is relocated.
SDK = '''import hashlib,json,os,subprocess,sys
from pathlib import Path
project=Path(sys.argv[1]); cc=os.environ['KB_FIXTURE_CXX']; root=project/'build_out'; root.mkdir(exist_ok=True)
for phase,source in [('host_codegen',project/'op_host/audit_add_custom.cpp'),('host_target',project/'op_host/audit_add_custom.cpp'),('device_recipe_shape',project/'op_kernel/audit_add_custom.cpp')]:
 if phase=='device_recipe_shape':
  copied=root/'copied_kernel';copied.mkdir();target=copied/source.name;target.write_bytes(source.read_bytes());source=target
 argv=[cc,'-std=c++17','-c',str(source),'-o',str(root/(phase+'.o'))]
 (root/(phase+'.json')).write_text(json.dumps({'argv':argv,'cwd':str(root),'CPATH_sha256':hashlib.sha256(os.environ.get('CPATH','').encode()).hexdigest()},indent=2))
 subprocess.run([sys.executable,os.environ['KB_FIXTURE_LEAF'],*argv],cwd=root,check=True)
 preprocess=[cc,'-std=c++17','-E',str(source),'-o',str(root/(phase+'.i'))]
 subprocess.run([sys.executable,os.environ['KB_FIXTURE_LEAF'],*preprocess],cwd=root,check=True)
'''
LEAF = 'import subprocess,sys\nsubprocess.run(sys.argv[1:],check=True)\n'
GENERATOR = '''import os,sys
from pathlib import Path
project=Path(sys.argv[sys.argv.index('-out')+1]);project.mkdir()
for name in ['op_host','op_kernel','build_out']:(project/name).mkdir()
layout=os.environ['KB_FIXTURE_LAYOUT']
for name in (['op_host','op_kernel'] if layout=='both' else [] if layout=='none' else [layout]):
 (project/name/'audit_add_custom_tiling.h').write_text('generated placeholder')
marker='npu_op_kernel_library(ascendc_kernels)' if os.environ['KB_FIXTURE_CMAKE']=='modern' else 'add_kernels_compile()'
(project/'op_kernel/CMakeLists.txt').write_text(marker+'\\n')
build=project/'build.sh';build.write_text('#!'+sys.executable+'\\nimport os,subprocess,sys\\nsubprocess.run([sys.executable,os.environ["KB_FIXTURE_SDK"],os.getcwd()],check=True)\\n');build.chmod(0o755)
installer=project/'build_out/custom_opp_fixture.run'
installer.write_text('#!'+sys.executable+'\\nfrom pathlib import Path\\nimport sys\\np=Path(next(x.split("=",1)[1] for x in sys.argv if x.startswith("--install-path=")))/"vendors/customize"\\np.mkdir(parents=True)\\n(p/"fixture-package.txt").write_text("local compiler fixture, not an NPU operator")\\n');installer.chmod(0o755)
'''
SETUP = '''import os,subprocess
subprocess.run([os.environ['KB_FIXTURE_CXX'],'-std=c++17','-shared','-fPIC','csrc/op.cpp','-o','fixture.so'],check=True)
'''


def fixture(layout, cmake='modern'):
    root = Path(tempfile.mkdtemp(prefix=layout+' with spaces ', dir=EVIDENCE))
    (root/'CppExtension/csrc').mkdir(parents=True)
    (root/'CppExtension/setup.py').write_text(SETUP)
    (root/'bin').mkdir(); (root/'tmp').mkdir(); (root/'ambient').mkdir()
    (root/'ambient/profile_sdk.h').write_text('#pragma once\nstruct FixtureSdkType {};\n')
    (root/'ambient/Python.h').write_text('#define FIXTURE_PYTHON_HEADER 7\n')
    (root/'ambient/audit_add_custom_tiling.h').write_text('#error ambient tiling must never override job-local tiling\n')
    for path,content in [('bin/msopgen',GENERATOR),('sdk.py',SDK),('leaf.py',LEAF)]:
        p=root/path;p.write_text('#!'+sys.executable+'\n'+content);p.chmod(0o755)
    env={'PATH':str(root/'bin')+os.pathsep+os.environ['PATH'],
         'KB_FIXTURE_LAYOUT':layout,'KB_FIXTURE_CMAKE':cmake,'KB_FIXTURE_CXX':CC,
         'KB_FIXTURE_SDK':str(root/'sdk.py'),'KB_FIXTURE_LEAF':str(root/'leaf.py'),
         'CPATH':os.pathsep+str(root/'ambient')+os.pathsep+os.pathsep,'ASCEND_CUSTOM_OPP_PATH':'original-opp-must-not-change',
         'KB_FIXTURE_SECRET':'PRIVATE_ENV_SENTINEL_NOT_FOR_LOGS','TMPDIR':str(root/'tmp'),
         'PYTHONDONTWRITEBYTECODE':'1'}
    return root,env


@unittest.skipUnless(CC,'A local C++ compiler is required')
class SharedIncludes(unittest.TestCase):
    def test_two_layouts_preprocess_compile_nested_sdk_and_relocated_kernel(self):
        for layout in ('op_host','op_kernel'):
            for cmake in ('modern','legacy'):
                with self.subTest(layout=layout,cmake=cmake):
                    root,env=fixture(layout,cmake);project=root/'AuditAddCustom'
                    original=json.dumps(SOURCES,sort_keys=True).encode()
                    with patch.dict(os.environ,env):
                        before=dict(os.environ)
                        outputs=compiler.compile_sources(SOURCES,BuildSettings('audit_add',root,'ai_core-Ascend910_9382'))
                        self.assertEqual(dict(os.environ),before)
                    self.assertEqual(json.dumps(SOURCES,sort_keys=True).encode(),original)
                    # Generated roots are canonical; inherited CPATH preserves
                    # its spelling (e.g. macOS /var aliases /private/var).
                    roots=[str(path.resolve()) for path in (project,project/'op_host',project/'op_kernel')]
                    headers=list(project.glob('op_*/*_tiling.h'))
                    self.assertEqual(len(headers),1)
                    self.assertEqual(headers[0].read_bytes(),HEADER.encode())
                    self.assertEqual((project/'op_host'/f'{NAME}.cpp').read_bytes(),SOURCES['host_operator_src'].encode())
                    self.assertEqual((project/'op_kernel'/f'{NAME}.cpp').read_bytes(),SOURCES['kernel_src'].encode())
                    for phase in ('host_codegen','host_target','device_recipe_shape'):
                        record=json.loads((project/'build_out'/f'{phase}.json').read_text())
                        self.assertEqual(record['CPATH_sha256'],hashlib.sha256(os.pathsep.join([*roots,str(root/'ambient')]).encode()).hexdigest())
                        self.assertFalse(any(arg.startswith('-I') for arg in record['argv']))
                        self.assertGreater((project/'build_out'/f'{phase}.o').stat().st_size,0)
                        self.assertIn('struct FixtureTiling',(project/'build_out'/f'{phase}.i').read_text())
                    cmake_text=(project/'op_kernel/CMakeLists.txt').read_text()
                    for item in roots:self.assertIn('"-I'+item+'"',cmake_text)
                    self.assertTrue(outputs.files)
                    logs=list(root.rglob('.kernelbridge-build-*.log'))
                    self.assertEqual(len(logs),4)
                    self.assertEqual(sum(bool(json.loads(p.read_text().splitlines()[0]).get('include_roots')) for p in logs),3)
                    for path in logs:
                        text=path.read_text();self.assertNotIn(env['KB_FIXTURE_SECRET'],text)
                        self.assertNotIn(env['ASCEND_CUSTOM_OPP_PATH'],text)
                        record=json.loads(text.splitlines()[0])
                        self.assertNotIn('environment',record)
                        if record['include_roots']:
                            self.assertEqual(record['include_roots'],roots)
                            self.assertEqual(record['inherited_include_count'],1)
                            self.assertEqual(record['inherited_include_sha256'],hashlib.sha256(json.dumps([str(root/'ambient')],separators=(',',':')).encode()).hexdigest())
                            self.assertNotIn(str(root/'ambient'),json.dumps(record))
                    (root/'source-identities.json').write_text(json.dumps({
                        'fields':{key:hashlib.sha256(value.encode()).hexdigest() for key,value in SOURCES.items()},
                        'layout':layout,'cmake':cmake,'compiler':CC,'compiler_sha256':hashlib.sha256(Path(CC).read_bytes()).hexdigest(),
                        'scope':'real local C++ preprocessing/object/shared-library compilation; fixture generator/installer, no CANN or NPU'},indent=2)+'\n')

    def test_missing_and_ambiguous_layout_still_fail_before_build(self):
        for layout in ('none','both'):
            with self.subTest(layout=layout):
                root,env=fixture(layout)
                with patch.dict(os.environ,env),self.assertRaisesRegex(ValueError,'expected one generated tiling header'):
                    compiler.compile_sources(SOURCES,BuildSettings('audit_add',root,'ai_core-Ascend910_9382'))
                self.assertFalse(list(root.rglob('*.o')))
                self.assertFalse((root/'opp').exists())

    def test_unrelated_cpp_failure_is_not_swallowed_and_logs_include_contract(self):
        root,env=fixture('op_kernel')
        invalid={**SOURCES,'host_operator_src':'#include "not_a_generated_header.h"\n'}
        with patch.dict(os.environ,env),self.assertRaisesRegex(RuntimeError,'not_a_generated_header.h'):
            compiler.compile_sources(invalid,BuildSettings('audit_add',root,'ai_core-Ascend910_9382'))
        self.assertFalse((root/'opp').exists())
        log=next((root/'AuditAddCustom').glob('.kernelbridge-build-*.log'))
        record=json.loads(log.read_text().splitlines()[0])
        self.assertEqual(record['include_contract'],'kernelbridge.generated-includes/v1')
        self.assertEqual(len(record['include_roots']),3)

    def test_unencodable_root_rejected_instead_of_creating_extra_search_paths(self):
        root=Path(tempfile.mkdtemp(prefix='bad:root',dir=EVIDENCE))
        (root/'op_host').mkdir();(root/'op_kernel').mkdir()
        with self.assertRaisesRegex(ValueError,'unambiguously'):compiler._project_include_roots(root)


if __name__=='__main__':unittest.main(verbosity=2)

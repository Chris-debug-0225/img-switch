import importlib.util
from pathlib import Path
import unittest
import tempfile
import zipfile
import subprocess
import sys
import ctypes

spec = importlib.util.spec_from_file_location('sanitize', Path(__file__).with_name('sanitize.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class SanitizeTests(unittest.TestCase):
    def test_only_cpython_documentation_example_is_removed_without_size_change(self):
        line = b"        splitroot('C:/Users/test-user') == ('C:', '/', 'Users/test-user')\n"
        data = b'before\x00\n' + line + b'\x00after'
        output, count = module.remove_example(data)
        self.assertEqual(count, 1)
        self.assertEqual(len(output), len(data))
        self.assertTrue(output.startswith(b'before\x00'))
        self.assertTrue(output.endswith(b'\x00after'))
        self.assertNotIn(b'/Users/', output)

    def test_unrelated_path_is_not_rewritten(self):
        data = b'/Users/test-user/private'
        self.assertEqual(module.remove_example(data), (data, 0))

    def test_standard_library_zip_is_copied_and_scrubbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / 'base_library.zip'
            line = b"        splitroot('C:/Users/test-user') == ('C:', '/', 'Users/test-user')\n"
            with zipfile.ZipFile(original, 'w') as z:
                z.writestr('ntpath.pyc', b'header\n' + line)
                z.writestr('other', b'unchanged')
            before = original.read_bytes()
            result = module.scrub_datas([('base_library.zip', str(original), 'DATA')], root / 'copy')
            self.assertEqual(original.read_bytes(), before)
            with zipfile.ZipFile(result[0][1]) as z:
                self.assertNotIn(b'/Users/', z.read('ntpath.pyc'))
                self.assertEqual(z.read('other'), b'unchanged')

    def test_unknown_spec_structure_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / 'app.spec'
            spec.write_text('unexpected', encoding='utf-8')
            with self.assertRaises(ValueError):
                module.patch_spec(spec, Path(tmp) / 'runtime')

    def test_framework_python_binary_is_copied_and_scrubbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / 'Python'
            line = b"        splitroot('C:/Users/test-user') == ('C:', '/', 'Users/test-user')\n"
            original.write_bytes(b'header\n' + line)
            before = original.read_bytes()
            result = module.scrub_binaries([('Python', str(original), 'BINARY')], root / 'copy')
            self.assertEqual(original.read_bytes(), before)
            self.assertFalse(b'/Users/' in Path(result[0][1]).read_bytes())

    @unittest.skipUnless(sys.platform == 'darwin', 'requires macOS clang and Mach-O')
    def test_strips_only_extension_modules_not_nested_engine(self):
        with tempfile.TemporaryDirectory(prefix='sanitize-debug-') as tmp:
            root = Path(tmp)
            source, binary, engine = root / 'module.c', root / 'module.so', root / 'img-switch-engine'
            source.write_text('int sample(void) { return 42; }\n', encoding='utf-8')
            subprocess.run(['cc', '-dynamiclib', '-g', '-fdebug-prefix-map=' + tmp + '=/Users/example-user/work', str(source), '-o', str(binary)], check=True, capture_output=True)
            engine.write_bytes(b'engine-archive')
            before = binary.read_bytes()
            self.assertIn(b'/Users/', before)
            result = module.scrub_binaries([('lib-dynload/module.so', str(binary), 'BINARY'),
                                           ('engine/bin/img-switch-engine', str(engine), 'BINARY')], root / 'copy')
            self.assertEqual(binary.read_bytes(), before)
            self.assertFalse(b'/Users/' in Path(result[0][1]).read_bytes())
            self.assertEqual(result[1][1], str(engine))

    @unittest.skipUnless(sys.platform == 'darwin', 'requires macOS clang and Mach-O')
    def test_scrubs_cpython_source_file_diagnostic_without_changing_loadability(self):
        with tempfile.TemporaryDirectory(prefix='sanitize-file-') as tmp:
            root = Path(tmp)
            source, binary = root / 'sample.c', root / 'sample.so'
            source.write_text('const char *sample(void) { return __FILE__; }\n', encoding='utf-8')
            prefix = '/Users/example-user/build/v3.12.10/Modules'
            subprocess.run(['cc', '-dynamiclib', '-ffile-prefix-map=' + tmp + '=' + prefix,
                            str(source), '-o', str(binary)], check=True, capture_output=True)
            before = binary.read_bytes()
            self.assertTrue(b'/Users/' in before)
            before_load_commands = subprocess.check_output(['otool', '-L', str(binary)], text=True).splitlines()[1:]
            result = module.scrub_binaries([('lib-dynload/sample.so', str(binary), 'BINARY')], root / 'copy')
            output = Path(result[0][1])
            self.assertEqual(binary.read_bytes(), before)
            self.assertFalse(b'/Users/' in output.read_bytes())
            self.assertEqual(before_load_commands, subprocess.check_output(['otool', '-L', str(output)], text=True).splitlines()[1:])
            subprocess.run(['codesign', '-s', '-', '-f', str(output)], check=True, capture_output=True)
            lib = ctypes.CDLL(str(output))
            lib.sample.restype = ctypes.c_char_p
            self.assertTrue(lib.sample().endswith(b'Modules/sample.c'))

if __name__ == '__main__':
    unittest.main()

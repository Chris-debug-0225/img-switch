import importlib.util
from pathlib import Path
import unittest
import tempfile
import zipfile

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

if __name__ == '__main__':
    unittest.main()

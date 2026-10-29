import importlib.util
from pathlib import Path
import tempfile
import unittest
import pycdlib

spec = importlib.util.spec_from_file_location('make_dmg', Path(__file__).with_name('make_dmg.py'))
module = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(module)
except FileNotFoundError:
    module = None

class RockRidgeTests(unittest.TestCase):
    def test_iso_preserves_symlinks_and_executable_mode(self):
        self.assertIsNotNone(module, 'disk image builder missing')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'input'
            root.mkdir()
            exe = root / 'program'
            exe.write_bytes(b'example')
            exe.chmod(0o755)
            (root / 'link').symlink_to('program')
            output = Path(temp) / 'image.iso'
            module.make_iso(root, output)
            iso = pycdlib.PyCdlib()
            iso.open(str(output))
            try:
                self.assertEqual(iso.get_record(rr_path='/link').rock_ridge.symlink_path(), b'program')
                self.assertEqual(iso.get_record(rr_path='/program').rock_ridge.get_file_mode() & 0o777, 0o755)
            finally:
                iso.close()

    def test_raw_image_rejects_provenance_name_and_marker(self):
        self.assertIsNotNone(module, 'disk image builder missing')
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / 'test.iso'
            image.write_bytes(b'hello com.apple.provenance hidden')
            with self.assertRaises(ValueError):
                module.check_raw_image(image, [])
            image.write_bytes(b'opaque-marker')
            with self.assertRaises(ValueError):
                module.check_raw_image(image, [b'opaque-marker'])

if __name__ == '__main__':
    unittest.main()

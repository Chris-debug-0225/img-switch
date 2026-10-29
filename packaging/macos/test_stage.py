import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('stage', Path(__file__).with_name('stage.py'))
stage = importlib.util.module_from_spec(spec) if spec else None
if spec and spec.loader:
    try:
        spec.loader.exec_module(stage)
    except FileNotFoundError:
        stage = None

class StageTests(unittest.TestCase):
    def test_only_required_files_copied(self):
        self.assertIsNotNone(stage, 'staging module is missing')
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            root = Path(src)
            for rel in stage.REQUIRED:
                file = root / rel
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('required')
            (root / 'engine/config.json').write_text('secret')
            (root / 'engine/extra.txt').write_text('not allowed')
            stage.stage(root, Path(dst))
            result = {str(p.relative_to(dst)) for p in Path(dst).rglob('*') if p.is_file()}
            self.assertEqual(result, set(stage.REQUIRED))

if __name__ == '__main__':
    unittest.main()

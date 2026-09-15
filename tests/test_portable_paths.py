import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("repair", ROOT / "scripts/repair-portable-paths.py")
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class PortablePathsTests(unittest.TestCase):
    def test_staging_uv_alias_is_repaired_before_environment_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "config").mkdir()
            (root / "config/newton-local.yaml").write_text("{}")
            base = root / "tools/uv-python"
            exe = base / "cpython-3.11.15-test/bin/python3.11"
            exe.parent.mkdir(parents=True)
            exe.write_text("fixture interpreter")
            alias = base / "cpython-3.11-test"
            alias.symlink_to("/Users/staging/Local_Fable/tools/uv-python/cpython-3.11.15-test")
            env = root / "services/.venv"
            (env / "bin").mkdir(parents=True)
            (env / "pyvenv.cfg").write_text("home = /old\nversion_info = 3.11.15\n")
            changes, missing = repair.repair(root)
            self.assertFalse(missing)
            self.assertEqual(alias.resolve(), exe.parent.parent.resolve())
            self.assertEqual((env / "bin/python3").resolve(), exe.resolve())
            self.assertEqual(repair.repair(root, True), ([], []))

    def test_relocated_entrypoint_uses_bundled_interpreter_and_relative_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "First Location"
            (root / "config").mkdir(parents=True)
            (root / "config/newton-local.yaml").write_text("{}")
            version = f"{sys.version_info.major}.{sys.version_info.minor}"
            interpreter = root / f"tools/uv-python/cpython-{version}-test/bin/python{version}"
            interpreter.parent.mkdir(parents=True)
            interpreter.symlink_to(sys.executable)
            env = root / "hermes/runtime/hermes-agent/venv"
            (env / "bin").mkdir(parents=True)
            (env / "pyvenv.cfg").write_text("home = /Users/staging/Local_Fable/old\nversion_info = " + version + ".0\ninclude-system-site-packages = true\n")
            entry = env / "bin/hermes"
            entry.write_text("#!/Users/staging/Local_Fable/venv/bin/python\nprint('portable-entry-ok')\n")
            entry.chmod(0o755)
            (env / "bin/activate.bat").write_bytes(b"@REM generated launcher\r\n@echo off\r\n")
            repair.repair(root)
            self.assertEqual(repair.repair(root, True), ([], []))
            self.assertEqual(subprocess.check_output([str(entry)], text=True).strip(), "portable-entry-ok")
            moved = root.with_name("Second Location")
            root.rename(moved)
            repair.repair(moved)
            self.assertEqual(subprocess.check_output([str(moved / entry.relative_to(root))], text=True).strip(), "portable-entry-ok")
            self.assertFalse((moved / entry.relative_to(root)).read_text().startswith("#!/Users/"))


if __name__ == "__main__":
    unittest.main()

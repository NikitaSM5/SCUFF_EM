"""Runtime selection must not import Qt into the wrong interpreter."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gui import bootstrap


class BootstrapTests(unittest.TestCase):
    def test_python311_stays_in_process(self):
        with patch.object(sys, "version_info", (3, 11, 9)), \
                patch.object(bootstrap.subprocess, "call") as call:
            bootstrap.ensure_gui_python()
        call.assert_not_called()

    def test_relaunch_preserves_arguments_exit_code_and_parent_environment(self):
        with patch.object(bootstrap.os, "name", "nt"), \
                patch.object(sys, "version_info", (3, 14, 0)), \
                patch.object(sys, "argv", ["gui/app.py", "results/with spaces/comparison.json"]), \
                patch.dict(os.environ, {"PYTHONHOME": "another-python"}, clear=True), \
                patch.object(bootstrap.subprocess, "call", return_value=7) as call:
            with self.assertRaises(SystemExit) as stopped:
                bootstrap.ensure_gui_python()
            self.assertEqual(stopped.exception.code, 7)
            command = call.call_args.args[0]
            self.assertEqual(command[:2], ["py", "-3.11"])
            self.assertEqual(Path(command[2]).name, "app.py")
            self.assertEqual(command[3:], sys.argv[1:])
            env = call.call_args.kwargs["env"]
            self.assertEqual(env[bootstrap.RELAUNCH_MARKER], "1")
            self.assertNotIn("PYTHONHOME", env)
            self.assertEqual(os.environ["PYTHONHOME"], "another-python")

    def test_bad_launcher_cannot_loop(self):
        with patch.object(bootstrap.os, "name", "nt"), \
                patch.object(sys, "version_info", (3, 14, 0)), \
                patch.dict(os.environ, {bootstrap.RELAUNCH_MARKER: "1"}), \
                patch.object(bootstrap.subprocess, "call") as call:
            with self.assertRaisesRegex(SystemExit, "selected another version"):
                bootstrap.ensure_gui_python()
        call.assert_not_called()

    def test_missing_launcher_reports_setup_command(self):
        with patch.object(bootstrap.os, "name", "nt"), \
                patch.object(sys, "version_info", (3, 14, 0)), \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(bootstrap.subprocess, "call", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(SystemExit, "pip install -r gui/requirements.txt"):
                bootstrap.ensure_gui_python()


if __name__ == "__main__":
    unittest.main()

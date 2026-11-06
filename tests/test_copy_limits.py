"""Independent local copies: complete bytes, explicit bounds and no model call."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import copy_contract, task_contract, task_child, task_sandbox, task_cgroup


class LocalCopyLimitTests(unittest.TestCase):
    def test_large_many_file_copy_uses_its_own_contract(self):
        large = "\ufeff" + "line\\with\ttabs\r\n" * 140000
        files = {"large.txt": large, **{f"part{n}.txt": "piece\n" for n in range(2000)}}
        with patch.object(task_contract, "MAX_FILES", 1), patch.object(task_contract, "MAX_TOTAL", 1):
            self.assertEqual(copy_contract.validate_files(files),
                             len(large.encode("utf-8")) + 2000 * len("piece\n"))
        with patch.object(task_sandbox.os, "geteuid", return_value=1000), \
                patch.object(task_sandbox.os, "getuid", return_value=1000), \
                patch.object(task_sandbox, "command", return_value=["synthetic"]), \
                patch.object(task_sandbox, "bounded_process", return_value=(0, b"checked", b"")) as child:
            result = task_sandbox.run_copy(files, "text")
        self.assertTrue(result["passed"])
        arguments = child.call_args.kwargs
        self.assertIs(arguments["copy_input"], True)
        self.assertGreater(len(arguments["input_bytes"]), 2 * 1024 * 1024)
        decoded = json.loads(arguments["input_bytes"])
        self.assertEqual({item["path"]: item["content"] for item in decoded["files"]}, files)
        self.assertEqual(decoded["check"], "text")
        self.assertEqual(arguments["limit"], 32768)
        with tempfile.TemporaryDirectory() as directory:
            task_child.materialize(decoded, directory)
            self.assertEqual(len(list(Path(directory).iterdir())), len(files))
            self.assertEqual((Path(directory) / "large.txt").read_bytes(), large.encode("utf-8"))

    def test_copy_path_can_be_deep_without_widening_edit_target_authority(self):
        name = "/".join(["package"] * 15 + ["test.py"])
        self.assertEqual(copy_contract.path(name), name)
        with self.assertRaises(ValueError):
            task_contract.path(name)
        for invalid in ("/etc/config", "../escape", "dir/../escape", "dir//file", "file/",
                        "/".join(["a"] * 17), "a" * 256, ".ai-desktop-tasks/secret", "x/.ai-task-private"):
            with self.subTest(path=invalid), self.assertRaises(ValueError):
                copy_contract.path(invalid)

    def test_overruns_and_invalid_text_refuse_before_the_runner(self):
        with patch.object(copy_contract, "MAX_FILES", 2), patch.object(copy_contract, "MAX_FILE", 4), \
                patch.object(copy_contract, "MAX_TOTAL", 6), \
                patch.object(task_sandbox.os, "geteuid", return_value=1000), \
                patch.object(task_sandbox.os, "getuid", return_value=1000), \
                patch.object(task_sandbox, "bounded_process") as child:
            for files in ({}, {"a": "", "b": "", "c": ""}, {"a": "12345"},
                          {"a": "1234", "b": "123"}, {"a": "\x00"}, {"a": "\r"}, {"../a": "x"}):
                with self.subTest(files=files), self.assertRaises(ValueError):
                    task_sandbox.run_copy(files, "text")
            child.assert_not_called()

    def test_edit_format_expansion_cannot_pass_a_test_which_undo_would_refuse(self):
        source = {"instruction": "change", "files": [
            {"path": name, "content": "a\n", "writable": True} for name in ("a", "b")]}
        replacements = [{"path": name, "content": "a\nb\n"} for name in ("a", "b")]
        originals = {name: {"content": "a\r\n"} for name in ("a", "b")}
        with patch.object(task_contract, "MAX_TOTAL", 10), \
                patch.object(task_sandbox.os, "geteuid", return_value=1000), \
                patch.object(task_sandbox.os, "getuid", return_value=1000), \
                patch.object(task_sandbox, "run_copy") as run:
            with self.assertRaisesRegex(ValueError, "formatted total"):
                task_sandbox.run(source, replacements, "text", originals=originals)
            run.assert_not_called()

    def test_bootstrap_and_resource_limits_cover_the_advertised_copy(self):
        self.assertEqual(task_child.MAX_PAYLOAD, copy_contract.MAX_PAYLOAD)
        self.assertEqual(task_child.MAX_FILE, copy_contract.MAX_FILE)
        self.assertEqual(task_child.MEMORY_BYTES, copy_contract.MEMORY_BYTES)
        self.assertEqual(task_cgroup.MEMORY, copy_contract.MEMORY_BYTES)
        command = task_sandbox.command()
        work = next(i for i, value in enumerate(command) if value == "/work" and command[i - 1] == "--tmpfs")
        self.assertEqual(command[work - 3:work], ["--size", str(copy_contract.WORK_BYTES), "--tmpfs"])
        self.assertGreater(copy_contract.WORK_BYTES, copy_contract.MAX_TOTAL)
        self.assertGreater(copy_contract.MEMORY_BYTES, 4 * copy_contract.MAX_PAYLOAD)


if __name__ == "__main__":
    unittest.main()

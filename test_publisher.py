#!/usr/bin/env python3
"""Security/stdio tests. Every fixture is retained; no cleanup or unlink."""
import concurrent.futures
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import types
import unittest
from unittest import mock
import uuid


HERE = Path(__file__).resolve().parent
TARGET = HERE / os.environ.get("PUBLISHER_FILE", "publisher.py")
SPEC = importlib.util.spec_from_file_location("publisher_under_test", TARGET)
P = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = P
SPEC.loader.exec_module(P)
ARTIFACTS = HERE / "test-artifacts" / ("run-" + uuid.uuid4().hex)
ARTIFACTS.mkdir(parents=True, exist_ok=False)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def create(path, data=b"original"):
    with Path(path).open("xb") as stream:
        stream.write(data)
    return Path(path)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.case = ARTIFACTS / (self._testMethodName + "-" + uuid.uuid4().hex)
        self.case.mkdir()
        self.output, self.source, self.outside = (self.case / name for name in ("output", "source", "outside"))
        for directory in (self.output, self.source, self.outside):
            directory.mkdir()
        self.publisher = P.Publisher(str(self.output), [str(self.source)])
        self.addCleanup(self.publisher.close)  # FD cleanup only; fixtures remain.

    @staticmethod
    def arguments(path="index.md", content="hello", slug="document"):
        return {"slug": slug, "files": [{"path": path, "content": content}]}

    def test_bundle_unicode_nested_receipts_and_repeat_new_publications(self):
        args = {"slug": "full-document", "files": [{"path": "index.md", "content": "完整正文\n"},
                {"path": "index.html", "content": "<!doctype html><h1>正文</h1>"},
                {"path": "assets/style.css", "content": "body { color: black; }"}]}
        first = self.publisher.publish(args)
        self.assertEqual(first["status"], "complete")
        before = {item["absolute_path"]: Path(item["absolute_path"]).read_bytes() for item in first["files"]}
        second = self.publisher.publish(args)
        self.assertEqual(second["status"], "complete")
        self.assertNotEqual(first["publication_directory"], second["publication_directory"])
        for item in first["files"] + second["files"]:
            data = Path(item["absolute_path"]).read_bytes()
            self.assertEqual(item["sha256"], sha(data))
            self.assertEqual(item["bytes"], len(data))
            self.assertTrue(item["complete"])
            self.assertTrue(Path(item["absolute_path"]).is_absolute())
        for path, data in before.items():
            self.assertEqual(Path(path).read_bytes(), data)

    def test_traversal_absolute_empty_hidden_alias_and_bad_suffix_refused(self):
        invalid = ["../index.md", "/tmp/index.md", "a//index.md", "a/./index.md", "a/../index.md", "a\\index.md",
                   ".git/config.md", "C:/index.md", "a/.secret.txt", "x /index.md", "x./index.md", "index.exe",
                   "a/\x00.txt", "e\u0301.txt", "a/" * 9 + "index.md"]
        for path in invalid:
            with self.subTest(path=repr(path)):
                result = self.publisher.publish(self.arguments(path=path))
                self.assertEqual(result["status"], "rejected")
                self.assertIsNone(result["publication_directory"])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_duplicate_case_and_parent_file_conflicts_preflight(self):
        for paths in [("index.md", "index.md"), ("Index.md", "index.md"), ("doc.md", "doc.md/child.txt"), ("assets/a.md", "Assets/b.md")]:
            result = self.publisher.publish({"slug": "doc", "files": [{"path": name, "content": "x"} for name in paths]})
            self.assertEqual(result["error"]["code"], "DUPLICATE_OR_CONFLICTING_PATH")
            self.assertEqual(result["created_directories"], [])

    def test_invalid_content_encoding_and_unknown_modes_preflight(self):
        for content in ["\ud800", "a\x00b", "a\x01b", "a\x7fb", 42, None]:
            self.assertEqual(self.publisher.publish(self.arguments(content=content))["status"], "rejected")
        for key, value in [("encoding", "base64"), ("overwrite", True), ("root", "/tmp"), ("requestId", "retry")]:
            args = self.arguments()
            args[key] = value
            self.assertEqual(self.publisher.publish(args)["status"], "rejected")
        self.assertEqual(list(self.output.iterdir()), [])

    def test_total_bytes_are_utf8_bytes_not_character_count(self):
        limited = P.Publisher(str(self.output), limits=P.Limits(max_files=2, max_total_bytes=5))
        self.addCleanup(limited.close)
        self.assertEqual(limited.publish(self.arguments(content="你你"))["error"]["code"], "BYTE_LIMIT")
        too_many = {"slug": "doc", "files": [{"path": str(i) + ".txt", "content": ""} for i in range(3)]}
        self.assertEqual(limited.publish(too_many)["error"]["code"], "FILE_LIMIT")
        self.assertEqual(list(self.output.iterdir()), [])

    def test_existing_publication_and_symlink_collision_are_not_reused(self):
        fixed = uuid.UUID(int=0)
        name = "document-" + fixed.hex
        existing = self.output / name
        existing.mkdir()
        marker = create(existing / "keep.txt")
        with mock.patch.object(P.uuid, "uuid4", return_value=fixed):
            result = self.publisher.publish(self.arguments())
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(marker.read_bytes(), b"original")
        self.assertEqual(sorted(p.name for p in existing.iterdir()), ["keep.txt"])
        second = uuid.UUID(int=1)
        os.symlink(str(self.outside), self.output / ("document-" + second.hex))
        with mock.patch.object(P.uuid, "uuid4", return_value=second):
            self.assertEqual(self.publisher.publish(self.arguments())["status"], "rejected")
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_symlink_component_injection_never_writes_target(self):
        outside = self.outside
        class Injected(P.Publisher):
            def _mkdir(inner, name, parent_fd):
                if name == "assets":
                    os.symlink(str(outside), name, dir_fd=parent_fd)
                else:
                    super()._mkdir(name, parent_fd)
        broker = Injected(str(self.output))
        self.addCleanup(broker.close)
        result = broker.publish(self.arguments(path="assets/index.txt"))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["files"], [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_preexisting_hardlink_file_in_new_directory_is_never_opened_for_write(self):
        sentinel = create(self.outside / "sentinel.txt", b"keep exactly")
        class Injected(P.Publisher):
            def _mkdir(inner, name, parent_fd):
                super()._mkdir(name, parent_fd)
                if parent_fd == inner.root_fd:
                    fd = os.open(name, P.DIR_FLAGS, dir_fd=parent_fd)
                    try:
                        os.link(str(sentinel), "index.md", dst_dir_fd=fd)
                    finally:
                        os.close(fd)
        broker = Injected(str(self.output))
        self.addCleanup(broker.close)
        result = broker.publish(self.arguments(content="must never replace sentinel"))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["files"], [])
        self.assertEqual(sentinel.read_bytes(), b"keep exactly")

    def test_output_root_with_symlink_segment_is_rejected_without_chmod(self):
        link = self.case / "root-link"
        os.symlink(str(self.output), link)
        before = stat.S_IMODE(self.output.stat().st_mode)
        with self.assertRaises(OSError):
            P.Publisher(str(link))
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), before)

    def test_concurrent_same_slug_never_collides_and_same_uuid_fails_closed(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.publisher.publish(self.arguments()), range(16)))
        self.assertTrue(all(result["status"] == "complete" for result in results))
        self.assertEqual(len({result["publication_directory"] for result in results}), 16)
        fixed = uuid.UUID(int=2)
        with mock.patch.object(P.uuid, "uuid4", return_value=fixed):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                colliding = list(executor.map(lambda _: self.publisher.publish(self.arguments()), range(6)))
        self.assertEqual(sum(result["status"] == "complete" for result in colliding), 1)
        self.assertEqual(sum(result["status"] == "rejected" for result in colliding), 5)

    def test_partial_write_keeps_completed_and_partial_files_and_precise_receipt(self):
        class DiskFailure(P.Publisher):
            calls = 0
            def _write(inner, fd, data):
                inner.calls += 1
                if inner.calls == 2:
                    return os.write(fd, data[:2])
                if inner.calls == 3:
                    raise OSError(errno.EIO, "injected disk error")
                return os.write(fd, data)
        broker = DiskFailure(str(self.output))
        self.addCleanup(broker.close)
        result = broker.publish({"slug": "doc", "files": [{"path": "first.md", "content": "done"},
                    {"path": "second.md", "content": "abcdef"}, {"path": "third.md", "content": "not reached"}]})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["total_bytes_written"], 6)
        self.assertEqual(len(result["files"]), 2)
        first, second = result["files"]
        self.assertTrue(first["complete"])
        self.assertFalse(second["complete"])
        self.assertEqual(second["bytes"], 2)
        self.assertEqual(second["sha256"], sha(b"ab"))
        self.assertEqual(Path(first["absolute_path"]).read_bytes(), b"done")
        self.assertEqual(Path(second["absolute_path"]).read_bytes(), b"ab")
        self.assertFalse((Path(result["publication_directory"]) / "third.md").exists())

    def source_args(self, path, digest):
        return {"slug": "copy", "files": [{"path": "index.md", "source_path": str(path), "expected_sha256": digest}]}

    def test_source_copy_repeat_and_multiple_roots_preserve_original_bytes(self):
        data = "22k正文\n中文 + html".encode("utf-8")
        source = create(self.source / "full.md", data)
        another = create(self.outside / "other.txt", b"outside root two")
        broker = P.Publisher(str(self.output), [str(self.source), str(self.outside)])
        self.addCleanup(broker.close)
        first = broker.publish(self.source_args(source, sha(data)))
        second = broker.publish(self.source_args(source, sha(data)))
        self.assertEqual(first["status"], "complete")
        self.assertEqual(second["status"], "complete")
        self.assertNotEqual(first["publication_directory"], second["publication_directory"])
        self.assertEqual(Path(first["files"][0]["absolute_path"]).read_bytes(), data)
        self.assertEqual(source.read_bytes(), data)
        self.assertEqual(broker.publish(self.source_args(another, sha(another.read_bytes())))["status"], "complete")

    def test_source_outside_and_source_disabled_and_stale_hash_create_nothing(self):
        source = create(self.source / "full.md", b"new version")
        outside = create(self.outside / "no.txt")
        self.assertEqual(self.publisher.publish(self.source_args(outside, sha(b"original")))["error"]["code"], "SOURCE_OUTSIDE_ROOTS")
        self.assertEqual(self.publisher.publish(self.source_args(source, sha(b"old version")))["error"]["code"], "SOURCE_HASH_MISMATCH")
        broker = P.Publisher(str(self.output))
        self.addCleanup(broker.close)
        self.assertEqual(broker.publish(self.source_args(source, sha(source.read_bytes())))["error"]["code"], "SOURCE_DISABLED")
        self.assertEqual(list(self.output.iterdir()), [])

    def test_source_symlink_hardlink_fifo_directory_invalid_utf8_and_binary_refused(self):
        source = create(self.source / "plain.txt")
        os.symlink(str(source), self.source / "symlink.txt")
        os.link(source, self.source / "hardlink.txt")
        os.mkfifo(self.source / "fifo.txt")
        (self.source / "directory.txt").mkdir()
        bad = create(self.source / "bad.txt", b"\xff")
        binary = create(self.source / "binary.txt", b"a\x00b")
        for name in ("symlink.txt", "hardlink.txt", "fifo.txt", "directory.txt"):
            result = self.publisher.publish(self.source_args(self.source / name, sha(b"original")))
            self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.publisher.publish(self.source_args(bad, sha(b"\xff")))["error"]["code"], "INVALID_UTF8")
        self.assertEqual(self.publisher.publish(self.source_args(binary, sha(b"a\x00b")))["error"]["code"], "NON_TEXT_CONTENT")
        self.assertEqual(source.read_bytes(), b"original")
        self.assertEqual(list(self.output.iterdir()), [])

    def test_source_path_length_and_mixed_content_are_rejected(self):
        result = self.publisher.publish(self.source_args(str(self.source) + "/" + "x" * 4096 + ".txt", "0" * 64))
        self.assertEqual(result["error"]["code"], "INVALID_SOURCE_PATH")
        args = self.source_args(self.source / "unused.md", "0" * 64)
        args["files"][0]["content"] = "not allowed together"
        self.assertEqual(self.publisher.publish(args)["error"]["code"], "INVALID_ARGUMENTS")
        self.assertEqual(list(self.output.iterdir()), [])

    def test_source_symlink_directory_escape_refused(self):
        target = create(self.outside / "outside.md")
        os.symlink(str(self.outside), self.source / "escape")
        result = self.publisher.publish(self.source_args(self.source / "escape" / target.name, sha(b"original")))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(list(self.output.iterdir()), [])

    def test_source_stat_change_is_detected_without_modifying_fixture(self):
        source = create(self.source / "full.md")
        real_fstat, calls = os.fstat, [0]
        def changed(fd):
            actual = real_fstat(fd)
            calls[0] += 1
            if calls[0] == 2:
                values = {name: getattr(actual, name) for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink", "st_mode")}
                values["st_mtime_ns"] += 1
                return types.SimpleNamespace(**values)
            return actual
        with mock.patch.object(P.os, "fstat", side_effect=changed):
            result = self.publisher.publish(self.source_args(source, sha(b"original")))
        self.assertEqual(result["error"]["code"], "SOURCE_CHANGED")
        self.assertEqual(list(self.output.iterdir()), [])


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.root = ARTIFACTS / (self._testMethodName + "-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.publisher = P.Publisher(str(self.root))
        self.addCleanup(self.publisher.close)

    @staticmethod
    def init(request_id=1):
        return {"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "security-test", "version": "1"}}}

    def stream(self, messages, limits=None):
        if limits:
            self.publisher.limits = limits
        source = io.BytesIO(b"".join((json.dumps(item) + "\n").encode() for item in messages))
        output = io.BytesIO()
        code = P.StdioServer(self.publisher).serve(source, output)
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_initialize_ready_ping_list_publish_and_unknown_tool(self):
        secret_text = "document contents must never be echoed as diagnostics"
        messages = [self.init(), {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "publish_document", "arguments": PublisherTests.arguments(content=secret_text)}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "overwrite", "arguments": {}}}]
        code, replies = self.stream(messages)
        self.assertEqual(code, 0)
        self.assertEqual(len(replies), 5)
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        tools = replies[2]["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["publish_document"])
        self.assertFalse(tools[0]["annotations"]["idempotentHint"])
        self.assertFalse(replies[3]["result"]["isError"])
        self.assertEqual(replies[3]["result"]["structuredContent"]["status"], "complete")
        self.assertNotIn(secret_text, json.dumps(replies))
        self.assertEqual(replies[4]["error"]["code"], -32602)

    def test_preinitialize_and_invalid_arguments_do_not_publish(self):
        _, replies = self.stream([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.init(2),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "publish_document", "arguments": {"root": "/etc"}}}])
        self.assertEqual(replies[0]["error"]["code"], -32002)
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_bad_json_duplicate_keys_unpaired_unicode_and_binary_frame(self):
        for raw in [b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n', b'\xff\n', b'NaN\n', b'1e999\n', b'[]\n', b'{\n']:
            output = io.BytesIO()
            P.StdioServer(self.publisher).serve(io.BytesIO(raw), output)
            self.assertIn("error", json.loads(output.getvalue().splitlines()[0]))

    def test_oversized_and_unterminated_frames_fail_closed_without_allocating_unbounded_input(self):
        self.publisher.limits = P.Limits(max_frame_bytes=256)
        for raw in [b"x" * 257 + b"\n", b'{"jsonrpc":"2.0","id":1,"method":"ping"}']:
            output = io.BytesIO()
            code = P.StdioServer(self.publisher).serve(io.BytesIO(raw), output)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], -32700)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_real_subprocess_isolated_python_stdio_no_llm(self):
        data = self.init()
        wire = json.dumps(data) + "\n" + '{"jsonrpc":"2.0","method":"notifications/initialized"}\n' + '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        result = subprocess.run([sys.executable, "-I", "-B", str(TARGET), "--root", str(self.root)], input=wire.encode(), capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[1]["result"]["tools"][0]["name"], "publish_document")
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    print("Retained test fixtures: " + str(ARTIFACTS), file=sys.stderr)
    unittest.main(verbosity=2)

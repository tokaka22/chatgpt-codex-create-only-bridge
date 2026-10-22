#!/usr/bin/env python3
"""Bounded, create-only text publication over handshake-era MCP stdio.

No delete, rename, truncate, chmod, network, command, or caller-selected root.
The operator must keep output/source directory namespaces trusted: same-UID
processes outside the read-only worker are not isolated by a Python process.
"""
import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import unicodedata
import uuid
from dataclasses import dataclass


PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
TEXT_SUFFIXES = frozenset((".md", ".markdown", ".html", ".htm", ".txt", ".css", ".json", ".csv", ".tsv"))
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class Limits:
    max_files: int = 32
    max_total_bytes: int = 4 * 1024 * 1024
    max_frame_bytes: int = 32 * 1024 * 1024
    max_depth: int = 8
    max_path_chars: int = 512


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _absolute_parts(path):
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise Refused("INVALID_ROOT_OR_SOURCE", "An explicit absolute non-root path is required.")
    parts = path[1:].split("/")
    if any(not part or part in (".", "..") or "\x00" in part or "\\" in part for part in parts):
        raise Refused("INVALID_ROOT_OR_SOURCE", "Empty, dot, parent, NUL, and backslash path segments are forbidden.")
    try:
        path.encode("utf-8", "strict")
    except UnicodeError:
        raise Refused("INVALID_UTF8", "Paths must be valid UTF-8.")
    return tuple(parts)


def _open_absolute_directory(path):
    parts = _absolute_parts(path)
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in parts:
            nxt = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd, parts
    except BaseException:
        os.close(fd)
        raise


def _text_bytes(value):
    if not isinstance(value, str):
        raise Refused("INVALID_CONTENT", "Content must be a UTF-8 text string.")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        raise Refused("INVALID_UTF8", "Content contains invalid Unicode or encoding.")
    if any((ord(char) < 32 and char not in "\t\r\n") or ord(char) == 127 for char in value):
        raise Refused("NON_TEXT_CONTENT", "NUL and binary control characters are forbidden.")
    return encoded


def _relative_parts(path, limits):
    if not isinstance(path, str) or not path or len(path) > limits.max_path_chars:
        raise Refused("INVALID_PATH", "A bounded relative text-file path is required.")
    if path.startswith("/") or "\\" in path or unicodedata.normalize("NFC", path) != path:
        raise Refused("INVALID_PATH", "Paths must be relative NFC POSIX paths without backslashes.")
    parts = path.split("/")
    if len(parts) > limits.max_depth:
        raise Refused("PATH_DEPTH", "The relative path is too deep.")
    for part in parts:
        if not part or part.startswith(".") or part.endswith((".", " ")) or ":" in part:
            raise Refused("INVALID_PATH", "Empty, hidden, dot, parent, colon, and ambiguous segments are forbidden.")
        if any(ord(c) < 32 or ord(c) == 127 for c in part):
            raise Refused("INVALID_PATH", "Control characters in paths are forbidden.")
        try:
            if len(part.encode("utf-8", "strict")) > 128:
                raise Refused("INVALID_PATH", "A path segment is too long.")
        except UnicodeError:
            raise Refused("INVALID_UTF8", "Paths must be valid UTF-8.")
    if os.path.splitext(parts[-1])[1].lower() not in TEXT_SUFFIXES:
        raise Refused("TEXT_SUFFIX_REQUIRED", "Only documented text-file suffixes are supported.")
    return tuple(parts)


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


class Publisher:
    def __init__(self, root, source_roots=(), limits=None):
        self.limits = limits or Limits()
        self.root = root
        self.root_fd = None
        self.sources = []
        try:
            self.root_fd, _ = _open_absolute_directory(root)
            for source in source_roots:
                fd, parts = _open_absolute_directory(source)
                self.sources.append((source, parts, fd))
            self.sources.sort(key=lambda item: len(item[1]), reverse=True)
        except BaseException:
            self.close()
            raise

    def close(self):
        for _, _, fd in self.sources:
            os.close(fd)
        self.sources = []
        if self.root_fd is not None:
            os.close(self.root_fd)
            self.root_fd = None

    def read_source(self, path, expected_sha256, budget):
        if not self.sources:
            raise Refused("SOURCE_DISABLED", "The operator has not configured any source roots.")
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
            raise Refused("INVALID_SOURCE_HASH", "A lowercase SHA-256 source hash is required.")
        if not isinstance(path, str) or not 1 <= len(path) <= 4096:
            raise Refused("INVALID_SOURCE_PATH", "A bounded absolute source path is required.")
        parts = _absolute_parts(path)
        selected = next((item for item in self.sources if parts[:len(item[1])] == item[1] and len(parts) > len(item[1])), None)
        if selected is None:
            raise Refused("SOURCE_OUTSIDE_ROOTS", "The source is outside operator-configured read roots.")
        _, root_parts, root_fd = selected
        relative = parts[len(root_parts):]
        _relative_parts("/".join(relative), self.limits)
        parent = os.dup(root_fd)
        fd = None
        try:
            for part in relative[:-1]:
                nxt = os.open(part, DIR_FLAGS, dir_fd=parent)
                os.close(parent)
                parent = nxt
            fd = os.open(relative[-1], READ_FLAGS, dir_fd=parent)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise Refused("SOURCE_NOT_REGULAR_UNLINKED", "The source must be a regular file with exactly one hard link.")
            if before.st_size > budget:
                raise Refused("BYTE_LIMIT", "The source exceeds the remaining bundle byte limit.")
            chunks, length = [], 0
            while True:
                chunk = os.read(fd, min(65536, budget - length + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                if length > budget:
                    raise Refused("BYTE_LIMIT", "The source grew beyond the bundle byte limit.")
            after = os.fstat(fd)
            current = os.stat(relative[-1], dir_fd=parent, follow_symlinks=False)
            if _fingerprint(before) != _fingerprint(after) or _fingerprint(after) != _fingerprint(current) or length != before.st_size:
                raise Refused("SOURCE_CHANGED", "The source changed while it was being read.")
            data = b"".join(chunks)
            if hashlib.sha256(data).hexdigest() != expected_sha256:
                raise Refused("SOURCE_HASH_MISMATCH", "The source bytes do not match expected_sha256.")
            try:
                text = data.decode("utf-8", "strict")
            except UnicodeError:
                raise Refused("INVALID_UTF8", "The source is not strictly encoded UTF-8 text.")
            _text_bytes(text)
            return data
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent)

    def prepare(self, arguments):
        if not isinstance(arguments, dict) or set(arguments) != {"slug", "files"}:
            raise Refused("INVALID_ARGUMENTS", "Exactly slug and files are required; no requestId, root, encoding, or overwrite field is accepted.")
        slug, files = arguments["slug"], arguments["files"]
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", slug):
            raise Refused("INVALID_SLUG", "Use a lowercase ASCII slug of one to 48 characters.")
        if not isinstance(files, list) or not 1 <= len(files) <= self.limits.max_files:
            raise Refused("FILE_LIMIT", "The file count is outside operator limits.")
        prepared, names, spellings, total = [], set(), {}, 0
        for item in files:
            if not isinstance(item, dict):
                raise Refused("INVALID_ARGUMENTS", "Each file must be an object.")
            keys = set(item)
            if keys not in ({"path", "content"}, {"path", "source_path", "expected_sha256"}):
                raise Refused("INVALID_ARGUMENTS", "Use exactly path+content or path+source_path+expected_sha256.")
            parts = _relative_parts(item["path"], self.limits)
            key = tuple(part.casefold() for part in parts)
            for depth in range(1, len(parts) + 1):
                folded, original = key[:depth], parts[:depth]
                if folded in spellings and spellings[folded] != original:
                    raise Refused("DUPLICATE_OR_CONFLICTING_PATH", "Case-alias directory and file spellings are forbidden.")
                spellings[folded] = original
            if key in names or any(key[:len(other)] == other or other[:len(key)] == key for other in names):
                raise Refused("DUPLICATE_OR_CONFLICTING_PATH", "Duplicate, case-alias, and file/directory-conflicting paths are forbidden.")
            names.add(key)
            if "content" in item:
                data = _text_bytes(item["content"])
            else:
                data = self.read_source(item["source_path"], item["expected_sha256"], self.limits.max_total_bytes - total)
            total += len(data)
            if total > self.limits.max_total_bytes:
                raise Refused("BYTE_LIMIT", "The total UTF-8 bytes exceed operator limits.")
            prepared.append((parts, data))
        return slug, prepared

    def _mkdir(self, name, parent_fd):
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)

    def _write(self, fd, data):
        return os.write(fd, data)

    def publish(self, arguments):
        receipt = {"status": "rejected", "publication_id": None, "publication_directory": None,
                   "files": [], "created_directories": [], "total_bytes_written": 0,
                   "idempotency": "not-supported; each successful call creates a new UUID publication"}
        directories = {}
        try:
            slug, prepared = self.prepare(arguments)
            publication_id = uuid.uuid4().hex
            name = slug + "-" + publication_id
            publication_path = self.root + "/" + name
            self._mkdir(name, self.root_fd)  # EEXIST is refusal, never reuse or overwrite.
            receipt.update(status="partial", publication_id=publication_id, publication_directory=publication_path)
            receipt["created_directories"].append(publication_path)
            pfd = os.open(name, DIR_FLAGS, dir_fd=self.root_fd)
            directories[()] = pfd
            os.fsync(self.root_fd)
            for parts, data in prepared:
                current = ()
                for segment in parts[:-1]:
                    nxt = current + (segment,)
                    if nxt not in directories:
                        self._mkdir(segment, directories[current])
                        receipt["created_directories"].append(publication_path + "/" + "/".join(nxt))
                        directories[nxt] = os.open(segment, DIR_FLAGS, dir_fd=directories[current])
                        os.fsync(directories[current])
                    current = nxt
                parent = directories[current]
                fd = os.open(parts[-1], CREATE_FLAGS, 0o600, dir_fd=parent)
                entry = {"relative_path": "/".join(parts), "absolute_path": publication_path + "/" + "/".join(parts),
                         "sha256": hashlib.sha256(b"").hexdigest(), "bytes": 0, "complete": False}
                receipt["files"].append(entry)
                digest = hashlib.sha256()
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise Refused("OUTPUT_NOT_NEW_REGULAR", "The exclusively created output is not a new regular file.")
                    view = memoryview(data)
                    while entry["bytes"] < len(data):
                        count = self._write(fd, view[entry["bytes"]:entry["bytes"] + 65536])
                        if not isinstance(count, int) or count <= 0:
                            raise Refused("WRITE_FAILED", "The output write made no progress.")
                        written = view[entry["bytes"]:entry["bytes"] + count]
                        digest.update(written)
                        entry["bytes"] += count
                        receipt["total_bytes_written"] += count
                        entry["sha256"] = digest.hexdigest()
                    os.fsync(fd)
                    after = os.fstat(fd)
                    linked = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                    if _fingerprint(after) != _fingerprint(linked) or after.st_nlink != 1 or after.st_size != len(data):
                        raise Refused("OUTPUT_CHANGED", "The new output changed during publication.")
                    os.fsync(parent)
                    entry["complete"] = True
                finally:
                    os.close(fd)
            os.fsync(pfd)
            receipt["status"] = "complete"
        except Refused as error:
            receipt["error"] = {"code": error.code, "message": error.message}
        except OSError as error:
            code = "PATH_EXISTS_OR_UNSAFE" if error.errno in (17, 20, 40) else "FILESYSTEM_FAILURE"
            receipt["error"] = {"code": code, "message": "Publication refused or stopped by the filesystem; created entries are retained.", "errno": error.errno}
        except Exception:
            receipt["error"] = {"code": "INTERNAL_FAILURE", "message": "Publication stopped; created entries are retained. No rollback was attempted."}
        finally:
            for fd in directories.values():
                os.close(fd)
        return receipt


def tool_definition(limits):
    path_schema = {"type": "string", "minLength": 1, "maxLength": limits.max_path_chars}
    return {"name": "publish_document", "title": "Publish a new text document bundle",
            "description": "Create a new server-generated slug+UUID publication. Existing files are never changed or deleted. Use content or an explicitly allowed source_path with expected_sha256. No idempotent retry: every call creates a different publication. Partial failures retain created entries and return receipts.",
            "inputSchema": {"type": "object", "additionalProperties": False, "required": ["slug", "files"],
                "properties": {"slug": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,47}$"},
                    "files": {"type": "array", "minItems": 1, "maxItems": limits.max_files,
                        "items": {"oneOf": [
                            {"type": "object", "additionalProperties": False, "required": ["path", "content"], "properties": {"path": path_schema, "content": {"type": "string"}}},
                            {"type": "object", "additionalProperties": False, "required": ["path", "source_path", "expected_sha256"], "properties": {"path": path_schema, "source_path": {"type": "string", "minLength": 1, "maxLength": 4096}, "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}}
                        ]}}}},
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}}


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _bounded_integer(value):
    if len(value) > 24:
        raise ValueError("integer too long")
    return int(value)


def _bounded_float(value):
    if len(value) > 128:
        raise ValueError("float too long")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON number")
    return result


def _no_nonfinite(value):
    raise ValueError("nonfinite JSON number")


class StdioServer:
    def __init__(self, publisher):
        self.publisher, self.negotiated, self.ready = publisher, None, False

    @staticmethod
    def error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def dispatch(self, message):
        if not isinstance(message, dict) or set(message) - {"jsonrpc", "id", "method", "params"} or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return self.error(None, -32600, "Invalid JSON-RPC request.")
        method, request_id, params = message["method"], message.get("id"), message.get("params", {})
        notification = "id" not in message
        if notification:
            if method == "notifications/initialized" and self.negotiated:
                self.ready = True
            return None
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)) or (isinstance(request_id, str) and len(request_id) > 256):
            return self.error(None, -32600, "A bounded string or integer request id is required.")
        if not isinstance(params, dict):
            return self.error(request_id, -32602, "Params must be an object.")
        if method == "ping":
            result = {}
        elif method == "initialize":
            if self.negotiated:
                return self.error(request_id, -32600, "Already initialized.")
            version = params.get("protocolVersion")
            if not isinstance(version, str) or not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict):
                return self.error(request_id, -32602, "initialize requires protocolVersion, capabilities, and clientInfo.")
            client = params["clientInfo"]
            if any(not isinstance(client.get(key), str) or not 1 <= len(client[key]) <= 256 for key in ("name", "version")):
                return self.error(request_id, -32602, "clientInfo requires bounded nonempty name and version strings.")
            self.negotiated = version if version in PROTOCOLS else PROTOCOLS[0]
            result = {"protocolVersion": self.negotiated, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "create_only_documents", "version": "1.0.0"},
                      "instructions": "Publish only with publish_document. Read-only worker permissions stay in force. New calls always use new publication directories; no overwrite, rollback, or idempotent retry is available."}
        elif not self.ready:
            return self.error(request_id, -32002, "Initialize and send notifications/initialized first.")
        elif method == "tools/list":
            if set(params) - {"cursor", "_meta"} or params.get("cursor") not in (None, ""):
                return self.error(request_id, -32602, "This tool list has no pagination.")
            result = {"tools": [tool_definition(self.publisher.limits)]}
        elif method == "tools/call":
            if set(params) - {"name", "arguments", "_meta"} or params.get("name") != "publish_document":
                return self.error(request_id, -32602, "Unknown tool or unsupported call fields.")
            receipt = self.publisher.publish(params.get("arguments"))
            result = {"content": [{"type": "text", "text": json.dumps(receipt, ensure_ascii=True, separators=(",", ":"))}],
                      "isError": receipt["status"] != "complete"}
            if self.negotiated in ("2025-11-25", "2025-06-18"):
                result["structuredContent"] = receipt
        else:
            return self.error(request_id, -32601, "Method not found.")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def serve(self, incoming, outgoing):
        while True:
            line = incoming.readline(self.publisher.limits.max_frame_bytes + 1)
            if not line:
                return 0
            fatal = len(line) > self.publisher.limits.max_frame_bytes or not line.endswith(b"\n")
            if fatal:
                response = self.error(None, -32700, "Frame too large or missing final newline; connection closed.")
            else:
                try:
                    message = json.loads(line.decode("utf-8", "strict"), object_pairs_hook=_no_duplicate_keys,
                                         parse_int=_bounded_integer, parse_float=_bounded_float, parse_constant=_no_nonfinite)
                    response = self.dispatch(message)
                except (ValueError, UnicodeError, RecursionError):
                    response = self.error(None, -32700, "Invalid bounded UTF-8 JSON frame.")
            if response is not None:
                outgoing.write((json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8"))
                outgoing.flush()
            if fatal:
                return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Operator-owned existing absolute output directory; no symlink segments.")
    parser.add_argument("--source-root", action="append", default=[], help="An explicitly allowed existing read directory; repeatable.")
    parser.add_argument("--max-files", type=int, default=32)
    parser.add_argument("--max-total-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--max-frame-bytes", type=int, default=32 * 1024 * 1024)
    args = parser.parse_args(argv)
    if not 1 <= args.max_files <= 256 or not 1 <= args.max_total_bytes <= 64 * 1024 * 1024 or not 256 <= args.max_frame_bytes <= 128 * 1024 * 1024:
        parser.error("Operator limits are outside supported bounds.")
    publisher = None
    try:
        publisher = Publisher(args.root, args.source_root, Limits(args.max_files, args.max_total_bytes, args.max_frame_bytes))
        return StdioServer(publisher).serve(sys.stdin.buffer, sys.stdout.buffer)
    except (OSError, Refused):
        print("Startup refused: output/source roots must be existing directories with no symlink segments.", file=sys.stderr)
        return 2
    finally:
        if publisher is not None:
            publisher.close()


if __name__ == "__main__":
    raise SystemExit(main())

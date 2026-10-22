# ChatGPT ↔ Codex Create-Only Bridge

[简体中文](README.zh-CN.md) · [Installation](docs/INSTALL.md) · [Security model](SECURITY.md) · [Upstream attribution](UPSTREAM.md)

Let ChatGPT read a registered local project through Codex and publish complete documents into new version directories. Existing files stay read-only. The web conversation owns the writing and language style; the local worker reads sources, saves supplied text, and verifies the result.

This repository contains the **create-only extensions and deployment kit**, plus patches for an independently maintained upstream MCP bridge. It is not a standalone ChatGPT client, a replacement Codex implementation, or an official OpenAI product.

```text
ChatGPT web
    │ MCP over an authenticated connection / separately operated tunnel
    ▼
Protocol adapter (loopback, bearer token by default)
    ▼
Patched upstream bridge (projects, agents, persisted background jobs)
    ▼
Operator runtime guard → Codex app-server (read-only / never)
    │ confirmed active turn, exact publish_document function
    ▼
Publisher → output-root/document-name-<new UUID>/index.md + index.html
```

## What is included

- **Create-only publisher:** a fixed output root, fresh UUID directories, exclusive file creation, optional hash-verified source copying, and accurate complete/partial receipts. No overwrite, deletion, rename, shell, or networking interface.
- **Native runtime guard:** verifies the effective read-only configuration, owns fresh threads and active turns, rejects old execution contexts and arbitrary tool/configuration overrides, and dispatches one operator-owned publication function.
- **MCP compatibility adapter:** official SDK v2 frontend and legacy SDK backend; preserves schemas and caller metadata, enforces loopback Host/Origin and auth checks, rejects unsupported protocol semantics, and does not cancel background work when an observer disconnects.
- **Deployment configuration generator:** creates a new private runtime directory, native wrapper, isolated Codex home, and core launcher. It does not change an existing installation or start/restart services.
- **Upstream patches:** certificate-only CA bundle recognition, job retention, native asynchronous input/provenance, and named permission checks. Create-only publishing itself uses no writable sandbox/profile.

## Quick start

Requires Node.js 22+, Python 3.11+, Git, the tested Codex CLI **0.153.3**, and your own working Codex sign-in. Native sandbox behavior depends on the host OS; the end-to-end reference deployment was tested on macOS. ChatGPT custom-plugin access and a remote connection/tunnel must be available to your account.

```sh
git clone https://github.com/tokaka22/chatgpt-codex-create-only-bridge.git
cd chatgpt-codex-create-only-bridge
npm ci --ignore-scripts --no-audit --no-fund
npm test
```

Follow [the installation guide](docs/INSTALL.md) to prepare the pinned upstream core, generate a private policy, authenticate your own isolated Codex home, and connect ChatGPT. This is an operator-oriented v0.1 deployment kit; tunnel/account provisioning is deliberately separate.

An example request after connecting:

> Read the latest registered project materials. Write the complete document in this conversation's style, publish a new Markdown and standalone HTML version through Mini/local Codex, and verify the actual files and navigation. Do not modify, delete or overwrite any existing file. Return the new directory and verification results when finished.

Ordinary checks do not ask for shell approval because they stay read-only. Publication uses the narrow create-only function. Configure the ChatGPT connector's confirmation preference for this trusted tool yourself; this repository does not change the web account's permission settings.

## Publication contract

```json
{
  "slug": "research-design",
  "files": [
    {"path": "index.md", "content": "# Complete new document\n"},
    {"path": "index.html", "content": "<!doctype html><meta charset=utf-8><h1>Document</h1>"}
  ]
}
```

Each successful call creates another `research-design-<uuid>` directory. A published file is never edited again. Revision means publishing a complete new version. To copy a permitted existing source without repeating a long body, provide `source_path` and its exact `expected_sha256` instead of `content`.

Defaults are **32 UTF-8 text files / 4 MiB total per publication**. Supported suffixes: Markdown, HTML, TXT, CSS, JSON, CSV and TSV. All source hashes are checked before creating a publication. `complete` confirms writes and fsyncs succeeded; `partial` preserves already-created output and reports it accurately. There is no automatic rollback or idempotent publication retry. After a connection failure, inspect the original job/result before considering another publication.

## Verification

The source release includes 22 publisher checks, 36 guard checks, 26 adapter checks, and configuration-generator checks. Tests use retained local fixtures and mock HTTP services; they make no model calls or tunnel/account changes.

A private reference deployment additionally published a complete nine-chapter document twice through the actual ChatGPT web connector with zero interaction requests, verified the previous version's hashes, and completed a 70-second job while the independent tunnel was restarted. This is historical integration evidence, not a promise about every OS, future CLI version or account. No private documents, credentials, screenshots, job identifiers or databases are included here. See [validation notes](docs/VALIDATION.md).

The guard is pinned to an observed native app-server contract. Audit and test new CLI versions before changing the pin. See [OpenAI's explanation of app-server as the integration layer](https://developers.openai.com/blog/codex-as-a-platform).

## License

MIT for this repository. Retain upstream notices when redistributing the patch or upstream code; see [UPSTREAM.md](UPSTREAM.md) and [licenses/upstream-MIT.txt](licenses/upstream-MIT.txt). Model access, accounts and managed services are separate from this code license.

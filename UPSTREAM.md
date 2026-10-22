# Upstream attribution

This is an independent add-on and deployment kit for [menaje/codex-mcp-bridge-for-chatgpt](https://github.com/menaje/codex-mcp-bridge-for-chatgpt), used under MIT. The patch is based on public upstream commit `8bb81b1b27745532b7bc2113bb00e66c43df3bbb` (0.4.1). The original notice is retained in [licenses/upstream-MIT.txt](licenses/upstream-MIT.txt).

The patch contains certificate-bundle detection, job-history retention, native asynchronous input/provenance, and named-output-policy changes developed during this integration. The create-only workflow does not require a writable permission profile.

# Upstream attribution

The name in the following link belongs only to the historical third-party
upstream and is not a former or alternate name for this repository.

This project is based on
[DeepCogNeural/codex-gpt-bridge](https://github.com/DeepCogNeural/codex-gpt-bridge),
used under the MIT License.

Codex MCP Bridge for ChatGPT intentionally removes the reverse
Codex-to-Responses-API integration and unauthenticated Cloudflare quick-tunnel
workflow. It focuses on a smaller ChatGPT-to-Codex bridge for OpenAI Secure MCP
Tunnel with stricter local policy defaults.

The publisher, runtime guard, protocol adapter, packaging scripts and tests are distributed under this repository's MIT license. Codex and MCP SDKs remain separately installed dependencies under their respective licenses. This project is not an official OpenAI or MCP product.

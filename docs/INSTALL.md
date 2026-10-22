# Operator installation

Use a fresh checkout and new runtime directory. These instructions do not upgrade or overwrite a live deployment. The publisher can be tested without ChatGPT, Codex inference or a tunnel.

## 1. Dependencies and tests

Install Node.js 22+, Python 3.11+, Git, and Codex CLI **0.153.3** from their official distributions. Resolve and pass an absolute executable path to the generator. Keep your own authentication private.

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm test
```

The test suite retains its newly created fixtures under `test-artifacts/` and does not delete existing files. This directory is ignored by Git.

## 2. Prepare a new upstream core

From the add-on repository root:

```sh
git clone https://github.com/menaje/codex-mcp-bridge-for-chatgpt.git upstream-core
git -C upstream-core checkout 8bb81b1b27745532b7bc2113bb00e66c43df3bbb
git -C upstream-core apply --check ../patches/upstream-0.4.1-create-only.patch
git -C upstream-core apply ../patches/upstream-0.4.1-create-only.patch
cd upstream-core
npm ci
npm run build
cd ..
```

The pinned base is public upstream 0.4.1. The patch adds fixes developed in the reference integration; documentation examples in that patch have been generalized. Keep the upstream license and attribution. Native `better-sqlite3` may require the usual local compiler tooling. `upstream-core` is a separate checkout, not a copy of private deployment state.

## 3. Generate a private configuration

Create the output directory yourself outside the source project. Set paths appropriate to your own host:

```sh
mkdir "$HOME/New-Documents"
python3 configure.py \
  --destination "$HOME/.local/share/create-only-bridge-runtime-v1" \
  --codex /absolute/path/to/codex \
  --core "$PWD/upstream-core" \
  --project /absolute/path/to/project \
  --output "$HOME/New-Documents"
```

Repeat `--source /absolute/path` to allow source copying from additional document directories. The project and output directories are already included as source roots. The generator requires a nonexistent destination and refuses a different native CLI version. It creates only a new policy, wrapper, core launcher and empty dedicated Codex home. It does not copy login data or change global Codex configuration.

Use your normal supported Codex login flow against that dedicated home:

```sh
CODEX_HOME="$HOME/.local/share/create-only-bridge-runtime-v1/codex-home" \
  /absolute/path/to/codex login
```

Do not commit that runtime directory. Inspect any project `AGENTS.md` and local skill/tool settings before admitting a project. If an enabled inherited tool fails the guard check, disable it explicitly in the isolated worker configuration after review; do not weaken the guard.

## 4. Start the core and adapter separately

Choose a private bearer token using your own secret manager and inject it into **both processes** as `CODEX_MCP_BRIDGE_TOKEN`. The generated launcher requires it. No token is generated, stored or printed by this kit. Never pass tokens in shell command arguments or paste them into chat.

Core process, with its environment already populated:

```sh
node "$HOME/.local/share/create-only-bridge-runtime-v1/start-core.mjs"
```

Adapter process, with the same environment token:

```sh
MCP_ADAPTER_BACKEND_URL=http://127.0.0.1:8878/mcp \
  MCP_ADAPTER_PORT=8876 node adapter.mjs
```

The adapter uses `MCP_ADAPTER_BEARER_TOKEN` if supplied, otherwise `CODEX_MCP_BRIDGE_TOKEN`. This guide uses authenticated loopback on both sides. Do not disable authentication just to make a public URL work.

Register the project using the upstream bridge's supported project/settings workflow. See its [README](https://github.com/menaje/codex-mcp-bridge-for-chatgpt). A policy source root alone does not create the upstream project's registry entry.

## 5. Connect ChatGPT

Operate the tunnel as a **third independent process**, targeting `http://127.0.0.1:8876/mcp`, with authentication appropriate to that transport and your account. Use current [Secure MCP Tunnel documentation](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) and your upstream bridge's setup instructions. Tunnel provisioning, eligibility and credentials are not included in this repository.

Add/refresh your own ChatGPT connector, verify its tools and choose its confirmation preference. If you want routine read/publish calls to proceed without per-call web confirmation, set that preference for this particular trusted connector yourself. It does not grant the worker general write permission.

For persistent deployment use three independent user services (core, adapter, tunnel) with their own logs and restart policy. Inject secrets through your service manager/secret store. Avoid a launcher that terminates core jobs when tunnel startup or health checking fails. This kit deliberately does not install or replace system/user services.

## 6. First acceptance run

1. Request a fresh Agent/Job for the exact registered project.
2. Ask it to read a harmless existing text source and compute its hash using `shasum` or `python3 -c`.
3. Publish one complete Markdown and HTML bundle through the directly available `publish_document` function.
4. Require the actual complete receipt, paths, bytes, hashes and navigation checks.
5. Publish a different new version and confirm every previous file's hash is unchanged.

Use no heredoc, here-string, write redirection or temporary file in read-only verification. Wait for the original job's real terminal state. On transient query failure, query that same job ID rather than submitting a duplicate publication. A new version means a new UUID directory, not patching an existing file.

## Standalone publisher

For a synthetic local MCP stdio client, the standard-library publisher can run independently:

```sh
python3 -I -B publisher.py --root /absolute/existing/output \
  --source-root /absolute/existing/source
```

In the guarded Codex workflow the publisher is called directly by the operator guard; it is intentionally disabled as a second MCP capability. Do not expose a writable shell or an extra unguarded file tool to that worker.

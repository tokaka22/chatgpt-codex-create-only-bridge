# Security model and scope

This kit addresses one specific boundary: the model may read existing project files and publish new text documents, but may not modify, replace, rename or delete existing user files.

## Enforcement

- Native Codex filesystem commands remain OS-sandboxed read-only, with no network access for commands and no permission escalation. Model inference still requires the native client's own network connection.
- The guard checks effective configuration and native thread/turn confirmation, rejects inherited enabled tools/plugins, unknown overrides and resumed old execution contexts, and only accepts its exact function from a confirmed active owned turn.
- Publisher source hashes, a fixed root, pinned directory descriptors, no-follow traversal, fresh UUID directories and `O_EXCL` creation constrain the write capability. Content is validated before publication. Existing permissions are never changed by the publication API.
- The adapter binds only to loopback and requires a bearer token by default. Explicit unauthenticated loopback mode exists solely for an externally authenticated local transport; it must not be exposed as an unprotected public HTTP service.

## Trust assumptions

The operator, installed publisher/guard code, configured roots, native CLI, upstream core and connection authentication are trusted. Keep the publication namespace outside other untrusted writers' control. This is not isolation from a separate malicious same-UID process, an administrator or a compromised host. It does not promise confidentiality against arbitrary read-only commands outside a project; do not advertise it as a filesystem read jail.

Treat source documents, tool results and website content as untrusted input. Secret scanning remains enabled in the upstream bridge, but it is not a substitute for choosing what a remote chat may read. Register only intended project roots and never include authentication stores or credential-bearing environment files in a project.

Runtime state, login data, `.env`, credentials, logs, databases, screenshots, real jobs and private source documents are intentionally excluded from the source release. Inject tokens only through the process environment or your own secret manager; do not put them in a repository, example, issue, log or model prompt.

Web connector confirmation settings belong to the account owner. Local never-approval settings do not authorize unrelated tools, machines, accounts or projects. Read-only violations are rejected rather than automatically granted broader permissions.

## Version and failure behavior

The native protocol integration is pinned to tested Codex 0.153.3. Future versions need explicit compatibility review. The adapter rejects unsupported new input/state/task semantics rather than fabricating support.

Publication is not idempotent. A partial or disconnected publication may leave complete or partial new files; these are retained. Check the original job and output before retrying. The publisher provides no cleanup or overwrite shortcut.

If reporting a suspected vulnerability, share a minimal synthetic reproduction and affected version. Do not post live keys, private documents or account/session identifiers in public issues. Use GitHub private vulnerability reporting if the repository owner has enabled it.

#!/usr/bin/env python3
"""Generate a fresh, private deployment directory; never install or restart services."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

HERE = Path(__file__).resolve().parent

def toml(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '[' + ','.join(toml(v) for v in value) + ']'
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k) + '=' + toml(v) for k, v in value.items()) + '}'
    raise ValueError('Unsupported operator configuration value')

def configure(destination, cli, core, project, output, sources):
    destination = Path(os.path.abspath(destination))
    if os.path.lexists(destination):
        raise ValueError('Destination must be new; no existing file or directory is adopted')
    cli = Path(cli).resolve(strict=True)
    core = Path(core).resolve(strict=True)
    project = Path(project).resolve(strict=True)
    output = Path(output).resolve(strict=True)
    sources = list(dict.fromkeys([str(project), str(output), *[str(Path(p).resolve(strict=True)) for p in sources]]))
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise ValueError('Codex executable is unavailable')
    if not (core / 'dist/cli.js').is_file():
        raise ValueError('Build the patched upstream core first')
    for directory in [project, output, *map(Path, sources)]:
        if not directory.is_dir() or directory == Path(directory.anchor):
            raise ValueError('Project/output/source roots must be existing non-root directories')
    template = json.loads((HERE / 'examples/policy-template.json').read_text())
    version = subprocess.run([str(cli), '--version'], capture_output=True, text=True, check=True, timeout=15).stdout
    if not re.search(r'(?<![\d.])' + re.escape(template['tested_codex_version']) + r'(?![\d.])', version):
        raise ValueError('This release requires tested Codex ' + template['tested_codex_version'])
    helper = HERE / 'publisher.py'
    publisher = {'command': sys.executable, 'args': ['-I', '-B', str(helper), '--root', str(output)],
                 'cwd': str(output), 'enabled': False, 'required': False,
                 'startup_timeout_sec': 20, 'tool_timeout_sec': 60,
                 'enabled_tools': ['publish_document'], 'default_tools_approval_mode': 'approve',
                 'tools': {'publish_document': {'approval_mode': 'approve'}}, 'environment_id': 'local'}
    for source in sources:
        publisher['args'] += ['--source-root', source]
    settings = dict(template['settings'])
    settings['mcp_servers.create_only_documents'] = toml(publisher)
    cli_args = ['app-server', '--listen', 'stdio://']
    for key, value in settings.items():
        cli_args += ['-c', key + '=' + value]
    policy = {'cli': str(cli), 'cli_args': cli_args, 'project': str(project),
              'publisher_server': 'create_only_documents', 'publisher_tool': 'publish_document',
              'publisher': publisher, 'root': str(output), 'source_roots': sources,
              'publisher_mode': 'operator-direct-function',
              'publisher_sha256': hashlib.sha256(helper.read_bytes()).hexdigest(),
              **{k: template[k] for k in ['required_false_features', 'required_true_features',
                                         'disabled_mcp_servers', 'disabled_plugins']}}
    destination.mkdir(mode=0o700)
    (destination / 'codex-home').mkdir(mode=0o700)
    with (destination / 'policy.json').open('x') as f:
        json.dump(policy, f, indent=2)
    wrapper = ('#!/bin/sh\nexec env CODEX_HOME=' + shlex.quote(str(destination / 'codex-home')) + ' ' +
               shlex.quote(sys.executable) + ' -I -B ' + shlex.quote(str(HERE / 'runtime_guard.py')) +
               ' --policy ' + shlex.quote(str(destination / 'policy.json')) + ' "$@"\n')
    with (destination / 'codex-create-only').open('x') as f:
        f.write(wrapper)
    (destination / 'codex-create-only').chmod(0o700)
    environment = {
        'CODEX_MCP_BRIDGE_CODEX': str(destination / 'codex-create-only'),
        'CODEX_MCP_BRIDGE_HOST': '127.0.0.1', 'CODEX_MCP_BRIDGE_PORT': '8878',
        'CODEX_MCP_BRIDGE_DEFAULT_SANDBOX': 'read-only',
        'CODEX_MCP_BRIDGE_DEFAULT_ACCESS_STRATEGY': 'read-only',
        'CODEX_MCP_BRIDGE_ALLOW_WRITE': '0', 'CODEX_MCP_BRIDGE_ALLOW_DANGER_FULL_ACCESS': '0',
        'CODEX_MCP_BRIDGE_APPROVAL_POLICY': 'never', 'CODEX_MCP_BRIDGE_DISABLE_SECRET_SCAN': '0',
        'CODEX_MCP_BRIDGE_STATE_DATABASE_FILE': str(destination / 'state.sqlite'),
        'CODEX_MCP_BRIDGE_MODEL_CATALOG_STATE_FILE': str(destination / 'models.json'),
        'CODEX_MCP_BRIDGE_ROOTS': str(project),
    }
    launcher = "import {pathToFileURL} from 'node:url';\n"
    launcher += "if (!process.env.CODEX_MCP_BRIDGE_TOKEN) throw new Error('Inject CODEX_MCP_BRIDGE_TOKEN into this process environment');\n"
    launcher += "for (const key of Object.keys(process.env)) if(key.startsWith('CODEX_GPT_BRIDGE_')) delete process.env[key];\n"
    launcher += "for (const key of ['NODE_OPTIONS','OPENAI_API_KEY','CODEX_API_KEY','CONTROL_PLANE_API_KEY','CODEX_MCP_BRIDGE_NO_AUTH','CODEX_MCP_BRIDGE_DEFAULT_PERMISSION_PROFILE','CODEX_MCP_BRIDGE_EXPECTED_WRITABLE_ROOTS']) delete process.env[key];\n"
    launcher += 'Object.assign(process.env,' + json.dumps(environment) + ');\n'
    launcher += 'process.chdir(' + json.dumps(str(core)) + ');\n'
    launcher += 'await import(pathToFileURL(' + json.dumps(str(core / 'dist/cli.js')) + ').href);\n'
    with (destination / 'start-core.mjs').open('x') as f:
        f.write(launcher)
    return {'runtime_directory': str(destination), 'output_root': str(output), 'codex_version': template['tested_codex_version']}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--codex', required=True)
    parser.add_argument('--core', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--source', action='append', default=[])
    a = parser.parse_args()
    print(json.dumps(configure(a.destination, a.codex, a.core, a.project, a.output, a.source), indent=2))

if __name__ == '__main__':
    main()

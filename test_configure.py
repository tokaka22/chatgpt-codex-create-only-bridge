import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid
import configure

class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / 'test-artifacts' / ('configure-' + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.core = self.root / 'core'
        (self.core / 'dist').mkdir(parents=True)
        (self.core / 'dist/cli.js').write_text('// fixture\n')
        self.project = self.root / 'project'
        self.output = self.root / 'output'
        self.project.mkdir()
        self.output.mkdir()
        self.cli = self.root / 'codex'
        self.cli.write_text('#!/bin/sh\nprintf "codex-cli 0.153.3\\n"\n')
        self.cli.chmod(0o700)
        self.dest = self.root / 'runtime'
    def call(self):
        return configure.configure(self.dest, self.cli, self.core, self.project, self.output, [])
    def test_generated_policy_preserves_boundaries_and_paths(self):
        self.call()
        p = json.loads((self.dest / 'policy.json').read_text())
        self.assertEqual(p['root'], str(self.output))
        self.assertEqual(p['publisher_sha256'], hashlib.sha256((configure.HERE / 'publisher.py').read_bytes()).hexdigest())
        settings = dict(v.split('=',1) for i,v in enumerate(p['cli_args']) if i and p['cli_args'][i-1] == '-c')
        self.assertEqual(settings['sandbox_mode'], '"read-only"')
        self.assertEqual(settings['approval_policy'], '"never"')
        self.assertEqual(settings['features.code_mode_host'], 'true')
        self.assertEqual(settings['features.plugins'], 'false')
        self.assertFalse(p['publisher']['enabled'])
        self.assertIn('CODEX_HOME=', (self.dest / 'codex-create-only').read_text())
        self.assertNotIn('NO_AUTH=1', (self.dest / 'start-core.mjs').read_text())
    def test_existing_destination_is_never_modified(self):
        self.dest.mkdir()
        marker = self.dest / 'marker'
        marker.write_text('keep')
        with self.assertRaises(ValueError): self.call()
        self.assertEqual(marker.read_text(),'keep')
    def test_broken_symlink_destination_refused(self):
        self.dest.symlink_to(self.root / 'missing')
        with self.assertRaises(ValueError): self.call()
        self.assertTrue(self.dest.is_symlink())
    def test_untested_native_version_does_not_create_runtime(self):
        with patch('configure.subprocess.run') as run:
            run.return_value.stdout='codex-cli 9.0.0'
            with self.assertRaises(ValueError): self.call()
        self.assertFalse(self.dest.exists())
    def test_missing_core_build_does_not_create_runtime(self):
        with self.assertRaises(FileNotFoundError):
            configure.configure(self.dest, self.cli, self.root/'missing-core', self.project,self.output,[])
        self.assertFalse(self.dest.exists())

if __name__ == '__main__': unittest.main(verbosity=2)

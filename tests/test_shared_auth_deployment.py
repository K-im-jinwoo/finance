import base64
import copy
import hashlib
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('shared_installer', ROOT / 'scripts/oracle_install_shared_auth.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

class SharedDeploymentTests(unittest.TestCase):
    def bundle(self):
        def item(raw):
            return {'base64': base64.b64encode(raw).decode(), 'sha256': hashlib.sha256(raw).hexdigest()}
        return {'files': {name: item((ROOT / 'scripts' / name).read_bytes()) for name in installer.FILES},
                'override': item((ROOT / 'deploy/shared-auth/collector.override.yaml').read_bytes())}

    def test_deployment_accepts_exact_sources_and_rejects_tampering(self):
        bundle = self.bundle()
        decoded, _ = installer.decode_bundle(bundle)
        self.assertEqual(set(decoded), installer.FILES)
        name = next(iter(installer.FILES))
        bundle['files'][name]['base64'] = base64.b64encode(b'print("changed")').decode()
        with self.assertRaisesRegex(ValueError, 'SOURCE_HASH_MISMATCH'):
            installer.decode_bundle(bundle)
        bundle = self.bundle()
        bundle['files']['unapproved.py'] = bundle['files'][name]
        with self.assertRaisesRegex(ValueError, 'UNEXPECTED_BUNDLE_FILES'):
            installer.decode_bundle(bundle)
        bundle = self.bundle()
        bundle['override']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'OVERRIDE_HASH_MISMATCH'):
            installer.decode_bundle(bundle)

    def test_collector_clone_preserves_settings_without_modifying_original(self):
        original = {'Config': {'Env': ['PYTHONPATH=/app/src'], 'Image': 'same-image',
                              'Cmd': ['serve', '--database', '/var/lib/stock/stock-assistant.sqlite3'],
                              'Labels': {'same': 'value'}, 'User': '1001:1001'},
                    'HostConfig': {'Mounts': [{'Type': 'bind', 'Source': '/existing-secret',
                                              'Target': '/run/secrets/toss-client-id', 'ReadOnly': True}],
                                   'Binds': ['/original-db:/var/lib/stock:rw'], 'ReadonlyRootfs': True,
                                   'RestartPolicy': {'Name': 'unless-stopped'}, 'Memory': 1234},
                    'NetworkSettings': {'Networks': {'original-network': {'Aliases': ['stock-assistant']}}}}
        before = copy.deepcopy(original)
        result = installer.collector_spec(original)
        self.assertEqual(original, before)
        for key in ('Image', 'Cmd', 'Labels', 'User'):
            self.assertEqual(result[key], original['Config'][key])
        self.assertEqual(result['Env'], original['Config']['Env'] + ['STOCK_TOSS_AUTH_CACHE_DIR=/run/stock-shared-auth'])
        for key in ('Binds', 'ReadonlyRootfs', 'RestartPolicy', 'Memory'):
            self.assertEqual(result['HostConfig'][key], original['HostConfig'][key])
        mounts = result['HostConfig']['Mounts']
        self.assertEqual(mounts[0], original['HostConfig']['Mounts'][0])
        self.assertEqual([(m['Target'], m['ReadOnly']) for m in mounts[1:]],
                         [('/opt/stock-shared-auth', True), ('/run/stock-shared-auth', False)])
        original['Config']['Env'].append('STOCK_TOSS_AUTH_CACHE_DIR=/existing')
        with self.assertRaisesRegex(ValueError, 'ALREADY_PRESENT'):
            installer.collector_spec(original)

if __name__ == '__main__':
    unittest.main()

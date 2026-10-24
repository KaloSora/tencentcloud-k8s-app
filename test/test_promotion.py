import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


spec = importlib.util.spec_from_file_location(
    'promotion', Path(__file__).resolve().parents[1] / 'ci' / 'jenkins' / 'promotion.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class FakeGitHub:
    def __init__(self, files=None, prior=None):
        self.files = files or []
        self.prior = prior
        self.blobs = {}
        self.calls = []
        self.base = 'base'
        self.unchanged = False

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if '/git/ref/heads/' in path:
            return {'object': {'sha': self.base}}
        if method == 'GET' and '/git/commits/' in path:
            return {'tree': {'sha': 'tree'}}
        if method == 'GET' and '/git/trees/' in path:
            return {'tree': self.files, 'truncated': False}
        if '/commits/' in path and method == 'GET':
            return {'sha': 'a' * 40}
        if method == 'POST' and path.endswith('/git/trees'):
            return {'sha': 'tree' if self.unchanged else 'new-tree'}
        if method == 'POST' and path.endswith('/git/commits'):
            return {'sha': 'new-commit'}
        if method == 'POST' and path.endswith('/git/refs'):
            return {}
        if method == 'POST' and path.endswith('/pulls'):
            return {'html_url': 'https://github.com/owner/gitops/pull/1'}
        raise AssertionError((method, path, data))

    def blob(self, repo, sha):
        return json.dumps(self.prior if sha == "release" else self.blobs[sha])


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.env = dict(TARGET_ENV='dev', IMAGE_REPOSITORY='harbor.core.com/library/com.tencentcloud/k8s-demo-app',
                        IMAGE_DIGEST='sha256:' + 'b' * 64, JIRA='JIRA-1', VERSION='1.0.0-SNAPSHOT',
                        GITOPS_REPO='owner/gitops',
                        GITOPS_BRANCH='main', APP_REPO='owner/app', BUILD_URL='https://jenkins/build/1')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.labels = {'org.opencontainers.image.version': self.env['VERSION'],
                       'org.opencontainers.image.revision': 'a' * 40,
                       'org.opencontainers.image.source': 'https://github.com/owner/app.git',
                       'com.tencentcloud.image.jira': 'JIRA-1,JIRA-2'}

    def fixture(self):
        api = FakeGitHub()
        for target in p.ENVIRONMENTS:
            path = f'{p.ROOT}/{target}/manifests/deployment.json'
            api.files.append({'path': path, 'type': 'blob', 'sha': target})
            api.blobs[target] = {
                'apiVersion': 'apps/v1', 'kind': 'Deployment',
                'metadata': {'name': 'k8s-demo-app', 'namespace': target},
                'spec': {'replicas': 3, 'template': {'spec': {
                    'imagePullSecrets': [{'name': 'harbor-pull'}],
                    'containers': [{'name': 'app', 'image': 'old-image',
                                    'resources': {'requests': {'cpu': '100m'}}},
                                   {'name': 'sidecar', 'image': 'sidecar:v1'}]}}}}
        return api

    def prepare(self, api=None):
        return p.prepare(api or self.fixture(), self.env, self.labels)

    def prior(self, environment):
        api = self.fixture()
        api.files.append({'path': f'{p.ROOT}/{environment}/release.json', 'type': 'blob', 'sha': 'release'})
        api.prior = dict(imageRepository=self.env['IMAGE_REPOSITORY'], imageDigest=self.env['IMAGE_DIGEST'],
                         sourceCommit='a' * 40, jira=self.env['JIRA'], version=self.env['VERSION'])
        api.blobs[environment]['spec']['template']['spec']['containers'][0]['image'] = (
            self.env['IMAGE_REPOSITORY'] + '@' + self.env['IMAGE_DIGEST'])
        return api

    def test_rejects_bad_parameters(self):
        for key, value in [('TARGET_ENV', '../../prod'), ('IMAGE_DIGEST', 'latest'),
                           ('JIRA', 'jira-1'), ('JIRA', ''), ('JIRA', 'JIRA-1,JIRA-2'),
                           ('VERSION', ''), ('VERSION', '1.0/evil'), ('VERSION', 'x' * 129),
                           ('IMAGE_REPOSITORY', 'evil.example/app'),
                           ('IMAGE_REPOSITORY', 'harbor.core.com/library/app:latest')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                p.validate({**self.env, key: value})

    def test_dev_changes_only_dev_and_its_application(self):
        plan = self.prepare()
        self.assertEqual(plan['release']['jira'], 'JIRA-1')
        self.assertEqual(plan['release']['version'], '1.0.0-SNAPSHOT')
        self.assertEqual(plan['release']['sourceCommit'], 'a' * 40)
        for entry in plan['entries']:
            self.assertTrue(entry['path'].startswith(p.ROOT + '/dev/') or
                            entry['path'] == 'argocd/applications/k8s-demo-app-dev.yaml')
        document = next(json.loads(e['content']) for e in plan['entries'] if e['path'].endswith('/deployment.json'))
        self.assertEqual(document['spec']['template']['spec']['containers'][0]['image'],
                         self.env['IMAGE_REPOSITORY'] + '@' + self.env['IMAGE_DIGEST'])
        app = next(json.loads(e['content']) for e in plan['entries'] if e['path'].startswith('argocd/'))
        self.assertEqual(app['spec']['destination']['namespace'], 'dev')
        self.assertTrue(app['spec']['source']['path'].endswith('/dev/manifests'))
        self.assertNotIn('helm', app['spec']['source'])
        self.assertNotIn('kustomize', app['spec']['source'])
        self.assertFalse(app['spec']['source']['directory']['recurse'])

    def test_promotes_exact_release_for_all_higher_environments(self):
        for target, prior in [('uat', 'dev'), ('preprod', 'uat'), ('prod', 'preprod')]:
            with self.subTest(target=target):
                self.env['TARGET_ENV'] = target
                plan = self.prepare(self.prior(prior))
                deployment = next(json.loads(e['content']) for e in plan['entries'] if e['path'].endswith('/deployment.json'))
                self.assertEqual(deployment['metadata']['namespace'], target)
                self.assertEqual(deployment['spec']['template']['spec']['containers'][0]['image'],
                                 self.env['IMAGE_REPOSITORY'] + '@' + self.env['IMAGE_DIGEST'])

    def test_rejects_skipped_environment(self):
        self.env['TARGET_ENV'] = 'prod'
        with self.assertRaisesRegex(ValueError, 'promote there first'):
            self.prepare()

    def test_rejects_different_release_coordinates(self):
        self.env['TARGET_ENV'] = 'uat'
        for key in ('imageRepository', 'imageDigest', 'sourceCommit', 'jira', 'version'):
            with self.subTest(key=key):
                api = self.prior('dev')
                api.prior[key] = 'different'
                with self.assertRaisesRegex(ValueError, 'must match'):
                    self.prepare(api)

    def test_only_changes_app_image_preserving_all_environment_settings(self):
        api = self.fixture()
        expected = copy.deepcopy(api.blobs['dev'])
        expected['spec']['template']['spec']['containers'][0]['image'] = (
            self.env['IMAGE_REPOSITORY'] + '@' + self.env['IMAGE_DIGEST'])
        plan = self.prepare(api)
        document = next(json.loads(e['content']) for e in plan['entries'] if e['path'].endswith('/deployment.json'))
        self.assertEqual(document, expected)
        self.assertEqual(len(plan['entries']), 3)
        self.assertFalse(any('/service.json' in e['path'] for e in plan['entries']))

    def test_missing_target_manifest_fails_without_writes(self):
        api = FakeGitHub()
        with self.assertRaisesRegex(ValueError, 'merge the GitOps environment manifests first'):
            self.prepare(api)
        self.assertTrue(all(method == 'GET' for method, _, _ in api.calls))

    def test_rejects_wrong_namespace_and_missing_or_duplicate_app_container(self):
        for mutation in ('namespace', 'missing', 'duplicate'):
            with self.subTest(mutation=mutation):
                api = self.fixture()
                document = api.blobs['dev']
                if mutation == 'namespace':
                    document['metadata']['namespace'] = 'prod'
                elif mutation == 'missing':
                    document['spec']['template']['spec']['containers'] = []
                else:
                    document['spec']['template']['spec']['containers'].append({'name': 'app', 'image': 'other'})
                with self.assertRaises(ValueError):
                    self.prepare(api)

    def test_rejects_prior_release_metadata_that_disagrees_with_deployment(self):
        self.env['TARGET_ENV'] = 'uat'
        api = self.prior('dev')
        api.blobs['dev']['spec']['template']['spec']['containers'][0]['image'] = 'different'
        with self.assertRaisesRegex(ValueError, 'does not match its release metadata'):
            self.prepare(api)

    def test_publish_creates_atomic_commit_branch_and_pr_without_updating_main(self):
        api = self.fixture()
        plan = self.prepare(api)
        result = p.publish(api, plan)
        self.assertEqual(result['status'], 'pr_created')
        mutations = [(method, path, data) for method, path, data in api.calls if method != 'GET']
        self.assertEqual([path.rsplit('/', 1)[-1] for _, path, _ in mutations],
                         ['trees', 'commits', 'refs', 'pulls'])
        self.assertEqual(mutations[2][2]['ref'], 'refs/heads/' + plan['branch'])
        self.assertNotEqual(mutations[2][2]['ref'], 'refs/heads/main')
        self.assertEqual(mutations[-1][2]['base'], 'main')
        self.assertIn('JIRA-1', mutations[-1][2]['title'])
        self.assertIn('1.0.0-SNAPSHOT', mutations[-1][2]['body'])
        self.assertIn('a' * 40, mutations[-1][2]['body'])

    def test_rejects_missing_or_mismatched_image_metadata_without_github_calls(self):
        for key, value in [('org.opencontainers.image.version', '2.0.0'),
                           ('com.tencentcloud.image.jira', 'JIRA-10'),
                           ('org.opencontainers.image.revision', 'abc'),
                           ('org.opencontainers.image.source', 'https://github.com/other/app.git')]:
            for labels in ({**self.labels, key: value}, {k: v for k, v in self.labels.items() if k != key}):
                with self.subTest(key=key, labels=labels):
                    api = self.fixture()
                    with self.assertRaises(ValueError):
                        p.prepare(api, self.env, labels)
                    self.assertEqual(api.calls, [])

    def test_old_prior_release_requires_repromotion(self):
        self.env['TARGET_ENV'] = 'uat'
        api = self.prior('dev')
        del api.prior['jira']
        del api.prior['version']
        with self.assertRaisesRegex(ValueError, 'must match'):
            self.prepare(api)

    def test_rejects_stale_base_without_writes(self):
        plan = self.prepare()
        api = FakeGitHub()
        api.base = 'advanced'
        with self.assertRaisesRegex(ValueError, 'base changed'):
            p.publish(api, plan)
        self.assertTrue(all(method == 'GET' for method, _, _ in api.calls))

    def test_no_change_does_not_create_branch_or_pr(self):
        plan = self.prepare()
        api = FakeGitHub()
        api.unchanged = True
        self.assertEqual(p.publish(api, plan)['status'], 'unchanged')
        self.assertFalse(any(path.endswith(('/git/refs', '/pulls')) for _, path, _ in api.calls))

    def test_registry_digest_is_verified(self):
        config = self.directory / 'docker.json'
        config.write_text(json.dumps({'auths': {'harbor.core.com': {'auth': 'dXNlcjpwYXNz'}}}))
        config_bytes = json.dumps({'config': {'Labels': self.labels}}).encode()
        config_digest = 'sha256:' + hashlib.sha256(config_bytes).hexdigest()
        content = json.dumps({'schemaVersion': 2, 'config': {'digest': config_digest}}).encode()
        digest = 'sha256:' + hashlib.sha256(content).hexdigest()
        class Response:
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return self.data
        with patch.object(p, 'urlopen', side_effect=[Response(content), Response(config_bytes)]) as request:
            self.assertEqual(p.check_image(self.env['IMAGE_REPOSITORY'], digest, str(config)), self.labels)
            self.assertTrue(request.call_args_list[1].args[0].full_url.endswith('/blobs/' + config_digest))
        with patch.object(p, 'urlopen', return_value=Response(content)):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                p.check_image(self.env['IMAGE_REPOSITORY'], self.env['IMAGE_DIGEST'], str(config))
        with patch.object(p, 'urlopen', side_effect=[Response(content), Response(b'{}')]):
            with self.assertRaisesRegex(ValueError, 'config digest does not match'):
                p.check_image(self.env['IMAGE_REPOSITORY'], digest, str(config))
        index = b'{"schemaVersion":2,"manifests":[]}'
        with patch.object(p, 'urlopen', return_value=Response(index)):
            with self.assertRaisesRegex(ValueError, 'single-platform'):
                p.check_image(self.env['IMAGE_REPOSITORY'], 'sha256:' + hashlib.sha256(index).hexdigest(), str(config))

    def test_registry_rejects_credentials_to_foreign_token_endpoint(self):
        config = self.directory / 'docker.json'
        config.write_text(json.dumps({'auths': {'harbor.core.com': {'auth': 'dXNlcjpwYXNz'}}}))
        error = HTTPError('http://harbor.core.com', 401, 'Unauthorized',
                          {'WWW-Authenticate': 'Bearer realm="https://evil.example/token"'}, None)
        with patch.object(p, 'urlopen', side_effect=error) as request:
            with self.assertRaisesRegex(ValueError, 'Unexpected Harbor token endpoint'):
                p.check_image(self.env['IMAGE_REPOSITORY'], self.env['IMAGE_DIGEST'], str(config))
            self.assertEqual(request.call_count, 1)

    def test_registry_404_identifies_failed_operation_and_image(self):
        config = self.directory / 'docker.json'
        config.write_text(json.dumps({'auths': {'harbor.core.com': {'auth': 'dXNlcjpwYXNz'}}}))
        manifest = b'{"config":{"digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}'
        digest = 'sha256:' + hashlib.sha256(manifest).hexdigest()

        class Response:
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return self.data

        challenge = HTTPError('http://harbor.core.com', 401, 'Unauthorized',
                              {'WWW-Authenticate': 'Bearer realm="http://harbor.core.com/service/token"'}, None)
        for operation, responses in (
                ('manifest fetch', []),
                ('manifest fetch', [challenge, Response(b'{"token":"test-token"}')]),
                ('token fetch', [challenge]),
                ('config blob fetch', [Response(manifest)])):
            with self.subTest(operation=operation, requests=len(responses)):
                missing = HTTPError('http://harbor.core.com', 404, 'Not Found', {}, None)
                with patch.object(p, 'urlopen', side_effect=responses + [missing]):
                    with self.assertRaises(ValueError) as raised:
                        p.check_image(self.env['IMAGE_REPOSITORY'], digest, str(config))
                message = str(raised.exception)
                self.assertIn(f'Harbor {operation} failed', message)
                self.assertIn(self.env['IMAGE_REPOSITORY'] + '@' + digest, message)
                self.assertIn('HTTP 404', message)
                self.assertEqual('build/image-ref.txt' in message, operation == 'manifest fetch')
                self.assertNotIn('dXNlcjpwYXNz', message)
                self.assertNotIn('test-token', message)

    def test_registry_bearer_authentication(self):
        config = self.directory / 'docker.json'
        config.write_text(json.dumps({'auths': {'harbor.core.com': {'auth': 'dXNlcjpwYXNz'}}}))
        config_bytes = json.dumps({'config': {'Labels': self.labels}}).encode()
        config_digest = 'sha256:' + hashlib.sha256(config_bytes).hexdigest()
        content = json.dumps({'schemaVersion': 2, 'config': {'digest': config_digest}}).encode()
        digest = 'sha256:' + hashlib.sha256(content).hexdigest()
        class Response:
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return self.data
        challenge = HTTPError('http://harbor.core.com', 401, 'Unauthorized',
                              {'WWW-Authenticate': 'Bearer realm="http://harbor.core.com/service/token",service="harbor-registry"'}, None)
        with patch.object(p, 'urlopen', side_effect=[challenge, Response(b'{"token":"test-token"}'), Response(content), Response(config_bytes)]) as request:
            self.assertEqual(p.check_image(self.env['IMAGE_REPOSITORY'], digest, str(config)), self.labels)
            self.assertEqual(request.call_count, 4)
            self.assertIn('scope=repository%3Alibrary%2Fcom.tencentcloud%2Fk8s-demo-app%3Apull',
                          request.call_args_list[1].args[0].full_url)
            self.assertEqual(request.call_args_list[2].args[0].get_header('Authorization'), 'Bearer test-token')
            self.assertEqual(request.call_args_list[3].args[0].get_header('Authorization'), 'Bearer test-token')


if __name__ == '__main__':
    unittest.main()

"""Prepare a GitOps promotion and publish it as a PR. Uses Python stdlib only."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


ENVIRONMENTS = ('dev', 'uat', 'preprod', 'prod')
ROOT = 'apps/k8s-demo-app/environments'
PLAN = Path('build/promotion-plan.json')
RESULT = Path('build/promotion-result.json')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authenticated requests must not forward credentials to redirects.
        return None


def urlopen(request, timeout):
    return build_opener(NoRedirect()).open(request, timeout=timeout)


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, data=None):
        request = Request('https://api.github.com/' + path, method=method,
                          data=None if data is None else json.dumps(data).encode(),
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Accept': 'application/vnd.github+json',
                                   'Content-Type': 'application/json',
                                   'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            # Do not dump response bodies, request headers, or credentials.
            raise ValueError(f'GitHub {method} {path}: HTTP {error.code}') from None

    def blob(self, repo, sha):
        value = self.request('GET', f'repos/{repo}/git/blobs/{sha}')
        return base64.b64decode(value['content']).decode()


def validate(env):
    target = env['TARGET_ENV']
    repository = env['IMAGE_REPOSITORY']
    digest = env['IMAGE_DIGEST']
    jira = env['JIRA']
    version = env['VERSION']
    if target not in ENVIRONMENTS:
        raise ValueError('Unsupported TARGET_ENV')
    if not re.fullmatch(r'harbor\.core\.com/[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)+', repository):
        raise ValueError('Invalid Harbor IMAGE_REPOSITORY')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise ValueError('Invalid IMAGE_DIGEST')
    if not re.fullmatch(r'[A-Z][A-Z0-9]*-[0-9]+', jira):
        raise ValueError('Invalid JIRA')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', version):
        raise ValueError('Invalid VERSION')
    return target, repository, digest, jira, version


def check_image(repository, digest, config_path='/registry-auth/config.json'):
    """Verify manifest and config digests, then return the image config labels."""
    registry, image_path = repository.split('/', 1)
    auths = json.loads(Path(config_path).read_text())['auths']
    config = next((auths[key] for key in (registry, 'http://' + registry, 'https://' + registry)
                   if key in auths), None)
    if not config or not config.get('auth'):
        raise ValueError('Harbor Docker config must contain an auth entry for ' + registry)
    authorization = 'Basic ' + config['auth']
    url = f'http://{registry}/v2/{image_path}/manifests/{digest}'
    headers = {'Accept': ', '.join(('application/vnd.oci.image.manifest.v1+json',
                                   'application/vnd.oci.image.index.v1+json',
                                   'application/vnd.docker.distribution.manifest.v2+json',
                                   'application/vnd.docker.distribution.manifest.list.v2+json'))}
    try:
        try:
            response = urlopen(Request(url, headers={**headers, 'Authorization': authorization}), timeout=30)
        except HTTPError as error:
            challenge = error.headers.get('WWW-Authenticate', '')
            if error.code != 401 or not challenge.lower().startswith('bearer '):
                raise
            fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
            realm = fields.get('realm', '')
            parsed = urlparse(realm)
            if parsed.hostname != registry or parsed.scheme not in ('http', 'https') or parsed.query:
                raise ValueError('Unexpected Harbor token endpoint')
            token_url = realm + '?' + urlencode({'service': fields.get('service', 'harbor-registry'),
                                                 'scope': f'repository:{image_path}:pull'})
            with urlopen(Request(token_url, headers={'Authorization': authorization}), timeout=30) as token_response:
                token_body = json.load(token_response)
            token = token_body.get('token') or token_body['access_token']
            authorization = 'Bearer ' + token
            response = urlopen(Request(url, headers={**headers, 'Authorization': authorization}), timeout=30)
        with response:
            manifest_bytes = response.read()
            actual = 'sha256:' + hashlib.sha256(manifest_bytes).hexdigest()
        if actual != digest:
            raise ValueError('Harbor manifest digest does not match IMAGE_DIGEST')
        manifest = json.loads(manifest_bytes)
        config_digest = manifest.get('config', {}).get('digest', '')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', config_digest):
            raise ValueError('Image must be a single-platform manifest with a sha256 config digest')
        config_url = f'http://{registry}/v2/{image_path}/blobs/{config_digest}'
        with urlopen(Request(config_url, headers={'Authorization': authorization}), timeout=30) as response:
            config_bytes = response.read()
        if 'sha256:' + hashlib.sha256(config_bytes).hexdigest() != config_digest:
            raise ValueError('Harbor image config digest does not match its manifest')
        return json.loads(config_bytes).get('config', {}).get('Labels') or {}
    except HTTPError as error:
        raise ValueError(f'Harbor manifest verification failed: HTTP {error.code}') from None


def json_text(value):
    return json.dumps(value, indent=2, sort_keys=True) + '\n'


def deployment(api, repo, files, prefix, target):
    path = prefix + '/manifests/deployment.json'
    item = files.get(path)
    if not item:
        raise ValueError(f'Missing {path}; merge the GitOps environment manifests first')
    document = json.loads(api.blob(repo, item['sha']))
    if (document.get('apiVersion') != 'apps/v1' or document.get('kind') != 'Deployment'
            or document.get('metadata', {}).get('name') != 'k8s-demo-app'):
        raise ValueError(f'{path} must define the k8s-demo-app Deployment')
    if document['metadata'].get('namespace', target) != target:
        raise ValueError(f'{path} namespace must match {target}')
    containers = document.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
    matches = [container for container in containers if container.get('name') == 'app']
    if len(matches) != 1:
        raise ValueError(f'{path} must contain exactly one app container')
    return path, document, matches[0]


def prepare(api, env, labels):
    target, repository, digest, jira, version = validate(env)
    if labels.get('org.opencontainers.image.version') != version:
        raise ValueError('VERSION does not match the image application metadata; rebuild images missing application labels')
    if jira not in labels.get('com.tencentcloud.image.jira', '').split(','):
        raise ValueError('JIRA does not match the image application metadata; rebuild images missing application labels')
    source = labels.get('org.opencontainers.image.revision', '')
    if not re.fullmatch(r'[a-f0-9]{40}', source):
        raise ValueError('Image application revision must be a full 40-character lowercase Git SHA')
    if labels.get('org.opencontainers.image.source') != f'https://github.com/{env["APP_REPO"]}.git':
        raise ValueError('Image source metadata must match APP_REPO HTTPS clone URL')
    repo = env['GITOPS_REPO']
    base_branch = env['GITOPS_BRANCH']
    base = api.request('GET', f'repos/{repo}/git/ref/heads/{quote(base_branch, safe="")}')['object']['sha']
    commit = api.request('GET', f'repos/{repo}/git/commits/{base}')
    tree = api.request('GET', f'repos/{repo}/git/trees/{commit["tree"]["sha"]}?recursive=1')
    if tree.get('truncated'):
        raise ValueError('GitOps tree is truncated; refusing incomplete promotion')
    files = {item['path']: item for item in tree['tree'] if item['type'] == 'blob'}
    source_info = api.request('GET', f'repos/{env["APP_REPO"]}/commits/{source}')
    if source_info['sha'] != source:
        raise ValueError('Image application revision could not be resolved exactly')
    prefix = f'{ROOT}/{target}'
    release = {'imageRepository': repository, 'imageDigest': digest, 'sourceCommit': source,
               'jira': jira, 'version': version}
    image = repository + '@' + digest
    if target != 'dev':
        previous = ENVIRONMENTS[ENVIRONMENTS.index(target) - 1]
        previous_prefix = f'{ROOT}/{previous}'
        marker = files.get(f'{previous_prefix}/release.json')
        if not marker:
            raise ValueError(f'No release in {previous}; promote there first')
        prior = json.loads(api.blob(repo, marker['sha']))
        if any(prior.get(key) != value for key, value in release.items()):
            raise ValueError(f'Selected image, Jira, version and application revision must match the release in {previous}')
        _, _, previous_container = deployment(api, repo, files, previous_prefix, previous)
        if previous_container.get('image') != image:
            raise ValueError(f'Deployment image in {previous} does not match its release metadata')

    path, document, container = deployment(api, repo, files, prefix, target)
    # Preserve every destination setting, including sidecar images and pull secrets.
    container['image'] = image
    application = {
        'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'Application',
        'metadata': {'name': 'k8s-demo-app-' + target, 'namespace': 'argocd'},
        'spec': {'project': 'business-apps',
                 'source': {'repoURL': f'https://github.com/{repo}.git', 'targetRevision': base_branch,
                            'path': prefix + '/manifests', 'directory': {'recurse': False}},
                 'destination': {'server': 'https://kubernetes.default.svc', 'namespace': target},
                 'syncPolicy': {'automated': {'prune': False, 'selfHeal': True}}}}
    entries = []
    for output_path, value in ((path, document), (f'{prefix}/release.json', release),
                               (f'argocd/applications/k8s-demo-app-{target}.yaml', application)):
        entries.append({'path': output_path, 'mode': '100644', 'type': 'blob', 'content': json_text(value)})
    return {'repository': repo, 'baseBranch': base_branch, 'baseCommit': base,
            'baseTree': commit['tree']['sha'], 'target': target, 'release': release,
            'branch': f'promotion/{target}-{digest[7:19]}-{uuid.uuid4().hex[:12]}',
            'buildUrl': env.get('BUILD_URL', ''), 'entries': entries}


def publish(api, plan):
    repo = plan['repository']
    current = api.request('GET', f'repos/{repo}/git/ref/heads/{quote(plan["baseBranch"], safe="")}')['object']['sha']
    if current != plan['baseCommit']:
        raise ValueError('GitOps base changed after preparation; rerun against the latest main')
    tree = api.request('POST', f'repos/{repo}/git/trees',
                       {'base_tree': plan['baseTree'], 'tree': plan['entries']})
    if tree['sha'] == plan['baseTree']:
        return {'status': 'unchanged', 'message': 'Target environment already declares this release'}
    title = (f'Promote k8s-demo-app to {plan["target"]}: {plan["release"]["jira"]} '
             f'{plan["release"]["version"]} {plan["release"]["imageDigest"][7:19]}')
    commit = api.request('POST', f'repos/{repo}/git/commits',
                         {'message': title, 'tree': tree['sha'], 'parents': [plan['baseCommit']]})
    api.request('POST', f'repos/{repo}/git/refs',
                {'ref': 'refs/heads/' + plan['branch'], 'sha': commit['sha']})
    release = plan['release']
    body = (f'Promote `{release["imageRepository"]}@{release["imageDigest"]}` to `{plan["target"]}`.\n\n'
            f'Jira: `{release["jira"]}`; version: `{release["version"]}`\n\n'
            f'Application commit: `{release["sourceCommit"]}`\n\n'
            f'Jenkins build: {plan["buildUrl"]}\n\n'
            'Before merging, verify the prior environment is healthy, required tests and '
            'acceptance have passed, and this candidate has not been superseded. '
            'This job checks the prior environment declaration, not its live deployment status.\n\n'
            'After merge, Argo CD reconciles the target. PR creation is not deployment success.')
    pr = api.request('POST', f'repos/{repo}/pulls',
                     {'title': title, 'head': plan['branch'], 'base': plan['baseBranch'], 'body': body})
    return {'status': 'pr_created', 'url': pr['html_url'], 'branch': plan['branch']}


def main():
    api = GitHub(os.environ['GITHUB_TOKEN'])
    if sys.argv[1:] == ['prepare']:
        validate(os.environ)
        labels = check_image(os.environ['IMAGE_REPOSITORY'], os.environ['IMAGE_DIGEST'])
        plan = prepare(api, os.environ, labels)
        PLAN.parent.mkdir(parents=True, exist_ok=True)
        PLAN.write_text(json_text(plan))
        print(f'Prepared {plan["target"]}: {plan["release"]["imageRepository"]}@{plan["release"]["imageDigest"]}')
    elif sys.argv[1:] == ['publish']:
        result = publish(api, json.loads(PLAN.read_text()))
        RESULT.write_text(json_text(result))
        print(json_text(result))
    else:
        raise ValueError('Usage: promotion.py prepare|publish')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        print(f'Promotion failed: {error}', file=sys.stderr)
        sys.exit(1)

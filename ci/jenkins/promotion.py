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
ROOT = 'apps'
SOURCE_ROOT = 'deploy/environments'
MANIFESTS = ('deployment.json', 'service.json')
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
    operation = 'manifest fetch'
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
            operation = 'token fetch'
            with urlopen(Request(token_url, headers={'Authorization': authorization}), timeout=30) as token_response:
                token_body = json.load(token_response)
            token = token_body.get('token') or token_body['access_token']
            authorization = 'Bearer ' + token
            operation = 'manifest fetch'
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
        operation = 'config blob fetch'
        with urlopen(Request(config_url, headers={'Authorization': authorization}), timeout=30) as response:
            config_bytes = response.read()
        if 'sha256:' + hashlib.sha256(config_bytes).hexdigest() != config_digest:
            raise ValueError('Harbor image config digest does not match its manifest')
        return json.loads(config_bytes).get('config', {}).get('Labels') or {}
    except HTTPError as error:
        message = f'Harbor {operation} failed for {repository}@{digest}: HTTP {error.code}'
        if error.code == 404 and operation == 'manifest fetch':
            message += ('. Verify IMAGE_REPOSITORY and IMAGE_DIGEST come from the same successful '
                        'build/image-ref.txt and build/image-digest.txt; remove only the tag from '
                        'image-ref.txt and preserve the full repository path, including Maven groupId. '
                        'Also check that the artifact has not been deleted from Harbor.')
        raise ValueError(message) from None


def json_text(value):
    return json.dumps(value, indent=2, sort_keys=True) + '\n'


def manifest(api, repo, files, path):
    item = files.get(path)
    if not item:
        raise ValueError(f'Missing {path} in {repo}; commit deployment configuration before building the image')
    if item.get('mode', '100644') != '100644':
        raise ValueError(f'{path} must be a regular non-executable file')
    document = json.loads(api.blob(repo, item['sha']))
    if (not isinstance(document, dict) or not isinstance(document.get('metadata'), dict)
            or not isinstance(document.get('spec'), dict)):
        raise ValueError(f'{path} must be a JSON object with metadata and spec objects')
    return document


def artifact_id(labels, repository):
    # The build pipeline writes Maven's effective artifactId to this image label.
    value = labels.get('org.opencontainers.image.title', '')
    if not isinstance(value, str) or not re.fullmatch(r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?', value):
        raise ValueError('Image artifactId (org.opencontainers.image.title) must be a Kubernetes Service name: 1-63 lowercase letters, digits or hyphens, starting with a letter and ending with a letter or digit')
    if repository.rsplit('/', 1)[-1] != value:
        raise ValueError('Image artifactId must match the final IMAGE_REPOSITORY component')
    return value


def render_artifact(value, artifact):
    """Substitute an explicit token in JSON values without executing templates."""
    if isinstance(value, str):
        return value.replace('${ARTIFACT_ID}', artifact)
    if isinstance(value, list):
        return [render_artifact(item, artifact) for item in value]
    if isinstance(value, dict):
        return {key: render_artifact(item, artifact) for key, item in value.items()}
    return value


def validate_deployment(document, path, target, artifact):
    if (document.get('apiVersion') != 'apps/v1' or document.get('kind') != 'Deployment'
            or document.get('metadata', {}).get('name') != artifact):
        raise ValueError(f'{path} must define the {artifact} Deployment; use ${{ARTIFACT_ID}} for its name')
    if document['metadata'].get('namespace', target) != target:
        raise ValueError(f'{path} namespace must match {target}')
    template = document['spec'].get('template', {})
    if not isinstance(template, dict) or not isinstance(template.get('spec'), dict):
        raise ValueError(f'{path} must contain a Pod template spec')
    containers = template['spec'].get('containers', [])
    if not isinstance(containers, list) or any(not isinstance(c, dict) for c in containers):
        raise ValueError(f'{path} containers must be an array of objects')
    matches = [container for container in containers if container.get('name') == 'app']
    if len(matches) != 1:
        raise ValueError(f'{path} must contain exactly one app container')
    return matches[0]


def source_manifests(api, repo, tree_sha, image, artifact):
    tree = api.request('GET', f'repos/{repo}/git/trees/{tree_sha}?recursive=1')
    if tree.get('truncated'):
        raise ValueError('Application tree is truncated; refusing incomplete promotion')
    files = {item['path']: item for item in tree['tree'] if item['type'] == 'blob'}
    bundle = {}
    for target in ENVIRONMENTS:
        prefix = f'{SOURCE_ROOT}/{target}/manifests/'
        expected = {prefix + name for name in MANIFESTS}
        unexpected = {item['path'] for item in tree['tree']
                      if item['path'].startswith(prefix) and item['path'] not in expected}
        if unexpected:
            raise ValueError(f'Unsupported deployment configuration: {sorted(unexpected)}; only Deployment and Service JSON files are supported')
        documents = {}
        for name in MANIFESTS:
            path = prefix + name
            document = render_artifact(manifest(api, repo, files, path), artifact)
            if name == 'deployment.json':
                validate_deployment(document, path, target, artifact)['image'] = image
            else:
                if (document.get('apiVersion') != 'v1' or document.get('kind') != 'Service'
                        or document.get('metadata', {}).get('name') != artifact):
                    raise ValueError(f'{path} must define the {artifact} Service; use ${{ARTIFACT_ID}} for its name')
                if document['metadata'].get('namespace', target) != target:
                    raise ValueError(f'{path} namespace must match {target}')
                if document.get('spec', {}).get('type', 'ClusterIP') != 'ClusterIP':
                    raise ValueError(f'{path} must use an internal ClusterIP Service')
            document['metadata']['namespace'] = target
            documents[name] = document
        bundle[target] = documents
    # One identity covers all environment configurations of the release, so
    # environment-specific replica/resource differences still promote together.
    digest = 'sha256:' + hashlib.sha256(json_text(bundle).encode()).hexdigest()
    return bundle, digest


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
    artifact = artifact_id(labels, repository)
    root = f'{ROOT}/{artifact}/environments'
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
    image = repository + '@' + digest
    bundle, config_digest = source_manifests(api, env['APP_REPO'], source_info['commit']['tree']['sha'], image, artifact)
    prefix = f'{root}/{target}'
    release = {'imageRepository': repository, 'imageDigest': digest, 'sourceCommit': source,
               'jira': jira, 'version': version, 'configDigest': config_digest, 'artifactId': artifact}
    if target != 'dev':
        previous = ENVIRONMENTS[ENVIRONMENTS.index(target) - 1]
        previous_prefix = f'{root}/{previous}'
        marker = files.get(f'{previous_prefix}/release.json')
        if not marker:
            raise ValueError(f'No release in {previous}; promote there first')
        prior = json.loads(api.blob(repo, marker['sha']))
        if any(prior.get(key) != value for key, value in release.items()):
            raise ValueError(f'Selected image, Jira, version, application revision and configuration must match the release in {previous}; re-promote there first if it uses the old release format')
        for name, expected in bundle[previous].items():
            path = f'{previous_prefix}/manifests/{name}'
            if manifest(api, repo, files, path) != expected:
                raise ValueError(f'{path} does not match its release metadata and source configuration')

    application = {
        'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'Application',
        'metadata': {'name': artifact + '-' + target, 'namespace': 'argocd'},
        'spec': {'project': 'business-apps',
                 'source': {'repoURL': f'https://github.com/{repo}.git', 'targetRevision': base_branch,
                            'path': prefix + '/manifests', 'directory': {'recurse': False}},
                 'destination': {'server': 'https://kubernetes.default.svc', 'namespace': target},
                 'syncPolicy': {'automated': {'prune': False, 'selfHeal': True}}}}
    entries = []
    outputs = [(f'{prefix}/manifests/{name}', value) for name, value in bundle[target].items()]
    outputs.extend(((f'{prefix}/release.json', release),
                    (f'argocd/applications/{artifact}-{target}.yaml', application)))
    for output_path, value in outputs:
        entries.append({'path': output_path, 'mode': '100644', 'type': 'blob', 'content': json_text(value)})
    return {'repository': repo, 'baseBranch': base_branch, 'baseCommit': base,
            'baseTree': commit['tree']['sha'], 'target': target, 'release': release,
            'branch': f'promotion/{artifact}/{target}-{digest[7:19]}-{uuid.uuid4().hex[:12]}',
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
    title = (f'Promote {plan["release"]["artifactId"]} to {plan["target"]}: {plan["release"]["jira"]} '
             f'{plan["release"]["version"]} {plan["release"]["imageDigest"][7:19]}')
    commit = api.request('POST', f'repos/{repo}/git/commits',
                         {'message': title, 'tree': tree['sha'], 'parents': [plan['baseCommit']]})
    api.request('POST', f'repos/{repo}/git/refs',
                {'ref': 'refs/heads/' + plan['branch'], 'sha': commit['sha']})
    release = plan['release']
    body = (f'Promote `{release["imageRepository"]}@{release["imageDigest"]}` to `{plan["target"]}`.\n\n'
            f'Jira: `{release["jira"]}`; version: `{release["version"]}`\n\n'
            f'Application commit: `{release["sourceCommit"]}`\n\n'
            f'Configuration bundle: `{release["configDigest"]}` (all environments, from the application commit).\n\n'
            'Deployment and Service are generated from app-owned configuration; review their changes together with the image.\n\n'
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

# tencentcloud-k8s-app

A standard Maven Java 11 HTTP sample application with no third-party runtime dependencies for validating container image builds, deployments, and health checks in a Kubernetes CI/CD pipeline. JUnit 5 is used for optional local tests.

## HTTP Endpoints

| Path | Purpose |
| --- | --- |
| `/` | Application response |
| `/healthz` | Kubernetes liveness probe |
| `/readyz` | Kubernetes readiness probe |

## Build and Test Locally

Requires JDK 11 or later and Maven 3.6.3 or later. Project coordinates are defined
in `pom.xml`: `com.tencentcloud:k8s-demo-app:1.0.0-SNAPSHOT`.

```sh
mvn -B -ntp -DskipTests clean package
java -jar target/k8s-demo-app-1.0.0-SNAPSHOT.jar
curl http://localhost:18080/healthz
```

The JAR filename follows Maven's default `<artifactId>-<version>.jar` convention.
To run tests explicitly, use `mvn test` or `./scripts/test.sh`.
`./scripts/build.sh` is a convenience wrapper for Maven packaging with tests
skipped. Sources use the standard `src/main/java` and `src/test/java` layout;
Maven writes compiled classes, test reports, and JARs under `target/`.

The application listens on port `18080` by default. Use the `PORT` environment variable to change the listening port.

## Build and Run the Container Image

```sh
mvn -B -ntp -DskipTests clean package
docker build -t k8s-demo-app:local .
docker run --rm -p 18080:18080 k8s-demo-app:local
curl http://localhost:18080/
```

The Dockerfile packages the JAR already built by Maven from `target/*.jar` into
a Java 11 runtime image. Run Maven before building the container image; Kaniko
does not recompile the application. The container runs as a non-root user with
UID `10001`.

## Jenkins: Build and Publish to Harbor

Configure a Multibranch Pipeline with script path `ci/jenkins/Jenkinsfile`.
The pipeline checks out the branch selected by Jenkins (including `JIRA-1`),
runs `mvn -B -ntp -DskipTests clean package` in the Maven container, reads
`project.groupId`, `project.artifactId` and `project.version` through Maven Help Plugin, then uses
Kaniko to package the resulting JAR and push the image. Maven resolves the
effective project values, including inherited values and property expressions.
Automated tests are temporarily skipped (`-DskipTests` still compiles test
sources). It stops after publishing; it does not deploy the application or
update the GitOps repository.

The registry is `harbor.core.com` over HTTP, without an explicit port. Set the
`HARBOR_PROJECT` build parameter to an existing Harbor project; the default is
`library`. The published image is
`harbor.core.com/<project>/<groupId>/<artifactId>:<version>-<branch>-<12-char-SHA>-<build-number>`.
For example: `harbor.core.com/library/com.tencentcloud.k8s.app/k8s-demo-app:1.0.0-SNAPSHOT-JIRA-1-abcdef123456-7`.
Unsupported branch tag characters are replaced with `-`,
and the branch portion is limited to 40 characters. The commit and build number
distinguish builds across branches and rebuilds. Unsupported artifact IDs or
versions fail before publishing rather than silently changing the Maven coordinates.
Change the artifact ID and version in each branch's `pom.xml`; no Jenkinsfile
image name change is needed.

The job archives the Maven JAR, resolved Maven coordinates, `build/image-ref.txt`,
and `build/image-digest.txt` after a successful push. Only configure this
publishing job to build trusted branches. Ensure Jenkins branch discovery
includes the branches you want to build, then configure repository webhooks or
periodic multibranch scanning. The infrastructure currently filters branches
with `jenkins_pipeline_branch_includes = "JIRA-*"`; include `main` there as well
if you want automatic builds of `main`.

Before running the job:

- Ensure Jenkins agents run in `cicd-build` with ServiceAccount `jenkins-agent`.
- Ensure agent Pods can resolve `harbor.core.com` and access its Ingress on port
  80. Harbor must use `externalURL: http://harbor.core.com` without HTTPS redirects.
- Create the Harbor project and a robot account with pull/push access to it.
- Provision `harbor-docker-config` in `cicd-build` as a
  `kubernetes.io/dockerconfigjson` Secret, with a `.dockerconfigjson` key. Its
  Docker auth configuration must contain credentials for `harbor.core.com`.
  The Pod template maps that key to `/kaniko/.docker/config.json` read-only.

For example, using an existing, securely stored Docker authentication file:

```sh
kubectl -n cicd-build create secret generic harbor-docker-config \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson=/secure/path/config.json
```

Keep authentication files outside the repository. HTTP sends credentials and
image traffic without encryption, so use this setup on a trusted test network.
Kaniko explicitly allows HTTP only for `harbor.core.com` via
`--insecure-registry`; no host Docker socket or Docker daemon is required for
the build. Base images still come from the registries specified in the Dockerfile.

## Deploy to Kubernetes

Deployments are managed through Jenkins promotion and Argo CD. Environment
Deployment and Service source manifests are maintained alongside the code in
`deploy/environments/<environment>/manifests/`. Promotion generates the target
environment files in
[`tencentcloud-k8s-gitops`](https://github.com/KaloSora/tencentcloud-k8s-gitops)
and opens a PR; Argo CD continues to watch that repository.

### Jenkins promotion parameters and Harbor 404 errors

Configure the promotion job with `ci/jenkins/Jenkinsfile.promotion`. From a
single successful build, copy `build/image-ref.txt` into `IMAGE_REPOSITORY`
and remove its final `:<tag>` suffix; copy `build/image-digest.txt` into
`IMAGE_DIGEST`. Preserve the full repository path, including Maven groupId.
The repository parameter defaults to `harbor.core.com/library/com.tencentcloud.k8s.app/`;
append the image name, for example `k8s-demo-app`. Adjust the prefix if the
successful build used different Maven coordinates or a different Harbor project.
Use the same build's
Maven version and a Jira key recorded in its image labels for `VERSION` and `JIRA`.

For the current `pom.xml` and the default Harbor project, the repository is
`harbor.core.com/library/com.tencentcloud.k8s.app/k8s-demo-app`. Use the
successful build artifacts as the source of truth for any existing image.
A manifest HTTP 404 means the requested repository/digest could not be fetched;
check the paired artifacts and whether Harbor still retains the image.
The diagnostic identifies manifest, token, or config blob requests separately.

### Application-owned deployment configuration

Maintain these files in the application repository for each of `dev`, `uat`,
`preprod` and `prod`:

```text
deploy/environments/<environment>/manifests/deployment.json
deploy/environments/<environment>/manifests/service.json
```

Commit code and deployment changes together before building the image. Promotion
verifies the image metadata, resolves its full `sourceCommit` on GitHub, and reads
all four environments from that exact commit's tree. It never uses the current
application branch or the trusted pipeline checkout as the configuration source,
and it never executes application-revision scripts. A missing configuration is
an error; there is no fallback to GitOps or a newer branch.

The supported files are exactly `deployment.json` (`apps/v1` Deployment) and
`service.json` (`v1` internal ClusterIP Service), both named `k8s-demo-app`.
Other files inside the manifests directories, symlinks and executable files are
rejected. These checks validate the supported structure, not the complete
Kubernetes API schema. The Deployment must contain exactly one container named
`app`; sidecars and other settings are copied from the source configuration.
Namespaces may be omitted or match the environment; promotion sets them
explicitly. The `app` image is always replaced with the verified
`repository@sha256:...`; source placeholder images are never deployed.

Promotion atomically creates or replaces **only the target environment's**
Deployment, Service, `release.json` and Argo CD Application through a GitOps PR.
There is no need to seed the target's manifests manually. Edit replicas,
resources, probes and other application configuration here, not in the generated
GitOps files: a later promotion replaces destination settings with the source
configuration. Argo CD projects, namespace lifecycle and credentials retain
platform ownership; do not put secrets in these JSON files.

`release.json` records the image, source commit, Jira key, version and
`configDigest`. The latter is a SHA-256 of the canonical rendered configuration
bundle for **all four environments**, including the resolved image and namespaces.
This common identity allows each environment to have different replica counts or
resources while ensuring the whole release is pinned. The resulting Git tree,
not just the image digest, determines whether a promotion is unchanged.

Promote in order `dev → uat → preprod → prod`. Higher environments require the
same release identity in the preceding environment, and its Deployment and
Service must still match that commit's rendered configuration. Jenkins checks
Git declarations; verify the actual environment's health before continuing.

### First promotion and migration

1. Merge the pipeline changes into the trusted branch used by the promotion job.
   Give its GitHub credential read access to the application repository in addition
   to its existing GitOps write/PR access.
2. Include `deploy/` in the application branch being built (currently `JIRA-*`),
   commit it and build a **new image**. Images whose source commit predates these
   files cannot use the new promotion flow. Configuration-only changes also need
   a new build so that the image metadata points to the new source commit.
3. Ensure Terraform has created the four namespaces, provision Harbor pull
   credentials in each, and retain the existing Argo CD bootstrap and
   `business-apps` project in GitOps.
4. Promote to `dev`, review the generated manifests and merge the GitOps PR.
   Then continue through the environments in order after checking deployment health.

Existing GitOps manifests can remain until each environment is promoted. Their
configuration is replaced by the app-owned files for that environment. Existing
release records without `configDigest` must be replaced by a new `dev` promotion
and subsequent promotions in order. Existing unreleased Argo CD Applications
should not be manually synced before their first real promotion. Promotion
enables automatic sync (`prune: false`, `selfHeal: true`) for its target only.

Run the Python regression tests with:

```sh
python3 -m unittest discover -s test -p 'test_*.py'
```

### Terraform-managed promotion credentials

The infrastructure repository accepts `TF_VAR_gitops_credentials_id` (default
`gitops-github-token`) and the required sensitive `TF_VAR_gitops_github_token`.
The token must be nonempty and contain no whitespace. With Jenkins enabled,
Terraform creates a Secret in the Jenkins controller namespace and
JCasC creates a Jenkins **Secret text** credential. JCasC exposes its ID through
`JENKINS_GITOPS_CREDENTIALS_ID`; `ci/jenkins/Jenkinsfile.promotion` uses that ID,
falling back to `gitops-github-token` for manually configured installations.
Keep this Jenkinsfile on the branch used by the promotion job up to date.
The agent Pod template needs no token or credential ID configuration: the
pipeline binds `GITHUB_TOKEN` only inside `withCredentials` blocks.

The token remains in Terraform state/saved plans, so protect those files. Token
rotation restarts the controller to refresh its Secret-backed environment.

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
Deployment and Service manifests are maintained in the
[`tencentcloud-k8s-gitops`](https://github.com/KaloSora/tencentcloud-k8s-gitops)
repository.

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

### Initialize environment configuration in GitOps

Application-specific Deployment and Service files are maintained in
`tencentcloud-k8s-gitops` under
`apps/k8s-demo-app/environments/<environment>/manifests/` for `dev`, `uat`,
`preprod` and `prod`. Each environment maps to the namespace of the same name.
Review and merge these baseline files into the GitOps repository's `main`
branch before running promotion. The application repository does not own
initialization templates, and the promotion job has no initialization mode.

Initial Argo CD Applications omit automatic sync because the baseline uses an
`:unreleased` placeholder image. Do not manually sync them before the first
promotion. Run promotion to `dev` using a real image from a successful build;
merging that PR replaces the placeholder with its verified digest, writes
`release.json`, and enables automatic sync for the target environment.
Continue in order `dev → uat → preprod → prod`. Ensure Terraform has created
the namespaces and provision Harbor pull credentials in each namespace.
Promotion preserves existing Deployment settings and Service configuration.

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

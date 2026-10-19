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
`project.artifactId` and `project.version` through Maven Help Plugin, then uses
Kaniko to package the resulting JAR and push the image. Maven resolves the
effective project values, including inherited values and property expressions.
Automated tests are temporarily skipped (`-DskipTests` still compiles test
sources). It stops after publishing; it does not deploy the application or
update the GitOps repository.

The registry is `harbor.core.com` over HTTP, without an explicit port. Set the
`HARBOR_PROJECT` build parameter to an existing Harbor project; the default is
`library`. The published image is
`harbor.core.com/<project>/<artifactId>:<version>-<branch>-<12-char-SHA>-<build-number>`.
For example: `harbor.core.com/library/k8s-demo-app:1.0.0-SNAPSHOT-jira-1-abcdef123456-7`.
Branch names are lowercased, unsupported tag characters are replaced with `-`,
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

### Using Helm

The version-controlled chart is located in `charts/k8s-demo-app`. Requires Helm 3 or later and access to the target cluster.

After building and pushing the image, validate the chart and deploy with the same image tag:

```sh
IMAGE_REPOSITORY=registry.example.com/team/k8s-demo-app
# Use the published tag from the Jenkins build/image-ref.txt artifact.
IMAGE_TAG=1.0.0-SNAPSHOT-jira-1-abcdef123456-7

helm lint ./charts/k8s-demo-app
helm template k8s-demo-app ./charts/k8s-demo-app \
  --set-string image.repository="${IMAGE_REPOSITORY}" \
  --set-string image.tag="${IMAGE_TAG}"

helm upgrade --install k8s-demo-app ./charts/k8s-demo-app \
  --namespace demo --create-namespace \
  --set-string image.repository="${IMAGE_REPOSITORY}" \
  --set-string image.tag="${IMAGE_TAG}" \
  --wait --timeout 3m
```

Replace the example tag with one that exists in your registry. The image must be accessible to cluster nodes. For private registries, create an image pull secret in the target namespace and reference it through `imagePullSecrets` in your values file.

Configure replicas, ports, and resource limits in `values.yaml`, or supply environment-specific overrides using `-f values-dev.yaml`. CI should reuse the chart and override the image tag for each build. To publish the chart as a separate artifact, run `helm package ./charts/k8s-demo-app --destination build/charts` and increment `Chart.yaml`'s `version` whenever the chart changes.

To check the application after installation:

```sh
kubectl -n demo port-forward service/k8s-demo-app 18080:80
# In another terminal:
curl http://localhost:18080/healthz
```

Use a separate release name or namespace when testing alongside an existing deployment. Helm does not automatically adopt resources previously created with `kubectl apply`.

### Using Plain Kubernetes Manifests

After the pipeline builds and pushes the image, replace the image in the manifest with the actual registry address and an immutable tag:

```sh
sed "s|image: k8s-demo-app:latest|image: registry.example.com/team/k8s-demo-app:${GIT_COMMIT}|" \
  k8s/deployment.yaml | kubectl apply -f -
kubectl rollout status deployment/k8s-demo-app
```

Typical pipeline steps:

1. Package with Maven, read the artifact ID and version, and build the container image.
2. Push the image (the current Jenkins pipeline stops here).
3. Replace the image in `k8s/deployment.yaml` and run `kubectl apply`.
4. Run `kubectl rollout status deployment/k8s-demo-app`.

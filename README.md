# tencentcloud-k8s-app

A Java 11 HTTP sample application with no third-party dependencies for validating tests, container image builds, deployments, and health checks in a Kubernetes CI/CD pipeline.

## HTTP Endpoints

| Path | Purpose |
| --- | --- |
| `/` | Application response |
| `/healthz` | Kubernetes liveness probe |
| `/readyz` | Kubernetes readiness probe |

## Build and Test Locally

Requires JDK 11 or later:

```sh
./scripts/test.sh
java -jar build/app.jar
curl http://localhost:18080/healthz
```

The application listens on port `18080` by default. Use the `PORT` environment variable to change the listening port.

## Build and Run the Container Image

```sh
docker build -t k8s-demo-app:local .
docker run --rm -p 18080:18080 k8s-demo-app:local
curl http://localhost:18080/
```

The image uses a multi-stage build and runs as a non-root user with UID `10001`.

## Deploy to Kubernetes

### Using Helm

The version-controlled chart is located in `charts/k8s-demo-app`. Requires Helm 3 or later and access to the target cluster.

After building and pushing the image, validate the chart and deploy with the same image tag:

```sh
IMAGE_REPOSITORY=registry.example.com/team/k8s-demo-app
IMAGE_TAG="${GIT_COMMIT}"

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

For local use, replace `${GIT_COMMIT}` with a tag that exists in your registry. The image must be accessible to cluster nodes. For private registries, create an image pull secret in the target namespace and reference it through `imagePullSecrets` in your values file.

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

1. `./scripts/test.sh`
2. `docker build -t <registry>/k8s-demo-app:<commit> .`
3. Push the image.
4. Replace the image in `k8s/deployment.yaml` and run `kubectl apply`.
5. Run `kubectl rollout status deployment/k8s-demo-app`.

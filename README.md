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
curl http://localhost:8080/healthz
```

Use the `PORT` environment variable to change the listening port.

## Build and Run the Container Image

```sh
docker build -t k8s-demo-app:local .
docker run --rm -p 8080:8080 k8s-demo-app:local
curl http://localhost:8080/
```

The image uses a multi-stage build and runs as a non-root user with UID `10001`.

## Deploy to Kubernetes

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

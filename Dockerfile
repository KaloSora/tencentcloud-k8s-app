FROM eclipse-temurin:11-jdk-alpine AS builder

WORKDIR /workspace
COPY src/main/java ./src/main/java
COPY scripts/build.sh ./scripts/build.sh
RUN chmod +x ./scripts/build.sh && ./scripts/build.sh

FROM eclipse-temurin:11-jre-alpine

RUN addgroup -S app && adduser -S -G app -u 10001 app
WORKDIR /app
COPY --from=builder --chown=app:app /workspace/build/app.jar ./app.jar

USER app
EXPOSE 18080
ENV PORT=18080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["java", "-jar", "/app/app.jar"]

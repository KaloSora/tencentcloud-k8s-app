FROM eclipse-temurin:11-jre-alpine

RUN addgroup -S app && adduser -S -G app -u 10001 app
WORKDIR /app
# Maven produces a single application JAR before the container build.
COPY --chown=app:app target/*.jar ./app.jar

USER app
EXPOSE 18080
ENV PORT=18080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["java", "-jar", "/app/app.jar"]

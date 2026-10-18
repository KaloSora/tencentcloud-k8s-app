package com.tencentcloud.k8sdemo;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;

/**
 * A dependency-free HTTP application for exercising a Kubernetes CI/CD pipeline.
 */
public final class App {
    private App() {
    }

    public static void main(String[] args) throws IOException {
        int port = readPort();
        HttpServer server = start(port);
        Runtime.getRuntime().addShutdownHook(new Thread(() -> server.stop(1)));
        System.out.printf("k8s-demo-app listening on port %d%n", port);
    }

    public static HttpServer start(int port) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/", exchange -> {
            if (!"/".equals(exchange.getRequestURI().getPath())) {
                respond(exchange, 404, "{\"status\":\"not_found\"}");
                return;
            }
            respond(exchange, 200, "{\"app\":\"k8s-demo-app\",\"message\":\"Hello from Kubernetes!\"}");
        });
        server.createContext("/healthz", exchange ->
                respond(exchange, 200, "{\"status\":\"ok\"}"));
        server.createContext("/readyz", exchange ->
                respond(exchange, 200, "{\"status\":\"ready\"}"));
        server.start();
        return server;
    }

    private static int readPort() {
        String value = System.getenv().getOrDefault("PORT", "8080");
        try {
            int port = Integer.parseInt(value);
            if (port < 1 || port > 65535) {
                throw new IllegalArgumentException("PORT must be between 1 and 65535");
            }
            return port;
        } catch (NumberFormatException exception) {
            throw new IllegalArgumentException("PORT must be a number", exception);
        }
    }

    private static void respond(HttpExchange exchange, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.getResponseHeaders().set("Cache-Control", "no-store");
        exchange.sendResponseHeaders(status, bytes.length);
        try {
            exchange.getResponseBody().write(bytes);
        } finally {
            exchange.close();
        }
    }
}

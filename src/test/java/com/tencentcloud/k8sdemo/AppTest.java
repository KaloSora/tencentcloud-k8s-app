package com.tencentcloud.k8sdemo;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.stream.Collectors;

/** Verifies the sample application's HTTP endpoints through Maven Surefire. */
public final class AppTest {
    @Test
    void endpointsReturnExpectedResponses() throws Exception {
        HttpServer server = App.start(0);
        int port = server.getAddress().getPort();
        try {
            assertResponse(port, "/", 200, "k8s-demo-app");
            assertResponse(port, "/healthz", 200, "\"status\":\"ok\"");
            assertResponse(port, "/readyz", 200, "\"status\":\"ready\"");
            assertResponse(port, "/missing", 404, "not_found");
            System.out.println("All application tests passed.");
        } finally {
            server.stop(0);
        }
    }

    private static void assertResponse(int port, String path, int expectedStatus, String expectedBody)
            throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(
                "http://127.0.0.1:" + port + path).openConnection();
        connection.setConnectTimeout(2_000);
        connection.setReadTimeout(2_000);

        int status = connection.getResponseCode();
        if (status != expectedStatus) {
            throw new AssertionError(path + ": expected HTTP " + expectedStatus + ", got " + status);
        }

        try (BufferedReader reader = new BufferedReader(new InputStreamReader(
                status >= 400 ? connection.getErrorStream() : connection.getInputStream(),
                StandardCharsets.UTF_8))) {
            String body = reader.lines().collect(Collectors.joining("\n"));
            if (!body.contains(expectedBody)) {
                throw new AssertionError(path + ": unexpected body: " + body);
            }
        } finally {
            connection.disconnect();
        }
    }
}

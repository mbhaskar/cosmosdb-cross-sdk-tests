package com.azure.cosmos.testrunner;

import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.Map;

/**
 * CLI entry point for the Java test runner.
 *
 * Reads a job JSON on stdin (or --input file) and writes the result JSON to
 * stdout (or --output file). Logs go to stderr so stdout stays clean JSON.
 *
 * Job schema: { "scenario": {...}, "config": {...}, "sdk_version": "4.63.0" }
 */
public class Main {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    @SuppressWarnings("unchecked")
    public static void main(String[] args) throws IOException {
        String inputPath = null;
        String outputPath = null;
        for (int i = 0; i < args.length - 1; i++) {
            if ("--input".equals(args[i])) inputPath = args[i + 1];
            if ("--output".equals(args[i])) outputPath = args[i + 1];
        }

        String raw = inputPath != null
                ? new String(Files.readAllBytes(Paths.get(inputPath)), StandardCharsets.UTF_8)
                : new String(System.in.readAllBytes(), StandardCharsets.UTF_8);

        Map<String, Object> job = MAPPER.readValue(raw, Map.class);
        Map<String, Object> scenario = (Map<String, Object>) job.get("scenario");
        Map<String, Object> config = (Map<String, Object>) job.getOrDefault("config", new java.util.LinkedHashMap<>());
        String sdkVersion = String.valueOf(job.getOrDefault("sdk_version", "unknown"));
        // Surface the requested SDK source (published|local) to the executor.
        config.put("sdk_source", String.valueOf(job.getOrDefault("sdk_source", "published")));

        ScenarioExecutor executor = new ScenarioExecutor(
                scenario, config, sdkVersion, line -> System.err.println(line));
        Map<String, Object> result = executor.run();

        String out = MAPPER.writerWithDefaultPrettyPrinter().writeValueAsString(result);
        if (outputPath != null) {
            Files.write(Paths.get(outputPath), out.getBytes(StandardCharsets.UTF_8));
        } else {
            System.out.println(out);
        }
        // azure-cosmos may keep non-daemon threads alive; force a clean exit.
        System.exit(0);
    }
}

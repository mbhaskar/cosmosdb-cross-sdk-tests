package com.azure.cosmos.testrunner;

import java.util.List;
import java.util.Map;

/** Common interface implemented by the mock and real-SDK backends. */
public interface Backend {
    Metrics metrics();

    OpResult createClient(String connectionMode);

    OpResult createDatabase(String dbId, boolean createIfNotExists);

    OpResult createContainer(String dbId, String containerId, String partitionKey, boolean createIfNotExists);

    OpResult createItem(String dbId, String containerId, Map<String, Object> item);

    OpResult readItem(String dbId, String containerId, String itemId, Object partitionKey);

    OpResult replaceItem(String dbId, String containerId, String itemId, Object partitionKey, Map<String, Object> item);

    OpResult upsertItem(String dbId, String containerId, Map<String, Object> item);

    OpResult deleteItem(String dbId, String containerId, String itemId, Object partitionKey);

    OpResult queryItems(String dbId, String containerId, String query,
                        List<Map<String, Object>> parameters, Object partitionKey, boolean crossPartition);

    OpResult deleteDatabase(String dbId);
}

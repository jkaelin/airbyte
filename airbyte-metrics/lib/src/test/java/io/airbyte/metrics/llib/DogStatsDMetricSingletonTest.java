/*
 * Copyright (c) 2021 Airbyte, Inc., all rights reserved.
 */

package io.airbyte.metrics.llib;

import io.airbyte.metrics.lib.AirbyteMetricsRegistry;
import io.airbyte.metrics.lib.DatadogClientConfiguration;
import io.airbyte.metrics.lib.DogStatsDMetricSingleton;
import io.airbyte.metrics.lib.MetricEmittingApps;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

public class DogStatsDMetricSingletonTest {

  @AfterEach
  void tearDown() {
    DogStatsDMetricSingleton.flush();
  }

  @Test
  @DisplayName("there should be no exception if we attempt to emit metrics while publish is false")
  public void testPublishTrueNoEmitError() {
    Assertions.assertDoesNotThrow(() -> {
      DogStatsDMetricSingleton.initialize(MetricEmittingApps.WORKER, new DatadogClientConfiguration("localhost", "1000", false));
      DogStatsDMetricSingleton.gauge(AirbyteMetricsRegistry.KUBE_POD_PROCESS_CREATE_TIME_MILLISECS, 1);
    });
  }

  @Test
  @DisplayName("there should be no exception if we attempt to emit metrics while publish is true")
  public void testPublishFalseNoEmitError() {
    Assertions.assertDoesNotThrow(() -> {
      DogStatsDMetricSingleton.initialize(MetricEmittingApps.WORKER, new DatadogClientConfiguration("localhost", "1000", true));
      DogStatsDMetricSingleton.gauge(AirbyteMetricsRegistry.KUBE_POD_PROCESS_CREATE_TIME_MILLISECS, 1);
    });
  }

  @Test
  @DisplayName("there should be no exception if we attempt to emit metrics without initializing")
  public void testNoInitializeNoEmitError() {
    Assertions.assertDoesNotThrow(() -> {
      DogStatsDMetricSingleton.gauge(AirbyteMetricsRegistry.KUBE_POD_PROCESS_CREATE_TIME_MILLISECS, 1);
    });
  }

}

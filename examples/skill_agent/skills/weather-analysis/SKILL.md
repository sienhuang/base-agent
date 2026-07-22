---
name: weather-analysis
version: 1.0.0
description: Verify weather data before presenting a concise answer.
argument-hint: "[city]"
allowed-tools:
  - get_weather
required-tools:
  - get_weather
required-permissions:
  - weather:read
---

# Weather analysis

1. Extract the requested city.
2. Call `get_weather` exactly once.
3. State that the answer is based on the verified Tool result.

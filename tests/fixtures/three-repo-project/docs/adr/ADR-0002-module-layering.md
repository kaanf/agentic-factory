---
schemaVersion: factory.adr/v1
id: ADR-0002
title: Layered modules in the service
status: accepted
revision: 1
research: [docs/adr/research/ADR-0002-options.md]
critique: [docs/adr/research/ADR-0002-critique.md]
constraints:
  - id: ADR-0002-C1
    description: Web layer depends only on the domain layer.
  - id: ADR-0002-C2
    description: Domain types do not carry persistence annotations.
---

# ADR-0002: Module layering

Decision text.

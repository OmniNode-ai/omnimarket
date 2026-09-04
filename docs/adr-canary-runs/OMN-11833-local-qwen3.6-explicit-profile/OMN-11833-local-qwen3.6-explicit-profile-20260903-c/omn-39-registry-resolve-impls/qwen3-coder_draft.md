# ADR: The ComponentRegistry.resolveImplementations() method will be invoked synchronously within the useMemo hook during RegistryProvider construction to ensure components are ready by the first render.

**Status**: Proposed
**Date**: 2026-09-03
**Related**: a6b8c3bcebaa85b3cb1700ff12b5ab3751e1c6a944060d9f7a8a2854cdcaf538
**Extraction Model**: qwen3-coder
**Confidence**: 0.95
**Canary Run ID**: OMN-11833-local-qwen3.6-explicit-profile-20260903-c
**Run ID**: OMN-11833-local-qwen3.6-explicit-profile-20260903-c

## Context

- The current implementation is synchronous under the hood, so the registry is ready by first render.
- Add a `void r.resolveImplementations()` call inside the existing `useMemo` in `src/registry/RegistryProvider.tsx`.
- The current implementation is synchronous under the hood, so the registry is ready by first render.

## Decision

**The ComponentRegistry.resolveImplementations() method will be invoked synchronously within the useMemo hook during RegistryProvider construction to ensure components are ready by the first render.** (type: ARCHITECTURE)

## Consequences


## Source Evidence

- a6b8c3bcebaa85b3cb1700ff12b5ab3751e1c6a944060d9f7a8a2854cdcaf538

## Extraction Metadata

- pipeline_version: adr-canary-bus-adapter-v1
- model_id: qwen3-coder
- confidence: 0.95
- timestamp: 2026-09-03T17:38:55.335161+00:00
- prompt_template_id: adr_decision_extraction_v1
- prompt_template_version: 1.0.0

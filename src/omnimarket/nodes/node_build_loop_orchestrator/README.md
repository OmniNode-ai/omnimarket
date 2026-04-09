# Build Loop Orchestrator Node

This node orchestrates the build loop process, coordinating build phases and managing execution lifecycle.

## Overview
The Build Loop Orchestrator manages the execution flow of build processes, handling command intents, dispatching tasks, and aggregating results.

## Integration
1. **Configuration**: Provide `LiveRunnerConfig` for build environment setup
2. **Initiation**: Send `OrchestratorStartCommand` to begin the build loop
3. **Phase Handling**: React to `PhaseCommandIntent` for build phase instructions
4. **Completion**: Process `OrchestratorCompletedEvent` for final results
5. **Monitoring**: Use `DispatchMetrics` and `DispatchTrace` for observability

## Key Components
- `OrchestratorStartCommand`: Initiates the build loop process
- `PhaseCommandIntent`: Manages individual build phase instructions
- `LiveRunnerConfig`: Configuration for build environment
- `OrchestratorCompletedEvent`: Signals build loop completion
- `DispatchMetrics`: Performance metrics and KPIs
- `DispatchTrace`: Detailed execution tracing
- `OrchestratorState`: Current orchestrator status
- `LoopCycleSummary`: Summary of build loop iteration

## Usage Example
```python
from omnimarket.nodes.node_build_loop_orchestrator.models import (
    OrchestratorStartCommand,
    LiveRunnerConfig
)

# Configure and start build loop
config = LiveRunnerConfig(concurrency=4, timeout=300)
command = OrchestratorStartCommand(build_id="B67890", config=config)
# Send command to orchestrator handler
```

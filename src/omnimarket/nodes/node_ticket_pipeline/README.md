# Ticket Pipeline Node

This node manages the processing pipeline for tickets in the omnimarket system.

## Overview
The Ticket Pipeline Node handles the lifecycle of ticket processing through distinct phases, providing event-driven updates and maintaining state throughout the process.

The current bounded market-owned slice wires:

- `PRE_FLIGHT`: deterministic input validation with no side effects.
- `IMPLEMENT`: compile-only dispatch-worker prompt generation via `node_dispatch_worker`; no agent spawn, TaskCreate call, deploy, or runtime mutation.

Later side-effect phases stop explicitly as `blocked/not_implemented` until they are wired.

## Integration
1. **Initiation**: Send a `PipelineStartCommand` to begin processing
2. **Event Handling**: Subscribe to `PipelinePhaseEvent` for phase-by-phase updates
3. **Completion**: Listen for `PipelineCompletedEvent` to finalize processing
4. **State Management**: Query `PipelineState` for current processing status

## Key Components
- `PipelineStartCommand`: Initiates the ticket processing pipeline
- `PipelinePhaseEvent`: Emitted during each processing phase
- `PipelineCompletedEvent`: Signals successful pipeline completion
- `PipelineState`: Maintains current pipeline status

## Usage Example
```python
from omnimarket.nodes.node_ticket_pipeline.models import PipelineStartCommand

# Initialize pipeline
command = PipelineStartCommand(ticket_id="T12345", payload={...})
# Send command to pipeline handler
```

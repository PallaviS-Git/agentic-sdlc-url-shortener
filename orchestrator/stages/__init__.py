"""
Shared SDLC stage package.

Concrete stage implementations for the three assignment scenarios live in
``orchestrator.scenarios`` (greenfield, brownfield, ambiguous) so each
scenario remains a self-contained, reviewable module.

This package is reserved for stages that are shared across scenarios.
Import scenario stages from ``orchestrator.scenarios.<name>`` directly.
"""

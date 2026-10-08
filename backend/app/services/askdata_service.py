"""Compatibility imports for the former AskData service module."""

from .dataflow_service import DataFlowService

AskDataService = DataFlowService

__all__ = ["DataFlowService", "AskDataService"]

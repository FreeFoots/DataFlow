"""Compatibility imports for the standalone request understanding agent."""
from .querying.request_understanding_agent import (
    PreparedRequest, RequestUnderstandingAgent, RetrievalIntent, RouteName, ResponseType,
)

RequestPreprocessor = RequestUnderstandingAgent

__all__ = ["RequestPreprocessor", "RequestUnderstandingAgent", "PreparedRequest",
           "RetrievalIntent", "RouteName", "ResponseType"]

"""Provider adapters behind one interface. LiteLLM is imported only when a call is made."""

from workflowval.models.base import Message, Model, ModelError, Reply, ToolCall, ToolSpec, Usage
from workflowval.models.fake import FakeModel, FakeScriptExhausted, call
from workflowval.models.litellm_adapter import LiteLLMModel

__all__ = [
    "FakeModel",
    "FakeScriptExhausted",
    "LiteLLMModel",
    "Message",
    "Model",
    "ModelError",
    "Reply",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "call",
]

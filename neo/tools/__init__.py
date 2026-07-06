"""Domain modules for the Neo toolbox.

Each module contributes one mixin of tool implementations; the Toolbox in
neo.services.tools composes them behind the registry. Modules hold behavior
only; path safety, dispatch, and the tool catalog stay in the composition
layer.
"""

from .base import ToolResult, ToolboxHelpers

__all__ = ["ToolResult", "ToolboxHelpers"]

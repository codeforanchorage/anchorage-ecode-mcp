"""Core interfaces and data models for OpenContext plugins.

This module defines the abstract base classes and data models that all plugins
must implement. The core framework is universal and never modified by governments.
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PluginType(str, Enum):
    """Types of plugins supported by OpenContext."""

    OPEN_DATA = "open_data"
    CUSTOM_API = "custom_api"
    DATABASE = "database"
    ANALYTICS = "analytics"


class ToolInputError(ValueError):
    """Raised when a tool rejects the caller's arguments.

    A marker, not a behaviour change: it exists so the outer handler can
    tell "the caller asked for something invalid" from "this server
    broke", and log the first at WARNING with no traceback. A traceback
    is a claim that the server failed; spending one on "you forgot guid"
    is what makes real faults hard to find.

    Deliberately NOT inferred from ValueError. json.JSONDecodeError
    subclasses ValueError, so a malformed eCode360 payload would be
    misfiled as a caller mistake and silently lose its stack trace.
    Upstream faults stay plain ValueError/RuntimeError and keep theirs.

    Subclasses ValueError so every existing `except ValueError` keeps
    working, which makes converting a raise site mechanical and safe.
    """


class InvalidToolParamsError(ValueError):
    """Raised when a tools/call request is itself malformed.

    Covers the cases the MCP tools spec calls "requests that fail to
    satisfy the CallToolRequest schema" -- a missing tool name, or
    `arguments` that is not an object. Mapped to JSON-RPC -32602
    ("Invalid params"): the request never described a valid call, so it
    is neither a server fault (-32603) nor a tool execution error.

    Subclasses ValueError for the same reason ToolInputError does.
    """


class UnknownToolError(ValueError):
    """Raised when tools/call names a tool this server does not expose.

    Mapped to JSON-RPC -32602 with the message shape the MCP tools spec
    uses ("Unknown tool: <name>") rather than the generic -32603
    "Internal error": naming a missing tool is a caller mistake, not a
    server fault. The available-tool list travels in `data` so a model
    can self-correct instead of just being told no.

    Subclasses ValueError deliberately: it is an argument problem, and
    existing `except ValueError` handlers around execute_tool keep
    working unchanged.
    """

    def __init__(self, tool_name: str, available: str = "") -> None:
        self.tool_name = tool_name
        self.available = available
        super().__init__(f"Unknown tool: {tool_name}")


class ToolDefinition(BaseModel):
    """Definition of an MCP tool provided by a plugin."""

    name: str = Field(..., description="Tool name (without plugin prefix)")
    title: Optional[str] = Field(
        default=None,
        description=(
            "Optional human-readable display name. Clients resolve a tool's "
            "display name as title -> annotations.title -> name, so this is "
            "what users see in a tool picker while `name` stays the stable "
            "programmatic identifier."
        ),
    )
    description: str = Field(..., description="Human-readable tool description")
    input_schema: Dict[str, Any] = Field(
        ..., description="JSON Schema for tool input parameters"
    )
    output_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional JSON Schema for the tool's structured result. "
            "Declaring one is a commitment: the MCP spec requires that "
            "structured results conform to it, and clients may validate. "
            "Only set this for tools that return structured_content."
        ),
    )
    annotations: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional MCP tool annotations (e.g. readOnlyHint, openWorldHint) "
            "that hint at a tool's behavior to clients."
        ),
    )


class ToolResult(BaseModel):
    """Result of executing a tool."""

    content: List[Dict[str, Any]] = Field(
        default_factory=list, description="Tool output content"
    )
    structured_content: Optional[Any] = Field(
        default=None,
        description=(
            "Optional machine-readable result, emitted as the MCP "
            "`structuredContent` field. Must conform to the tool's "
            "output_schema when one is declared. The human-readable "
            "`content` is still sent alongside it."
        ),
    )
    success: bool = Field(..., description="Whether the tool execution succeeded")
    error_message: Optional[str] = Field(
        None, description="Error message if execution failed"
    )


class MCPPlugin(ABC):
    """Abstract base class for all OpenContext plugins.

    All plugins must inherit from this class and implement all required methods.
    Plugins are discovered automatically and loaded by the Plugin Manager.
    """

    # Class attributes that must be set by plugin implementations
    plugin_name: str = ""
    plugin_type: PluginType = PluginType.CUSTOM_API
    plugin_version: str = "1.0.0"

    def __init__(self, config: Dict[str, Any]) -> None:
        """Initialize plugin with configuration.

        Args:
            config: Plugin-specific configuration dictionary from config.yaml
        """
        self.config = config
        self._initialized = False

    @abstractmethod
    async def initialize(self) -> bool:
        """Initialize the plugin and verify it can connect to its data source.

        This method should:
        - Create HTTP clients, database connections, etc.
        - Test connectivity to the data source
        - Validate configuration
        - Set self._initialized = True on success

        Returns:
            True if initialization succeeded, False otherwise

        Raises:
            Exception: If initialization fails critically
        """
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """Clean up plugin resources.

        This method should:
        - Close HTTP clients
        - Close database connections
        - Release any other resources
        - Set self._initialized = False
        """
        pass

    @abstractmethod
    def get_tools(self) -> List[ToolDefinition]:
        """Get list of tools provided by this plugin.

        Tool names should NOT include the plugin prefix (e.g., use "search_datasets"
        not "ckan__search_datasets"). The Plugin Manager will add the prefix automatically
        using double underscores (e.g., "ckan__search_datasets").

        Returns:
            List of tool definitions
        """
        pass

    @abstractmethod
    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        """Execute a tool by name.

        Args:
            tool_name: Name of the tool (without plugin prefix)
            arguments: Tool input arguments

        Returns:
            ToolResult with content, success flag, and optional error message
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the plugin is healthy and can reach its data source.

        Returns:
            True if healthy, False otherwise
        """
        pass

    @property
    def is_initialized(self) -> bool:
        """Check if plugin has been successfully initialized."""
        return self._initialized


class DataPlugin(MCPPlugin):
    """Extended interface for data source plugins.

    This interface provides common data operations that most open data plugins
    will implement. Plugins can inherit from this instead of MCPPlugin directly
    if they provide dataset search and query capabilities.
    """

    @abstractmethod
    async def search_datasets(
        self, query: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search for datasets matching a query.

        Args:
            query: Search query string
            limit: Maximum number of results to return

        Returns:
            List of dataset metadata dictionaries
        """
        pass

    @abstractmethod
    async def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        """Get detailed metadata for a specific dataset.

        Args:
            dataset_id: Unique identifier for the dataset

        Returns:
            Dataset metadata dictionary
        """
        pass

    @abstractmethod
    async def query_data(
        self,
        resource_id: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Query data from a specific resource/dataset.

        Args:
            resource_id: Unique identifier for the resource
            filters: Optional filters to apply to the query
            limit: Maximum number of records to return

        Returns:
            List of data records
        """
        pass

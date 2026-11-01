"""Tools primitive — tool system with ABC interface and built-in tools."""

from __future__ import annotations

from ghost.tools._stubs import BaseTool, ToolExecutor, ToolSpec

# Import built-in tools to trigger @ToolRegistry.register() decorators.
# Each is wrapped in try/except so the package loads even before the
# individual tool modules are created.
try:
    import ghost.tools.calculator  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.think  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.retrieval  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.llm_tool  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.file_read  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.web_search  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.weather  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.fx_macro  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.code_interpreter  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.code_interpreter_docker  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.repl  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.storage_tools  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.mcp_adapter  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.channel_tools  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.http_request  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.docker_shell_exec  # noqa: F401
    import ghost.tools.shell_exec  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.memory_manage  # noqa: F401
except ImportError:
    pass
try:
    import ghost.tools.user_profile_manage  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.skill_manage  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.file_write  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.apply_patch  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.git_tool  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.db_query  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.pdf_tool  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.image_tool  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.audio_tool  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.knowledge_tools  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.text_to_speech  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.digest_collect  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.scan_chunks  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.knowledge_sql  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.apple_calendar  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.knowledge_search  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.browser  # noqa: F401
    import ghost.tools.browser_axtree  # noqa: F401
except ImportError:
    pass

try:
    import ghost.tools.proactive_tools  # noqa: F401
except ImportError:
    pass

__all__ = ["BaseTool", "ToolExecutor", "ToolSpec"]

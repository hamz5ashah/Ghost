"""Data source connectors for Deep Research."""

from ghost.connectors._stubs import (
    Attachment,
    BaseConnector,
    Document,
    SyncStatus,
)
from ghost.connectors.store import KnowledgeStore

__all__ = ["Attachment", "BaseConnector", "Document", "KnowledgeStore", "SyncStatus"]

# Auto-register built-in connectors
import ghost.connectors.obsidian  # noqa: F401

try:
    import ghost.connectors.gmail  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.gmail_imap  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.gdrive  # noqa: F401
except ImportError:
    pass  # httpx may not be installed

try:
    import ghost.connectors.notion  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.granola  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.gcontacts  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.imessage  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.apple_notes  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.apple_music  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.apple_contacts  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.apple_calendar  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.slack_connector  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.outlook  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.imap  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.gcalendar  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.dropbox  # noqa: F401
except ImportError:
    pass  # httpx may not be installed

try:
    import ghost.connectors.whatsapp  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.oura  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.apple_health  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.strava  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.spotify  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.google_tasks  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.weather  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.github_notifications  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.hackernews  # noqa: F401
except ImportError:
    pass

try:
    import ghost.connectors.news_rss  # noqa: F401
except ImportError:
    pass

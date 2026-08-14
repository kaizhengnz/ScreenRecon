"""Vision provider implementations.

Each provider is one file that defines a class conforming to
:class:`screenrecon.vision.Provider`, plus any provider-specific helpers
(SDK exception translation, message-format conversion). Importing this
package registers every provider with the dispatcher via
:func:`screenrecon.vision.register`.

Adding a provider: create ``providers/<name>.py`` with a ``Provider`` class,
then import + register it here. Only vision.py needs to know that this
package exists.
"""

from __future__ import annotations

from ..vision import register
from .anthropic import AnthropicProvider

register(AnthropicProvider())

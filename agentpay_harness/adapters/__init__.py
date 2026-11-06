"""Adapters — plug-in interfaces for connecting AI agents to the harness.

An *adapter* translates between the harness's internal event model and the
specific protocol that a given AI agent framework uses.  Each adapter is a
subclass of :class:`BaseAdapter` and must implement the ``send_task`` and
``receive_actions`` coroutines.
"""

from __future__ import annotations

__all__: list[str] = []

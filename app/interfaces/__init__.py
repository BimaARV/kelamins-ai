"""User-facing interfaces (Phase 6). Interactive Telegram bot via long-polling.

Telegram only for now; Discord joins later. The alert-path delivery lives in
``app/alert_engine/channels.py``; this package is the interactive counterpart
(commands + natural-language chat routed through the KELA AI gateway).
"""

__version__ = "0.1.0"
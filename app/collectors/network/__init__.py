"""Network monitoring collectors.

FACT RULE (spec section 10): UP/DOWN/timeout status comes ONLY from actual
checks (ping/TCP/HTTP/DNS/SNMP). The AI layer may interpret an incident, but it
never decides a target's status.
"""

from app.collectors.network.checks import run_check

__all__ = ["run_check"]
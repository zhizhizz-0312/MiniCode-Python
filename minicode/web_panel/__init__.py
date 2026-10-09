"""A local web panel over the four stages.

Module map:

* ``api``    -- views assembled from existing artefacts, plus the two write actions
* ``render`` -- one hand-written page (no framework, no external resources)
* ``server`` -- localhost-only, token-gated HTTP, read-only by default

The constraints are consequences of decisions made earlier in the project.  The
repository has no runtime dependencies, so the page is inline HTML/CSS/JS served by
``http.server`` rather than a front-end build.  And the text the panel displays -- draft
bodies, skill descriptions -- is untrusted content, so it is escaped on the way out and
never fed back to the model: a panel that echoed untrusted text into an agent prompt
would be a new injection channel, which is the thing stage two exists to prevent.
"""

from __future__ import annotations

from minicode.web_panel.server import LOOPBACK, PanelServer, serve

__all__ = ["LOOPBACK", "PanelServer", "serve"]

"""The planner's code index as it runs in its container, I: Serena behind a relay that owns the
socket the planner's wrapper connects to, and the provisioning run that fills I's store before.

Everything here runs in I, which is taken to be compromised by the repository from its start, so
none of it is trusted for what it does there; it is vivibox's code only in that the host mounts
it read-only, and it makes I work rather than keep anything out. Standard library only (the
provisioning run imports Serena, in Serena's own interpreter), and nothing of vivibox's outside
this package: the host's vivibox is not in I.

`serena` is what both runs share: where Serena and its language servers keep their state in I's
store, and the configuration of Serena's that vivibox writes there at each start, never the
project's. `relay` is the steady I's program; `provision` the provisioning run's."""

# Vendored hgraph core

This package is the Horizon-owned copy of the graph storage, analysis, sync,
dashboard data shaping, text rendering, and command-line implementation from
<https://github.com/AxelDlv00/hgraph>.

Imported revision: `f30b15e8822388085779d6417a83a2e8a07a2473`
(2026-07-27, “Improve sync warning coverage”).

Horizon intentionally does not retain an external `hgraph` dependency. Update
this directory by manually porting reviewed changes from that repository. The
standalone hgraph workspace manifest, web server, and static site are not
included: Horizon owns workspace selection and the dashboard UI, while
`horizon graph` exposes the project graph operations.

Intentional Horizon adaptations are kept during updates: the CLI is mounted
under `horizon graph`, and the dashboard data retains semantic `group` values
used by Horizon's graph view even though standalone hgraph no longer renders
that axis.

Both projects are licensed under Apache-2.0; Horizon's root `LICENSE` applies.

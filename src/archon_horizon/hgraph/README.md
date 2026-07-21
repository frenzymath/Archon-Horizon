# Vendored hgraph core

This package is the Horizon-owned copy of the graph storage, analysis, sync,
dashboard data shaping, text rendering, and command-line implementation from
<https://github.com/AxelDlv00/hgraph>.

Imported revision: `0678df4d86978872181c9f601befe1fad876615e`
(2026-07-20, “Improve graph metadata and project documentation”).

Horizon intentionally does not retain an external `hgraph` dependency. Update
this directory by manually porting reviewed changes from that repository. The
separate hgraph web server/static site are not included: Horizon's dashboard is
the UI, and `horizon graph` exposes the project graph operations.

Both projects are licensed under Apache-2.0; Horizon's root `LICENSE` applies.

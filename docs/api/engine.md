# Engine: `stint.engine`

An [`Engine`](#stint.engine.Engine) holds a configured client + dialect. Lifecycle: **create → use → close**. Engines are async context managers and expose `reflect()` and `detect()` (confirm the dialect matches the live server).

There are two engines because the two dialect surfaces differ: `Engine` serves the company-managed surface, while `TmpEngine` serves the experimental team-managed dialect and is deliberately a separate type.

::: stint.engine

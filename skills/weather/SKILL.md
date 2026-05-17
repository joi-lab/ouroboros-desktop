---
name: weather
description: Live weather widget — looks up current conditions for any city via wttr.in (no API key).
version: 0.2.1
type: extension
entry: plugin.py
permissions: [net, tool, route, widget]
env_from_settings: []
when_to_use: User asks about weather, temperature, forecast, or current conditions in a specific city.
ui_tab:
  tab_id: widget
  title: Weather widget
  icon: cloud
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: form
        route: forecast
        method: GET
        target: result
        submit_label: Refresh
        fields:
          - name: city
            label: City
            type: text
            default: Moscow
            required: true
      - type: status
        target: result
        idle: Enter a city and press Refresh.
        loading: Loading...
        error: Weather lookup failed.
        success: Latest conditions
      - type: kv
        target: result
        fields:
          - label: City
            path: resolved_to
          - label: Temperature
            path: temp_c
          - label: Feels like
            path: feels_like_c
          - label: Condition
            path: condition
          - label: Humidity
            path: humidity_pct
          - label: Wind speed
            path: wind_kph
          - label: Wind direction
            path: wind_dir
---

# Weather skill (visual widget reference)

This is the v5 reference ``type: extension`` skill shipped with Ouroboros.
It demonstrates a small declarative widget extension:

- A manifest declaring ``type: extension`` + an ``ui_tab`` block so the
  top-level Widgets page knows how to render the weather card separately
  from the Installed skills catalogue.
- An ``entry: plugin.py`` that registers one HTTP route
  (``GET /api/extensions/weather/forecast?city=…``), one agent-callable
  tool (``ext_9_r_weather_fetch``), and one UI-tab declaration. Every
  registration goes through the frozen
  :class:`ouroboros.contracts.plugin_api.PluginAPI` v1 surface — the
  extension never touches ``logging``, ``starlette``, or the dispatcher
  directly.
- Four minimum permissions, one per registered surface:
  - ``net`` — the route + tool fetch ``wttr.in``.
  - ``tool`` — required by ``register_tool``.
  - ``route`` — required by ``register_route``.
  - ``widget`` — required by ``register_ui_tab``.

## Using the widget

1. **Enable**: open Skills → Installed, find ``weather``,
   click ``Enable`` (the extension auto-loads after a fresh review).
2. **View**: open the top-level Widgets page. Type a city name and the
   widget refreshes live — no agent message, no shell command.
3. **Agent use**: the same skill is callable from the agent surface as
   ``ext_9_r_weather_fetch(city="...")``. The output is identical JSON.

## Network policy

The widget contacts a single host (``wttr.in``) — the route handler
explicitly refuses any URL whose hostname is not on the allowlist, and
the operator's ``permissions: [net]`` declaration scopes that to the
review-time threat model.

## Data plane

No persistent state is written; the widget fetches on-demand and the
result is rendered directly. The extension's
:meth:`PluginAPI.get_state_dir` is unused. Skills that do manage
per-request assets should use ``api.skill_job_dir(job_id)`` so generated
files live under isolated ``jobs/<sanitized_id>-<hash>/{assets,output,tmp}``
folders.

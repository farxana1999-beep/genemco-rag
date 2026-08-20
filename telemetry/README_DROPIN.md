# Genemco telemetry schema drop-in

When Gerald sends the pre-made Pydantic schema (SOW Section 2):

1. Save it here as `genemco_schema.py`
2. It must expose a Pydantic model named `TelemetrySchema`
   (rename their class or add: `TelemetrySchema = TheirClassName`)
3. Put their test payloads in `data/telemetry_payloads/*.json`
4. Verify: `python -c "from telemetry.schema_hook import load_test_payloads; print(len(load_test_payloads()))"`

Until then, the built-in Frick Quantum HD-style default (`core/schemas.py:TelemetryAlarm`)
is used. Remember to log integration time via `log_adaptability_hours()` — 5-hour cap.

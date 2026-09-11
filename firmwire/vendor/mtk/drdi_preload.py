"""Mount point for the external DRDI experiment; not a bundled adapter.

The isolated Docker test overlays its readback harness here. Normal boot never
imports this module. Do not enable FIRMWIRE_DRDI_PRELOAD without that overlay.
"""
raise RuntimeError("DRDI experiment requires the explicitly mounted test adapter")

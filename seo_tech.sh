#!/bin/bash
# Cron wrapper: run the deterministic SEO Tech Sweep with the Hermes venv Python
# (needs yaml + googleapiclient). Registered as a --no-agent script cron.
exec ~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/seo_tech.py "$@"

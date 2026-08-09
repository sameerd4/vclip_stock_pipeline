#!/usr/bin/env python3
"""Backward-compatible launcher for the new Stockify command."""

from vclip_pipeline.cli import stockify_entry

raise SystemExit(stockify_entry())

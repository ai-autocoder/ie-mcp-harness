"""Capture of the page's network traffic and the redaction that protects it.

``redact`` imports neither ``config`` nor ``session`` (``session`` builds the process's
``REDACTOR`` from it), so there is no import cycle.
"""

"""CCAI call-log tagging service for the DataPlatform AI Engineer appliance.

SharePoint (CCAI_SHAREPOINT_SITE / CCAI_SHAREPOINT_FOLDER) → download →
parse (CCAI Insights JSON · plain text · WebVTT · CSV manifest) → tag against
the 104-leaf contact-reason taxonomy (LLM via the gateway, or the offline
TF-IDF tagger) → Postgres schema `ccai` → self-service portal at /ccai/.
"""

__version__ = "1.0.0"

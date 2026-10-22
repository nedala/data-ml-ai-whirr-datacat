"""Dispute Desk: shared billing-disputes inbox → structured case → SAP BRIM.

    poll (bsp_connectors.m365) → extract (rules | LLM/VLM via bsp_common.llm)
    → validate (warehouse raw.*) → review queue → submit (bsp_connectors.sap_brim:
    OData API or Playwright in parallel visible browser windows)
"""

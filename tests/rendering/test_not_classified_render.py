"""When the SDS answered from states "not classified", the undetermined lines quote it.

The backend keeps `insufficient_hazard_data: true` (no hazard basis to answer from) and adds
`sds_not_classified`. Saying "we have no hazard data on file" would then be false, so the three
undetermined renderings quote the sheet instead, while still saying it is not a low-hazard finding.
"""
import asyncio

import server
from msds_mcp.disclosures import _insufficient_lines
from msds_mcp.route_tools import _handling_block

NC = {"quote": "Not a hazardous substance or mixture.", "supplier": "Example Supplier",
      "revision_date": "2026-01-01"}


def test_generic_undetermined_lines_quote_the_sheet():
    out = "\n".join(_insufficient_lines({"insufficient_hazard_data": True, "sds_not_classified": NC}, "Storage"))
    assert "Not a hazardous substance or mixture." in out and "Example Supplier" in out
    assert "NOT the same as harmless" in out and "NOT a low-hazard finding" in out
    assert "no SDS hazard data on file" not in out
    plain = "\n".join(_insufficient_lines({"insufficient_hazard_data": True}, "Storage"))
    assert "no SDS hazard data on file" in plain, "without the field the old sentence stays"


def test_ppe_tool_quotes_the_sheet_and_still_gives_no_level():
    async def _fake(chemicals, lang=None, suppliers=None):
        return {"results": [{"chemical_name": "Sodium sulfate", "cas": "7647-01-0", "signal_word": "",
                             "h_codes": [], "ppe": {}, "minimum_ppe_level": None,
                             "insufficient_hazard_data": True, "sds_not_classified": NC}],
                "unresolved": [], "documents": []}
    orig = server._direct_ppe
    server._direct_ppe = _fake
    try:
        res = asyncio.run(server.get_ppe_recommendation(["sodium sulfate"]))
    finally:
        server._direct_ppe = orig
    out = res.content[0].text
    assert "CANNOT BE DETERMINED" in out and "Not a hazardous substance or mixture." in out
    assert "contains no hazard data" not in out and "**None**" not in out


def test_route_handling_quotes_the_sheet():
    out = "\n".join(_handling_block({"ppe": {"results": [
        {"chemical_name": "Sodium sulfate", "minimum_ppe_level": None, "insufficient_hazard_data": True,
         "sds_not_classified": NC}]}}))
    assert "CANNOT BE DETERMINED" in out and "Not a hazardous substance or mixture." in out
    assert "no hazard data on this record" not in out

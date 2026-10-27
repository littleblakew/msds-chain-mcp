"""CI-1444: `validate_sds` rendering red lines, and the `msds_id` hand-off from upload.

The backend guarantees the data; the template is where a quality check turns into a
false assurance:
- `notice` first and verbatim (no issue found ≠ compliant);
- every issue keeps its quoted original lines;
- skipped checks are said out loud (they did not look, so they found nothing).
"""
import asyncio

import httpx
import pytest

import server

_NOTICE = "Automated checks. Finding no issue does not certify the SDS as compliant."


def _issue(**kw):
    base = {"code": "signal_word_vs_hazards", "category": "contradiction", "sections": [2],
            "evidence": ["Signal word WARNING", "H314 Causes severe skin burns"],
            "message": "Section 2 prints Warning but lists a Danger-only hazard."}
    base.update(kw)
    return base


def _doc(msds_id=7, issues=None, skipped=None, **kw):
    out = {"document": {"msds_id": msds_id, "chemical_name": "2-Bromo acid",
                        "supplier": "Matrix", "revision_date": "2018-10-15",
                        "revision_date_from_original": True, "uploaded_at": None},
           "issues": [] if issues is None else issues,
           "checks_run": ["check_signal_word"], "checks_skipped": skipped or []}
    out.update(kw)
    return out


def test_one_document_red_lines():
    data = {"notice": _NOTICE, "jurisdiction": "US",
            **_doc(issues=[_issue()], skipped=["check_flash_point_vs_flammable_class"])}
    text = server._format_sds_quality(data, "en")

    body = text.split("\n", 1)[1]
    assert body.lstrip().startswith(f"> {_NOTICE}"), "notice must come first, verbatim"
    assert "**[Contradiction]** §2" in text
    assert '"Signal word WARNING"' in text and '"H314 Causes severe skin burns"' in text, (
        "the quoted original lines are the finding's whole evidence; they must reach the user")
    assert "flash point vs flammable class" in text, "skipped checks must be named"
    assert "`msds_id`: 7" in text


def test_no_issue_is_never_called_compliant():
    text = server._format_sds_quality({"notice": _NOTICE, **_doc()}, "en")

    assert server._SDS_QUALITY_STRINGS["en"]["no_issues"] in text
    own_lines = text.replace(_NOTICE, "")
    assert "compliant" not in own_lines.replace("does not mean the SDS is compliant", "")


def test_newer_version_is_mentioned():
    data = _doc(newer_version={"canonical_id": 9, "revision_date": "2024-02-01",
                               "supplier": "Matrix"})
    assert "2024-02-01 (Matrix)" in server._format_sds_quality(data, "en")


def test_session_form_summary_and_truncation():
    data = {"notice": _NOTICE, "jurisdiction": "US",
            "documents": [_doc(1, [_issue()]), _doc(2)],
            "summary": {"documents": 2, "with_contradictions": 1, "with_issues": 1},
            "truncated": True}
    text = server._format_sds_quality(data, "en")

    assert "2 SDS checked · 1 with a contradiction · 1 with any issue" in text
    assert "`msds_id`: 1" in text and "`msds_id`: 2" in text
    assert server._SDS_QUALITY_STRINGS["en"]["truncated"] in text


def test_session_with_no_uploads_says_so():
    text = server._format_sds_quality(
        {"notice": _NOTICE, "documents": [], "summary": {"documents": 0},
         "truncated": False}, "en")
    assert server._SDS_QUALITY_STRINGS["en"]["empty"] in text


def test_labels_follow_the_language():
    text = server._format_sds_quality({"notice": "n", **_doc(issues=[_issue()])}, "ja")
    assert text.startswith(f"**{server._SDS_QUALITY_STRINGS['ja']['title']}**")
    assert "**[矛盾]**" in text


def test_every_language_has_every_label():
    keys = set(server._SDS_QUALITY_STRINGS["en"])
    for lang in server._CATALOG_LANGS:
        assert set(server._SDS_QUALITY_STRINGS[lang]) == keys, lang


class _Recorder:
    """httpx.AsyncClient stand-in: records the request, answers with `status`/`body`."""
    calls: list = []
    status = 200
    body: dict = {}

    def __init__(self, *a, timeout=None, **kw):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, **kw):
        type(self).calls.append((url, json, self.timeout))
        return httpx.Response(type(self).status, json=type(self).body,
                              request=httpx.Request("POST", url))


@pytest.fixture
def recorder(monkeypatch):
    from request_identity import get_caller_credential, set_caller_credential
    prev = get_caller_credential()
    set_caller_credential("sk-msds-test")
    _Recorder.calls, _Recorder.status, _Recorder.body = [], 200, {}
    monkeypatch.setattr(server.httpx, "AsyncClient", _Recorder)
    yield _Recorder
    set_caller_credential(prev)


def test_with_msds_id_checks_that_one_document(recorder):
    recorder.body = {"notice": _NOTICE, "jurisdiction": "US", **_doc(42)}
    res = asyncio.run(server.validate_sds("S-1", 42))

    url, body, timeout = recorder.calls[0]
    assert url.endswith("/api/v2/validate-sds")
    assert body["session_id"] == "S-1" and body["msds_id"] == 42
    assert body["jurisdiction"] == "US"
    assert timeout == server.TIMEOUT
    assert "`msds_id`: 42" in res.content[0].text
    assert res.structured_content["document"]["msds_id"] == 42


def test_without_msds_id_checks_the_whole_session(recorder):
    recorder.body = {"notice": _NOTICE, "documents": [], "summary": {"documents": 0},
                     "truncated": False}
    asyncio.run(server.validate_sds("S-1"))

    url, body, timeout = recorder.calls[0]
    assert url.endswith("/api/v2/validate-sds/session")
    assert "msds_id" not in body
    assert timeout == server.TIMEOUT_MULTI


@pytest.mark.parametrize("status", [403, 404])
def test_not_yours_and_missing_read_the_same(recorder, status):
    recorder.status, recorder.body = status, {"detail": "SDS not found in this session"}
    res = asyncio.run(server.validate_sds("S-1", 3))

    assert not getattr(res, "isError", False)
    assert res.content[0].text == server._SDS_QUALITY_STRINGS["en"]["not_found"]


def test_without_a_credential_nothing_is_called(monkeypatch):
    from request_identity import get_caller_credential, set_caller_credential
    prev = get_caller_credential()
    set_caller_credential(None)
    _Recorder.calls = []
    monkeypatch.setattr(server.httpx, "AsyncClient", _Recorder)
    monkeypatch.delenv("MSDS_API_KEY", raising=False)
    monkeypatch.setattr(server, "API_KEY", "", raising=False)
    try:
        res = asyncio.run(server.validate_sds("S-1", 3))
    finally:
        set_caller_credential(prev)
    assert "Authentication required" in res.content[0].text
    # The call log still posts (that is `_reported`'s job); the backend check must not.
    assert not [c for c in _Recorder.calls if "/validate-sds" in c[0]]


def _upload(monkeypatch, results):
    """Run `upload_msds_pdf` against a canned backend: session create + upload."""
    from request_identity import get_caller_credential, set_caller_credential

    class _Client(_Recorder):
        async def get(self, url, **kw):
            return httpx.Response(200, content=b"%PDF-1.4 x",
                                  request=httpx.Request("GET", url))

        async def post(self, url, json=None, headers=None, files=None, **kw):
            body = ({"results": results, "summary": {"success": len(results)}}
                    if url.endswith("/upload") else {"session_id": "S-9"})
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    prev = get_caller_credential()
    set_caller_credential("sk-msds-test")
    monkeypatch.setattr(server.httpx, "AsyncClient", _Client)
    try:
        return asyncio.run(server.upload_msds_pdf("https://example.com/a.pdf"))
    finally:
        set_caller_credential(prev)


def test_upload_prints_msds_id_and_points_at_validate_sds(monkeypatch):
    res = _upload(monkeypatch, [{"status": "success", "chemical_name": "Acetone",
                                 "cas_number": "67-64-1", "msds_id": 501}])
    text = res.content[0].text

    assert "msds_id: 501" in text
    assert 'validate_sds(session_id="S-9"' in text
    assert res.structured_content["results"][0]["msds_id"] == 501


def test_upload_without_a_stored_record_does_not_offer_validation(monkeypatch):
    res = _upload(monkeypatch, [{"status": "failed", "chemical_name": None,
                                 "fail_reason": "not an SDS", "msds_id": None}])
    text = res.content[0].text

    assert "msds_id" not in text
    assert "validate_sds" not in text

# Adversarial TDD: test scenarios designed by Hermes (GPT-5.4), adapted &
# verified by Claude. Covers the OAuth 2.1 provider in oauth.py.
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

import oauth
from oauth import oauth_app, validate_bearer_token, _verify_pkce, _clients, _auth_codes, _tokens


@pytest.fixture(autouse=True)
def clear_oauth_stores():
    _clients.clear()
    _auth_codes.clear()
    _tokens.clear()
    yield
    _clients.clear()
    _auth_codes.clear()
    _tokens.clear()


@pytest.fixture()
def client():
    with TestClient(oauth_app) as test_client:
        yield test_client


@pytest.fixture()
def pkce_pair():
    verifier = oauth.secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def register_client(client, redirect_uri="https://client.example/callback", client_name="Test Client"):
    response = client.post(
        "/oauth/register",
        json={"client_name": client_name, "redirect_uris": [redirect_uri]},
    )
    assert response.status_code == 201
    return response.json()


def authorize_code(
    client,
    *,
    client_id,
    redirect_uri,
    api_key="sk-msds-test",
    code_challenge="",
    code_challenge_method="S256",
    state=None,
):
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
    }
    if state is not None:
        params["state"] = state

    response = client.post(
        "/oauth/authorize",
        params=params,
        data={"api_key": api_key},
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = response.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert parsed.scheme and parsed.netloc
    assert "code" in query
    return query["code"][0], response, parsed, query


def exchange_code(client, *, code, client_id, code_verifier):
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
    )


def perform_authorization_code_flow(client, verifier, challenge, method="S256"):
    redirect_uri = "https://client.example/callback"
    registration = register_client(client, redirect_uri=redirect_uri)
    client_id = registration["client_id"]
    code, _, _, _ = authorize_code(
        client,
        client_id=client_id,
        redirect_uri=redirect_uri,
        api_key="sk-msds-live",
        code_challenge=challenge,
        code_challenge_method=method,
        state="opaque-state",
    )
    token_response = exchange_code(
        client,
        code=code,
        client_id=client_id,
        code_verifier=verifier,
    )
    return registration, code, token_response


def test_metadata_discovery_returns_expected_oauth_server_metadata(client):
    response = client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    payload = response.json()
    expected_issuer = oauth.ISSUER_URL.rstrip("/")

    assert payload["issuer"] == expected_issuer
    assert payload["authorization_endpoint"] == f"{expected_issuer}/oauth/authorize"
    assert payload["token_endpoint"] == f"{expected_issuer}/oauth/token"
    assert payload["registration_endpoint"] == f"{expected_issuer}/oauth/register"
    assert payload["response_types_supported"] == ["code"]
    assert payload["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert payload["token_endpoint_auth_methods_supported"] == ["none"]
    assert payload["scopes_supported"] == ["chemical-safety"]
    assert "S256" in payload["code_challenge_methods_supported"]
    assert "plain" in payload["code_challenge_methods_supported"]


def test_dynamic_client_registration_returns_created_client(client):
    redirect_uris = ["https://client.example/callback"]

    response = client.post(
        "/oauth/register",
        json={"client_name": "Chemical CLI", "redirect_uris": redirect_uris},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["client_id"].startswith("mcp_")
    assert payload["client_name"] == "Chemical CLI"
    assert payload["redirect_uris"] == redirect_uris
    assert payload["token_endpoint_auth_method"] == "none"
    assert payload["client_id"] in _clients
    assert _clients[payload["client_id"]].redirect_uris == redirect_uris


def test_dynamic_client_registration_requires_redirect_uris(client):
    response = client.post("/oauth/register", json={"client_name": "Chemical CLI"})

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_client_metadata",
        "error_description": "redirect_uris required",
    }


def test_authorize_get_renders_form_and_auto_registers_unknown_client(client):
    client_id = "mcp_preexisting_external_client"
    redirect_uri = "https://unknown-client.example/callback"

    response = client.get(
        "/oauth/authorize",
        params={"client_id": client_id, "redirect_uri": redirect_uri},
    )

    assert response.status_code == 200
    assert "Authorize MSDS Chain" in response.text
    assert "MSDS Chain API Key" in response.text
    assert client_id in _clients
    assert _clients[client_id].client_name == "MCP Client"
    assert _clients[client_id].redirect_uris == [redirect_uri]
    # ADVERSARIAL NOTE: GET /oauth/authorize auto-registers any unknown client_id and trusts the supplied redirect_uri.


@pytest.mark.parametrize(
    "params",
    [
        {"redirect_uri": "https://client.example/callback"},
        {"client_id": "mcp_missing_redirect"},
        {},
    ],
)
def test_authorize_get_requires_client_id_and_redirect_uri(client, params):
    response = client.get("/oauth/authorize", params=params)

    assert response.status_code == 400
    assert "Missing client_id or redirect_uri" in response.text


def test_authorize_get_rejects_unregistered_redirect_uri_for_existing_client(client):
    # Hardened: a registered client must use a pre-registered redirect_uri.
    # The allowlist must NOT be widened from an inbound request parameter.
    registration = register_client(client, redirect_uri="https://client.example/initial")
    client_id = registration["client_id"]
    new_redirect = "https://client.example/alternate"

    response = client.get(
        "/oauth/authorize",
        params={"client_id": client_id, "redirect_uri": new_redirect},
    )

    assert response.status_code == 400
    assert "not registered" in response.text
    assert new_redirect not in _clients[client_id].redirect_uris


@pytest.mark.parametrize(
    "bad_uri",
    [
        "javascript:alert(1)",
        "data:text/html,<script>1</script>",
        "http://evil.example/callback",  # plaintext http on a non-loopback host
        "not-a-url",
        "ftp://client.example/callback",
    ],
)
def test_authorize_rejects_dangerous_redirect_uri(client, bad_uri):
    response = client.get(
        "/oauth/authorize",
        params={"client_id": "mcp_attacker", "redirect_uri": bad_uri},
    )

    assert response.status_code == 400
    assert "invalid redirect_uri" in response.text
    # Dangerous URI must not auto-register a client either.
    assert "mcp_attacker" not in _clients


def test_authorize_allows_http_loopback_redirect_uri(client):
    # Native/CLI clients (e.g. mcp-remote) use http://localhost:PORT/callback.
    loopback = "http://127.0.0.1:8976/callback"
    response = client.get(
        "/oauth/authorize",
        params={"client_id": "mcp_cli", "redirect_uri": loopback},
    )

    assert response.status_code == 200
    assert _clients["mcp_cli"].redirect_uris == [loopback]


def test_register_rejects_dangerous_redirect_uri(client):
    response = client.post(
        "/oauth/register",
        json={"client_name": "Evil CLI", "redirect_uris": ["javascript:alert(1)"]},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


def test_authorize_post_redirects_with_code_and_preserves_state(client, pkce_pair):
    verifier, challenge = pkce_pair
    registration = register_client(client)
    client_id = registration["client_id"]
    redirect_uri = registration["redirect_uris"][0]

    code, response, parsed, query = authorize_code(
        client,
        client_id=client_id,
        redirect_uri=redirect_uri,
        api_key="sk-msds-authz",
        code_challenge=challenge,
        code_challenge_method="S256",
        state="csrf-state",
    )

    assert response.headers["location"].startswith(f"{redirect_uri}?")
    assert parsed.scheme == "https"
    assert parsed.netloc == "client.example"
    assert query["code"] == [code]
    assert query["state"] == ["csrf-state"]
    assert code in _auth_codes
    stored = _auth_codes[code]
    assert stored.client_id == client_id
    assert stored.redirect_uri == redirect_uri
    assert stored.code_challenge == challenge
    assert stored.code_challenge_method == "S256"
    assert stored.api_key == "sk-msds-authz"


def test_authorize_post_requires_api_key(client):
    registration = register_client(client)
    response = client.post(
        "/oauth/authorize",
        params={
            "client_id": registration["client_id"],
            "redirect_uri": registration["redirect_uris"][0],
        },
        data={"api_key": ""},
    )

    assert response.status_code == 400
    assert "API key required" in response.text


def test_full_authorization_code_flow_exchanges_code_for_tokens(client, pkce_pair):
    verifier, challenge = pkce_pair

    registration, code, response = perform_authorization_code_flow(client, verifier, challenge)

    assert code not in _auth_codes
    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "Bearer"
    assert payload["expires_in"] == 3600
    assert payload["scope"] == "chemical-safety"
    assert payload["access_token"]
    assert payload["refresh_token"]
    assert payload["access_token"] in _tokens
    assert _tokens[payload["access_token"]]["api_key"] == "sk-msds-live"
    assert _tokens[payload["access_token"]]["client_id"] == registration["client_id"]
    assert _tokens[payload["access_token"]]["refresh_token"] == payload["refresh_token"]
    assert f"refresh:{payload['refresh_token']}" in _tokens


@pytest.mark.parametrize(
    ("method", "good_verifier", "challenge", "bad_verifier"),
    [
        pytest.param(
            "S256",
            lambda pair: pair[0],
            lambda pair: pair[1],
            "definitely-wrong-verifier",
            id="s256",
        ),
        pytest.param(
            "plain",
            lambda pair: "plain-secret-verifier",
            lambda pair: "plain-secret-verifier",
            "wrong-plain-verifier",
            id="plain",
        ),
    ],
)
def test_token_exchange_rejects_wrong_pkce_verifier(client, pkce_pair, method, good_verifier, challenge, bad_verifier):
    good = good_verifier(pkce_pair)
    challenge_value = challenge(pkce_pair)
    redirect_uri = "https://client.example/callback"
    registration = register_client(client, redirect_uri=redirect_uri)
    code, _, _, _ = authorize_code(
        client,
        client_id=registration["client_id"],
        redirect_uri=redirect_uri,
        api_key="sk-msds-authz",
        code_challenge=challenge_value,
        code_challenge_method=method,
    )

    response = exchange_code(
        client,
        code=code,
        client_id=registration["client_id"],
        code_verifier=bad_verifier,
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_grant",
        "error_description": "PKCE verification failed",
    }
    assert code not in _auth_codes
    assert not any(not key.startswith("refresh:") for key in _tokens)
    # ADVERSARIAL NOTE: auth codes are popped before PKCE verification, so a single bad verifier attempt permanently burns the code.


def test_plain_pkce_flow_succeeds_when_verifier_matches_challenge(client):
    verifier = "plain-verifier-value"
    challenge = verifier
    redirect_uri = "https://client.example/plain-callback"
    registration = register_client(client, redirect_uri=redirect_uri)
    code, _, _, _ = authorize_code(
        client,
        client_id=registration["client_id"],
        redirect_uri=redirect_uri,
        api_key="sk-msds-plain",
        code_challenge=challenge,
        code_challenge_method="plain",
    )

    response = exchange_code(
        client,
        code=code,
        client_id=registration["client_id"],
        code_verifier=verifier,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "Bearer"
    assert payload["scope"] == "chemical-safety"


def test_auth_code_is_single_use(client, pkce_pair):
    verifier, challenge = pkce_pair
    registration = register_client(client)
    code, _, _, _ = authorize_code(
        client,
        client_id=registration["client_id"],
        redirect_uri=registration["redirect_uris"][0],
        api_key="sk-msds-once",
        code_challenge=challenge,
        code_challenge_method="S256",
    )

    first = exchange_code(
        client,
        code=code,
        client_id=registration["client_id"],
        code_verifier=verifier,
    )
    second = exchange_code(
        client,
        code=code,
        client_id=registration["client_id"],
        code_verifier=verifier,
    )

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json() == {"error": "invalid_grant"}


def test_expired_auth_code_returns_invalid_grant_with_code_expired(client, pkce_pair):
    verifier, challenge = pkce_pair
    registration = register_client(client)
    code, _, _, _ = authorize_code(
        client,
        client_id=registration["client_id"],
        redirect_uri=registration["redirect_uris"][0],
        api_key="sk-msds-expired-code",
        code_challenge=challenge,
        code_challenge_method="S256",
    )
    _auth_codes[code].expires_at = oauth.time.time() - 1

    response = exchange_code(
        client,
        code=code,
        client_id=registration["client_id"],
        code_verifier=verifier,
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_grant",
        "error_description": "code expired",
    }
    assert code not in _auth_codes


def test_token_exchange_rejects_client_id_mismatch(client, pkce_pair):
    verifier, challenge = pkce_pair
    registration = register_client(client)
    code, _, _, _ = authorize_code(
        client,
        client_id=registration["client_id"],
        redirect_uri=registration["redirect_uris"][0],
        api_key="sk-msds-client-mismatch",
        code_challenge=challenge,
        code_challenge_method="S256",
    )

    response = exchange_code(
        client,
        code=code,
        client_id="mcp_some_other_client",
        code_verifier=verifier,
    )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_grant"}
    assert code not in _auth_codes
    assert _tokens == {}


def test_refresh_token_grant_rotates_refresh_token_and_old_refresh_becomes_invalid(client, pkce_pair):
    verifier, challenge = pkce_pair
    _, _, token_response = perform_authorization_code_flow(client, verifier, challenge)
    original_payload = token_response.json()
    original_access = original_payload["access_token"]
    original_refresh = original_payload["refresh_token"]

    refresh_response = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": original_refresh},
    )

    assert refresh_response.status_code == 200
    rotated = refresh_response.json()
    assert rotated["access_token"] != original_access
    assert rotated["refresh_token"] != original_refresh
    assert rotated["token_type"] == "Bearer"
    assert rotated["expires_in"] == 3600
    assert rotated["scope"] == "chemical-safety"
    assert f"refresh:{original_refresh}" not in _tokens
    assert f"refresh:{rotated['refresh_token']}" in _tokens

    old_refresh_retry = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": original_refresh},
    )

    assert old_refresh_retry.status_code == 400
    assert old_refresh_retry.json() == {"error": "invalid_grant"}
    # ADVERSARIAL NOTE: refresh rotation invalidates the old refresh token but leaves prior access tokens active until TTL expiry.


def test_expired_refresh_token_returns_invalid_grant_and_is_removed(client, pkce_pair):
    verifier, challenge = pkce_pair
    _, _, token_response = perform_authorization_code_flow(client, verifier, challenge)
    refresh_token = token_response.json()["refresh_token"]
    key = f"refresh:{refresh_token}"
    _tokens[key]["expires_at"] = oauth.time.time() - 1

    response = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_grant"}
    assert key not in _tokens


def test_unsupported_grant_type_returns_unsupported_grant_type_error(client):
    response = client.post(
        "/oauth/token",
        data={"grant_type": "client_credentials"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "unsupported_grant_type"}


def test_validate_bearer_token_returns_api_key_for_valid_access_token(client, pkce_pair):
    verifier, challenge = pkce_pair
    _, _, token_response = perform_authorization_code_flow(client, verifier, challenge)
    access_token = token_response.json()["access_token"]

    result = validate_bearer_token(f"Bearer {access_token}")

    assert result == "sk-msds-live"


@pytest.mark.parametrize(
    "authorization",
    [
        "Basic abc123",
        "bearer lowercase-token",
        "Token something",
        "",
    ],
)
def test_validate_bearer_token_rejects_non_bearer_prefixes(authorization):
    assert validate_bearer_token(authorization) is None


def test_validate_bearer_token_returns_none_for_unknown_token():
    assert validate_bearer_token("Bearer not-a-real-token") is None


def test_validate_bearer_token_returns_none_and_purges_expired_token():
    _tokens["expired-access"] = {
        "api_key": "sk-msds-expired",
        "client_id": "mcp_test",
        "expires_at": oauth.time.time() - 1,
        "refresh_token": "refresh-value",
    }

    result = validate_bearer_token("Bearer expired-access")

    assert result is None
    assert "expired-access" not in _tokens


def test_verify_pkce_direct_unit_cases(pkce_pair):
    verifier, challenge = pkce_pair

    assert _verify_pkce(verifier, challenge, "S256") is True
    assert _verify_pkce("wrong-verifier", challenge, "S256") is False
    assert _verify_pkce("plain-secret", "plain-secret", "plain") is True
    assert _verify_pkce("plain-secret", "different-secret", "plain") is False
    assert _verify_pkce(verifier, challenge, "unknown-method") is False


def test_authorization_code_without_pkce_parameters_cannot_be_exchanged(client):
    registration = register_client(client)
    code, _, _, _ = authorize_code(
        client,
        client_id=registration["client_id"],
        redirect_uri=registration["redirect_uris"][0],
        api_key="sk-msds-no-pkce",
        code_challenge="",
        code_challenge_method="S256",
    )

    response = exchange_code(
        client,
        code=code,
        client_id=registration["client_id"],
        code_verifier="some-verifier",
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_grant",
        "error_description": "PKCE verification failed",
    }
    # ADVERSARIAL NOTE: authorize() allows creating auth codes with an empty code_challenge even though token exchange will always reject them.

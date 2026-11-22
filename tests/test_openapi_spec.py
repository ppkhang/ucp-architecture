from pathlib import Path

import yaml
from openapi_spec_validator import validate

from ucp.common.errors import HTTP_STATUS, ErrorCode

SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "openapi.yaml"
SPEC = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
INTENT_METHODS = {
    "create_checkout",
    "select_shipping_method",
    "apply_promotion",
    "cancel_checkout",
    "extend_checkout",
    "confirm_order",
    "cancel_order",
}


def test_spec_is_valid_openapi():
    validate(SPEC)


def test_error_enum_has_23_prefixed_codes():
    codes = list(ErrorCode)
    assert len(codes) == 23
    assert all(c.value.startswith("UCP_") for c in codes)
    assert set(HTTP_STATUS) == set(codes)


def test_spec_error_codes_match_enum():
    schema = SPEC["components"]["schemas"]["ErrorCode"]
    assert set(schema["enum"]) == {c.value for c in ErrorCode}
    assert len(schema["enum"]) == len(set(schema["enum"]))


def test_spec_error_statuses_match_enum():
    mapping = SPEC["components"]["schemas"]["ErrorCode"]["x-http-status"]
    assert mapping == {c.value: HTTP_STATUS[c] for c in ErrorCode}


def test_intent_requires_idempotency_key():
    params = SPEC["paths"]["/intent"]["post"]["parameters"]
    key = next(p for p in params if p["name"] == "Idempotency-Key")
    assert key["in"] == "header"
    assert key["required"] is True


def test_intent_methods_are_the_seven_v1_1_intents():
    mapping = SPEC["paths"]["/intent"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["discriminator"]["mapping"]
    assert set(mapping) == INTENT_METHODS


def test_v1_1_endpoints_present():
    paths = SPEC["paths"]
    assert "get" in paths["/confirm/{proposal_id}"]
    assert "post" in paths["/platform-events/{platform}"]
    assert "get" in paths["/events"]


def test_no_initiate_payment_scope():
    assert "initiate_payment" not in SPEC["components"]["schemas"]["Scope"]["enum"]
    assert "cancel_order" in SPEC["components"]["schemas"]["Scope"]["enum"]


def test_error_responses_use_only_known_statuses():
    known = set(HTTP_STATUS.values())
    for path in SPEC["paths"].values():
        for op in path.values():
            for status in op["responses"]:
                if status.startswith(("4", "5")):
                    assert int(status) in known

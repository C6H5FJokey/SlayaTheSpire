"""线协议客户端单测（无网络）。"""

import json


from spire_agent.bridge import (
    FRAME_ACTION,
    FRAME_CONFIGURE,
    FRAME_CONFIGURED,
    FRAME_HELLO,
    IncompatibleMod,
    PROTOCOL,
    Message,
    ProtocolError,
    backoff_delay,
    parse_line,
)


def test_message_line_round_trip():
    msg = Message(type=FRAME_ACTION, id=7, payload={"kind": "end_turn", "args": {}}, reply_to=3)
    parsed = parse_line(msg.to_line())
    assert parsed is not None
    assert parsed.type == FRAME_ACTION
    assert parsed.id == 7
    assert parsed.reply_to == 3
    assert parsed.payload == {"kind": "end_turn", "args": {}}


def test_message_line_carries_protocol_version():
    line = Message(type="ping", id=1).to_line()
    assert json.loads(line)["v"] == PROTOCOL


def test_parse_line_rejects_malformed_input():
    assert parse_line("") is None
    assert parse_line("   \n") is None
    assert parse_line("not json at all") is None
    assert parse_line("[1, 2, 3]") is None
    assert parse_line('"just a string"') is None
    assert parse_line('{"id": 4, "payload": {}}') is None


def test_parse_line_tolerates_missing_fields():
    parsed = parse_line('{"type": "hello"}')
    assert parsed is not None
    assert parsed.id == 0
    assert parsed.payload == {}
    assert parsed.reply_to is None


def test_backoff_delay_is_exponential_then_capped():
    assert [backoff_delay(i) for i in range(1, 5)] == [1.0, 2.0, 4.0, 8.0]
    assert backoff_delay(0) == 1.0
    assert backoff_delay(20) == 30.0
    assert backoff_delay(3, base=0.5, cap=2.0) == 2.0


def test_protocol_error_is_a_runtime_error():
    assert issubclass(ProtocolError, RuntimeError)


def test_incompatible_mod_is_a_protocol_error():
    """协议 v2：旧 jar 属于"重试也没用"，必须能跟一般协议错误区分开。"""
    assert issubclass(IncompatibleMod, ProtocolError)


def test_frame_names_are_the_wire_contract():
    assert (FRAME_HELLO, FRAME_CONFIGURE, FRAME_CONFIGURED) == (
        "hello",
        "configure",
        "configured",
    )

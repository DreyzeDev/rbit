"""`@file#Lx-Ly` attachment range parsing."""

from rbit.agent import AgentEngine


def test_attachment_range_specs() -> None:
    assert AgentEngine._attachment_spec("src/app.py#L10-L20") == ("src/app.py", 10, 11)
    assert AgentEngine._attachment_spec("src/app.py#L5") == ("src/app.py", 5, 160)
    assert AgentEngine._attachment_spec("src/app.py") == ("src/app.py", 1, 160)
    assert AgentEngine._attachment_spec("a.py#L20-L10") == ("a.py", 10, 11)
    assert AgentEngine._attachment_spec("a.py#L1-L1000") == ("a.py", 1, 500)

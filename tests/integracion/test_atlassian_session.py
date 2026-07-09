from codeguardian.atlassian import atlassian_rovo_session


def test_atlassian_rovo_session_returns_async_context_manager():
    session_context = atlassian_rovo_session()

    assert hasattr(session_context, "__aenter__")
    assert hasattr(session_context, "__aexit__")

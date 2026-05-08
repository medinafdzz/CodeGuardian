from agent import clean_replacement_text, normalize_code_block


def test_clean_replacement_text_converts_literal_newlines_and_removes_fences():
    text = "``first line\\nsecond line``"

    assert clean_replacement_text(text) == "first line\nsecond line"


def test_normalize_code_block_trims_outer_space_and_trailing_line_space():
    text = "\r\n  alpha  \r\n  beta\t \r\n\r\n"

    assert normalize_code_block(text) == "alpha\n  beta"


def test_normalize_code_block_returns_empty_string_for_empty_input():
    assert normalize_code_block("") == ""
    assert normalize_code_block(None) == ""

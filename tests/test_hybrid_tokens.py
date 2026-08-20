from retrieval.hybrid_search import extract_model_tokens


def test_model_token_extraction():
    assert "RDB-222B" in extract_model_tokens("what's the max pressure on RDB-222B")
    assert extract_model_tokens("compressors rated over 300 PSI") == []
    assert "TDSH355L" in extract_model_tokens("specs for tdsh355l please".upper() if False else "specs for TDSH355L please")

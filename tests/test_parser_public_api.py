def test_parser_reexports_public_model_types():
    from jobcockpit.ingestion.parser import ParsedLocation, ProviderTimingContract

    assert ParsedLocation.__name__ == "ParsedLocation"
    assert ProviderTimingContract.__name__ == "ProviderTimingContract"

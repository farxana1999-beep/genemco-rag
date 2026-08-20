"""Table linearization tests — both layouts, model reattachment."""
import pandas as pd

from ingestion.pdf_parser import _linearize_table, _split_value_unit


def test_split_value_unit():
    assert _split_value_unit("300 PSI") == (300.0, "PSI")
    assert _split_value_unit("460V") == (460.0, "V")
    assert _split_value_unit("Ammonia") == ("Ammonia", None)
    assert _split_value_unit("1,750 RPM") == (1750.0, "RPM")


def test_layout_models_in_header():
    # Layout B: specs down first column, models across header
    df = pd.DataFrame({
        "Specification": ["Max Working Pressure (PSI)", "Horsepower"],
        "RDB-222B": ["300", "250"],
        "RWB-II-177": ["250", "200"],
    })
    recs = _linearize_table(df, "manual.pdf", 12)
    by_key = {(r.model, r.spec): r for r in recs}
    assert by_key[("RDB-222B", "max_working_pressure")].value == 300.0
    assert by_key[("RWB-II-177", "horsepower")].value == 200.0


def test_layout_models_in_first_column_with_reattachment():
    # Layout A: models down first column; continuation row has blank model cell
    df = pd.DataFrame({
        "Model": ["RDB-222B", ""],
        "Pressure": ["300 PSI", ""],
        "HP": ["", "250"],
    })
    recs = _linearize_table(df, "manual.pdf", 5)
    # blank continuation row must be reattached to RDB-222B
    hp = [r for r in recs if r.spec == "hp"]
    assert hp and hp[0].model == "RDB-222B" and hp[0].value == 250.0

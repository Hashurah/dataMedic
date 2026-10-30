"""Discovery and ingestion: formats, encodings, headers, malformed and unsupported files."""


import pytest

from datamedic.common import ROW_ID, DataMedicError, parse_date, parse_number
from datamedic.loader import compare_schemas, discover, load_table


@pytest.mark.parametrize("key", ["json", "jsonl", "parquet", "tsv", "xml"])
def test_formats_load_same_table(data, key):
    df, meta = load_table(data["formats"][key])
    assert list(df.columns) == [ROW_ID, "id", "city", "score"]
    assert len(df) == 30
    assert df[ROW_ID].tolist() == list(range(1, 31))


def test_json_finds_records_inside_wrapper(data):
    _, meta = load_table(data["formats"]["json"])
    assert meta["json_path"] == "$.data"


def test_jsonl_bad_line_is_reported_not_dropped_silently(data):
    _, meta = load_table(data["formats"]["jsonl"])
    assert len(meta["malformed_rows"]) == 1


def test_excel_with_several_sheets_requires_a_choice(data):
    with pytest.raises(DataMedicError, match="several sheets"):
        load_table(data["formats"]["xlsx"])
    df, meta = load_table(data["formats"]["xlsx"], sheet="Scores")
    assert len(df) == 30 and meta["sheet"] == "Scores"
    info = discover(data["formats"]["xlsx"])
    assert [s["sheet"] for s in info["sheets"]] == ["Scores", "Old copy"]
    assert any("Several sheets" in w for w in info["warnings"])


def test_sqlite_lists_tables_and_foreign_keys(data):
    info = discover(data["formats"]["sqlite"])
    orders = next(t for t in info["tables"] if t["table"] == "orders")
    assert orders["foreign_keys"] == [{"column": "customer_id", "references": "customers.id"}]
    with pytest.raises(DataMedicError, match="several tables"):
        load_table(data["formats"]["sqlite"])
    df, _ = load_table(data["formats"]["sqlite"], table="orders")
    assert len(df) == 3


def test_semicolon_cp1252_decimal_comma(data):
    df, meta = load_table(data["formats"]["semicolon"])
    assert meta["delimiter"] == ";" and meta["encoding"] == "cp1252"
    assert df["ville"].tolist()[0] == "Montréal"


def test_ragged_file_preamble_short_and_long_rows(data):
    df, meta = load_table(data["ragged"])
    assert meta["preamble_rows_skipped"] == 2
    assert list(df.columns) == [ROW_ID, "id", "name_of_item", "qty"]
    assert df["id"].tolist() == ["1", "2", "4"]
    assert meta["short_rows_padded"] == 1
    assert len(meta["malformed_rows"]) == 1 and "EXTRA" in meta["malformed_rows"][0]["raw"]


def test_corrupted_file_gives_clear_error(data):
    with pytest.raises(DataMedicError, match="binary data"):
        load_table(data["corrupted"])


def test_unsupported_file_gives_clear_error(data):
    with pytest.raises(DataMedicError, match="Unsupported file type"):
        load_table(data["unsupported"])


def test_missing_and_empty_files(tmp_path):
    with pytest.raises(DataMedicError, match="not found"):
        load_table(str(tmp_path / "nope.csv"))
    p = tmp_path / "empty.csv"
    p.write_text("")
    with pytest.raises(DataMedicError, match="empty"):
        load_table(str(p))


def test_values_load_as_raw_text(data):
    df, _ = load_table(data["mixed_types"])
    assert df["zip_code"].tolist()[:3] == ["02139", "10001", "00501"]
    assert "$1,200" in df["value"].tolist()


def test_schema_comparison_of_related_files(data):
    infos = [discover(p) for p in data["multitable"]]
    cmp = compare_schemas(infos)
    assert cmp["pairs"][0]["shared_columns"] == ["customer_id"]


@pytest.mark.parametrize("raw,dec,expected", [
    ("$1,200", ".", 1200.0), ("(15.00)", ".", -15.0), ("1.234,50", ",", 1234.5), ("45%", ".", 45.0),
    ("€4,50", ",", 4.5), ("1 234", ".", 1234.0), ("abc", ".", None), ("12-3", ".", None), ("1e3", ".", 1000.0)])
def test_parse_number(raw, dec, expected):
    assert parse_number(raw, dec) == expected


def test_parse_date_respects_order_and_rejects_impossible():
    assert parse_date("25/01/2024").day == 25
    assert parse_date("03/04/2024") is None  # ambiguous without a column-level decision
    assert parse_date("03/04/2024", "dmy").month == 4
    assert parse_date("03/04/2024", "mdy").month == 3
    assert parse_date("2024-13-45") is None
    assert parse_date("Jan 7, 2024").day == 7

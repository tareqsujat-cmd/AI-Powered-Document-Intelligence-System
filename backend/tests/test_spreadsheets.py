import csv
import datetime as dt
import time
import types
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import openpyxl
import pytest
import xlrd
import xlwt
from defusedxml import DefusedXmlException
from msoffcrypto.format.ooxml import OOXMLFile
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from xlwt.CompoundDoc import XlsDoc

from app.core.config import Settings
from app.db.enums import FileType, PageStatus, TextSource
from app.processing import spreadsheets
from app.processing.errors import (
    CorruptedFileError,
    EncryptedFileError,
    ProcessingError,
    UnsupportedFileError,
)
from app.processing.results import PageResult
from app.processing.spreadsheets import iter_spreadsheet_pages, normalize_cell

Rows = list[list[Any]]
MAIN_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'

LINE_ITEMS: Rows = [
    ["Description", "Qty", "Unit Price", "Amount"],
    ["Widget", 2, 5.5, 11],
    ["Gadget", 1, 20, 20],
    ["Bolt", 10, 0.25, 2.5],
]


def write_xlsx(path: Path, sheets: dict[str, Rows]) -> Path:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    workbook.save(path)
    return path


def write_xls(path: Path, sheets: dict[str, Rows]) -> Path:
    workbook = xlwt.Workbook()
    for name, rows in sheets.items():
        sheet = workbook.add_sheet(name)
        for row_index, row in enumerate(rows):
            for column, value in enumerate(row):
                if value is not None:
                    sheet.write(row_index, column, value)
    workbook.save(str(path))
    return path


def write_csv(path: Path, sheets: dict[str, Rows]) -> Path:
    [rows] = sheets.values()
    lines = [",".join("" if value is None else str(value) for value in row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


WRITERS: dict[FileType, tuple[Callable[[Path, dict[str, Rows]], Path], str]] = {
    FileType.XLSX: (write_xlsx, "book.xlsx"),
    FileType.XLS: (write_xls, "book.xls"),
    FileType.CSV: (write_csv, "book.csv"),
}


def rewrite_zip(path: Path, replacements: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path) as archive:
        members = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    members.update(replacements)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def add_shared_strings(path: Path, xml: bytes, sheet: bytes | None = None) -> None:
    """Give a workbook a shared string table. openpyxl itself writes strings inline."""
    with zipfile.ZipFile(path) as archive:
        types = archive.read("[Content_Types].xml")
    override = (
        b'<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-'
        b'officedocument.spreadsheetml.sharedStrings+xml"/></Types>'
    )
    parts = {
        "[Content_Types].xml": types.replace(b"</Types>", override),
        "xl/sharedStrings.xml": xml,
    }
    if sheet is not None:
        parts["xl/worksheets/sheet1.xml"] = sheet
    rewrite_zip(path, parts)


def sheet_xml(*rows: str, dimension: str = "") -> bytes:
    body = "".join(rows)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><worksheet {MAIN_NS}>{dimension}'
        f"<sheetData>{body}</sheetData></worksheet>"
    ).encode()


def read_pages(path: Path, file_type: FileType, settings: Settings) -> list[PageResult]:
    return list(iter_spreadsheet_pages(path, file_type, settings))


def read_csv_bytes(tmp_path: Path, data: bytes, settings: Settings) -> PageResult:
    path = tmp_path / "input.csv"
    path.write_bytes(data)
    [page] = read_pages(path, FileType.CSV, settings)
    return page


def read_rows(file_type: FileType, tmp_path: Path, rows: Rows, settings: Settings) -> PageResult:
    writer, name = WRITERS[file_type]
    path = writer(tmp_path / name, {"Data": rows})
    [page] = read_pages(path, file_type, settings)
    assert page.table is not None
    return page


@pytest.fixture
def limited_settings() -> Settings:
    return Settings(_env_file=None, max_sheet_rows=3)


class TestNormalizeCell:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, ""),
            ("", ""),
            ("  padded \t", "padded"),
            ("   ", ""),
            ("00123", "00123"),
            ("1e5", "1e5"),
            (7, "7"),
            (0, "0"),
            (12345678901234567890, "12345678901234567890"),
            (3.0, "3"),
            (-3.0, "-3"),
            (-0.0, "0"),
            (1234.5, "1234.5"),
            (1234.5000000001, "1234.5"),
            (1234.4999999999, "1234.5"),
            (0.1 + 0.2, "0.3"),
            (1234.56, "1234.56"),
            (1.23456789, "1.23456789"),
            (0.00001, "0.00001"),
            (1e15, "1000000000000000"),
            (1234567890123.45, "1234567890123.45"),
            (1e21, "1000000000000000000000"),
            (float("nan"), ""),
            (float("inf"), ""),
            (True, "true"),
            (False, "false"),
            (dt.datetime(2024, 1, 15), "2024-01-15"),
            (dt.datetime(2024, 1, 15, 14, 30), "2024-01-15T14:30:00"),
            (dt.datetime(2024, 1, 15, 14, 30, 0, 999_999), "2024-01-15T14:30:01"),
            (dt.datetime(2024, 1, 15, 23, 59, 59, 999_999), "2024-01-16"),
            (dt.date(2024, 1, 15), "2024-01-15"),
            (dt.time(9, 5), "09:05:00"),
            ("nul\x00 byte\x07", "nul byte"),
        ],
    )
    def test_converts(self, value: object, expected: str) -> None:
        assert normalize_cell(value) == expected


class TestWorkbooks:
    @pytest.mark.parametrize("file_type", [FileType.XLSX, FileType.XLS])
    def test_all_sheets_in_order(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        writer, name = WRITERS[file_type]
        path = writer(tmp_path / name, {"First": LINE_ITEMS, "Second": [["only", "row"]]})

        first, second = read_pages(path, file_type, settings)

        assert (first.page_number, first.sheet_name) == (1, "First")
        assert (second.page_number, second.sheet_name) == (2, "Second")
        for page in (first, second):
            assert page.source is TextSource.SPREADSHEET
            assert page.status is PageStatus.OK
            assert page.processing_ms >= 0
            assert page.error is None
        assert first.table is not None
        assert first.table.headers == ["Description", "Qty", "Unit Price", "Amount"]
        assert first.table.rows[0] == ["Widget", "2", "5.5", "11"]
        assert first.text.splitlines()[0] == "Sheet: First"
        assert "Description | Qty | Unit Price | Amount" in first.text
        assert "Bolt | 10 | 0.25 | 2.5" in first.text

    @pytest.mark.parametrize("file_type", [FileType.XLSX, FileType.XLS])
    def test_empty_sheet_among_others(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        writer, name = WRITERS[file_type]
        path = writer(tmp_path / name, {"A": LINE_ITEMS, "Blank": [], "C": [["x", 1]]})

        pages = read_pages(path, file_type, settings)

        assert [page.status for page in pages] == [
            PageStatus.OK,
            PageStatus.EMPTY,
            PageStatus.OK,
        ]
        blank = pages[1]
        assert blank.sheet_name == "Blank"
        assert blank.text == ""
        assert blank.table is not None
        assert (blank.table.headers, blank.table.rows) == ([], [])

    def test_xlsx_hidden_sheet_is_read(self, tmp_path: Path, settings: Settings) -> None:
        path = tmp_path / "book.xlsx"
        workbook = Workbook()
        workbook.active.append(["visible", 1])
        for state, name in (("hidden", "Secret"), ("veryHidden", "Deep")):
            sheet = workbook.create_sheet(name)
            sheet.sheet_state = state
            sheet.append([name, 2])
        workbook.save(path)

        pages = read_pages(path, FileType.XLSX, settings)

        assert [page.sheet_name for page in pages] == ["Sheet", "Secret", "Deep"]
        assert "Secret | 2" in pages[1].text
        assert "Deep | 2" in pages[2].text

    def test_xls_hidden_sheet_is_read(self, tmp_path: Path, settings: Settings) -> None:
        path = tmp_path / "book.xls"
        workbook = xlwt.Workbook()
        workbook.add_sheet("Visible").write(0, 0, "shown")
        hidden = workbook.add_sheet("Secret")
        hidden.visibility = 1
        hidden.write(0, 0, "concealed")
        workbook.save(str(path))

        pages = read_pages(path, FileType.XLS, settings)

        assert xlrd.open_workbook(str(path)).sheet_by_index(1).visibility == 1
        assert [page.text.splitlines()[-1] for page in pages] == ["shown", "concealed"]

    def test_xlsx_with_chart_sheet_keeps_only_worksheets(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        path = tmp_path / "book.xlsx"
        workbook = Workbook()
        workbook.active.append(["data", 1])
        chart = BarChart()
        chart.add_data(Reference(workbook.active, min_col=2, min_row=1, max_row=1))
        workbook.create_chartsheet("Chart").add_chart(chart)
        workbook.create_sheet("More").append(["more", 2])
        workbook.save(path)

        pages = read_pages(path, FileType.XLSX, settings)

        assert [(page.page_number, page.sheet_name) for page in pages] == [
            (1, "Sheet"),
            (2, "More"),
        ]

    def test_xlsx_opens_without_a_spreadsheet_extension(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        """Stored uploads may have any name, and openpyxl rejects other extensions given a path."""
        original = write_xlsx(tmp_path / "book.xlsx", {"A": LINE_ITEMS})
        stored = original.rename(tmp_path / "7f3a1c9e.upload")

        [page] = read_pages(stored, FileType.XLSX, settings)

        assert page.table is not None
        assert page.table.rows[1] == ["Gadget", "1", "20", "20"]

    def test_xlsx_with_wrong_dimension_keeps_all_cells(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        """Read-only mode would otherwise trust <dimension> and silently cut the sheet."""
        path = write_xlsx(tmp_path / "book.xlsx", {"A": [["x"]]})
        rows = (
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Item</t></is></c>'
            '<c r="B1" t="inlineStr"><is><t>Amount</t></is></c></row>',
            '<row r="2"><c r="A2" t="inlineStr"><is><t>Widget</t></is></c>'
            '<c r="B2"><v>5</v></c></row>',
            '<row r="3"><c r="A3" t="inlineStr"><is><t>Gadget</t></is></c>'
            '<c r="B3"><v>7</v></c></row>',
        )
        rewrite_zip(
            path,
            {"xl/worksheets/sheet1.xml": sheet_xml(*rows, dimension='<dimension ref="A1"/>')},
        )

        [page] = read_pages(path, FileType.XLSX, settings)

        assert page.table is not None
        assert page.table.headers == ["Item", "Amount"]
        assert page.table.rows == [["Widget", "5"], ["Gadget", "7"]]

    def test_xlsx_shared_strings(self, tmp_path: Path, settings: Settings) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"A": [["x"]]})
        strings = (
            f"<sst {MAIN_NS}><si><t>Item</t></si><si><t>Amount</t></si><si><t>Widget</t></si></sst>"
        )
        sheet = sheet_xml(
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>',
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>5</v></c></row>',
        )
        add_shared_strings(path, strings.encode(), sheet)

        [page] = read_pages(path, FileType.XLSX, settings)

        assert page.table is not None
        assert page.table.headers == ["Item", "Amount"]
        assert page.table.rows == [["Widget", "5"]]

    def test_xlsx_keeps_true_sheet_row_numbers_across_missing_rows(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        path = tmp_path / "book.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        for column, title in enumerate(["Item", "Amount"], start=1):
            sheet.cell(row=5, column=column, value=title)
        sheet.cell(row=9, column=1, value="Widget")
        sheet.cell(row=9, column=2, value=5)
        workbook.save(path)

        [page] = read_pages(path, FileType.XLSX, settings)

        assert page.table is not None
        assert page.table.header_row == 5
        assert page.table.rows == [["Widget", "5"]]

    def test_xlsx_merged_cells_in_read_only_mode(self, tmp_path: Path, settings: Settings) -> None:
        path = tmp_path / "book.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Quarterly Invoice Report", None, None])
        sheet.merge_cells("A1:C1")
        sheet.append(["Item", "Qty", "Amount"])
        sheet.append(["Grouped", 1, 10])
        sheet.append([None, 2, 20])
        sheet.merge_cells("A3:A4")
        workbook.save(path)

        [page] = read_pages(path, FileType.XLSX, settings)

        assert page.table is not None
        assert page.table.preamble == [["Quarterly Invoice Report"]]
        assert page.table.headers == ["Item", "Qty", "Amount"]
        assert page.table.rows == [["Grouped", "1", "10"], ["", "2", "20"]]

    @pytest.mark.parametrize("file_type", [FileType.XLSX, FileType.XLS])
    def test_blank_cells_stay_positional(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        rows: Rows = [["Item", "Qty", "Amount"], ["A", None, 5], ["B", 2, 6], ["C", 3, 7]]

        table = read_rows(file_type, tmp_path, rows, settings).table

        assert table is not None
        assert table.rows[0] == ["A", "", "5"]

    @pytest.mark.parametrize("file_type", [FileType.XLSX, FileType.XLS])
    def test_blank_rows_are_skipped_and_trailing_blanks_dropped(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        writer, name = WRITERS[file_type]
        rows: Rows = [["Item", "Amount", None], [None, None], ["Widget", 5, None]]
        path = writer(tmp_path / name, {"A": rows})

        [page] = read_pages(path, file_type, settings)

        assert page.table is not None
        assert page.table.headers == ["Item", "Amount"]
        assert page.table.rows == [["Widget", "5"]]
        assert page.table.header_row == 1


class TestDates:
    def test_xlsx_dates(self, tmp_path: Path, settings: Settings) -> None:
        path = write_xlsx(
            tmp_path / "book.xlsx",
            {
                "A": [
                    [
                        dt.datetime(2024, 3, 9),
                        dt.datetime(2024, 3, 9, 14, 30),
                        dt.date(2023, 12, 31),
                    ]
                ]
            },
        )

        [page] = read_pages(path, FileType.XLSX, settings)

        assert page.table is not None
        assert page.table.rows == [["2024-03-09", "2024-03-09T14:30:00", "2023-12-31"]]

    def test_xls_dates(self, tmp_path: Path, settings: Settings) -> None:
        path = tmp_path / "book.xls"
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("A")
        date_style = xlwt.easyxf(num_format_str="YYYY-MM-DD")
        time_style = xlwt.easyxf(num_format_str="YYYY-MM-DD HH:MM:SS")
        sheet.write(0, 0, dt.datetime(2024, 3, 9), date_style)
        sheet.write(0, 1, dt.datetime(2024, 3, 9, 14, 30), time_style)
        workbook.save(str(path))

        [page] = read_pages(path, FileType.XLS, settings)

        assert page.table is not None
        assert page.table.rows == [["2024-03-09", "2024-03-09T14:30:00"]]

    def test_xls_1904_date_system(self, tmp_path: Path, settings: Settings) -> None:
        path = tmp_path / "book.xls"
        workbook = xlwt.Workbook()
        workbook.dates_1904 = True
        sheet = workbook.add_sheet("A")
        # 2024-03-09 as a raw serial in the 1904 system, which writing a datetime does not produce.
        sheet.write(0, 0, 43898, xlwt.easyxf(num_format_str="YYYY-MM-DD"))
        workbook.save(str(path))

        [page] = read_pages(path, FileType.XLS, settings)

        assert page.table is not None
        assert page.table.rows == [["2024-03-09"]]


class TestXlsCellTypes:
    def test_bool_number_text_and_leading_zeros(self, tmp_path: Path, settings: Settings) -> None:
        path = write_xls(
            tmp_path / "b.xls", {"A": [[True, False, 3.0, 1234.5, "00123", 0.1 + 0.2]]}
        )

        [page] = read_pages(path, FileType.XLS, settings)

        assert page.table is not None
        assert page.table.rows == [["true", "false", "3", "1234.5", "00123", "0.3"]]
        assert page.warnings == []

    def test_error_cells_are_blank_with_a_warning(self, tmp_path: Path, settings: Settings) -> None:
        path = tmp_path / "book.xls"
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("A")
        sheet.write(0, 0, "before")
        sheet.row(0).set_cell_error(1, "#DIV/0!")
        sheet.write(0, 2, "after")
        workbook.save(str(path))

        [page] = read_pages(path, FileType.XLS, settings)

        assert page.table is not None
        assert page.table.rows == [["before", "", "after"]]
        assert "#DIV/0!" not in page.text
        assert page.warnings == [spreadsheets._error_cell_warning(1)]


class TestFormulas:
    def test_xlsx_formula_without_cached_value_warns_and_is_blank(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        """openpyxl stores formulas with no result, as every library-written file does."""
        path = tmp_path / "book.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Item", "Qty", "Price", "Total"])
        sheet.append(["Widget", 2, 5, "=B2*C2"])
        sheet.append(["Gadget", 3, 4, "=B3*C3"])
        sheet.append(["Sum", None, None, "=SUM(D2:D3)"])
        workbook.save(path)

        [page] = read_pages(path, FileType.XLSX, settings)

        assert page.table is not None
        assert page.table.rows == [["Widget", "2", "5"], ["Gadget", "3", "4"], ["Sum"]]
        assert "3 formula cell(s) have no cached value and were read as blank" in page.warnings[0]
        assert "=B2" not in page.text
        assert "10" not in page.text  # never computed

    def test_xlsx_formula_with_cached_value_uses_it(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"A": [["x"]]})
        xml = sheet_xml(
            '<row r="1"><c r="A1"><v>2</v></c><c r="B1"><f>A1*2</f><v>4</v></c></row>',
            '<row r="2"><c r="A2"><v>2.5</v></c>'
            '<c r="B2" t="str"><f>"a"&amp;"b"</f><v>ab</v></c></row>',
        )
        rewrite_zip(path, {"xl/worksheets/sheet1.xml": xml})

        [page] = read_pages(path, FileType.XLSX, settings)

        assert page.table is not None
        assert page.table.rows == [["2", "4"], ["2.5", "ab"]]
        assert page.warnings == []

    def test_xlsx_formula_cached_as_empty_string_does_not_warn(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        """Excel writes this for =IF(x,"",y), and openpyxl reads it just like a missing value."""
        path = write_xlsx(tmp_path / "book.xlsx", {"A": [["x"]]})
        xml = sheet_xml(
            '<row r="1"><c r="A1"><v>1</v></c><c r="B1" t="str"><f>""</f><v></v></c></row>'
        )
        rewrite_zip(path, {"xl/worksheets/sheet1.xml": xml})

        [page] = read_pages(path, FileType.XLSX, settings)

        assert page.table is not None
        assert page.table.rows == [["1"]]
        assert page.warnings == []

    @pytest.mark.parametrize(
        "cell",
        [
            '<c r="B1"><f>A1*2</f></c>',
            '<c r="B1"><f>A1*2</f><v/></c>',
            '<c r="B1"><f t="shared" si="0" ref="B1:B1">A1*2</f></c>',
            '<c r="B1"><f t="array" ref="B1">A1*2</f></c>',
            '<c r="B1" t="str"><f>A1</f></c>',
        ],
    )
    def test_xlsx_formula_forms_without_value_are_counted(
        self, cell: str, tmp_path: Path, settings: Settings
    ) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"A": [["x"]]})
        rewrite_zip(
            path,
            {
                "xl/worksheets/sheet1.xml": sheet_xml(
                    f'<row r="1"><c r="A1"><v>1</v></c>{cell}</row>'
                )
            },
        )

        [page] = read_pages(path, FileType.XLSX, settings)

        assert page.table is not None
        assert page.table.rows == [["1"]]
        assert "1 formula cell(s)" in page.warnings[0]

    def test_xlsx_formula_with_a_namespace_prefix_is_counted(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"A": [["x"]]})
        xml = (
            '<x:worksheet xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<x:sheetData><x:row r="1"><x:c r="A1"><x:v>1</x:v></x:c>'
            '<x:c r="B1"><x:f>A1*2</x:f></x:c></x:row></x:sheetData></x:worksheet>'
        )
        rewrite_zip(path, {"xl/worksheets/sheet1.xml": xml.encode()})

        [page] = read_pages(path, FileType.XLSX, settings)

        assert page.table is not None
        assert page.table.rows == [["1"]]
        assert "1 formula cell(s)" in page.warnings[0]

    def test_xlsx_formula_tag_across_a_read_chunk_boundary_is_counted(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"A": [["x"]]})
        head = f'<worksheet {MAIN_NS}><sheetData><row r="1"><c r="A1"><v>1</v></c><c r="B1">'
        padded = head + " " * (spreadsheets._READ_CHUNK_BYTES - len(head) - 1)
        xml = padded + "<f>A1*2</f></c></row></sheetData></worksheet>"
        assert xml[spreadsheets._READ_CHUNK_BYTES - 1 : spreadsheets._READ_CHUNK_BYTES + 2] == (
            "<f>"
        )
        rewrite_zip(path, {"xl/worksheets/sheet1.xml": xml.encode()})

        [page] = read_pages(path, FileType.XLSX, settings)

        assert "1 formula cell(s)" in page.warnings[0]

    def test_xlsx_without_formulas_skips_the_second_parse(
        self, tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_if_called(*_args: Any) -> None:
            raise AssertionError("the formula scan should have been skipped")

        path = write_xlsx(tmp_path / "book.xlsx", {"A": LINE_ITEMS})
        monkeypatch.setattr(spreadsheets, "iterparse", fail_if_called)

        [page] = read_pages(path, FileType.XLSX, settings)

        assert page.status is PageStatus.OK
        assert page.warnings == []

    def test_xlsx_counts_formulas_only_in_rows_it_kept(
        self, tmp_path: Path, limited_settings: Settings
    ) -> None:
        path = tmp_path / "book.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        for row in range(1, 11):
            sheet.append([f"line {row}", f"=A{row}"])
        workbook.save(path)

        [page] = read_pages(path, FileType.XLSX, limited_settings)

        assert page.table is not None
        assert len(page.table.rows) == 3
        assert any("3 formula cell(s)" in warning for warning in page.warnings)

    def test_xlsx_cached_error_values_are_blank_with_a_warning(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"A": [["x"]]})
        xml = sheet_xml(
            '<row r="1"><c r="A1"><v>1</v></c><c r="B1" t="e"><f>1/0</f><v>#DIV/0!</v></c>'
            '<c r="C1"><v>3</v></c></row>'
        )
        rewrite_zip(path, {"xl/worksheets/sheet1.xml": xml})

        [page] = read_pages(path, FileType.XLSX, settings)

        assert page.table is not None
        assert page.table.rows == [["1", "", "3"]]
        assert page.warnings == [spreadsheets._error_cell_warning(1)]


@pytest.mark.parametrize("file_type", [FileType.XLSX, FileType.XLS, FileType.CSV])
class TestHeaderDetection:
    def test_table_without_preamble(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        table = read_rows(file_type, tmp_path, LINE_ITEMS, settings).table

        assert table is not None
        assert table.headers == ["Description", "Qty", "Unit Price", "Amount"]
        assert table.header_row == 1
        assert table.preamble == []
        assert len(table.rows) == 3

    def test_header_on_row_8_under_a_preamble(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        preamble: Rows = [
            ["Acme Supplies Ltd"],
            ["Invoice No:", "INV-1"],
            ["Date:", "2024-01-15"],
            [None],
            ["Bill To:", "Beta Corp"],
            ["Currency", "EUR"],
            ["PO Ref", "77"],
        ]

        table = read_rows(file_type, tmp_path, [*preamble, *LINE_ITEMS], settings).table

        assert table is not None
        assert table.header_row == 8
        assert table.headers == ["Description", "Qty", "Unit Price", "Amount"]
        assert table.preamble[:3] == [
            ["Acme Supplies Ltd"],
            ["Invoice No:", "INV-1"],
            ["Date:", "2024-01-15"],
        ]
        assert ["Currency", "EUR"] in table.preamble
        assert len(table.preamble) == 6
        assert table.rows[0] == ["Widget", "2", "5.5", "11"]

    def test_preamble_precedes_header_in_text(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        rows: Rows = [["Invoice No:", "INV-1"], *LINE_ITEMS]

        text = read_rows(file_type, tmp_path, rows, settings).text

        assert text.index("Invoice No: | INV-1") < text.index("Description | Qty")
        assert text.index("Description | Qty") < text.index("Widget | 2")

    def test_numeric_sheet_has_no_header(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        rows: Rows = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]

        table = read_rows(file_type, tmp_path, rows, settings).table

        assert table is not None
        assert table.headers == []
        assert table.header_row is None
        assert table.preamble == []
        assert table.rows == [["1", "2", "3"], ["4", "5", "6"], ["7", "8", "9"]]

    def test_single_row_sheet_has_no_header(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        table = read_rows(file_type, tmp_path, [["Description", "Amount"]], settings).table

        assert table is not None
        assert table.headers == []
        assert table.rows == [["Description", "Amount"]]

    def test_data_rows_with_text_columns_are_not_a_header(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        rows: Rows = [
            ["Widget Blue", "Paris", 5],
            ["Gadget Red", "Lyon", 7],
            ["Bolt Green", "Nice", 9],
        ]

        table = read_rows(file_type, tmp_path, rows, settings).table

        assert table is not None
        assert table.headers == []
        assert len(table.rows) == 3

    def test_key_value_sheet_has_no_header(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        rows: Rows = [
            ["Invoice No", "INV-1"],
            ["Total", "100.00"],
            ["Tax", "20.00"],
        ]

        table = read_rows(file_type, tmp_path, rows, settings).table

        assert table is not None
        assert table.headers == []
        assert table.preamble == []
        assert len(table.rows) == 3

    def test_labels_ending_in_a_colon_are_not_a_header(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        rows: Rows = [["Supplier:", "Acme"], ["Invoice No:", 77], ["Total:", 100]]

        table = read_rows(file_type, tmp_path, rows, settings).table

        assert table is not None
        assert table.headers == []
        assert table.rows == [["Supplier:", "Acme"], ["Invoice No:", "77"], ["Total:", "100"]]

    def test_table_without_keywords_is_found(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        rows: Rows = [["Colour", "Count"], ["red", 1], ["blue", 2], ["green", 3]]

        table = read_rows(file_type, tmp_path, rows, settings).table

        assert table is not None
        assert table.headers == ["Colour", "Count"]

    def test_titles_containing_digits_are_still_a_header(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        rows: Rows = [["Item", "VAT 20%", "Total"], ["Widget", 2, 12], ["Gadget", 4, 24]]

        table = read_rows(file_type, tmp_path, rows, settings).table

        assert table is not None
        assert table.headers == ["Item", "VAT 20%", "Total"]
        assert len(table.rows) == 2

    def test_header_with_snake_case_titles(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        rows: Rows = [["invoice_number", "unit_price"], ["A", 1], ["B", 2]]

        table = read_rows(file_type, tmp_path, rows, settings).table

        assert table is not None
        assert table.headers == ["invoice_number", "unit_price"]

    def test_totals_rows_below_stay_data(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        rows: Rows = [*LINE_ITEMS, ["Subtotal", None, None, 33.5], ["Total", None, None, 40.2]]

        table = read_rows(file_type, tmp_path, rows, settings).table

        assert table is not None
        assert table.header_row == 1
        assert table.rows[-1] == ["Total", "", "", "40.2"]

    def test_duplicate_labels_are_not_a_header(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        rows: Rows = [["same", "same"], ["a", 1], ["b", 2]]

        table = read_rows(file_type, tmp_path, rows, settings).table

        assert table is not None
        assert table.headers == []


class TestRowLimit:
    @pytest.mark.parametrize("file_type", [FileType.XLSX, FileType.XLS, FileType.CSV])
    def test_stops_at_the_limit_with_a_warning(
        self, file_type: FileType, tmp_path: Path, limited_settings: Settings
    ) -> None:
        rows: Rows = [["Item", "Amount"], *[[f"line{i}", i] for i in range(10)]]

        page = read_rows(file_type, tmp_path, rows, limited_settings)

        assert page.status is PageStatus.OK
        assert page.table is not None
        assert page.table.headers == ["Item", "Amount"]
        assert page.table.rows == [["line0", "0"], ["line1", "1"], ["line2", "2"]]
        assert page.warnings == ["Only the first 3 data rows were read; the rest were dropped."]
        assert "line3" not in page.text

    @pytest.mark.parametrize("file_type", [FileType.XLSX, FileType.XLS, FileType.CSV])
    def test_exactly_at_the_limit_does_not_warn(
        self, file_type: FileType, tmp_path: Path, limited_settings: Settings
    ) -> None:
        rows: Rows = [["Item", "Amount"], ["a", 1], ["b", 2], ["c", 3]]

        page = read_rows(file_type, tmp_path, rows, limited_settings)

        assert page.table is not None
        assert len(page.table.rows) == 3
        assert page.warnings == []

    def test_blank_rows_do_not_count(self, tmp_path: Path, limited_settings: Settings) -> None:
        data = b"Item,Amount\n\n\n,\na,1\n\n,\nb,2\n\n\nc,3\n\n"

        page = read_csv_bytes(tmp_path, data, limited_settings)

        assert page.table is not None
        assert page.table.rows == [["a", "1"], ["b", "2"], ["c", "3"]]
        assert page.warnings == []

    def test_preamble_and_header_do_not_count(
        self, tmp_path: Path, limited_settings: Settings
    ) -> None:
        rows: Rows = [["Invoice No:", "INV-1"], ["Date:", "2024"], *LINE_ITEMS]

        page = read_rows(FileType.CSV, tmp_path, rows, limited_settings)

        assert page.table is not None
        assert len(page.table.rows) == 3
        assert page.warnings == []

    def test_limit_smaller_than_the_header_scan_window(self, tmp_path: Path) -> None:
        settings = Settings(_env_file=None, max_sheet_rows=2)
        rows: Rows = [[f"{i}", i] for i in range(10)]

        page = read_rows(FileType.CSV, tmp_path, rows, settings)

        assert page.table is not None
        assert page.table.rows == [["0", "0"], ["1", "1"]]
        assert "first 2 data rows" in page.warnings[0]

    def test_large_csv_stops_reading_at_the_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = Settings(_env_file=None, max_sheet_rows=1_000)
        path = tmp_path / "large.csv"
        lines = "".join(f"item{i},{i}.50,{i}\n" for i in range(200_000))
        path.write_text(f"Item,Price,Qty\n{lines}", encoding="utf-8")

        real_reader = csv.reader
        consumed = 0

        def counting_reader(*args: Any, **kwargs: Any) -> Iterator[list[str]]:
            nonlocal consumed
            for record in real_reader(*args, **kwargs):
                if kwargs.get("skipinitialspace"):  # the read itself, not delimiter detection
                    consumed += 1
                yield record

        monkeypatch.setattr(spreadsheets.csv, "reader", counting_reader)

        [page] = read_pages(path, FileType.CSV, settings)

        assert page.table is not None
        assert page.table.headers == ["Item", "Price", "Qty"]
        assert len(page.table.rows) == 1_000
        assert page.table.rows[-1] == ["item999", "999.50", "999"]
        assert "first 1000 data rows" in page.warnings[0]
        assert consumed == 1_002  # the header, the rows kept, and the one that revealed more
        assert page.text.count("\n") == 1_000

    def test_large_csv_under_the_limit_is_complete(self, tmp_path: Path) -> None:
        settings = Settings(_env_file=None, max_sheet_rows=250_000)
        path = tmp_path / "large.csv"
        lines = "".join(f"item{i},{i}.50,{i}\n" for i in range(200_000))
        path.write_text(f"Item,Price,Qty\n{lines}", encoding="utf-8")

        [page] = read_pages(path, FileType.CSV, settings)

        assert page.table is not None
        assert len(page.table.rows) == 200_000
        assert page.table.rows[199_999] == ["item199999", "199999.50", "199999"]
        assert page.warnings == []

    def test_xlsx_never_parses_past_the_limit(
        self, tmp_path: Path, limited_settings: Settings
    ) -> None:
        """Broken XML after the limit proves that neither pass of the sheet gets that far."""
        path = write_xlsx(tmp_path / "book.xlsx", {"A": [["x"]]})
        # Past the rows buffered for header detection, which are read whatever the limit is.
        filler = "".join(
            f'<row r="{i}"><c r="A{i}"><v>{i}</v></c><c r="B{i}"><v>{i}</v></c></row>'
            for i in range(1, 37)
        )
        # Without <dimension>, openpyxl reads to the end of the sheet while opening the workbook.
        broken = (
            f'<?xml version="1.0"?><worksheet {MAIN_NS}><dimension ref="A1:B99"/><sheetData>'
            f'{filler}<row r="37"><c r='
        )
        rewrite_zip(path, {"xl/worksheets/sheet1.xml": broken.encode()})

        [page] = read_pages(path, FileType.XLSX, limited_settings)

        assert page.status is PageStatus.OK
        assert page.table is not None
        assert page.table.rows == [["1", "1"], ["2", "2"], ["3", "3"]]


class TestCsvEncoding:
    TEXT = "Lieferant;Ort;Betrag\nMüller GmbH;Köln;€1.234,56\nCafé Größe;Paris;€12,50\n"

    def assert_western_rows(self, page: PageResult) -> None:
        assert page.table is not None
        assert page.table.headers == ["Lieferant", "Ort", "Betrag"]
        assert page.table.rows == [
            ["Müller GmbH", "Köln", "€1.234,56"],
            ["Café Größe", "Paris", "€12,50"],
        ]
        assert "Müller GmbH | Köln | €1.234,56" in page.text

    def test_utf8(self, tmp_path: Path, settings: Settings) -> None:
        page = read_csv_bytes(tmp_path, self.TEXT.encode("utf-8"), settings)

        self.assert_western_rows(page)
        assert page.warnings == []
        assert page.sheet_name is None
        assert page.page_number == 1
        assert page.source is TextSource.SPREADSHEET
        assert not page.text.startswith("Sheet:")

    def test_utf8_bom_is_removed(self, tmp_path: Path, settings: Settings) -> None:
        page = read_csv_bytes(tmp_path, self.TEXT.encode("utf-8-sig"), settings)

        self.assert_western_rows(page)
        assert page.warnings == []

    @pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
    def test_utf16_and_utf32_with_bom(self, codec: str, tmp_path: Path, settings: Settings) -> None:
        bom = {"utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}.get(codec, b"")

        page = read_csv_bytes(tmp_path, bom + self.TEXT.encode(codec), settings)

        self.assert_western_rows(page)
        assert page.warnings == []

    def test_cp1252_round_trips_and_warns(self, tmp_path: Path, settings: Settings) -> None:
        page = read_csv_bytes(tmp_path, self.TEXT.encode("cp1252"), settings)

        self.assert_western_rows(page)
        assert len(page.warnings) == 1
        assert "cp1252" in page.warnings[0]

    @pytest.mark.parametrize(
        "text",
        [
            "Müller GmbH,Café,€1.234\n",
            "Café\n",
            "Fournisseur,Montant\nSociété Générale,120\nCafé de la Gare,50\nHôtel Déjà Vu,20\n",
            "Proveedor,Importe\nCompañía Española,10\nJosé Núñez,20\n",
            "Supplier,Note\nAcme,“don’t” – 5 °C\nBeta™,£10\n",
        ],
    )
    def test_short_western_text_is_not_read_as_something_exotic(
        self, text: str, tmp_path: Path, settings: Settings
    ) -> None:
        page = read_csv_bytes(tmp_path, text.encode("cp1252"), settings)

        assert page.text.replace(" | ", ",") == text.strip()
        assert "cp1252" in page.warnings[0]

    def test_non_utf8_byte_late_in_the_file_is_found(self, tmp_path: Path) -> None:
        """The first megabytes are pure ASCII, so a sample of the start would look like UTF-8."""
        settings = Settings(_env_file=None, max_sheet_rows=300_000)
        padding = b"".join(b"item%d,1.50\n" % i for i in range(250_000))
        assert len(padding) > 3 * 1024 * 1024

        page = read_csv_bytes(tmp_path, padding + "Müller GmbH,2.50\n".encode("cp1252"), settings)

        assert page.table is not None
        assert page.table.rows[-1] == ["Müller GmbH", "2.50"]
        assert "cp1252" in page.warnings[0]

    @pytest.mark.parametrize(
        ("codec", "text"),
        [
            ("cp1251", "Поставщик услуг связи и интернета;Сумма платежа по счёту;1200\n" * 50),
            ("cp1250", "Dostawca;Kwota\nŁódź Sp. z o.o.;1200\nZażółć gęślą jaźń;340\n"),
            ("cp1253", "Προμηθευτής;Ποσό\nΑθήνα Εμπορική;1200\nΘεσσαλονίκη;340\n"),
            ("shift_jis", "請求書番号,合計金額\n株式会社東京,1200\n大阪商事株式会社,340\n"),
            ("gb18030", "发票号码,总金额\n北京有限公司,1200\n上海贸易公司,340\n"),
            ("euc_kr", "송장 번호,합계\n서울 주식회사,1200\n부산 무역,340\n"),
        ],
    )
    def test_other_encodings_are_not_forced_to_cp1252(
        self, codec: str, text: str, tmp_path: Path, settings: Settings
    ) -> None:
        page = read_csv_bytes(tmp_path, text.encode(codec), settings)

        assert page.text.replace(" | ", ";").replace(";", ",") == text.strip().replace(";", ",")
        assert "cp1252" not in page.warnings[0]

    def test_non_western_text_late_in_the_file_is_detected(self, tmp_path: Path) -> None:
        settings = Settings(_env_file=None, max_sheet_rows=300_000)
        padding = b"".join(b"item%d;1.50\n" % i for i in range(250_000))
        line = "Поставщик услуг связи и интернета;Сумма платежа по счёту\n"

        page = read_csv_bytes(tmp_path, padding + (line * 50).encode("cp1251"), settings)

        assert page.table is not None
        assert page.table.rows[-1] == line.strip().split(";")
        assert "cp1252" not in page.warnings[0]

    def test_sample_cut_inside_a_multibyte_character_still_detects(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        line = "株式会社東京;大阪商事株式会社;1200\n"
        data = (line * 40_000).encode("shift_jis")
        assert len(data) > spreadsheets._ENCODING_SAMPLE_BYTES

        page = read_csv_bytes(tmp_path, data, settings)

        assert page.table is not None
        assert page.table.rows[-1] == line.strip().split(";")

    def test_utf8_with_one_damaged_byte_stays_utf8(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        good = "Lieferant;Betrag\n" + "Größe Straße Müller Café;12,50\n" * 100
        data = good.encode("utf-8") + b"Bad \xff byte;1\n"

        page = read_csv_bytes(tmp_path, data, settings)

        assert page.table is not None
        assert page.table.rows[0] == ["Größe Straße Müller Café", "12,50"]
        assert page.table.rows[-1] == ["Bad \N{REPLACEMENT CHARACTER} byte", "1"]
        assert "utf-8" in page.warnings[0]

    def test_undefined_cp1252_bytes_never_crash(self, tmp_path: Path, settings: Settings) -> None:
        page = read_csv_bytes(tmp_path, b"Item,Note\nA\x81B,\x8d\x8f\x90\x9d\n", settings)

        assert page.status is PageStatus.OK
        assert page.warnings != []

    def test_utf8_multibyte_split_across_read_chunks(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        chunk = spreadsheets._READ_CHUNK_BYTES
        data = b"x" * (chunk - 2) + ",€5\n".encode()
        assert data[chunk - 1 : chunk + 2] == "€".encode()

        page = read_csv_bytes(tmp_path, data, settings)

        assert page.table is not None
        assert page.table.rows[0][-1] == "€5"
        assert page.warnings == []

    def test_utf8_truncated_mid_character_at_the_end(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        data = "Größe Straße Müller Café,1\n".encode() * 20 + b"cut,\xe2\x82"

        page = read_csv_bytes(tmp_path, data, settings)

        assert page.table is not None
        assert page.table.rows[0] == ["Größe Straße Müller Café", "1"]
        assert "utf-8" in page.warnings[0]


class TestCsvParsing:
    @pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
    def test_each_delimiter(self, delimiter: str, tmp_path: Path, settings: Settings) -> None:
        lines = [["Item", "Qty", "Amount"], ["Widget", "2", "10.50"], ["Gadget", "1", "7"]]
        data = "\n".join(delimiter.join(line) for line in lines) + "\n"

        page = read_csv_bytes(tmp_path, data.encode(), settings)

        assert page.table is not None
        assert page.table.headers == lines[0]
        assert page.table.rows == lines[1:]

    def test_decimal_comma_stays_in_one_cell_with_semicolons(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        data = b"Item;Amount\nWidget;1.234,56\nGadget;2.000,00\n"

        page = read_csv_bytes(tmp_path, data, settings)

        assert page.table is not None
        assert page.table.rows == [["Widget", "1.234,56"], ["Gadget", "2.000,00"]]

    def test_decimal_commas_on_every_row_with_no_header_still_mean_semicolons(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        page = read_csv_bytes(tmp_path, b"1,5;2,5\n3,5;4,5\n", settings)

        assert page.table is not None
        assert page.table.rows == [["1,5", "2,5"], ["3,5", "4,5"]]

    def test_one_column_defaults_to_comma_without_splitting(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        page = read_csv_bytes(tmp_path, b"alpha\nbeta\n", settings)

        assert page.table is not None
        assert page.table.rows == [["alpha"], ["beta"]]

    def test_short_preamble_lines_do_not_mislead_delimiter_detection(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        data = (
            b"Acme Ltd\nInvoice No: 5, dated today\n"
            b"Item;Qty;Amount\nWidget;2;1.234,50\nGadget;1;7,00\nBolt;9;3,00\n"
        )

        page = read_csv_bytes(tmp_path, data, settings)

        assert page.table is not None
        assert page.table.headers == ["Item", "Qty", "Amount"]
        assert page.table.preamble == [["Acme Ltd"], ["Invoice No: 5, dated today"]]
        assert page.table.rows[0] == ["Widget", "2", "1.234,50"]

    def test_quoted_fields_with_delimiters_and_newlines(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        data = (
            b'Description,Note,Amount\n"Widget, large","line one\nline two",5\n'
            b'"say ""hi""",plain,7\n'
        )

        page = read_csv_bytes(tmp_path, data, settings)

        assert page.table is not None
        assert page.table.rows == [
            ["Widget, large", "line one\nline two", "5"],
            ['say "hi"', "plain", "7"],
        ]
        assert "Widget, large | line one line two | 5" in page.text

    def test_crlf_and_bare_cr_line_endings(self, tmp_path: Path, settings: Settings) -> None:
        for newline in (b"\r\n", b"\r"):
            data = newline.join([b"Item,Amount", b"Widget,5", b"Gadget,7", b""])

            page = read_csv_bytes(tmp_path, data, settings)

            assert page.table is not None
            assert page.table.headers == ["Item", "Amount"]
            assert page.table.rows == [["Widget", "5"], ["Gadget", "7"]]

    def test_ragged_rows(self, tmp_path: Path, settings: Settings) -> None:
        data = b"Note\nItem,Qty,Amount\nA,1,2\nB,3\nC,4,5,extra,cells\nD,6,7\nE,8,9\n"

        page = read_csv_bytes(tmp_path, data, settings)

        assert page.table is not None
        assert page.table.preamble == [["Note"]]
        assert page.table.headers == ["Item", "Qty", "Amount"]
        assert page.table.rows[1] == ["B", "3"]
        assert page.table.rows[2] == ["C", "4", "5", "extra", "cells"]

    def test_blank_rows_are_skipped_and_row_numbers_stay_true(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        data = b"\n\n,,\nItem,Amount\n\nA,1\n   ,  \nB,2\n"

        page = read_csv_bytes(tmp_path, data, settings)

        assert page.table is not None
        assert page.table.header_row == 4
        assert page.table.rows == [["A", "1"], ["B", "2"]]

    def test_leading_zeros_and_strings_stay_exact(self, tmp_path: Path, settings: Settings) -> None:
        data = b"Code,Amount\n00123,1.50\n007,3.0\n"

        page = read_csv_bytes(tmp_path, data, settings)

        assert page.table is not None
        assert page.table.rows == [["00123", "1.50"], ["007", "3.0"]]

    def test_space_after_delimiter_before_a_quote(self, tmp_path: Path, settings: Settings) -> None:
        page = read_csv_bytes(tmp_path, b'Item, Note\nA, "x, y"\nB, "z"\n', settings)

        assert page.table is not None
        assert page.table.rows == [["Item", "Note"], ["A", "x, y"], ["B", "z"]]

    def test_nul_bytes_do_not_reach_the_output(self, tmp_path: Path, settings: Settings) -> None:
        data = b"Item,Amount\nWid\x00get,5\n\x00,\x00\nGad\x00get\x00,7\n"

        page = read_csv_bytes(tmp_path, data, settings)

        assert page.table is not None
        assert "\x00" not in page.text
        assert page.table.rows == [["Widget", "5"], ["Gadget", "7"]]
        assert "Widget | 5" in page.text

    @pytest.mark.parametrize("data", [b"", b"   \n\n  \n", b",,,\n,,\n", b"\xef\xbb\xbf"])
    def test_blank_files_are_empty_pages(
        self, data: bytes, tmp_path: Path, settings: Settings
    ) -> None:
        page = read_csv_bytes(tmp_path, data, settings)

        assert page.status is PageStatus.EMPTY
        assert page.text == ""
        assert page.table is not None
        assert page.table.rows == []

    def test_long_field_within_the_lifted_limit(self, tmp_path: Path, settings: Settings) -> None:
        long_text = "x" * 1_000_000
        data = f"Item,Amount\n{long_text},5\nshort,6\n".encode()

        page = read_csv_bytes(tmp_path, data, settings)

        assert page.table is not None
        assert page.table.rows[0] == [long_text, "5"]
        assert page.warnings == []

    def test_over_long_field_after_good_rows_keeps_them_with_a_warning(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        too_long = "x" * (spreadsheets._CSV_FIELD_LIMIT_CHARS + 1)
        data = f"Item,Amount\nWidget,5\nGadget,7\n{too_long},9\nBolt,1\n".encode()

        page = read_csv_bytes(tmp_path, data, settings)

        assert page.status is PageStatus.OK
        assert page.table is not None
        assert page.table.rows == [["Widget", "5"], ["Gadget", "7"]]
        assert page.warnings == [
            "Reading stopped at row 4 because the CSV is malformed there; "
            "the rows after it were dropped."
        ]

    def test_over_long_first_field_is_corrupted(self, tmp_path: Path, settings: Settings) -> None:
        path = tmp_path / "input.csv"
        path.write_text("x" * (spreadsheets._CSV_FIELD_LIMIT_CHARS + 1) + ",1\n", encoding="utf-8")

        with pytest.raises(CorruptedFileError) as caught:
            read_pages(path, FileType.CSV, settings)

        assert "field" not in caught.value.reason


class TestLaziness:
    @pytest.mark.parametrize("file_type", [FileType.XLSX, FileType.XLS, FileType.CSV])
    def test_nothing_is_touched_until_iterated(
        self, file_type: FileType, tmp_path: Path, settings: Settings
    ) -> None:
        pages = iter_spreadsheet_pages(tmp_path / "missing", file_type, settings)

        assert isinstance(pages, types.GeneratorType)
        with pytest.raises(FileNotFoundError):
            next(pages)

    def test_xlsx_first_sheet_is_available_before_the_second_is_read(
        self, tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"One": LINE_ITEMS, "Two": LINE_ITEMS})
        titles_read: list[str] = []
        real_read = spreadsheets._read_xlsx_sheet

        def spying_read(sheet: Any, number: int, max_rows: int) -> PageResult:
            titles_read.append(sheet.title)
            return real_read(sheet, number, max_rows)

        monkeypatch.setattr(spreadsheets, "_read_xlsx_sheet", spying_read)
        pages = iter_spreadsheet_pages(path, FileType.XLSX, settings)

        first = next(pages)
        assert first.sheet_name == "One"
        assert titles_read == ["One"]

        assert next(pages).sheet_name == "Two"
        assert titles_read == ["One", "Two"]

    def test_xlsx_rows_of_the_second_sheet_stay_unparsed(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        """A second sheet that breaks only past its start does not delay the first."""
        path = write_xlsx(tmp_path / "book.xlsx", {"One": LINE_ITEMS, "Two": LINE_ITEMS})
        broken = f'<worksheet {MAIN_NS}><dimension ref="A1"/><sheetData><row r="1"><c r='
        rewrite_zip(path, {"xl/worksheets/sheet2.xml": broken.encode()})

        pages = iter_spreadsheet_pages(path, FileType.XLSX, settings)

        assert next(pages).status is PageStatus.OK
        assert next(pages).status is PageStatus.FAILED

    def test_xls_loads_and_releases_one_sheet_at_a_time(
        self, tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = write_xls(tmp_path / "book.xls", {"One": LINE_ITEMS, "Two": LINE_ITEMS})
        books: list[Any] = []
        real_open = xlrd.open_workbook

        def spying_open(*args: Any, **kwargs: Any) -> Any:
            assert kwargs["on_demand"] is True
            book = real_open(*args, **kwargs)
            books.append(book)
            return book

        monkeypatch.setattr(spreadsheets.xlrd, "open_workbook", spying_open)
        pages = iter_spreadsheet_pages(path, FileType.XLS, settings)

        assert next(pages).sheet_name == "One"
        assert not books[0].sheet_loaded(0)
        assert not books[0].sheet_loaded(1)

        assert next(pages).sheet_name == "Two"
        assert not books[0].sheet_loaded(1)

        pages.close()
        assert books[0]._resources_released == 1

    def test_xlsx_workbook_is_closed_when_abandoned(
        self, tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"One": LINE_ITEMS, "Two": LINE_ITEMS})
        workbooks: list[Any] = []
        real_load = spreadsheets.load_workbook

        def spying_load(*args: Any, **kwargs: Any) -> Any:
            workbook = real_load(*args, **kwargs)
            workbooks.append(workbook)
            return workbook

        monkeypatch.setattr(spreadsheets, "load_workbook", spying_load)
        pages = iter_spreadsheet_pages(path, FileType.XLSX, settings)
        next(pages)
        archive = workbooks[0]._archive
        assert archive.fp is not None

        pages.close()

        assert archive.fp is None


def assert_safe_reason(error: ProcessingError, tmp_path: Path) -> None:
    assert error.reason
    assert str(tmp_path) not in error.reason
    assert "xl/" not in error.reason
    assert "Traceback" not in error.reason
    assert "b'" not in error.reason  # xlrd quotes raw file bytes in its messages


class TestCorruptWorkbooks:
    def test_truncated_xlsx(self, tmp_path: Path, settings: Settings) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"A": LINE_ITEMS})
        path.write_bytes(path.read_bytes()[: path.stat().st_size // 2])

        with pytest.raises(CorruptedFileError) as caught:
            read_pages(path, FileType.XLSX, settings)

        assert_safe_reason(caught.value, tmp_path)

    def test_zip_that_is_not_a_workbook(self, tmp_path: Path, settings: Settings) -> None:
        path = tmp_path / "book.xlsx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("hello.txt", "not a workbook")

        with pytest.raises(CorruptedFileError) as caught:
            read_pages(path, FileType.XLSX, settings)

        assert_safe_reason(caught.value, tmp_path)

    def test_content_types_without_a_workbook(self, tmp_path: Path, settings: Settings) -> None:
        path = tmp_path / "book.xlsx"
        content_types = (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", content_types)

        with pytest.raises(CorruptedFileError) as caught:
            read_pages(path, FileType.XLSX, settings)

        assert_safe_reason(caught.value, tmp_path)

    @pytest.mark.parametrize(
        ("member", "content"),
        [
            ("xl/workbook.xml", b"<workbook"),
            ("xl/workbook.xml", b"not xml at all"),
            ("xl/_rels/workbook.xml.rels", b"<Relationships"),
            ("xl/styles.xml", b"<styleSheet"),
            ("[Content_Types].xml", b"\xff\xfe garbage"),
        ],
    )
    def test_damaged_workbook_parts(
        self, member: str, content: bytes, tmp_path: Path, settings: Settings
    ) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"A": LINE_ITEMS})
        rewrite_zip(path, {member: content})

        with pytest.raises(CorruptedFileError) as caught:
            read_pages(path, FileType.XLSX, settings)

        assert_safe_reason(caught.value, tmp_path)

    def test_damaged_shared_strings(self, tmp_path: Path, settings: Settings) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"A": LINE_ITEMS})
        add_shared_strings(path, b"<sst><si><t>unterminated")

        with pytest.raises(CorruptedFileError) as caught:
            read_pages(path, FileType.XLSX, settings)

        assert_safe_reason(caught.value, tmp_path)

    def test_flipped_bytes_in_a_sheet_fail_only_that_sheet(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        path = write_xlsx(
            tmp_path / "book.xlsx", {"Good": LINE_ITEMS, "Bad": LINE_ITEMS, "Also Good": LINE_ITEMS}
        )
        padded = sheet_xml(
            *(
                f'<row r="{i}"><c r="A{i}"><v>{i}</v></c><c r="B{i}"><v>{i * 2}</v></c></row>'
                for i in range(1, 2000)
            ),
            dimension='<dimension ref="A1:B1999"/>',
        )
        with zipfile.ZipFile(path) as archive:
            members = {info.filename: archive.read(info.filename) for info in archive.infolist()}
        members["xl/worksheets/sheet2.xml"] = padded
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
            info = archive.getinfo("xl/worksheets/sheet2.xml")
            start, size = info.header_offset, info.compress_size
        raw = bytearray(path.read_bytes())
        # Inside the compressed data, past the part read to find the dimension.
        for offset in range(start + size // 2, start + size - 8):
            raw[offset] ^= 0xFF
        path.write_bytes(bytes(raw))

        good, bad, also_good = read_pages(path, FileType.XLSX, settings)

        assert good.status is PageStatus.OK
        assert also_good.status is PageStatus.OK
        assert also_good.sheet_name == "Also Good"
        assert bad.status is PageStatus.FAILED
        assert bad.page_number == 2
        assert bad.sheet_name == "Bad"
        assert bad.text == ""
        assert bad.error is not None
        assert "'Bad'" in bad.error
        assert str(tmp_path) not in bad.error

    def test_sheet_with_broken_xml_fails_alone(self, tmp_path: Path, settings: Settings) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"Good": LINE_ITEMS, "Bad": LINE_ITEMS})
        broken = f'<worksheet {MAIN_NS}><dimension ref="A1:B5"/><sheetData><row r="1"><c r='
        rewrite_zip(path, {"xl/worksheets/sheet2.xml": broken.encode()})

        good, bad = read_pages(path, FileType.XLSX, settings)

        assert good.status is PageStatus.OK
        assert bad.status is PageStatus.FAILED
        assert bad.table is None

    def test_failed_sheet_does_not_keep_partial_rows(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"Good": LINE_ITEMS})
        row = '<row r="1"><c r="A1"><v>111</v></c></row>'
        broken = f'<worksheet {MAIN_NS}><dimension ref="A1:B5"/><sheetData>{row}<row r="2"><c r='
        rewrite_zip(path, {"xl/worksheets/sheet1.xml": broken.encode()})

        [page] = read_pages(path, FileType.XLSX, settings)

        assert page.status is PageStatus.FAILED
        assert "111" not in page.text

    def test_garbage_xls(self, tmp_path: Path, settings: Settings) -> None:
        path = tmp_path / "book.xls"
        path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x07garbage" * 200)

        with pytest.raises(CorruptedFileError) as caught:
            read_pages(path, FileType.XLS, settings)

        assert_safe_reason(caught.value, tmp_path)

    def test_plain_text_as_xls(self, tmp_path: Path, settings: Settings) -> None:
        path = tmp_path / "book.xls"
        path.write_bytes(b"this is not a workbook")

        with pytest.raises(CorruptedFileError) as caught:
            read_pages(path, FileType.XLS, settings)

        assert_safe_reason(caught.value, tmp_path)

    def test_truncated_xls(self, tmp_path: Path, settings: Settings) -> None:
        path = write_xls(tmp_path / "book.xls", {"A": LINE_ITEMS})
        path.write_bytes(path.read_bytes()[: path.stat().st_size // 2])

        with pytest.raises(CorruptedFileError) as caught:
            read_pages(path, FileType.XLS, settings)

        assert_safe_reason(caught.value, tmp_path)

    def test_xlsx_handed_to_the_xls_reader(self, tmp_path: Path, settings: Settings) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"A": LINE_ITEMS})

        with pytest.raises(CorruptedFileError):
            read_pages(path, FileType.XLS, settings)

    def test_xls_handed_to_the_xlsx_reader(self, tmp_path: Path, settings: Settings) -> None:
        path = write_xls(tmp_path / "book.xls", {"A": LINE_ITEMS})

        with pytest.raises(CorruptedFileError):
            read_pages(path, FileType.XLSX, settings)

    def test_one_bad_xls_sheet_fails_alone(self, tmp_path: Path, settings: Settings) -> None:
        workbook = xlwt.Workbook()
        workbook.add_sheet("Good").write(0, 0, "fine")
        workbook.add_sheet("Bad").write(0, 0, "broken")
        stream = bytearray(workbook.get_biff_data())
        # The last BOF record starts the last sheet. xlrd finds it by offset, and needs it intact.
        bof = stream.rfind(b"\x09\x08\x10\x00")
        stream[bof : bof + 4] = b"\xff\xff\x10\x00"
        path = tmp_path / "book.xls"
        XlsDoc().save(str(path), bytes(stream))

        good, bad = read_pages(path, FileType.XLS, settings)

        assert good.status is PageStatus.OK
        assert good.text.endswith("fine")
        assert bad.status is PageStatus.FAILED
        assert bad.sheet_name == "Bad"
        assert bad.error is not None
        assert str(tmp_path) not in bad.error

    def test_xlrd_does_not_print_to_stdout(
        self, tmp_path: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_xls(tmp_path / "book.xls", {"A": LINE_ITEMS})
        # xlrd logs a warning for a file with trailing bytes that do not fill a whole sector.
        path.write_bytes(path.read_bytes() + b"\x00" * 100)

        read_pages(path, FileType.XLS, settings)

        assert capsys.readouterr().out == ""

    def test_not_a_spreadsheet_type(self, tmp_path: Path, settings: Settings) -> None:
        with pytest.raises(UnsupportedFileError):
            read_pages(tmp_path / "x.pdf", FileType.PDF, settings)


class TestEncrypted:
    def test_encrypted_xls(self, tmp_path: Path, settings: Settings) -> None:
        """A real FILEPASS record after the BOF, which is how Excel marks an encrypted workbook."""
        workbook = xlwt.Workbook()
        workbook.add_sheet("A").write(0, 0, "secret")
        stream = workbook.get_biff_data()
        bof_length = 4 + stream[2] + stream[3] * 256
        filepass = b"\x2f\x00\x06\x00" + b"\x00\x00\x01\x00\x01\x00"
        path = tmp_path / "book.xls"
        XlsDoc().save(str(path), stream[:bof_length] + filepass + stream[bof_length:])

        with pytest.raises(EncryptedFileError) as caught:
            read_pages(path, FileType.XLS, settings)

        assert caught.value.reason == "This Excel workbook is password protected."

    def test_encrypted_xlsx_does_not_crash(self, tmp_path: Path, settings: Settings) -> None:
        plain = write_xlsx(tmp_path / "plain.xlsx", {"A": LINE_ITEMS})
        path = tmp_path / "locked.xlsx"
        with plain.open("rb") as source, path.open("wb") as target:
            OOXMLFile(source).encrypt("password", target)
        assert path.read_bytes().startswith(b"\xd0\xcf\x11\xe0")

        with pytest.raises((CorruptedFileError, EncryptedFileError)) as caught:
            read_pages(path, FileType.XLSX, settings)

        assert_safe_reason(caught.value, tmp_path)


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY a "lollollollollollollollollollol">
 <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
 <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
 <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">
 <!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">
 <!ENTITY f "&e;&e;&e;&e;&e;&e;&e;&e;&e;&e;">
 <!ENTITY g "&f;&f;&f;&f;&f;&f;&f;&f;&f;&f;">
 <!ENTITY h "&g;&g;&g;&g;&g;&g;&g;&g;&g;&g;">
 <!ENTITY i "&h;&h;&h;&h;&h;&h;&h;&h;&h;&h;">
]>
"""


class TestXmlBombs:
    def test_openpyxl_uses_defusedxml(self) -> None:
        assert openpyxl.DEFUSEDXML is True

    def test_billion_laughs_in_shared_strings(self, tmp_path: Path, settings: Settings) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"A": LINE_ITEMS})
        body = f"<sst {MAIN_NS}><si><t>&i;</t></si></sst>".encode()
        add_shared_strings(path, BILLION_LAUGHS + body)
        started = time.perf_counter()

        with pytest.raises(CorruptedFileError) as caught:
            read_pages(path, FileType.XLSX, settings)

        assert time.perf_counter() - started < 5
        assert_safe_reason(caught.value, tmp_path)
        assert isinstance(caught.value.__cause__, ValueError)  # openpyxl wraps EntitiesForbidden
        assert isinstance(caught.value.__cause__.__cause__, DefusedXmlException)

    @pytest.mark.parametrize(
        ("member", "body"),
        [
            (
                "xl/worksheets/sheet1.xml",
                f'<worksheet {MAIN_NS}><sheetData><row r="1"><c r="A1" t="inlineStr">'
                "<is><t>&i;</t></is></c></row></sheetData></worksheet>",
            ),
            ("xl/workbook.xml", f"<workbook {MAIN_NS}>&i;</workbook>"),
        ],
    )
    def test_billion_laughs_in_other_parts(
        self, member: str, body: str, tmp_path: Path, settings: Settings
    ) -> None:
        path = write_xlsx(tmp_path / "book.xlsx", {"A": LINE_ITEMS})
        rewrite_zip(path, {member: BILLION_LAUGHS + body.encode()})
        started = time.perf_counter()

        with pytest.raises(CorruptedFileError) as caught:
            read_pages(path, FileType.XLSX, settings)

        assert time.perf_counter() - started < 5
        assert_safe_reason(caught.value, tmp_path)

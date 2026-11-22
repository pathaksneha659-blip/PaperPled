from src.export.sheets import _prepare_news, flatten, write_tab


class FakeWorksheet:
    def __init__(self):
        self.updates = []
        self.frozen = None
        self.cleared = False

    def clear(self):
        self.cleared = True

    def values_update(self, range_name, params, body):
        self.updates.append((range_name, params, body))

    def freeze(self, rows):
        self.frozen = rows


class FakeSpreadsheet:
    def __init__(self):
        self.worksheets = {}

    def worksheet(self, title):
        from gspread.exceptions import WorksheetNotFound
        if title not in self.worksheets:
            raise WorksheetNotFound(title)
        return self.worksheets[title]

    def add_worksheet(self, title, rows, cols):
        worksheet = FakeWorksheet()
        self.worksheets[title] = worksheet
        return worksheet


def test_flatten_uses_dotted_paths_and_joins_lists():
    assert flatten({
        "source": {"name": "Example"},
        "content": {"data": {"tags": ["a", "b"]}},
    }) == {
        "source.name": "Example",
        "content.data.tags": "a; b",
    }


def test_write_tab_raw_chunks_and_freezes_header():
    spreadsheet = FakeSpreadsheet()
    rows = [
        {"schemaVersion": "1", "recordType": "JOB", "source": {"name": "A", "url": "https://a"}},
        {"schemaVersion": "1", "recordType": "JOB", "source": {"name": "B", "url": "https://b"}},
    ]
    worksheet = write_tab(spreadsheet, "Jobs", rows, chunk=1)

    assert worksheet.updates[0][1] == {"valueInputOption": "RAW"}
    assert worksheet.updates[0][2]["values"][0][:4] == [
        "schemaVersion", "recordType", "source.name", "source.url",
    ]
    assert len(worksheet.updates) == 3
    assert worksheet.frozen == 1


def test_news_text_is_truncated_at_google_cell_limit():
    prepared = _prepare_news({"content": {"full_text": "x" * 45_001}})
    assert len(prepared["content"]["full_text"]) == 45_000
    assert prepared["content"]["full_text_truncated"] is True

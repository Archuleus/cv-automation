from __future__ import annotations

import json

import pytest

from jobbot.connectors.ats import Board
from jobbot.connectors.boards import (
    BoardRegistryError,
    load_boards,
    merge_boards,
    save_boards,
)


def _write(tmp_path, payload) -> object:
    path = tmp_path / "boards.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestLoad:
    def test_reads_valid_entries(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "boards": [
                    {
                        "provider": "greenhouse",
                        "token": "acme",
                        "name": "Acme",
                        "domain": "acme.com",
                        "country": "TR",
                    }
                ]
            },
        )

        boards = load_boards(path)

        assert len(boards) == 1
        assert boards[0].provider == "greenhouse"
        assert boards[0].country == "TR"

    def test_accepts_a_bare_list(self, tmp_path):
        path = _write(tmp_path, [{"provider": "lever", "token": "acme"}])

        assert len(load_boards(path)) == 1

    def test_defaults_name_to_the_token(self, tmp_path):
        path = _write(tmp_path, [{"provider": "lever", "token": "acme"}])

        assert load_boards(path)[0].name == "acme"

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert load_boards(tmp_path / "absent.json") == ()

    def test_drops_duplicate_tokens(self, tmp_path):
        path = _write(
            tmp_path,
            [
                {"provider": "lever", "token": "acme"},
                {"provider": "lever", "token": "ACME"},
            ],
        )

        assert len(load_boards(path)) == 1

    def test_same_token_on_two_providers_is_kept(self, tmp_path):
        path = _write(
            tmp_path,
            [
                {"provider": "lever", "token": "acme"},
                {"provider": "greenhouse", "token": "acme"},
            ],
        )

        assert len(load_boards(path)) == 2


class TestLoadRejections:
    def test_invalid_json(self, tmp_path):
        path = tmp_path / "boards.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(BoardRegistryError, match="not valid JSON"):
            load_boards(path)

    def test_unknown_provider_is_loud(self, tmp_path):
        # Silently skipping would mean a typo'd provider quietly polls nothing.
        path = _write(tmp_path, [{"provider": "myspace", "token": "acme"}])

        with pytest.raises(BoardRegistryError, match="unknown provider"):
            load_boards(path)

    def test_missing_token(self, tmp_path):
        path = _write(tmp_path, [{"provider": "lever"}])

        with pytest.raises(BoardRegistryError, match="provider and token"):
            load_boards(path)

    def test_entry_is_not_an_object(self, tmp_path):
        path = _write(tmp_path, ["greenhouse"])

        with pytest.raises(BoardRegistryError, match="not an object"):
            load_boards(path)

    def test_top_level_is_not_a_list(self, tmp_path):
        path = _write(tmp_path, {"boards": "acme"})

        with pytest.raises(BoardRegistryError, match="list of boards"):
            load_boards(path)


class TestSave:
    def test_round_trips(self, tmp_path):
        path = tmp_path / "out.json"
        original = (
            Board("lever", "zeta", "Zeta", "zeta.com", "TR"),
            Board("greenhouse", "alpha", "Alpha", "alpha.com", None),
        )

        save_boards(original, path)
        reloaded = load_boards(path)

        assert {(b.provider, b.token) for b in reloaded} == {
            (b.provider, b.token) for b in original
        }

    def test_writes_in_a_stable_order(self, tmp_path):
        path = tmp_path / "out.json"
        save_boards(
            [Board("lever", "zeta", "Z", ""), Board("greenhouse", "alpha", "A", "")], path
        )

        tokens = [b["token"] for b in json.loads(path.read_text(encoding="utf-8"))["boards"]]

        assert tokens == ["alpha", "zeta"]

    def test_creates_the_parent_directory(self, tmp_path):
        path = tmp_path / "nested" / "deep" / "boards.json"

        save_boards([Board("lever", "a", "A", "")], path)

        assert path.exists()


class TestMerge:
    def test_adds_new_boards(self):
        merged = merge_boards(
            [Board("lever", "a", "A", "")], [Board("greenhouse", "b", "B", "")]
        )

        assert len(merged) == 2

    def test_existing_entries_win(self):
        # A discovered board must not overwrite a curated name and domain.
        curated = Board("lever", "a", "Acme Corp", "acme.com", "TR")
        discovered = Board("lever", "A", "a", "", None)

        merged = merge_boards([curated], [discovered])

        assert len(merged) == 1
        assert merged[0].name == "Acme Corp"

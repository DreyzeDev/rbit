"""Editing files that are not UTF-8: cp1251, cp1252, UTF-16 with BOM, UTF-8 with stray bytes.

`edit` used to read such a file as UTF-8 with escapes, so the model's text (real Cyrillic)
never matched the bytes and the file could not be edited at all. Files now keep the
encoding they already have, and the model sees decoded text.
"""

from __future__ import annotations

import codecs
from pathlib import Path
from typing import Any

import pytest

from rbit.models import Session
from rbit.tools import (
    FileEncodingError,
    ToolExecutor,
    atomic_write_text,
    decode_file_bytes,
    detect_file_encoding,
    encode_file_text,
)

RU = "привет мир\nx = 1\n"


def _executor(tmp_path: Path) -> ToolExecutor:
    return ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: {})


def _put(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ detection


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"", "utf-8"),
        (b"plain ascii\n", "utf-8"),
        ("привет".encode("utf-8"), "utf-8"),
        (codecs.BOM_UTF8 + "привет".encode("utf-8"), "utf-8"),
        (RU.encode("cp1251"), "cp1251"),
        (codecs.BOM_UTF16_LE + "hi".encode("utf-16-le"), "utf-16-le"),
        (codecs.BOM_UTF16_BE + "hi".encode("utf-16-be"), "utf-16-be"),
        (codecs.BOM_UTF32_LE + "hi".encode("utf-32-le"), "utf-32-le"),
        (b"\x00\x01\x02\xff\xfe binary", "utf-8"),  # binary: unchanged byte-exact behaviour
    ],
)
def test_detect_file_encoding(raw: bytes, expected: str) -> None:
    assert detect_file_encoding(raw) == expected


def test_mostly_utf8_with_a_stray_byte_stays_utf8() -> None:
    raw = ("привет мир, это обычный текст в utf-8. " * 6).encode("utf-8") + b"\xff"
    assert detect_file_encoding(raw) == "utf-8"
    text, encoding = decode_file_bytes(raw)
    assert encoding == "utf-8" and text.startswith("привет мир")


def test_decode_encode_is_the_identity_for_every_byte_value() -> None:
    raw = bytes(range(32, 256)) * 3  # every high byte, including ones cp1251 has no letter for
    for guess in ("cp1251", "cp1252", "utf-8"):
        text = raw.decode(guess, errors="surrogateescape")
        assert encode_file_text(text, guess) == raw


def test_unencodable_characters_raise_a_clear_error() -> None:
    with pytest.raises(FileEncodingError) as info:
        encode_file_text("ok 😀 ok", "cp1251", "notes.txt")
    assert "cp1251" in str(info.value) and "notes.txt" in str(info.value) and "😀" in str(info.value)


# --------------------------------------------------------------- the tools


@pytest.mark.asyncio
async def test_read_shows_decoded_text_and_names_the_encoding(tmp_path: Path) -> None:
    _put(tmp_path, "a.txt", RU.encode("cp1251"))
    result = await _executor(tmp_path).execute("read", {"filePath": "a.txt"})
    assert result.ok is True
    assert "1: привет мир" in result.output
    assert "�" not in result.output
    assert "[file encoding: cp1251" in result.output


@pytest.mark.asyncio
async def test_edit_matches_decoded_text_and_keeps_cp1251(tmp_path: Path) -> None:
    path = _put(tmp_path, "a.txt", RU.encode("cp1251"))
    result = await _executor(tmp_path).execute(
        "edit", {"filePath": "a.txt", "oldString": "привет", "newString": "здравствуй"}
    )
    assert result.ok is True, result
    assert path.read_bytes() == "здравствуй мир\nx = 1\n".encode("cp1251")


@pytest.mark.asyncio
async def test_multi_edit_keeps_cp1251(tmp_path: Path) -> None:
    path = _put(tmp_path, "a.txt", RU.encode("cp1251"))
    result = await _executor(tmp_path).execute(
        "multi_edit",
        {
            "filePath": "a.txt",
            "edits": [
                {"oldString": "привет", "newString": "здравствуй"},
                {"oldString": "x = 1", "newString": "x = 2"},
            ],
        },
    )
    assert result.ok is True, result
    assert path.read_bytes() == "здравствуй мир\nx = 2\n".encode("cp1251")


@pytest.mark.asyncio
async def test_apply_patch_keeps_cp1251(tmp_path: Path) -> None:
    path = _put(tmp_path, "a.txt", RU.encode("cp1251"))
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n-привет мир\n+добрый день\n x = 1\n"
    result = await _executor(tmp_path).execute("apply_patch", {"patch": patch})
    assert result.ok is True, result
    assert path.read_bytes() == "добрый день\nx = 1\n".encode("cp1251")


@pytest.mark.asyncio
async def test_write_over_an_existing_cp1251_file_keeps_its_encoding(tmp_path: Path) -> None:
    path = _put(tmp_path, "a.txt", RU.encode("cp1251"))
    result = await _executor(tmp_path).execute("write", {"filePath": "a.txt", "content": "новый текст\n"})
    assert result.ok is True, result
    assert path.read_bytes() == "новый текст\n".encode("cp1251")


@pytest.mark.asyncio
async def test_a_new_file_is_still_utf8(tmp_path: Path) -> None:
    await _executor(tmp_path).execute("write", {"filePath": "new.txt", "content": "новый текст\n"})
    assert (tmp_path / "new.txt").read_bytes() == "новый текст\n".encode("utf-8")


@pytest.mark.asyncio
async def test_characters_cp1251_cannot_hold_are_refused_and_the_file_is_untouched(tmp_path: Path) -> None:
    original = RU.encode("cp1251")
    path = _put(tmp_path, "a.txt", original)
    result = await _executor(tmp_path).execute(
        "edit", {"filePath": "a.txt", "oldString": "привет", "newString": "привет 😀"}
    )
    assert result.ok is False
    assert "cp1251" in (result.output + result.summary)
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_undo_and_redo_round_trip_a_cp1251_file(tmp_path: Path) -> None:
    original = RU.encode("cp1251")
    path = _put(tmp_path, "a.txt", original)
    executor = _executor(tmp_path)
    await executor.execute("edit", {"filePath": "a.txt", "oldString": "привет", "newString": "пока"})
    edited = path.read_bytes()
    assert edited == "пока мир\nx = 1\n".encode("cp1251")

    assert "Reverted" in executor.undo_last()
    assert path.read_bytes() == original  # exact bytes, not a re-encoding
    assert "Restored" in executor.redo_last()
    assert path.read_bytes() == edited


@pytest.mark.asyncio
async def test_edit_keeps_cp1252_text_and_crlf(tmp_path: Path) -> None:
    path = _put(tmp_path, "menu.txt", "café\r\nthé\r\n".encode("cp1252"))
    result = await _executor(tmp_path).execute(
        "edit", {"filePath": "menu.txt", "oldString": "café", "newString": "crème"}
    )
    assert result.ok is True, result
    assert path.read_bytes() == "crème\r\nthé\r\n".encode("cp1252")


@pytest.mark.asyncio
async def test_edit_keeps_utf16_bom_and_crlf(tmp_path: Path) -> None:
    raw = codecs.BOM_UTF16_LE + "alpha\r\nbeta\r\n".encode("utf-16-le")
    path = _put(tmp_path, "u16.txt", raw)
    result = await _executor(tmp_path).execute(
        "edit", {"filePath": "u16.txt", "oldString": "alpha", "newString": "gamma"}
    )
    assert result.ok is True, result
    assert path.read_bytes() == codecs.BOM_UTF16_LE + "gamma\r\nbeta\r\n".encode("utf-16-le")


@pytest.mark.asyncio
async def test_utf8_with_bom_and_a_stray_byte_is_unchanged_behaviour(tmp_path: Path) -> None:
    raw = codecs.BOM_UTF8 + "привет мир\n".encode("utf-8") * 6 + b"tail \xff\n"
    path = _put(tmp_path, "mixed.txt", raw)
    result = await _executor(tmp_path).execute(
        "edit", {"filePath": "mixed.txt", "oldString": "tail ", "newString": "end "}
    )
    assert result.ok is True, result
    assert path.read_bytes() == raw.replace(b"tail ", b"end ")  # BOM and the stray byte survive


@pytest.mark.asyncio
async def test_grep_finds_cyrillic_inside_a_cp1251_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import rbit.tools as tools_module

    real_which = tools_module.shutil.which
    monkeypatch.setattr(
        tools_module.shutil, "which", lambda name, *a, **k: None if name == "rg" else real_which(name, *a, **k)
    )
    _put(tmp_path, "a.txt", RU.encode("cp1251"))
    result = await _executor(tmp_path).execute("grep", {"pattern": "привет"})
    assert result.ok is True
    assert "привет мир" in result.output


def test_atomic_write_text_keeps_the_encoding_of_an_existing_file(tmp_path: Path) -> None:
    path = _put(tmp_path, "a.txt", RU.encode("cp1251"))
    atomic_write_text(path, "другой текст\n")
    assert path.read_bytes() == "другой текст\n".encode("cp1251")
    with pytest.raises(FileEncodingError):
        atomic_write_text(path, "emoji 😀\n")
    assert path.read_bytes() == "другой текст\n".encode("cp1251")  # a refused write changes nothing


def _unused(_: Any) -> None:  # keeps `Any` imported for type checkers without noise
    return None


# --- grep: ripgrep cannot read legacy code pages ---------------------------------


def _legacy_workspace(tmp_path: Path) -> Path:
    (tmp_path / "utf8.txt").write_text("привет мир\n", encoding="utf-8")
    (tmp_path / "cp1251.txt").write_bytes("привет мир\n".encode("cp1251"))
    return tmp_path


def test_non_ascii_grep_skips_ripgrep_so_legacy_encoded_files_are_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ex = ToolExecutor(workspace=_legacy_workspace(tmp_path), session=Session())
    calls: list[str] = []

    def spy(self: Any, pattern: str, *_a: Any, **_k: Any) -> None:
        calls.append(pattern)
        return None

    monkeypatch.setattr(ToolExecutor, "_grep_with_ripgrep", spy)

    result = ex._tool_grep({"pattern": "привет"})

    assert calls == []  # ripgrep was not even tried
    assert "cp1251.txt" in result.output and "utf8.txt" in result.output
    assert "cp1251.txt" in ex._tool_grep({"pattern": "привет", "filesOnly": True}).output


def test_ascii_grep_still_uses_ripgrep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ex = ToolExecutor(workspace=_legacy_workspace(tmp_path), session=Session())
    calls: list[str] = []

    def spy(self: Any, pattern: str, *_a: Any, **_k: Any) -> Any:
        from rbit.tools import ToolResult

        calls.append(pattern)
        return ToolResult(True, "1 lines (rg)", "x.txt:1:hello")

    monkeypatch.setattr(ToolExecutor, "_grep_with_ripgrep", spy)

    assert ex._tool_grep({"pattern": "hello"}).output == "x.txt:1:hello"
    assert calls == ["hello"]

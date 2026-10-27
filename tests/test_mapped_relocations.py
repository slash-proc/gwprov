"""Exact sites must preserve numeric data, cross-file pointers, and chunk semantics."""

import struct
import zlib

import pytest

from gwprov.mapped_relocations import (
    mapped_declaration,
    relocate_exact_bytes,
    validate_exact_relocations,
)

BASE = 0xBEEF0000
CODE = 0xDEC00000


def table(groups=None):
    return {
        "method": "exact-relocation-sites",
        "version": 1,
        "groups": groups
        or [
            {"target": "assets.xip", "relocBase": BASE, "offsets": [0]},
            {"target": "code.xip", "relocBase": CODE, "offsets": [8]},
        ],
    }


def test_numeric_collision_cross_file_thumb_and_chunks():
    raw = struct.pack("<III", BASE + 5, BASE + 5, CODE + 1)
    placements = {"ASSETS.XIP": (0x90400000, 12), "code.xip": (0x92400000, 16)}
    result, count = relocate_exact_bytes(raw, table(), placements)
    assert count == 2
    assert struct.unpack("<III", result) == (0x90400005, BASE + 5, 0x92400001)
    for size in (4, 8):
        assert (
            b"".join(
                relocate_exact_bytes(
                    raw[i : i + size], table(), placements, source_offset=i, source_size=len(raw)
                )[0]
                for i in range(0, len(raw), size)
            )
            == result
        )
    assert struct.unpack("<III", raw) == (BASE + 5, BASE + 5, CODE + 1)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda t: t.update(version=2),
        lambda t: t.update(method="scan-anyway"),
        lambda t: t["groups"][0].update(relocBase=True),
        lambda t: t["groups"][0].update(relocBase=BASE + 1),
        lambda t: t["groups"][0].update(target="../assets.xip"),
        lambda t: t["groups"][0].update(offsets=[4, 0]),
        lambda t: t["groups"][0].update(offsets=[0, 0]),
        lambda t: t["groups"][0].update(offsets=[2]),
        lambda t: t["groups"][0].update(offsets=[12]),
        lambda t: t["groups"][1].update(offsets=[0]),
        lambda t: t["groups"][1].update(target="ASSETS.XIP"),
    ],
)
def test_invalid_tables_are_rejected(mutation):
    value = table()
    mutation(value)
    with pytest.raises(ValueError):
        validate_exact_relocations(value, source_size=12)


def test_bad_pointer_and_missing_target_are_rejected_before_publication():
    raw = struct.pack("<III", BASE + 5, BASE + 5, 0x12345678)
    with pytest.raises(ValueError, match="outside"):
        relocate_exact_bytes(
            raw, table(), {"assets.xip": (0x90400000, 12), "code.xip": (0x92400000, 16)}
        )
    with pytest.raises(ValueError, match="missing"):
        relocate_exact_bytes(raw, table(), {"assets.xip": (0x90400000, 12)})
    assert struct.unpack("<III", raw)[2] == 0x12345678
    with pytest.raises(ValueError):
        mapped_declaration({"relocations": table()}, "assets.xip", source_size=12)
    assert (
        mapped_declaration({"mapped": True, "relocBase": "0xDEC00000"}, "code.xip")["relocBase"]
        == CODE
    )


def test_real_frogfs_rebuild_uses_exact_sites_instead_of_legacy_scan(tmp_path):
    from gwprov.filesystem import _build_frogfs
    from gwprov.vendor.retrogo_sd.scripts.frogfs_pico8_ro import find_uncompressed_file_payload

    tree = tmp_path / "tree"
    (tree / "homebrews").mkdir(parents=True)
    assets = struct.pack("<II", BASE + 5, BASE + 5)
    code = struct.pack("<III", BASE + 1, CODE + 5, CODE + 5)
    (tree / "homebrews/assets.xip").write_bytes(assets)
    (tree / "homebrews/code.xip").write_bytes(code)
    work = tmp_path / "work"
    work.mkdir()
    image_path = tmp_path / "frogfs.bin"
    _build_frogfs(
        tree,
        image_path,
        work,
        extflash_offset=0x400000,
        mapped_artifacts=[("homebrews/assets.xip", BASE), ("homebrews/code.xip", CODE)],
        exact_relocations={
            "homebrews/assets.xip": table(
                [{"target": "assets.xip", "relocBase": BASE, "offsets": [0]}]
            ),
            "homebrews/code.xip": table(
                [
                    {"target": "assets.xip", "relocBase": BASE, "offsets": [0]},
                    {"target": "code.xip", "relocBase": CODE, "offsets": [4]},
                ]
            ),
        },
    )
    image = image_path.read_bytes()
    a, _ = find_uncompressed_file_payload(image, "homebrews/assets.xip")
    c, _ = find_uncompressed_file_payload(image, "homebrews/code.xip")
    assert struct.unpack_from("<II", image, a) == (0x90400000 + a + 5, BASE + 5)
    assert struct.unpack_from("<III", image, c) == (
        0x90400000 + a + 1,
        0x90400000 + c + 5,
        CODE + 5,
    )
    assert zlib.crc32(image[:-4]) == struct.unpack_from("<I", image, len(image) - 4)[0]
    assert (tree / "homebrews/assets.xip").read_bytes() == assets
    assert (tree / "homebrews/code.xip").read_bytes() == code


def test_converter_output_mapping_is_retained_and_unknown_method_fails_before_fetch(
    tmp_path, monkeypatch
):
    import hashlib
    import json
    from gwprov.dist import project

    assets = struct.pack("<III", BASE + 5, BASE + 5, CODE + 1)
    files = {"app.bin": b"app", "code.xip": bytes(16), "conv.wasm": b"\0asm"}

    def artifact(name, **extra):
        data = files[name]
        return {
            "filename": name,
            "url": name,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            **extra,
        }

    output = {
        "id": "assets",
        "filename": "assets.xip",
        "maxBytes": 12,
        "mapped": True,
        "relocations": table(),
    }
    manifest = {
        "schemaVersion": 1,
        "project": "exact",
        "tools": [
            {
                "id": "conv",
                "processor": {"type": "wasm", "version": 1},
                "binary": {**artifact("conv.wasm"), "file": "conv.wasm"},
                "limits": {"maxMemoryPages": 1, "maxOutputBytes": 12},
                "inputs": [
                    {
                        "id": "disc",
                        "required": True,
                        "allowMultiple": False,
                        "extensions": [".rom"],
                        "maxBytes": 4,
                        "strict": True,
                        "variants": [
                            {"id": "test", "sha1": hashlib.sha1(b"base").hexdigest(), "bytes": 4}
                        ],
                    }
                ],
                "outputs": [output],
            }
        ],
        "targets": [
            {
                "id": "gnw",
                "kind": "homebrew",
                "requiresAbi": {"version": 2, "minSize": 8},
                "artifacts": [
                    artifact("app.bin"),
                    artifact("code.xip", mapped=True, relocBase=CODE),
                ],
                "uses": [{"tool": "conv", "outputs": ["assets"], "required": True}],
            }
        ],
    }
    resolved = project.ResolvedProject(
        "test/exact", "https://x/versions.json", {"tag": "v1"}, "https://x/manifest.json", manifest
    )
    monkeypatch.setattr(project, "resolve_project", lambda *args, **kwargs: resolved)
    fetched = []

    def fetch(base, spec, **kwargs):
        name = spec.get("filename", spec.get("file"))
        fetched.append(name)
        return files[name]

    monkeypatch.setattr(project, "_fetch_checked", fetch)
    monkeypatch.setattr(
        project, "_run_wasm", lambda *args, **kwargs: [{"output_id": "assets", "bytes": assets}]
    )
    root = tmp_path / "content"
    input_path = tmp_path / "base.rom"
    input_path.write_bytes(b"base")
    project.install_project(
        "test/exact", output=root, variant="flash", input_files=[f"disc={input_path}"]
    )
    metadata = json.loads((root / "flash/.gwprov-projects.json").read_text())["test/exact:gnw"]
    mapped = next(item for item in metadata["mapped"] if item["path"] == "homebrews/assets.xip")
    assert mapped["relocations"] == table()
    assert (root / "flash/frogfs/homebrews/assets.xip").read_bytes() == assets
    fetched.clear()
    output["relocations"]["version"] = 99
    with pytest.raises(ValueError, match="unsupported"):
        project.install_project("test/exact", output=tmp_path / "bad", variant="flash")
    assert fetched == []

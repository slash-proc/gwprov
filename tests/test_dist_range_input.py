"""Processor v1 range-input extension coverage."""

import hashlib
import tempfile
from pathlib import Path

import wasmtime

from gwprov.dist.project import _run_wasm

RANGE_CONVERTER_WAT = r"""
(module
  (memory (export "memory") 1 2)
  (global $state (mut i32) (i32.const 0))
  (func (export "abi_version") (result i32) i32.const 1)
  (func (export "alloc") (param i32) (result i32) i32.const 0)
  (func (export "input_clear"))
  (func (export "input_add") (param i32 i32) (result i32) i32.const 0)
  (func (export "input_stream_add") (param i32 i32) (result i32) i32.const 0)
  (func (export "run") (param i32) (result i32) i32.const 1)
  (func (export "run_begin") (param i32) (result i32) i32.const 0)
  (func (export "run_step") (result i32)
    (if (i32.eq (global.get $state) (i32.const 0))
      (then (global.set $state (i32.const 1)) (return (i32.const 2)))
    )
    (if (i32.eq (global.get $state) (i32.const 2))
      (then (return (i32.const 0)))
    )
    i32.const 5
  )
  (func (export "input_request_index") (result i32) i32.const 0)
  (func (export "input_request_offset_lo") (result i32) i32.const -2147483644)
  (func (export "input_request_offset_hi") (result i32) i32.const 1)
  (func (export "input_request_len") (result i32) i32.const 4)
  (func (export "input_request_buffer_ptr") (result i32) i32.const 4096)
  (func (export "input_supply") (param $len i32) (param $ok i32) (result i32)
    (if (i32.ne (local.get $len) (i32.const 4)) (then (return (i32.const 3))))
    (if (i32.eqz (local.get $ok)) (then (return (i32.const 4))))
    (if (i32.ne (i32.load8_u (i32.const 4096)) (i32.const 83)) (then (return (i32.const 5))))
    (if (i32.ne (i32.load8_u (i32.const 4097)) (i32.const 77)) (then (return (i32.const 5))))
    (if (i32.ne (i32.load8_u (i32.const 4098)) (i32.const 71)) (then (return (i32.const 5))))
    (if (i32.ne (i32.load8_u (i32.const 4099)) (i32.const 33)) (then (return (i32.const 5))))
    (global.set $state (i32.const 2))
    i32.const 0
  )
  (func (export "stage_count") (result i32) i32.const 0)
  (func (export "stage_index") (result i32) i32.const 0)
  (func (export "stage_name_ptr") (param i32) (result i32) i32.const 0)
  (func (export "stage_name_len") (param i32) (result i32) i32.const 0)
  (func (export "output_count") (result i32) i32.const 1)
  (func (export "output_name_ptr") (param i32) (result i32) i32.const 1024)
  (func (export "output_name_len") (param i32) (result i32) i32.const 3)
  (func (export "output_ptr") (param i32) (result i32) i32.const 1027)
  (func (export "output_len") (param i32) (result i32) i32.const 4)
  (func (export "error_ptr") (result i32) i32.const 0)
  (func (export "error_len") (result i32) i32.const 0)
  (func (export "warnings_ptr") (result i32) i32.const 0)
  (func (export "warnings_len") (result i32) i32.const 0)
  (data (i32.const 1024) "outdone")
)
"""


def test_range_input_reads_sparse_file_above_four_gibibytes_and_signed_i32_boundary():
    wasm = wasmtime.wat2wasm(RANGE_CONVERTER_WAT)
    offset = (1 << 32) + 0x80000004
    size = offset + 4
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "large.iso"
        with path.open("wb") as source:
            source.seek(offset)
            source.write(b"SMG!")
        inputs = [
            {
                "filename": path.name,
                "path": path,
                "size": size,
                "sha1": "unused-by-runner",
                "bytes": None,
            }
        ]
        result = _run_wasm(
            wasm,
            {
                "processor": {"type": "wasm", "version": 1, "extensions": {"range-input": 1}},
                "binary": {"bytes": len(wasm), "sha256": hashlib.sha256(wasm).hexdigest()},
                "limits": {"maxMemoryPages": 2, "maxOutputBytes": 4},
                "outputs": [{"id": "out", "maxBytes": 4}],
            },
            inputs,
        )
    assert result == [{"output_id": "out", "bytes": b"done"}]


def test_unrecognised_processor_extension_is_refused():
    wasm = wasmtime.wat2wasm(RANGE_CONVERTER_WAT)
    tool = {
        "processor": {"type": "wasm", "version": 1, "extensions": {"unknown": 1}},
        "binary": {"bytes": len(wasm), "sha256": hashlib.sha256(wasm).hexdigest()},
        "limits": {"maxMemoryPages": 2, "maxOutputBytes": 4},
    }
    try:
        _run_wasm(wasm, tool, [])
    except ValueError as exc:
        assert "unsupported processor extensions" in str(exc)
    else:
        raise AssertionError("unknown processor extension was accepted")


def test_input_gate_hashes_a_range_source_without_loading_it_into_memory(tmp_path):
    from gwprov.dist.project import _input_files

    path = tmp_path / "sample.iso"
    payload = b"a browser host would read this in ranges"
    path.write_bytes(payload)
    digest = hashlib.sha1(payload).hexdigest()
    tool = {
        "processor": {"extensions": {"range-input": 1}},
        "inputs": [
            {
                "id": "disc",
                "required": True,
                "allowMultiple": False,
                "extensions": [".iso"],
                "maxBytes": 1024,
                "variants": [{"id": "sample", "sha1": digest, "bytes": len(payload)}],
            }
        ],
    }
    inputs, warnings = _input_files(tool, [f"disc={path}"], [], require_slots=True)
    assert warnings == []
    assert inputs[0]["bytes"] is None
    assert inputs[0]["path"] == path
    assert inputs[0]["size"] == len(payload)
    assert inputs[0]["sha1"] == digest

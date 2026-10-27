import hashlib

import pytest
import wasmtime

from gwprov.dist.project import (
    DEFAULT_WASM_FUEL,
    MAX_WASM_FUEL,
    _run_wasm,
    _validate_wasm_fuel,
)


def test_wasm_fuel_limits():
    assert _validate_wasm_fuel(DEFAULT_WASM_FUEL) == DEFAULT_WASM_FUEL
    assert _validate_wasm_fuel(MAX_WASM_FUEL) == MAX_WASM_FUEL
    for value in (0, -1, MAX_WASM_FUEL + 1, True, 1.5, "100"):
        with pytest.raises(ValueError, match="WASM fuel"):
            _validate_wasm_fuel(value)


def test_run_wasm_uses_the_requested_fuel_budget():
    wat = """(module
      (memory (export "memory") 1 2)
      (func (export "abi_version") (result i32) i32.const 1)
      (func (export "alloc") (param i32) (result i32) i32.const 0)
      (func (export "input_clear"))
      (func (export "input_add") (param i32 i32) (result i32) i32.const 0)
      (func (export "run") (param i32) (result i32) i32.const 0)
      (func (export "run_begin") (param i32) (result i32) i32.const 0)
      (func (export "run_step") (result i32)
        (local $remaining i32)
        i32.const 10000
        local.set $remaining
        block $done
          loop $work
            local.get $remaining
            i32.const 1
            i32.sub
            local.tee $remaining
            br_if $work
          end
        end
        i32.const 0)
      (func (export "stage_count") (result i32) i32.const 0)
      (func (export "stage_index") (result i32) i32.const 0)
      (func (export "stage_name_ptr") (param i32) (result i32) i32.const 0)
      (func (export "stage_name_len") (param i32) (result i32) i32.const 0)
      (func (export "output_count") (result i32) i32.const 0)
      (func (export "output_name_ptr") (param i32) (result i32) i32.const 0)
      (func (export "output_name_len") (param i32) (result i32) i32.const 0)
      (func (export "output_ptr") (param i32) (result i32) i32.const 0)
      (func (export "output_len") (param i32) (result i32) i32.const 0)
      (func (export "error_ptr") (result i32) i32.const 0)
      (func (export "error_len") (result i32) i32.const 0)
      (func (export "warnings_ptr") (result i32) i32.const 0)
      (func (export "warnings_len") (result i32) i32.const 0))"""
    wasm = wasmtime.wat2wasm(wat)
    tool = {
        "processor": {"type": "wasm", "version": 1},
        "binary": {"bytes": len(wasm), "sha256": hashlib.sha256(wasm).hexdigest()},
        "limits": {"maxMemoryPages": 2, "maxOutputBytes": 0},
        "outputs": [],
    }

    assert _run_wasm(wasm, tool, [], fuel=100_000) == []
    with pytest.raises(wasmtime.Trap, match="fuel"):
        _run_wasm(wasm, tool, [], fuel=100)

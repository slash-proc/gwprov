import pytest

from gwprov.qmp import parse_arm_registers


def test_parse_arm_registers_adds_cortex_m_aliases():
    registers = parse_arm_registers(
        "R00=00000001 R01=00000002 R13=24001000 R14=08001235 " "R15=08004567 XPSR=61000000"
    )

    assert registers["r0"] == 1
    assert registers["sp"] == registers["r13"] == 0x24001000
    assert registers["lr"] == registers["r14"] == 0x08001235
    assert registers["pc"] == registers["r15"] == 0x08004567
    assert registers["xpsr"] == 0x61000000


def test_parse_arm_registers_requires_stack_frame_registers():
    with pytest.raises(RuntimeError, match="SP, LR, and PC"):
        parse_arm_registers("R00=00000001 R13=24001000")

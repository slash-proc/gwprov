import unittest
from gwprov.debug_shell import DebugSession, RemoteGDBBackend


class WaitStopped(unittest.TestCase):
    def make_session(self, states, registers=None, register_error=None):
        session = DebugSession(object(), "hardware")
        remaining = iter(states)
        last = states[-1]

        def target_state():
            nonlocal last
            last = next(remaining, last)
            return last

        def read_registers():
            if register_error is not None:
                raise register_error
            return registers or {"pc": 0x08001234}

        session._target_state = target_state
        session.regs = read_registers
        return session

    def test_reset_is_reported_without_register_read(self):
        session = self.make_session(["reset"], register_error=RuntimeError("not halted"))
        result = session.wait_stopped(timeout=0.01, poll_interval=0.001)
        self.assertFalse(result["stopped"])
        self.assertEqual(result["status"], "reset")
        self.assertIn("not at a debug halt", result["reason"])

    def test_halt_to_reset_race_is_a_state_result_not_exception(self):
        session = self.make_session(
            ["halted", "reset"], register_error=RuntimeError("core is not halted")
        )
        result = session.wait_stopped(timeout=0.01, poll_interval=0.001)
        self.assertFalse(result["stopped"])
        self.assertEqual(result["observed_status"], "halted")
        self.assertEqual(result["status"], "reset")
        self.assertIn("core is not halted", result["register_error"])

    def test_stable_halt_returns_registers(self):
        session = self.make_session(["halted"], registers={"pc": 0x08001234})
        result = session.wait_stopped(timeout=0.01, poll_interval=0.001)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["status"], "halted")
        self.assertEqual(result["registers"], {"pc": 0x08001234})

    def test_remote_gdb_breakpoint_stays_halted_with_coherent_stop_pc(self):
        # RemoteGDBBackend has no PyOCD target object: stop state comes from
        # DHCSR, while register reads consult the backend's cached run state.
        backend = RemoteGDBBackend.__new__(RemoteGDBBackend)
        backend._is_running = True  # stale cache from before the hardware BKPT
        backend.register_numbers = {
            **{f"r{i}": i for i in range(13)},
            "sp": 13,
            "lr": 14,
            "pc": 15,
            "xpsr": 25,
        }
        pc = 0x08001235
        commands = []
        reads = []
        control = []

        def read_memory(address, size):
            reads.append((address, size))
            return (1 << 17).to_bytes(size, "little")  # DHCSR.S_HALT

        def send(command):
            commands.append(command)
            name = next(
                name
                for name, index in backend.register_numbers.items()
                if command == f"p{index:x}".encode("ascii")
            )
            value = pc if name == "pc" else 0
            return value.to_bytes(4, "little").hex().encode("ascii")

        backend.read_memory = read_memory
        backend._send_command = send
        backend._decode_hex = lambda reply, command: bytes.fromhex(reply.decode("ascii"))
        backend.halt = lambda: control.append("halt")
        backend.resume = lambda: control.append("resume")

        result = DebugSession(backend, "hardware").wait_stopped(timeout=0.01)

        self.assertTrue(result["stopped"])
        self.assertEqual(result["status"], "halted")
        self.assertEqual(result["registers"]["pc"], pc)
        self.assertFalse(backend._is_running)
        self.assertEqual(control, [])
        self.assertEqual(len(commands), 17)
        self.assertTrue(all(command.startswith(b"p") for command in commands))
        self.assertTrue(reads)
        self.assertTrue(all(item == (0xE000EDF0, 4) for item in reads))


if __name__ == "__main__":
    unittest.main()

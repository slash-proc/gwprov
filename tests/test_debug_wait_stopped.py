import unittest
from gwprov.debug_shell import DebugSession


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


if __name__ == "__main__":
    unittest.main()

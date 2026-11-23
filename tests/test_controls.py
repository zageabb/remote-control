"""GUI toolbar collapse behaviour without requiring a display server."""

import unittest
from unittest.mock import Mock

from remote_control.app import RemoteControlApp


class ToolbarCollapseTests(unittest.TestCase):
    def setUp(self):
        self.app = Mock()
        self.app.controls_collapsed = False
        self.app.connection_toolbar = Mock()
        self.app.direction_toolbar = Mock()
        self.app.viewer_toolbar = Mock()
        self.app.controls_toggle = Mock()
        self.app._redraw_last_frame = Mock()
        self.app.after_idle = Mock()
        self.toolbars = (
            self.app.connection_toolbar,
            self.app.direction_toolbar,
            self.app.viewer_toolbar,
        )

    def test_collapse_and_restore_preserve_all_three_toolbars(self):
        RemoteControlApp._set_controls_collapsed(self.app, True)
        self.assertTrue(self.app.controls_collapsed)
        for toolbar in self.toolbars:
            toolbar.grid_remove.assert_called_once_with()
        self.app.controls_toggle.configure.assert_called_with(text="Show controls ▼")

        RemoteControlApp._set_controls_collapsed(self.app, False)
        self.assertFalse(self.app.controls_collapsed)
        for toolbar in self.toolbars:
            toolbar.grid.assert_called_once_with()
        self.app.controls_toggle.configure.assert_called_with(text="Hide controls ▲")
        self.assertEqual(self.app.after_idle.call_count, 2)

    def test_repeated_state_is_no_op(self):
        RemoteControlApp._set_controls_collapsed(self.app, False)
        for toolbar in self.toolbars:
            toolbar.grid_remove.assert_not_called()
            toolbar.grid.assert_not_called()
        self.app.after_idle.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import os
import runpy
import sys
import unittest
from types import ModuleType
from unittest.mock import patch


class EntryPointTests(unittest.TestCase):
    def test_docker_host_port_and_no_browser(self):
        # Test startup wiring without opening the production database or sockets.
        fake_app = ModuleType('medik_pilot.app')
        fake_app.app = object()
        with patch.dict(sys.modules, {'medik_pilot.app': fake_app}), \
             patch.dict(os.environ, {'MEDIKTEST_HOST': '0.0.0.0',
                                     'MEDIKTEST_PORT': '18765',
                                     'MEDIKTEST_NO_BROWSER': '1'}), \
             patch('uvicorn.run') as server, patch('threading.Thread') as browser:
            runpy.run_module('medik_pilot.__main__', run_name='__main__')
        server.assert_called_once_with(fake_app.app, host='0.0.0.0', port=18765, reload=False)
        browser.assert_not_called()

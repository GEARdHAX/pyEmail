#!/usr/bin/env python3
import unittest
import tempfile
import os
from config import DRY_RUN, ENABLE_EMAIL_SENDING

class TestConfig(unittest.TestCase):
    def test_env_defaults(self):
        self.assertTrue(DRY_RUN, "DRY_RUN should default to true")
        self.assertFalse(ENABLE_EMAIL_SENDING, "ENABLE_EMAIL_SENDING should default to false")

    def test_env_override(self):
        os.environ['DRY_RUN'] = 'false'
        os.environ['ENABLE_EMAIL_SENDING'] = 'true'

        from importlib import reload
        import config
        reload(config)

        self.assertFalse(config.DRY_RUN, "DRY_RUN should be false when set to false")
        self.assertTrue(config.ENABLE_EMAIL_SENDING, "ENABLE_EMAIL_SENDING should be true when set to true")

class TestLogger(unittest.TestCase):
    def test_logger_initializes(self):
        from services.logger import get_logger
        logger = get_logger(__name__)
        self.assertIsNotNone(logger)

if __name__ == '__main__':
    unittest.main()
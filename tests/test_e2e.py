import asyncio
import tempfile
import unittest
from pathlib import Path

from demo.verify import verify


class EndToEndTests(unittest.TestCase):
    def test_record_learn_validate_and_real_stdio_client(self):
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(verify(Path(directory), verbose=False))

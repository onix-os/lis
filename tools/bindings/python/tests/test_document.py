import pathlib
import unittest
from lis_spec import parse_json, to_json

class TestLISPythonBindings(unittest.TestCase):
    def test_parse_examples(self):
        examples_dir = pathlib.Path(__file__).resolve().parent.parent.parent.parent.parent / "docs" / "examples"
        for json_file in examples_dir.glob("*.json"):
            text = json_file.read_text()
            doc = parse_json(text)
            self.assertTrue(doc.lis.startswith("0.1"))
            out = to_json(doc)
            self.assertIn("lis", out)

if __name__ == "__main__":
    unittest.main()

"""Tests for code parsing runtime."""

from strata.ingestion.tree_sitter_runtime import detect_language, run_query


def test_detect_language():
    """Test language detection from file extension."""
    assert detect_language("app.ts") == "typescript"
    assert detect_language("app.tsx") == "typescript"
    assert detect_language("app.js") == "typescript"
    assert detect_language("service.cs") == "csharp"
    assert detect_language("unknown.xyz") is None


def test_parse_typescript_class():
    """Test extracting class from TypeScript code."""
    code = b"""
    export class MyClass {
      myMethod(): string {
        return "hello";
      }
    }
    """
    captures = run_query(code, "typescript")
    assert "class.def" in captures
    assert len(captures["class.def"]) > 0
    assert captures["class.def"][0]["name"] == "MyClass"


def test_parse_typescript_function():
    """Test extracting function from TypeScript code."""
    code = b"""
    export function myFunc() {
      return "test";
    }

    const arrow = () => {};
    """
    captures = run_query(code, "typescript")
    assert "function.def" in captures
    assert len(captures["function.def"]) >= 1


def test_parse_typescript_import():
    """Test extracting imports from TypeScript code."""
    code = b"import { Foo } from './bar';"
    captures = run_query(code, "typescript")
    assert "import.def" in captures
    assert captures["import.def"][0]["path"] == "./bar"


def test_parse_csharp_class():
    """Test extracting class from C# code."""
    code = b"""
    public class MyService
    {
        public string GetValue()
        {
            return "test";
        }
    }
    """
    captures = run_query(code, "csharp")
    assert "class.def" in captures
    assert captures["class.def"][0]["name"] == "MyService"


def test_parse_csharp_method():
    """Test extracting methods from C# code."""
    code = b"""
    public class Service
    {
        public string GetValue() => "test";
        public async Task DoWork() { }
    }
    """
    captures = run_query(code, "csharp")
    assert "method.def" in captures
    assert len(captures["method.def"]) >= 2


def test_parse_csharp_using():
    """Test extracting using statements from C# code."""
    code = b"using System.Collections;"
    captures = run_query(code, "csharp")
    assert "using.def" in captures
    assert captures["using.def"][0]["namespace"] == "System.Collections"


def _function_names(captures):
    return [c["name"] for c in captures.get("function.def", [])]


def test_arrow_function_is_named_by_its_binding_not_its_parameter():
    code = b"""
    export const loadUser = async (id) => { return id; };
    const handlers = { onSave: (payload) => payload };
    const Card = memo((props) => props.children);
    items.map((item) => item);
    """
    names = _function_names(run_query(code, "typescript"))

    assert {"loadUser", "onSave", "Card"} <= set(names)
    assert "id" not in names and "payload" not in names and "item" not in names


def test_tsx_files_are_parsed_with_the_jsx_grammar():
    code = b"""
    export const Greeting = ({ name }: Props) => <div className="x">{name}</div>;
    function Other() { return <Greeting name="a" />; }
    """
    names = _function_names(run_query(code, "tsx"))

    assert names == ["Greeting", "Other"]


def test_describe_and_it_blocks_become_named_test_cases():
    code = b"""
    describe('uploadFile()', () => {
      it('sends no ACL for a private object', async () => {
        await uploadFile(input);
      });
      it.only('sends no ACL for a private object', () => {});
      test(`handles ${thing}`, () => {});
    });
    helper('not a test', () => {});
    """
    names = _function_names(run_query(code, "typescript"))

    assert names == [
        "describe: uploadFile()",
        "it: sends no ACL for a private object",
        "it: sends no ACL for a private object #2",
        "it: handles ${thing}",
    ]

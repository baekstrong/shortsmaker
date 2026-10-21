import json

from shortsmaker import ai


class FakeCtx:
    def run(self, args, *, cwd=None, input_text=None, timeout=None):
        self.args, self.input = args, json.loads(input_text)
        return "warning line\n" + json.dumps({"type": "result", "is_error": False, "structured_output": {"ok": "yes"}})


def test_claude_model_uses_claude_cli(tmp_path):
    ctx, image = FakeCtx(), tmp_path / "f.jpg"
    image.write_bytes(b"jpg")
    schema = ai.obj({"ok": ai.TEXT})
    assert ai.call(ctx, "hi", schema, tmp_path, images=[image]) == {"ok": "yes"}
    assert ctx.args[:2] == ["claude", "-p"]
    assert ctx.args[ctx.args.index("--model") + 1] == "claude-opus-5-5"
    assert ctx.args[ctx.args.index("--effort") + 1] == "medium"
    assert [c["type"] for c in ctx.input["message"]["content"]] == ["image", "text"]
    assert ai.models()[0] == "claude-opus-5-5"

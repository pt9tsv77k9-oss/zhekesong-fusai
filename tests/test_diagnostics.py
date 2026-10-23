"""未知异常不能吞掉，也不能把异常文本中的凭证写入日志。全部离线。"""

import json

import diagnostics


def test_error_log_contains_location_and_stage_but_no_exception_values(local_tmp_path, monkeypatch):
    tmp_path = local_tmp_path
    monkeypatch.setattr(diagnostics, "LOG_DIR", tmp_path / "logs")
    sensitive = "test-secret-do-not-record"
    try:
        try:
            raise ValueError(sensitive)
        except ValueError as inner:
            raise RuntimeError("payload=" + sensitive) from inner
    except RuntimeError as error:
        error_id = diagnostics.record_unexpected_error(error, "正在重新核验修正结果")
    contents = (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8")
    assert sensitive not in contents and "payload=" not in contents
    record = json.loads(contents)
    assert record["error_id"] == error_id
    assert record["stage"] == "正在重新核验修正结果"
    assert [item["type"] for item in record["exceptions"]] == ["RuntimeError", "ValueError"]
    assert any(p["file"].endswith("test_diagnostics.py") for item in record["exceptions"] for p in item["positions"])


def test_unwritable_log_does_not_hide_original_error(local_tmp_path, monkeypatch):
    tmp_path = local_tmp_path
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(diagnostics, "LOG_DIR", blocked)
    assert diagnostics.record_unexpected_error(RuntimeError("hidden"), "unknown") is None


def test_runtime_signature_changes_with_code_not_docs(local_tmp_path, monkeypatch):
    tmp_path = local_tmp_path
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    (tmp_path / "prompts").mkdir()
    source = tmp_path / "app.py"
    source.write_text("version = 1", encoding="utf-8")
    original = diagnostics.runtime_signature()
    (tmp_path / "README.md").write_text("some documentation", encoding="utf-8")
    assert diagnostics.runtime_signature() == original
    source.write_text("version = 2", encoding="utf-8")
    assert diagnostics.runtime_signature() != original
    second = diagnostics.runtime_signature()
    (tmp_path / "prompts/generate.txt").write_text("new rules", encoding="utf-8")
    assert diagnostics.runtime_signature() != second

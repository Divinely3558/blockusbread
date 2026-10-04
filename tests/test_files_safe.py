"""文件浏览安全：路径穿越 / 符号链接逃逸 / 文件名清洗。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.web.files import safe_resolve, sanitize_filename


def test_safe_resolve_root(tmp_path):
    assert safe_resolve(tmp_path, "") == tmp_path.resolve()


def test_safe_resolve_normal_child(tmp_path):
    sub = tmp_path / "movies"
    sub.mkdir()
    assert safe_resolve(tmp_path, "movies/a.mkv") == (sub / "a.mkv").resolve()


def test_safe_resolve_strips_leading_slash(tmp_path):
    # 绝对路径片段只当作卷根下的相对名，不能逃逸
    target = safe_resolve(tmp_path, "/etc/passwd")
    assert target == (tmp_path / "etc/passwd").resolve()


def test_safe_resolve_rejects_dotdot(tmp_path):
    with pytest.raises(HTTPException) as exc:
        safe_resolve(tmp_path, "../etc/passwd")
    assert exc.value.status_code == 400


def test_safe_resolve_rejects_nested_dotdot(tmp_path):
    with pytest.raises(HTTPException) as exc:
        safe_resolve(tmp_path, "a/b/../../../etc/passwd")
    assert exc.value.status_code == 400


def test_safe_resolve_rejects_symlink_escape(tmp_path):
    link = tmp_path / "link-out"
    link.symlink_to("/etc")
    with pytest.raises(HTTPException) as exc:
        safe_resolve(tmp_path, "link-out/passwd")
    assert exc.value.status_code == 400


def test_safe_resolve_rejects_nul(tmp_path):
    with pytest.raises(HTTPException) as exc:
        safe_resolve(tmp_path, "a\x00.txt")
    assert exc.value.status_code == 400


def test_sanitize_filename_strips_path():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("docs\\file.txt") == "file.txt"
    assert sanitize_filename("plain.txt") == "plain.txt"

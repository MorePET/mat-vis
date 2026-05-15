"""Visual QA spike tests (#446).

Covers pairing logic, report structure, verdict parsing, and CLI
wiring. VLM calls are mocked — the live bench is a separate step.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from scripts.visual_qa import (
    MaterialPair,
    QAReport,
    QAResult,
    _build_batch_content,
    _detect_media_type,
    _parse_verdicts,
    _upstream_preview_url,
)


# ── helpers ──────────────────────────────────────────────────────

# 1x1 red PNG (68 bytes)
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)

# 1x1 JPEG
_TINY_JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 50


def _pair(source="ambientcg", mid="Rock064", has_both=True) -> MaterialPair:
    return MaterialPair(
        source=source,
        material_id=mid,
        upstream_url=f"https://example.com/{mid}.png",
        upstream_bytes=_TINY_PNG if has_both else None,
        our_thumb_bytes=_TINY_PNG if has_both else None,
    )


# ── upstream URL resolution ──────────────────────────────────────


def test_upstream_url_ambientcg():
    entry = {"previewImage": {"256-PNG": "https://example.com/rock.png"}}
    assert _upstream_preview_url("ambientcg", "Rock064", entry) == "https://example.com/rock.png"


def test_upstream_url_polyhaven():
    url = _upstream_preview_url("polyhaven", "rock_face_04", {})
    assert "cdn.polyhaven.com" in url
    assert "rock_face_04" in url


def test_upstream_url_gpuopen():
    entry = {"renders": ["uuid-123"]}
    url = _upstream_preview_url("gpuopen", "some-id", entry)
    assert "uuid-123" in url
    assert "download_thumbnail" in url


def test_upstream_url_no_renders():
    assert _upstream_preview_url("gpuopen", "some-id", {"renders": []}) is None


# ── media type detection ─────────────────────────────────────────


def test_detect_png():
    assert _detect_media_type(_TINY_PNG) == "image/png"


def test_detect_jpeg():
    assert _detect_media_type(_TINY_JPG) == "image/jpeg"


def test_detect_fallback():
    assert _detect_media_type(b"unknown") == "image/png"


# ── MaterialPair ─────────────────────────────────────────────────


def test_pair_has_both():
    p = _pair(has_both=True)
    assert p.has_both


def test_pair_missing_upstream():
    p = _pair()
    p.upstream_bytes = None
    assert not p.has_both


# ── batch content building ───────────────────────────────────────


def test_build_batch_content_structure():
    pairs = [_pair(mid="A"), _pair(mid="B")]
    content = _build_batch_content(pairs)
    # Should have: per pair (text label + text "upstream" + image + text "ours" + image)
    # Plus final instruction text
    text_blocks = [b for b in content if b["type"] == "text"]
    image_blocks = [b for b in content if b["type"] == "image"]
    assert len(image_blocks) == 4  # 2 pairs × 2 images
    assert any("Judge all 2 materials" in b["text"] for b in text_blocks)


def test_build_batch_content_base64():
    pairs = [_pair()]
    content = _build_batch_content(pairs)
    img_block = next(b for b in content if b["type"] == "image")
    assert img_block["source"]["type"] == "base64"
    assert img_block["source"]["media_type"] == "image/png"


# ── verdict parsing ──────────────────────────────────────────────


def _mock_response(text: str):
    block = SimpleNamespace(text=text)
    return SimpleNamespace(content=[block])


def test_parse_verdicts_ok():
    pairs = [_pair(mid="Rock064")]
    resp = _mock_response(
        '[{"material_id": "Rock064", "verdict": "ok", '
        '"reason": "same stone texture", "confidence": "high"}]'
    )
    results = _parse_verdicts(pairs, resp)
    assert len(results) == 1
    assert results[0].verdict == "ok"
    assert results[0].confidence == "high"


def test_parse_verdicts_suspicious():
    pairs = [_pair(mid="Metal001")]
    resp = _mock_response(
        '[{"material_id": "Metal001", "verdict": "suspicious", '
        '"reason": "render is completely black", "confidence": "high"}]'
    )
    results = _parse_verdicts(pairs, resp)
    assert results[0].verdict == "suspicious"
    assert "black" in results[0].reason


def test_parse_verdicts_with_source_prefix():
    """material_id in response may include source/ prefix."""
    pairs = [_pair(source="gpuopen", mid="uuid-123")]
    resp = _mock_response(
        '[{"material_id": "gpuopen/uuid-123", "verdict": "ok", '
        '"reason": "", "confidence": "high"}]'
    )
    results = _parse_verdicts(pairs, resp)
    assert results[0].verdict == "ok"


def test_parse_verdicts_json_in_markdown():
    """VLM might wrap JSON in markdown code fence."""
    pairs = [_pair(mid="X")]
    resp = _mock_response(
        'Here are my results:\n```json\n'
        '[{"material_id": "X", "verdict": "ok", "reason": "", "confidence": "high"}]'
        '\n```'
    )
    results = _parse_verdicts(pairs, resp)
    assert results[0].verdict == "ok"


def test_parse_verdicts_unparseable():
    pairs = [_pair(mid="Y")]
    resp = _mock_response("I can't process these images.")
    results = _parse_verdicts(pairs, resp)
    assert results[0].verdict == "error"
    assert "parse" in results[0].reason.lower()


def test_parse_verdicts_missing_material():
    """VLM returns verdicts but misses one material."""
    pairs = [_pair(mid="A"), _pair(mid="B")]
    resp = _mock_response(
        '[{"material_id": "A", "verdict": "ok", "reason": "", "confidence": "high"}]'
    )
    results = _parse_verdicts(pairs, resp)
    assert results[0].verdict == "ok"
    assert results[1].verdict == "error"
    assert "No verdict" in results[1].reason


# ── QAReport ─────────────────────────────────────────────────────


def test_report_suspicious_filter():
    report = QAReport(release_tag="test", repo_id="test")
    report.results = [
        QAResult("a", "1", "ok"),
        QAResult("a", "2", "suspicious", "black render"),
        QAResult("a", "3", "ok"),
    ]
    assert len(report.suspicious) == 1
    assert report.suspicious[0].material_id == "2"


def test_report_to_dict_serializable():
    report = QAReport(release_tag="test", repo_id="test", model="claude-test")
    report.results = [QAResult("a", "1", "ok", "fine", "high")]
    d = report.to_dict()
    serialized = json.dumps(d)
    assert '"verdict": "ok"' in serialized
    assert '"model": "claude-test"' in serialized


def test_report_print_summary(capsys):
    report = QAReport(release_tag="v1", repo_id="r", model="m")
    report.results = [
        QAResult("a", "1", "suspicious", "black render", "high"),
    ]
    report.print_summary()
    captured = capsys.readouterr()
    assert "suspicious: 1" in captured.out
    assert "black render" in captured.out

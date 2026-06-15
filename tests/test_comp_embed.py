"""Tests for the comp embed building helpers, especially the chunking
logic that prevents long sections from being silently truncated at the
1024-char-per-field Discord limit."""
from cogs.comp import _chunk_lines_for_field, _build_comp_embed


def test_chunk_packs_under_limit_in_one_chunk():
    lines = [f"line {i}" for i in range(10)]
    chunks = _chunk_lines_for_field(lines, limit=1024)
    assert len(chunks) == 1
    assert chunks[0] == "\n".join(lines)


def test_chunk_splits_when_total_exceeds_limit():
    # 30 lines of ~50 chars each = ~1530 chars → must split.
    lines = [f"slot {i:02d} — Dual Swords · Knight Helmet · cape Lymhurst" for i in range(30)]
    chunks = _chunk_lines_for_field(lines, limit=1024)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 1024
    # Every line must appear somewhere — nothing dropped.
    joined = "\n".join(chunks)
    for ln in lines:
        assert ln in joined


def test_chunk_truncates_a_single_too_long_line():
    huge = "x" * 2000
    chunks = _chunk_lines_for_field([huge], limit=1024)
    assert len(chunks) == 1
    assert len(chunks[0]) <= 1024
    assert chunks[0].endswith("…")


def test_chunk_empty_input():
    assert _chunk_lines_for_field([]) == []


def _make_slot(i: int, build_type: str = "dps") -> dict:
    return {
        "slot_order": i,
        "role": f"DPS {i}",
        "build_type": build_type,
        "weapon": "Dual Swords",
        "head": "Assassin Hood",
        "chest": "Hellion Jacket",
        "shoes": "Mercenary Shoes",
        "cape": "Lymhurst Cape",
        "ip_min": 1400,
        "required": 1,
    }


def test_build_comp_embed_renders_all_slots_when_large_section():
    """A 30-slot DPS section used to silently chop at 1024 chars
    (~13 lines). The new chunker must keep all 30."""
    comp = {"id": 1, "name": "ZvZ Standard", "content_type": "ZvZ", "description": ""}
    slots = [_make_slot(i) for i in range(1, 31)]
    embed = _build_comp_embed(comp, slots)

    # Concatenate every field value and confirm each slot appears.
    body = "\n".join(f.value for f in embed.fields)
    for i in range(1, 31):
        assert f"{i}. DPS {i}" in body, f"slot #{i} missing from embed"
    # No truncation footer either.
    assert embed.footer is not None
    assert "truncated" not in (embed.footer.text or "")


def test_build_comp_embed_adds_cont_label_when_section_splits():
    comp = {"id": 2, "name": "Big DPS", "content_type": "ZvZ", "description": ""}
    slots = [_make_slot(i) for i in range(1, 31)]
    embed = _build_comp_embed(comp, slots)
    names = [f.name for f in embed.fields]
    # First field shows the count, follow-ups labeled (cont.).
    assert any("⚔️ DPS (30)" == n for n in names)
    assert any("(cont.)" in n for n in names)


def test_build_comp_embed_truncates_only_when_real_limit_hit():
    """500 slots is well past the 25-field / ~6000-char budget."""
    comp = {"id": 3, "name": "Insane", "content_type": "ZvZ", "description": ""}
    slots = [_make_slot(i) for i in range(1, 501)]
    embed = _build_comp_embed(comp, slots)
    assert embed.footer is not None
    assert "truncated" in (embed.footer.text or "")
    # Embed must still be under Discord's hard 25-field cap.
    assert len(embed.fields) <= 25

"""Tests for the evidence store's operator-facing helpers.

The write side of the store is exercised through the ingest tests; what is
tested here is the one piece an operator interacts with directly — resolving a
structure argument typed on the command line to a stored join key.
"""

import pytest

from src.store import StructureNotFound, resolve_structure_key


class TestResolveStructureKey:
    """Operators type what CLAUDE.md and the published records show: a bare
    structure number. Accept it when unambiguous, refuse to guess when not."""

    def _conn(self, *keys):
        from src.db import connect

        conn = connect(":memory:")
        for key in keys:
            conn.execute("INSERT INTO structures (struct_norm) VALUES (?)", (key,))
        return conn

    def test_an_exact_key_resolves(self):
        conn = self._conn("AL013450", "IA013450")
        assert resolve_structure_key(conn, "AL013450") == "AL013450"

    def test_a_bare_number_resolves_when_only_one_state_uses_it(self):
        conn = self._conn("AL013450", "IA999999")
        assert resolve_structure_key(conn, "13450") == "AL013450"

    def test_a_bare_number_shared_by_two_states_refuses_to_guess(self):
        conn = self._conn("AL013450", "IA013450")
        with pytest.raises(StructureNotFound) as exc:
            resolve_structure_key(conn, "013450")
        message = str(exc.value)
        assert "AL013450" in message and "IA013450" in message

    def test_a_longer_number_is_not_matched_by_its_suffix(self):
        # Searching for 13450 must not resolve to a bridge numbered 913450.
        conn = self._conn("AL913450")
        with pytest.raises(StructureNotFound):
            resolve_structure_key(conn, "13450")

    def test_an_unknown_number_says_how_to_populate_the_index(self):
        conn = self._conn()
        with pytest.raises(StructureNotFound) as exc:
            resolve_structure_key(conn, "013450")
        assert "src.ingest.nbi" in str(exc.value)

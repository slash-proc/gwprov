"""Primitive variable indexes must never select a same-name wrong definition."""
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from gwprov.debug_types import (_select_variable, _file_identity,
                                 _variable_index, clear_layout_caches)


class VariableSelection(unittest.TestCase):
    def test_link_address_disambiguates_without_runtime_rebase(self):
        records = [(0, 1, 'first.c', 0x24000000),
                   (100, 101, 'second.c', 0x24000004)]
        self.assertEqual(_select_variable(records, 'same', symbol_address=0x24000004), records[1])
        with self.assertRaises(KeyError):
            _select_variable(records, 'same', symbol_address=0x90000004)
        with self.assertRaises(ValueError):
            _select_variable(records, 'same')

    def test_hints_fallback_and_duplicate_definitions_fail_closed(self):
        records = [(0, 1, 'first.c', 8), (100, 101, 'second.c', 12)]
        self.assertEqual(_select_variable(records, 'same', '/src/second.c'), records[1])
        with self.assertRaises(ValueError):
            _select_variable(records, 'same', 'missing.c')
        with self.assertRaises(ValueError):
            _select_variable(records + [(200, 201, 'second.c', 12)], 'same', 'second.c', 12)
        with self.assertRaises(KeyError):
            _select_variable([], 'declaration_only')

    def test_file_identity_and_reload_cache_invalidation(self):
        # Tests use repository-owned scratch, never RAM-backed /tmp.
        scratch = Path('build/test-variable-index'); scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            target = Path(folder) / 'fixture'; target.write_bytes(b'one')
            first = _file_identity(target); target.write_bytes(b'longer')
            self.assertNotEqual(first, _file_identity(target))
        with patch('gwprov.debug_types.ELFFile') as image:
            image.return_value.has_dwarf_info.return_value = True
            image.return_value.get_dwarf_info.return_value.iter_CUs.return_value = []
            with tempfile.TemporaryDirectory(dir=scratch) as folder:
                target = Path(folder) / 'fixture'; target.write_bytes(b'')
                identity = _file_identity(target)
                _variable_index(str(target), identity); _variable_index(str(target), identity)
                self.assertEqual(image.call_count, 1)
                clear_layout_caches(); _variable_index(str(target), identity)
                self.assertEqual(image.call_count, 2)


class IndexDefinitions(unittest.TestCase):
    def test_declaration_alias_definition_and_block_shadow(self):
        class Die:
            def __init__(self, offset, name=None, location=None, declaration=False, specification=None, tag='DW_TAG_variable'):
                self.offset = offset; self.tag = tag; self.attributes = {}
                if name is not None: self.attributes['DW_AT_name'] = SimpleNamespace(value=name.encode())
                if location is not None: self.attributes['DW_AT_location'] = SimpleNamespace(value=location)
                if declaration: self.attributes['DW_AT_declaration'] = SimpleNamespace(value=True)
                if specification is None: self.attributes['DW_AT_type'] = SimpleNamespace(value=20)
                else: self.attributes['DW_AT_specification'] = SimpleNamespace(value=specification.offset)
                self.specification = specification
            def get_DIE_from_attribute(self, name):
                assert name == 'DW_AT_specification'; return self.specification
        declaration = Die(10, 'global', declaration=True)
        definition = Die(11, location=[3, 0, 0, 0, 36], specification=declaration)
        declaration_only = Die(12, 'missing', declaration=True)
        block = Die(13, tag='DW_TAG_lexical_block')
        top = Die(0, 'fixture.c'); top.iter_children = lambda: iter([declaration, definition, declaration_only, block])
        class Cu:
            cu_offset = 0
            def __getitem__(self, name): return 4
            def get_top_DIE(self): return top
        scratch = Path('build/test-variable-index'); scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder, patch('gwprov.debug_types.ELFFile') as image:
            image.return_value.has_dwarf_info.return_value = True
            image.return_value.little_endian = True
            image.return_value.get_dwarf_info.return_value.iter_CUs.return_value = [Cu()]
            path = Path(folder) / 'fixture'; path.write_bytes(b'')
            clear_layout_caches()
            index = _variable_index(str(path), _file_identity(path))
            self.assertEqual(index, {'global': ((0, 11, 'fixture.c', 0x24000000),)})

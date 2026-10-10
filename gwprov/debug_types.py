"""Read target C globals using their ELF DWARF layouts, without app-specific offsets."""
from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path

from elftools.elf.elffile import ELFFile


def _name(die):
    attr = die.attributes.get('DW_AT_name')
    return attr.value.decode(errors='replace') if attr else None


def _layout(die):
    tag = die.tag
    if tag in {'DW_TAG_typedef', 'DW_TAG_const_type', 'DW_TAG_volatile_type',
               'DW_TAG_restrict_type', 'DW_TAG_atomic_type'}:
        return _layout(die.get_DIE_from_attribute('DW_AT_type'))
    size_attr = die.attributes.get('DW_AT_byte_size')
    size = int(size_attr.value) if size_attr else None
    result = {'kind': tag.removeprefix('DW_TAG_'), 'name': _name(die), 'size': size}
    if tag == 'DW_TAG_pointer_type':
        result['size'] = size or die.cu['address_size']
    elif tag in {'DW_TAG_base_type', 'DW_TAG_enumeration_type'}:
        encoding = die.attributes.get('DW_AT_encoding')
        if tag == 'DW_TAG_enumeration_type' and 'DW_AT_type' in die.attributes:
            result['encoding'] = _layout(die.get_DIE_from_attribute('DW_AT_type')).get('encoding', 7)
        else:
            result['encoding'] = int(encoding.value) if encoding else 7
    elif tag == 'DW_TAG_structure_type':
        members = []
        for child in die.iter_children():
            if child.tag != 'DW_TAG_member':
                continue
            if 'DW_AT_bit_size' in child.attributes:
                raise ValueError(f'bit fields are not yet supported: {_name(die)}.{_name(child)}')
            location = child.attributes.get('DW_AT_data_member_location')
            if location is None or not isinstance(location.value, int):
                raise ValueError(f'nonconstant member offset: {_name(die)}.{_name(child)}')
            members.append({'name': _name(child), 'offset': int(location.value),
                            'type': _layout(child.get_DIE_from_attribute('DW_AT_type'))})
        result['members'] = members
    elif tag == 'DW_TAG_array_type':
        element = _layout(die.get_DIE_from_attribute('DW_AT_type'))
        dimensions = []
        for child in die.iter_children():
            if child.tag != 'DW_TAG_subrange_type':
                continue
            count = child.attributes.get('DW_AT_count')
            upper = child.attributes.get('DW_AT_upper_bound')
            lower = child.attributes.get('DW_AT_lower_bound')
            if count is not None and isinstance(count.value, int):
                dimensions.append(int(count.value))
            elif upper is not None and isinstance(upper.value, int):
                dimensions.append(int(upper.value) - (int(lower.value) if lower else 0) + 1)
            else:
                raise ValueError(f'array has no fixed count: {_name(die)}')
        if not dimensions:
            raise ValueError('array has no dimensions')
        for count in reversed(dimensions):
            element = {'kind': 'array_type', 'name': None,
                       'size': count * element['size'], 'count': count, 'element': element}
        result = element
    else:
        raise ValueError(f'unsupported DWARF type: {tag} {_name(die)}')
    if result['size'] is None:
        raise ValueError(f'incomplete DWARF type: {tag} {_name(die)}')
    return result


@lru_cache(maxsize=32)
def _cu_names(path: str, mtime_ns: int):
    """Cache primitive CU offsets only; retain no DIE or closed ELF stream."""
    result = {}
    with Path(path).open('rb') as stream:
        dwarf = ELFFile(stream).get_dwarf_info()
        for cu in dwarf.iter_CUs():
            name = Path(_name(cu.get_top_DIE()) or '').name
            result.setdefault(name, []).append(cu.cu_offset)
    return {name: tuple(offsets) for name, offsets in result.items()}


def _find_variable(dwarf, symbol, source_hint=None, cu_offsets=None):
    """Prefer a unique hinted compilation unit, then preserve full-scan lookup."""
    def find(cu):
        return next((die for die in cu.get_top_DIE().iter_children()
                     if die.tag == 'DW_TAG_variable' and _name(die) == symbol
                     and 'DW_AT_type' in die.attributes), None)
    if source_hint:
        candidates = ([dwarf.get_CU_at(offset) for offset in cu_offsets]
                      if cu_offsets is not None else
                      [cu for cu in dwarf.iter_CUs()
                       if Path(_name(cu.get_top_DIE()) or '').name == Path(source_hint).name])
        if len(candidates) == 1:
            variable = find(candidates[0])
            if variable is not None:
                return variable
    for cu in dwarf.iter_CUs():
        variable = find(cu)
        if variable is not None:
            return variable
    return None


def _file_identity(path):
    stat = Path(path).stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=16)
def _variable_index(path, identity):
    """Index primitive offsets only, never retain DIEs or ELF streams.

    Only located definitions qualify. A located definition may inherit its
    type/name from a specification; standalone declarations never qualify.
    """
    result = {}
    with Path(path).open('rb') as stream:
        image = ELFFile(stream)
        if not image.has_dwarf_info():
            raise ValueError(f'ELF has no DWARF types: {path}')
        dwarf = image.get_dwarf_info()
        for cu in dwarf.iter_CUs():
            source = Path(_name(cu.get_top_DIE()) or '').name
            for die in cu.get_top_DIE().iter_children():
                if die.tag != 'DW_TAG_variable':
                    continue
                declaration = die.attributes.get('DW_AT_declaration')
                if declaration is not None and declaration.value:
                    continue
                location = die.attributes.get('DW_AT_location')
                typed = (die.get_DIE_from_attribute('DW_AT_specification')
                         if 'DW_AT_specification' in die.attributes else die)
                name = _name(die) or _name(typed)
                if not name or location is None or 'DW_AT_type' not in typed.attributes:
                    continue
                expression = location.value
                address = None
                if isinstance(expression, (bytes, list)) and len(expression) == cu['address_size'] + 1 and expression[0] == 3:
                    address = int.from_bytes(bytes(expression[1:]), 'little' if image.little_endian else 'big')
                result.setdefault(name, []).append((cu.cu_offset, die.offset, source, address))
    return {name: tuple(records) for name, records in result.items()}


def _select_variable(records, symbol, source_hint=None, symbol_address=None):
    candidates = list(records)
    if symbol_address is not None:
        exact = [item for item in candidates if item[3] == symbol_address]
        if exact:
            candidates = exact
        elif any(item[3] is not None for item in candidates):
            raise KeyError(f'no DWARF definition at ELF address for {symbol!r}')
    if source_hint:
        hinted = [item for item in candidates if item[2] == Path(source_hint).name]
        if len(hinted) == 1:
            candidates = hinted
    if not candidates:
        raise KeyError(f'no located DWARF variable definition for {symbol!r}')
    if len(candidates) != 1:
        raise ValueError(f'ambiguous DWARF variable definition for {symbol!r}')
    return candidates[0]


def _indexed_variable(dwarf, path, identity, symbol, source_hint=None, symbol_address=None):
    cu_offset, die_offset, _, _ = _select_variable(
        _variable_index(path, identity).get(symbol, ()), symbol, source_hint, symbol_address)
    cu = dwarf.get_CU_at(cu_offset)
    definition = cu.get_DIE_from_refaddr(die_offset)
    return (definition.get_DIE_from_attribute('DW_AT_specification')
            if 'DW_AT_specification' in definition.attributes else definition)


@lru_cache(maxsize=128)
def _variable_layout(path: str, mtime_ns, symbol: str, source_hint=None, symbol_address=None):
    with Path(path).open('rb') as stream:
        image = ELFFile(stream)
        if not image.has_dwarf_info():
            raise ValueError(f'ELF has no DWARF types: {path}')
        dwarf = image.get_dwarf_info()
        variable = _indexed_variable(dwarf, path, mtime_ns, symbol, source_hint, symbol_address)
        if variable is not None:
            return {**_layout(variable.get_DIE_from_attribute('DW_AT_type')),
                    'byteorder': 'little' if image.little_endian else 'big'}
    raise KeyError(f'no DWARF variable type for {symbol!r} in {path}')


def variable_layout(path: Path, symbol: str, source_hint=None, symbol_address=None) -> dict:
    return _variable_layout(str(path), _file_identity(path), symbol, source_hint, symbol_address)


def decode_value(layout: dict, data: bytes, byteorder: str | None = None):
    order = byteorder or layout.get('byteorder', 'little')
    size = layout['size']
    if len(data) != size:
        raise ValueError(f'expected {size} bytes, received {len(data)}')
    kind = layout['kind']
    if kind == 'structure_type':
        return {member['name']: decode_value(member['type'],
                data[member['offset']:member['offset'] + member['type']['size']], order)
                for member in layout['members']}
    if kind == 'array_type':
        element = layout['element']; width = element['size']
        return [decode_value(element, data[i * width:(i + 1) * width], order)
                for i in range(layout['count'])]
    encoding = layout.get('encoding', 7)
    if encoding == 4:  # DW_ATE_float
        formats = {4: 'f', 8: 'd'}
        if size not in formats:
            raise ValueError(f'unsupported floating point size: {size}')
        return struct.unpack(('<' if order == 'little' else '>') + formats[size], data)[0]
    value = int.from_bytes(data, order, signed=encoding in {5, 6})
    return bool(value) if encoding == 2 else value


def _compile_member_path(variable, members, byteorder):
    die = variable.get_DIE_from_attribute("DW_AT_type")
    operations = []
    qualifiers = {"DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
                  "DW_TAG_restrict_type", "DW_TAG_atomic_type"}
    for member_name in members:
        while die.tag in qualifiers:
            die = die.get_DIE_from_attribute("DW_AT_type")
        while die.tag == "DW_TAG_pointer_type":
            operations.append({"kind": "dereference", "size": _layout(die)["size"]})
            die = die.get_DIE_from_attribute("DW_AT_type")
            while die.tag in qualifiers:
                die = die.get_DIE_from_attribute("DW_AT_type")
        if die.tag != "DW_TAG_structure_type":
            raise ValueError(f"cannot select {member_name!r} from {die.tag}")
        member = next((child for child in die.iter_children()
                       if child.tag == "DW_TAG_member" and _name(child) == member_name), None)
        if member is None:
            raise KeyError(f"no member {member_name!r} in {_name(die)}")
        location = member.attributes.get("DW_AT_data_member_location")
        if location is None or not isinstance(location.value, int):
            raise ValueError(f"nonconstant member offset: {member_name}")
        operations.append({"kind": "offset", "bytes": int(location.value)})
        die = member.get_DIE_from_attribute("DW_AT_type")
    return {"operations": operations, "type": {**_layout(die), "byteorder": byteorder}}


@lru_cache(maxsize=128)
def _path_layouts(path: str, mtime_ns, symbol: str, paths: tuple, source_hint=None, symbol_address=None):
    """Resolve many members of one global with a single DWARF scan."""
    with Path(path).open("rb") as stream:
        image = ELFFile(stream)
        if not image.has_dwarf_info():
            raise ValueError(f"ELF has no DWARF types: {path}")
        dwarf = image.get_dwarf_info()
        variable = _indexed_variable(dwarf, path, mtime_ns, symbol, source_hint, symbol_address)
        if variable is None:
            raise KeyError(f"no DWARF variable type for {symbol!r} in {path}")
        result = {}
        byteorder = "little" if image.little_endian else "big"
        for members in paths:
            try:
                result[members] = _compile_member_path(variable, members, byteorder)
            except (KeyError, ValueError) as error:
                result[members] = {"error_type": type(error).__name__, "error": str(error)}
        return result


def path_layouts(path: Path, symbol: str, paths, source_hint=None, symbol_address=None) -> dict:
    """Return plans or explicit errors for selected member paths of one global."""
    paths = tuple(tuple(members) for members in paths)
    return _path_layouts(str(path), _file_identity(path), symbol, paths, source_hint, symbol_address)


def path_layout(path: Path, symbol: str, members: tuple[str, ...], source_hint=None, symbol_address=None) -> dict:
    result = path_layouts(path, symbol, (members,), source_hint, symbol_address)[members]
    if "error" in result:
        exception = KeyError if result["error_type"] == "KeyError" else ValueError
        raise exception(result["error"])
    return result


def clear_layout_caches():
    """Symbol reloads must not retain plans from older ELF identities."""
    _variable_layout.cache_clear()
    _cu_names.cache_clear()
    _variable_index.cache_clear()
    _path_layouts.cache_clear()

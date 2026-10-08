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


@lru_cache(maxsize=128)
def _variable_layout(path: str, mtime_ns: int, symbol: str):
    with Path(path).open('rb') as stream:
        image = ELFFile(stream)
        if not image.has_dwarf_info():
            raise ValueError(f'ELF has no DWARF types: {path}')
        dwarf = image.get_dwarf_info()
        for cu in dwarf.iter_CUs():
            for die in cu.get_top_DIE().iter_children():
                if die.tag != 'DW_TAG_variable' or _name(die) != symbol:
                    continue
                if 'DW_AT_type' in die.attributes:
                    return {**_layout(die.get_DIE_from_attribute('DW_AT_type')),
                            'byteorder': 'little' if image.little_endian else 'big'}
    raise KeyError(f'no DWARF variable type for {symbol!r} in {path}')


def variable_layout(path: Path, symbol: str) -> dict:
    return _variable_layout(str(path), path.stat().st_mtime_ns, symbol)


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

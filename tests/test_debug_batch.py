import unittest
from pathlib import Path
from unittest.mock import patch
from gwprov.debug_shell import DebugSession

class BatchPaths(unittest.TestCase):
 def fixture(self):
  class Symbols:
   def owner(self, root):
    if root=='missing':raise KeyError(root)
    return Path('/fake-'+root+'.elf')
   def __getitem__(self, root):return {'a':100,'b':300}[root]
  class Memory:
   def __init__(self):self.calls=[];self.pointer=200
   def read_memory(self,address,size):
    self.calls.append((address,size))
    if address==104 and size==4:return self.pointer.to_bytes(4,'little')
    return bytes((address+i)&255 for i in range(size))
  memory=Memory();session=DebugSession(memory,'offline');session.symbols=Symbols()
  kind={'kind':'base_type','size':1,'encoding':7,'byteorder':'little'}
  def plans(owner,root,paths):
   result={}
   for members in paths:
    field=members[0]
    if field=='missing':result[members]={'error_type':'KeyError','error':'no member'};continue
    ops=[{'kind':'offset','bytes':{'x':0,'y':1,'z':10,'p':4,'q':4}[field]}]
    if field in('p','q'):ops += [{'kind':'dereference','size':4},{'kind':'offset','bytes':0 if field=='p' else 1}]
    result[members]={'operations':ops,'type':kind}
   return result
  return session,memory,plans
 def test_exact_union_pointer_reuse_and_order(self):
  s,m,p=self.fixture()
  with patch('gwprov.debug_types.path_layouts',side_effect=p):r=s.read_paths(['a.x','a.y','a.z','a.p','a.q','b.x'])
  self.assertEqual(list(r['values']),['a.x','a.y','a.z','a.p','a.q','b.x'])
  self.assertEqual(r['pointer_reads'],1)
  self.assertEqual(r['regions'],[{'address':100,'size':2},{'address':110,'size':1},{'address':200,'size':2},{'address':300,'size':1}])
  self.assertEqual(m.calls.count((104,4)),1)
  self.assertEqual(r['values']['a.p'],200)
  m.pointer=220
  with patch('gwprov.debug_types.path_layouts',side_effect=p):r2=s.read_paths(['a.p'])
  self.assertEqual(r2['values']['a.p'],220)
 def test_errors_are_explicit_and_limits_fail_loudly(self):
  s,m,p=self.fixture()
  with patch('gwprov.debug_types.path_layouts',side_effect=p):
   r=s.read_paths(['a.x','a.missing','missing.x'],errors='collect')
   self.assertEqual(list(r['errors']),['a.missing','missing.x'])
   self.assertEqual(r['values'],{'a.x':100})
   with self.assertRaises(KeyError):s.read_paths(['a.missing'])
   with self.assertRaises(ValueError):s.read_paths(['a.x','a.z'],max_total_bytes=1)
   m.pointer=0
   self.assertEqual(s.read_paths(['a.p'],errors='collect')['errors']['a.p']['type'],'ValueError')
   with self.assertRaises(ValueError):s.read_paths(['a.p'])


class CuHints(unittest.TestCase):
 def test_unique_hint_and_safe_fallback(self):
  from gwprov.debug_types import _find_variable
  class Attr:
   def __init__(self,value):self.value=value
  class Die:
   def __init__(self,name,children=(),variable=False):
    self.attributes={'DW_AT_name':Attr(name.encode())}
    self.tag='DW_TAG_variable' if variable else 'DW_TAG_compile_unit'
    if variable:self.attributes['DW_AT_type']=Attr(1)
    self.children=children
   def iter_children(self):return iter(self.children)
  class Cu:
   def __init__(self,name,variables):self.top=Die(name,variables)
   def get_top_DIE(self):return self.top
  original=Die('counter',variable=True);hinted=Die('counter',variable=True)
  cus=[Cu('first.c',[original]),Cu('/build/local.c',[hinted])]
  class Dwarf:
   def iter_CUs(self):return iter(cus)
   def get_CU_at(self,offset):return cus[offset]
  dwarf=Dwarf()
  self.assertIs(_find_variable(dwarf,'counter','local.c',(1,)),hinted)
  self.assertIs(_find_variable(dwarf,'counter','missing.c',()),original)
  self.assertIs(_find_variable(dwarf,'counter','local.c',(0,1)),original)
  self.assertIsNone(_find_variable(dwarf,'absent','local.c',(1,)))

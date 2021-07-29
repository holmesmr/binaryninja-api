# coding=utf-8
# Copyright (c) 2015-2021 Vector 35 Inc
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to
# deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.

import struct
import threading
import queue
import traceback
import ctypes
import abc
import json
import inspect
import os
from pathlib import Path
from typing import Callable, Generator, Optional, Union, Tuple, List, Mapping, Any
from dataclasses import dataclass, make_dataclass

from collections import defaultdict, OrderedDict


# Binary Ninja components
import binaryninja
from . import _binaryninjacore as core
from .enums import (AnalysisState, SymbolType,
	Endianness, ModificationStatus, StringType, SegmentFlag, SectionSemantics, FindFlag,
	TypeClass, BinaryViewEventType, FunctionGraphType, TagReferenceType, TagTypeType, TypeFieldReference)
from . import associateddatastore # required for _BinaryViewAssociatedDataStore
from . import log
from . import typelibrary
from . import fileaccessor
from . import databuffer
from . import basicblock
from . import lineardisassembly
from . import metadata
from . import highlight
from . import settings
from . import variable
from . import architecture
from . import filemetadata
from . import lowlevelil
from . import mediumlevelil
from . import highlevelil
from . import workflow
# The following are imported as such to allow the linter disambiguate the module name
# from properties and methods of the same name
from . import function as _function
from . import types as _types
from . import platform as _platform


PathType = Union[str, os.PathLike]
InstructionsType = Generator[Tuple[List['_function.InstructionTextToken'], int], None, None]
NotificationType = Mapping['BinaryDataNotification', 'BinaryDataNotificationCallbacks']
ProgressFuncType = Callable[[int, int], bool]

class ReferenceSource:
	def __init__(self, func:Optional['_function.Function'], arch:Optional['architecture.Architecture'],
		addr:int):
		self._function = func
		self._arch = arch
		self._address = addr

	def __repr__(self):
		if self._arch:
			return f"<ref: {self._arch.name}@{self._address:#x}>"
		else:
			return f"<ref: {self._address:#x}>"

	def __eq__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return (self.function, self.arch, self.address) == (other.function, other.arch, other.address)

	def __ne__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return not (self == other)

	def __lt__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return self.address < other.address

	def __gt__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return self.address > other.address

	def __ge__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return self.address >= other.address

	def __le__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return self.address <= other.address

	def __hash__(self):
		return hash((self._function, self._arch, self._address))

	@property
	def function(self):
		return self._function

	@property
	def arch(self):
		return self._arch

	@property
	def address(self):
		return self._address


class BinaryDataNotification:
	def __init__(self):
		pass

	def data_written(self, view:'BinaryView', offset:int, length:int) -> None:
		pass

	def data_inserted(self, view:'BinaryView', offset:int, length:int) -> None:
		pass

	def data_removed(self, view:'BinaryView', offset:int, length:int) -> None:
		pass

	def function_added(self, view:'BinaryView', func:'_function.Function') -> None:
		pass

	def function_removed(self, view:'BinaryView', func:'_function.Function') -> None:
		pass

	def function_updated(self, view:'BinaryView', func:'_function.Function') -> None:
		pass

	def function_update_requested(self, view:'BinaryView', func:'_function.Function') -> None:
		pass

	def data_var_added(self, view:'BinaryView', var:'DataVariable') -> None:
		pass

	def data_var_removed(self, view:'BinaryView', var:'DataVariable') -> None:
		pass

	def data_var_updated(self, view:'BinaryView', var:'DataVariable') -> None:
		pass

	def data_metadata_updated(self, view:'BinaryView', offset:int) -> None:
		pass

	def tag_type_updated(self, view:'BinaryView', tag_type) -> None:
		pass

	def tag_added(self, view:'BinaryView', tag:'Tag', ref_type:TagReferenceType, auto_defined:bool,
		arch:Optional['architecture.Architecture'], func:Optional[_function.Function], addr:int) -> None:
		pass

	def tag_updated(self, view:'BinaryView', tag:'Tag', ref_type:TagReferenceType, auto_defined:bool,
		arch:Optional['architecture.Architecture'], func:Optional[_function.Function], addr:int) -> None:
		pass

	def tag_removed(self, view:'BinaryView', tag:'Tag', ref_type:TagReferenceType, auto_defined:bool,
		arch:Optional['architecture.Architecture'], func:Optional[_function.Function], addr:int) -> None:
		pass

	def symbol_added(self, view:'BinaryView', sym:'_types.Symbol') -> None:
		pass

	def symbol_updated(self, view:'BinaryView', sym:'_types.Symbol') -> None:
		pass

	def symbol_removed(self, view:'BinaryView', sym:'_types.Symbol') -> None:
		pass

	def string_found(self, view:'BinaryView', string_type:StringType, offset:int, length:int) -> None:
		pass

	def string_removed(self, view:'BinaryView', string_type:StringType, offset:int, length:int) -> None:
		pass

	def type_defined(self, view:'BinaryView', name:'_types.QualifiedName', type:'_types.Type') -> None:
		pass

	def type_undefined(self, view:'BinaryView', name:'_types.QualifiedName', type:'_types.Type') -> None:
		pass

	def type_ref_changed(self, view:'BinaryView', name:'_types.QualifiedName', type:'_types.Type') -> None:
		pass


class StringReference:
	_decodings = {
		StringType.AsciiString: "ascii",
		StringType.Utf8String: "utf-8",
		StringType.Utf16String: "utf-16",
		StringType.Utf32String: "utf-32",
	}
	def __init__(self, bv:'BinaryView', string_type:StringType, start:int, length:int):
		self._type = string_type
		self._start = start
		self._length = length
		self._view = bv

	def __repr__(self):
		return f"<{self._type}: {self._start:#x}, len {self._length:#x}>"

	def __str__(self):
		return self.value

	def __len__(self):
		return self._length

	@property
	def value(self) -> str:
		return self._view.read(self._start, self._length).decode(self._decodings[self._type])

	@property
	def raw(self) -> bytes:
		return self._view.read(self._start, self._length)

	@property
	def type(self) -> StringType:
		return self._type

	@type.setter
	def type(self, value:StringType):
		self._type = value

	@property
	def start(self) -> int:
		return self._start

	@start.setter
	def start(self, value:int):
		self._start = value

	@property
	def length(self) -> int:
		return self._length

	@property
	def view(self) -> 'BinaryView':
		return self._view


class AnalysisCompletionEvent:
	"""
	The ``AnalysisCompletionEvent`` object provides an asynchronous mechanism for receiving
	callbacks when analysis is complete. The callback runs once. A completion event must be added
	for each new analysis in order to be notified of each analysis completion.  The
	AnalysisCompletionEvent class takes responsibility for keeping track of the object's lifetime.

	:Example:
		>>> def on_complete(self):
		...     print("Analysis Complete", self._view)
		...
		>>> evt = AnalysisCompletionEvent(bv, on_complete)
		>>>
	"""
	_pending_analysis_completion_events = {}
	def __init__(self, view:'BinaryView', callback:Union[Callable[['AnalysisCompletionEvent'], None], Callable[[], None]]):
		self._view = view
		self.callback = callback
		self._cb = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(self._notify)
		self.handle = core.BNAddAnalysisCompletionEvent(self._view.handle, None, self._cb)
		self.__class__._pending_analysis_completion_events[id(self)] = self

	def __del__(self):
		if id(self) in self.__class__._pending_analysis_completion_events:
			del self.__class__._pending_analysis_completion_events[id(self)]
		if core is not None:
			core.BNFreeAnalysisCompletionEvent(self.handle)

	def _notify(self, ctxt):
		if id(self) in self.__class__._pending_analysis_completion_events:
			del self.__class__._pending_analysis_completion_events[id(self)]
		try:
			arg_offset = inspect.ismethod(self.callback)
			callback_spec = inspect.getargspec(self.callback)
			if len(callback_spec.args) > arg_offset:
				self.callback(self) # type: ignore
			else:
				self.callback() # type: ignore
		except:
			log.log_error(traceback.format_exc())

	def _empty_callback(self):
		pass

	def cancel(self) -> None:
		"""
		The ``cancel`` method will cancel analysis for an :class:`AnalysisCompletionEvent`.

		.. warning:: This method should only be used when the system is being shut down and no further analysis should be done afterward.

		"""
		self.callback = self._empty_callback
		core.BNCancelAnalysisCompletionEvent(self.handle)
		if id(self) in self.__class__._pending_analysis_completion_events:
			del self.__class__._pending_analysis_completion_events[id(self)]

	@property
	def view(self) -> 'BinaryView':
		return self._view

	@view.setter
	def view(self, value:'BinaryView'):
		self._view = value


class BinaryViewEvent:
	"""
	The ``BinaryViewEvent`` object provides a mechanism for receiving callbacks	when a BinaryView
	is Finalized or the initial analysis is finished. The BinaryView finalized callbacks run before the
	initial analysis starts. The callbacks run one-after-another in the same order as they get registered.
	It is a good place to modify the BinaryView to add extra information to it.

	For newly opened binaries, the initial analysis completion callbacks run after the initial analysis,
	as well as linear sweep	and signature matcher (if they are configured to run), completed. For loading
	old databases, the callbacks run after the database is loaded, as well as any automatic analysis
	update finishes.

	The callback function receives a BinaryView as its parameter. It is possible to call
	BinaryView.add_analysis_completion_event() on it to set up other callbacks for analysis completion.

	:Example:
		>>> def callback(bv):
		... 	print('start: 0x%x' % bv.start)
		...
		>>> BinaryViewType.add_binaryview_finalized_event(callback)
	"""
	BinaryViewEventCallback = Callable[['BinaryView'], None]
	# This has no functional purposes;
	# we just need it to stop Python from prematurely freeing the object
	_binaryview_events = {}

	@classmethod
	def register(cls, event_type:BinaryViewEventType, callback:BinaryViewEventCallback) -> None:
		callback_obj = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(core.BNBinaryView))(lambda ctxt, view: cls._notify(view, callback))
		core.BNRegisterBinaryViewEvent(event_type, callback_obj, None)
		cls._binaryview_events[len(cls._binaryview_events)] = callback_obj

	@staticmethod
	def _notify(view:core.BNBinaryView, callback:BinaryViewEventCallback) -> None:
		try:
			file_metadata = filemetadata.FileMetadata(handle = core.BNGetFileForView(view))
			view_obj = BinaryView(file_metadata = file_metadata, handle = core.BNNewViewReference(view))
			callback(view_obj)
		except:
			log.log_error(traceback.format_exc())


class ActiveAnalysisInfo:
	def __init__(self, func:'_function.Function', analysis_time:int, update_count:int, submit_count:int):
		self._func = func
		self._analysis_time = analysis_time
		self._update_count = update_count
		self._submit_count = submit_count

	def __repr__(self):
		return "<ActiveAnalysisInfo %s, analysis_time %d, update_count %d, submit_count %d>" % (self._func, self._analysis_time, self._update_count, self._submit_count)

	@property
	def func(self) -> '_function.Function':
		return self._func

	@property
	def analysis_time(self) -> int:
		return self._analysis_time

	@property
	def update_count(self) -> int:
		return self._update_count

	@property
	def submit_count(self) -> int:
		return self._submit_count


class AnalysisInfo:
	def __init__(self, state:AnalysisState, analysis_time:int, active_info:List[ActiveAnalysisInfo]):
		self._state = AnalysisState(state)
		self._analysis_time = analysis_time
		self._active_info = active_info

	def __repr__(self):
		return f"<AnalysisInfo {self._state}, analysis_time {self._analysis_time}, active_info {len(self._active_info)}>"

	@property
	def state(self) -> AnalysisState:
		return self._state

	@property
	def analysis_time(self) -> int:
		return self._analysis_time

	@property
	def active_info(self) -> List[ActiveAnalysisInfo]:
		return self._active_info


class AnalysisProgress:
	def __init__(self, state:AnalysisState, count:int, total:int):
		self._state = state
		self._count = count
		self._total = total

	def __str__(self):
		if self._state == AnalysisState.InitialState:
			return "Initial"
		if self._state == AnalysisState.HoldState:
			return "Hold"
		if self._state == AnalysisState.IdleState:
			return "Idle"
		if self._state == AnalysisState.DisassembleState:
			return "Disassembling (%d/%d)" % (self._count, self._total)
		if self._state == AnalysisState.AnalyzeState:
			return "Analyzing (%d/%d)" % (self._count, self._total)
		return "Extended Analysis"

	def __repr__(self):
		return "<progress: %s>" % str(self)

	@property
	def state(self) -> AnalysisState:
		return self._state

	@property
	def count(self) -> int:
		return self._count

	@property
	def total(self) -> int:
		return self._total


class BinaryDataNotificationCallbacks:
	def __init__(self, view:'BinaryView', notify:'BinaryDataNotification'):
		self._view = view
		self._notify = notify
		self._cb = core.BNBinaryDataNotification()
		self._cb.context = 0
		self._cb.dataWritten = self._cb.dataWritten.__class__(self._data_written)
		self._cb.dataInserted = self._cb.dataInserted.__class__(self._data_inserted)
		self._cb.dataRemoved = self._cb.dataRemoved.__class__(self._data_removed)
		self._cb.functionAdded = self._cb.functionAdded.__class__(self._function_added)
		self._cb.functionRemoved = self._cb.functionRemoved.__class__(self._function_removed)
		self._cb.functionUpdated = self._cb.functionUpdated.__class__(self._function_updated)
		self._cb.functionUpdateRequested = self._cb.functionUpdateRequested.__class__(self._function_update_requested)
		self._cb.dataVariableAdded = self._cb.dataVariableAdded.__class__(self._data_var_added)
		self._cb.dataVariableRemoved = self._cb.dataVariableRemoved.__class__(self._data_var_removed)
		self._cb.dataVariableUpdated = self._cb.dataVariableUpdated.__class__(self._data_var_updated)
		self._cb.dataMetadataUpdated = self._cb.dataMetadataUpdated.__class__(self._data_metadata_updated)
		self._cb.tagTypeUpdated = self._cb.tagTypeUpdated.__class__(self._tag_type_updated)
		self._cb.tagAdded = self._cb.tagAdded.__class__(self._tag_added)
		self._cb.tagUpdated = self._cb.tagUpdated.__class__(self._tag_updated)
		self._cb.tagRemoved = self._cb.tagRemoved.__class__(self._tag_removed)
		self._cb.symbolAdded = self._cb.symbolAdded.__class__(self._symbol_added)
		self._cb.symbolUpdated = self._cb.symbolUpdated.__class__(self._symbol_updated)
		self._cb.symbolRemoved = self._cb.symbolRemoved.__class__(self._symbol_removed)
		self._cb.stringFound = self._cb.stringFound.__class__(self._string_found)
		self._cb.stringRemoved = self._cb.stringRemoved.__class__(self._string_removed)
		self._cb.typeDefined = self._cb.typeDefined.__class__(self._type_defined)
		self._cb.typeUndefined = self._cb.typeUndefined.__class__(self._type_undefined)
		self._cb.typeReferenceChanged = self._cb.typeReferenceChanged.__class__(self._type_ref_changed)

	def _register(self) -> None:
		core.BNRegisterDataNotification(self._view.handle, self._cb)

	def _unregister(self) -> None:
		core.BNUnregisterDataNotification(self._view.handle, self._cb)

	def _data_written(self, ctxt, view:core.BNBinaryView, offset:int, length:int) -> None:
		try:
			self._notify.data_written(self._view, offset, length)
		except OSError:
			log.log_error(traceback.format_exc())

	def _data_inserted(self, ctxt, view:core.BNBinaryView, offset:int, length:int) -> None:
		try:
			self._notify.data_inserted(self._view, offset, length)
		except:
			log.log_error(traceback.format_exc())

	def _data_removed(self, ctxt, view:core.BNBinaryView, offset:int, length:int) -> None:
		try:
			self._notify.data_removed(self._view, offset, length)
		except:
			log.log_error(traceback.format_exc())

	def _function_added(self, ctxt, view:core.BNBinaryView, func:core.BNFunction) -> None:
		try:
			self._notify.function_added(self._view, _function.Function(self._view, core.BNNewFunctionReference(func)))
		except:
			log.log_error(traceback.format_exc())

	def _function_removed(self, ctxt, view:core.BNBinaryView, func:core.BNFunction) -> None:
		try:
			self._notify.function_removed(self._view, _function.Function(self._view, core.BNNewFunctionReference(func)))
		except:
			log.log_error(traceback.format_exc())

	def _function_updated(self, ctxt, view:core.BNBinaryView, func:core.BNFunction) -> None:
		try:
			self._notify.function_updated(self._view, _function.Function(self._view, core.BNNewFunctionReference(func)))
		except:
			log.log_error(traceback.format_exc())

	def _function_update_requested(self, ctxt, view:core.BNBinaryView, func:core.BNFunction) -> None:
		try:
			self._notify.function_update_requested(self._view, _function.Function(self._view, core.BNNewFunctionReference(func)))
		except:
			log.log_error(traceback.format_exc())

	def _data_var_added(self, ctxt, view:core.BNBinaryView, var:core.BNDataVariable) -> None:
		try:
			self._notify.data_var_added(self._view, DataVariable.from_core_struct(var[0], self._view))
		except:
			log.log_error(traceback.format_exc())

	def _data_var_removed(self, ctxt, view:core.BNBinaryView, var:core.BNDataVariable) -> None:
		try:
			self._notify.data_var_removed(self._view, DataVariable.from_core_struct(var[0], self._view))
		except:
			log.log_error(traceback.format_exc())

	def _data_var_updated(self, ctxt, view:core.BNBinaryView, var:core.BNDataVariable) -> None:
		try:
			self._notify.data_var_updated(self._view, DataVariable.from_core_struct(var[0], self._view))
		except:
			log.log_error(traceback.format_exc())

	def _data_metadata_updated(self, ctxt, view:core.BNBinaryView, offset:int) -> None:
		try:
			self._notify.data_metadata_updated(self._view, offset)
		except:
			log.log_error(traceback.format_exc())

	def _tag_type_updated(self, ctxt, view:core.BNBinaryView, tag_type:core.BNTagType) -> None:
		try:
			core_tag_type = core.BNNewTagTypeReference(tag_type)
			assert core_tag_type is not None, "core.BNNewTagTypeReference returned None"
			self._notify.tag_type_updated(self._view, TagType(core_tag_type))
		except:
			log.log_error(traceback.format_exc())

	def _tag_added(self, ctxt, view:core.BNBinaryView, tag_ref:core.BNTagReference) -> None:
		try:
			ref_type = tag_ref[0].refType
			auto_defined = tag_ref[0].autoDefined
			core_tag = core.BNNewTagReference(tag_ref[0].tag)
			assert core_tag is not None, "core.BNNewTagReference returned None"
			tag = Tag(core_tag)
			# Null for data tags (not in any arch or function)
			if ctypes.cast(tag_ref[0].arch, ctypes.c_void_p).value is None:
				arch = None
			else:
				arch =  architecture.CoreArchitecture._from_cache(tag_ref[0].arch)
			if ctypes.cast(tag_ref[0].func, ctypes.c_void_p).value is None:
				func = None
			else:
				func = _function.Function(self._view, core.BNNewFunctionReference(tag_ref[0].func))
			addr = tag_ref[0].addr
			self._notify.tag_added(self._view, tag, ref_type, auto_defined, arch, func, addr)
		except:
			log.log_error(traceback.format_exc())

	def _tag_updated(self, ctxt, view:core.BNBinaryView, tag_ref:core.BNTagReference) -> None:
		try:
			ref_type = tag_ref[0].refType
			auto_defined = tag_ref[0].autoDefined
			core_tag = core.BNNewTagReference(tag_ref[0].tag)
			assert core_tag is not None
			tag = Tag(core_tag)
			# Null for data tags (not in any arch or function)
			if ctypes.cast(tag_ref[0].arch, ctypes.c_void_p).value is None:
				arch = None
			else:
				arch = architecture.CoreArchitecture._from_cache(tag_ref[0].arch)
			if ctypes.cast(tag_ref[0].func, ctypes.c_void_p).value is None:
				func = None
			else:
				func = _function.Function(self._view, core.BNNewFunctionReference(tag_ref[0].func))
			addr = tag_ref[0].addr
			self._notify.tag_updated(self._view, tag, ref_type, auto_defined, arch, func, addr)
		except:
			log.log_error(traceback.format_exc())

	def _tag_removed(self, ctxt, view:core.BNBinaryView, tag_ref:core.BNTagReference) -> None:
		try:
			ref_type = tag_ref[0].refType
			auto_defined = tag_ref[0].autoDefined
			core_tag = core.BNNewTagReference(tag_ref[0].tag)
			assert core_tag is not None, "core.BNNewTagReference returned None"
			tag = Tag(core_tag)
			# Null for data tags (not in any arch or function)
			if ctypes.cast(tag_ref[0].arch, ctypes.c_void_p).value is None:
				arch = None
			else:
				arch = architecture.CoreArchitecture._from_cache(tag_ref[0].arch)
			if ctypes.cast(tag_ref[0].func, ctypes.c_void_p).value is None:
				func = None
			else:
				func = _function.Function(self._view, core.BNNewFunctionReference(tag_ref[0].func))
			addr = tag_ref[0].addr
			self._notify.tag_removed(self._view, tag, ref_type, auto_defined, arch, func, addr)
		except:
			log.log_error(traceback.format_exc())

	def _symbol_added(self, ctxt, view:core.BNBinaryView, sym:core.BNSymbol) -> None:
		try:
			self._notify.symbol_added(self._view, _types.Symbol(None, None, None, handle = core.BNNewSymbolReference(sym)))
		except:
			log.log_error(traceback.format_exc())

	def _symbol_updated(self, ctxt, view:core.BNBinaryView, sym:core.BNSymbol) -> None:
		try:
			self._notify.symbol_updated(self._view, _types.Symbol(None, None, None, handle = core.BNNewSymbolReference(sym)))
		except:
			log.log_error(traceback.format_exc())

	def _symbol_removed(self, ctxt, view:core.BNBinaryView, sym:core.BNSymbol) -> None:
		try:
			self._notify.symbol_removed(self._view, _types.Symbol(None, None, None, handle = core.BNNewSymbolReference(sym)))
		except:
			log.log_error(traceback.format_exc())

	def _string_found(self, ctxt, view:core.BNBinaryView, string_type:int, offset:int, length:int) -> None:
		try:
			self._notify.string_found(self._view, StringType(string_type), offset, length)
		except:
			log.log_error(traceback.format_exc())

	def _string_removed(self, ctxt, view:core.BNBinaryView, string_type:int, offset:int, length:int) -> None:
		try:
			self._notify.string_removed(self._view, StringType(string_type), offset, length)
		except:
			log.log_error(traceback.format_exc())

	def _type_defined(self, ctxt, view:core.BNBinaryView, name:str, type_obj:'_types.Type') -> None:
		try:
			qualified_name = _types.QualifiedName._from_core_struct(name[0])
			self._notify.type_defined(self._view, qualified_name, _types.Type.create(core.BNNewTypeReference(type_obj), platform = self._view.platform))
		except:
			log.log_error(traceback.format_exc())

	def _type_undefined(self, ctxt, view:core.BNBinaryView, name:str, type_obj:'_types.Type') -> None:
		try:
			qualified_name = _types.QualifiedName._from_core_struct(name[0])
			self._notify.type_undefined(self._view, qualified_name, _types.Type.create(core.BNNewTypeReference(type_obj), platform = self._view.platform))
		except:
			log.log_error(traceback.format_exc())

	def _type_ref_changed(self, ctxt, view:core.BNBinaryView, name:str, type_obj:'_types.Type') -> None:
		try:
			qualified_name = _types.QualifiedName._from_core_struct(name[0])
			self._notify.type_ref_changed(self._view, qualified_name, _types.Type.create(core.BNNewTypeReference(type_obj), platform = self._view.platform))
		except:
			log.log_error(traceback.format_exc())

	@property
	def view(self) -> 'BinaryView':
		return self._view

	@property
	def notify(self) -> 'BinaryDataNotification':
		return self._notify


class _BinaryViewTypeMetaclass(type):
	def __iter__(self):
		binaryninja._init_plugins()
		count = ctypes.c_ulonglong()
		types = core.BNGetBinaryViewTypes(count)
		if types is None:
			return
		try:
			for i in range(0, count.value):
				yield BinaryViewType(types[i])
		finally:
			core.BNFreeBinaryViewTypeList(types)

	def __getitem__(self, value):
		binaryninja._init_plugins()
		view_type = core.BNGetBinaryViewTypeByName(str(value))
		if view_type is None:
			raise KeyError("'%s' is not a valid view type" % str(value))
		return BinaryViewType(view_type)


class BinaryViewType(metaclass=_BinaryViewTypeMetaclass):
	def __init__(self, handle:core.BNBinaryViewTypeHandle):
		# Used to force Python callback objects to not get garbage collected
		_platform_recognizers = {}
		_handle = core.BNBinaryViewTypeHandle
		self.handle = ctypes.cast(handle, _handle)

	def __repr__(self):
		return f"<view type: '{self.name}'>"

	def __eq__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return ctypes.addressof(self.handle.contents) == ctypes.addressof(other.handle.contents)

	def __ne__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return not (self == other)

	def __hash__(self):
		return hash(ctypes.addressof(self.handle.contents))

	@property
	def name(self) -> str:
		"""BinaryView name (read-only)"""
		return core.BNGetBinaryViewTypeName(self.handle)

	@property
	def long_name(self) -> str:
		"""BinaryView long name (read-only)"""
		return core.BNGetBinaryViewTypeLongName(self.handle)

	@property
	def is_deprecated(self) -> bool:
		"""returns if the BinaryViewType is deprecated (read-only)"""
		return core.BNIsBinaryViewTypeDeprecated(self.handle)

	def create(self, data:'BinaryView') -> Optional['BinaryView']:
		view = core.BNCreateBinaryViewOfType(self.handle, data.handle)
		if view is None:
			return None
		return BinaryView(file_metadata=data.file, handle=view)

	def open(self, src:PathType, file_metadata:'filemetadata.FileMetadata'=None) -> Optional['BinaryView']:
		"""
		``open`` opens an instance of a particular BinaryViewType and returns it, or None if not possible.

		:param str src: path to filename or bndb to open
		:param FileMetadata file_metadata: Optional parameter for a :py:class:`FileMetadata` object
		:return: returns a :py:class:`BinaryView` object for the given filename
		:rtype: :py:class:`BinaryView` or ``None``
		"""
		data = BinaryView.open(src, file_metadata)
		if data is None:
			return None
		return self.create(data)

	@classmethod
	def get_view_of_file(cls, filename:PathType, update_analysis:bool=True,
		progress_func:Optional[ProgressFuncType]=None) -> Optional['BinaryView']:
		"""
		``get_view_of_file`` opens and returns the first available :py:class:`BinaryView`, excluding a Raw :py:class:`BinaryViewType` unless no other view is available

		:param str filename: path to filename or bndb to open
		:param bool update_analysis: whether or not to run :func:`update_analysis_and_wait` after opening a :py:class:`BinaryView`, defaults to ``True``
		:param callback progress_func: optional function to be called with the current progress and total count
		:return: returns a :py:class:`BinaryView` object for the given filename
		:rtype: :py:class:`BinaryView` or ``None``
		"""
		sqlite = b"SQLite format 3"
		if not isinstance(filename, str):
			filename = str(filename)

		isDatabase = filename.endswith(".bndb")
		if isDatabase:
			f = open(filename, 'rb')
			if f is None or f.read(len(sqlite)) != sqlite:
				return None
			f.close()
			view = filemetadata.FileMetadata().open_existing_database(filename, progress_func)
		else:
			view = BinaryView.open(filename)

		if view is None:
			return None
		for available in view.available_view_types:
			if available.name != "Raw":
				if isDatabase:
					bv = view.get_view_of_type(available.name)
				else:
					bv = available.open(filename)
				break
		else:
			if isDatabase:
				bv = view.get_view_of_type("Raw")
			else:
				bv = cls["Raw"].open(filename) # type: ignore

		if bv is not None and update_analysis:
			bv.update_analysis_and_wait()
		return bv

	@classmethod
	def get_view_of_file_with_options(cls, filename:str, update_analysis:Optional[bool]=True,
		progress_func:Optional[ProgressFuncType]=None, options:Mapping[str, Any]={}) -> Optional['BinaryView']:
		"""
		``get_view_of_file_with_options`` opens, generates default load options (which are overridable), and returns the first available \
		:py:class:`BinaryView`. If no :py:class:`BinaryViewType` is available, then a ``Mapped`` :py:class:`BinaryViewType` is used to load \
		the :py:class:`BinaryView` with the specified load options. The ``Mapped`` view type attempts to auto-detect the architecture of the \
		file during initialization. If no architecture is detected or specified in the load options, then the ``Mapped`` view type fails to \
		initialize and returns ``None``.

		.. note:: Calling this method without providing options is not necessarily equivalent to simply calling :func:`get_view_of_file`. This is because \
		a :py:class:`BinaryViewType` is in control of generating load options, this method allows an alternative default way to open a file. For \
		example, opening a relocatable object file with :func:`get_view_of_file` sets 'loader.imageBase' to `0`, whereas enabling the **'files.pic.autoRebase'** \
		setting and opening with :func:`get_view_of_file_with_options` sets **'loader.imageBase'** to ``0x400000`` for 64-bit binaries, or ``0x10000`` for 32-bit binaries.

		.. note:: Although general container file support is not complete, support for Universal archives exists. It's possible to control the architecture preference \
		with the **'files.universal.architecturePreference'** setting. This setting is scoped to SettingsUserScope and can be modified as follows ::

			>>> Settings().set_string_list("files.universal.architecturePreference", ["arm64"])

		It's also possible to override the **'files.universal.architecturePreference'** user setting by specifying it directly with :func:`get_view_of_file_with_options`.
		This specific usage of this setting is experimental and may change in the future ::

			>>> bv = BinaryViewType.get_view_of_file_with_options('/bin/ls', options={'files.universal.architecturePreference': ['arm64']})

		:param str filename: path to filename or bndb to open
		:param bool update_analysis: whether or not to run :func:`update_analysis_and_wait` after opening a :py:class:`BinaryView`, defaults to ``True``
		:param callback progress_func: optional function to be called with the current progress and total count
		:param dict options: a dictionary in the form {setting identifier string : object value}
		:return: returns a :py:class:`BinaryView` object for the given filename or ``None``
		:rtype: :py:class:`BinaryView` or ``None``

		:Example:

			>>> BinaryViewType.get_view_of_file_with_options('/bin/ls', options={'loader.imageBase': 0xfffffff0000, 'loader.macho.processFunctionStarts' : False})
			<BinaryView: '/bin/ls', start 0xfffffff0000, len 0xa290>
			>>>
		"""
		sqlite = b"SQLite format 3"
		isDatabase = filename.endswith(".bndb")
		if isDatabase:
			f = open(filename, 'rb')
			if f is None or f.read(len(sqlite)) != sqlite:
				return None
			f.close()
			view = filemetadata.FileMetadata().open_database_for_configuration(filename)
		else:
			view = BinaryView.open(filename)

		if view is None:
			return None
		bvt = None
		universal_bvt = None
		for available in view.available_view_types:
			if available.name == "Universal":
				universal_bvt = available
				continue
			if bvt is None and available.name != "Raw":
				bvt = available

		if bvt is None:
			bvt = cls["Mapped"] # type: ignore

		default_settings = settings.Settings(bvt.name + "_settings")
		default_settings.deserialize_schema(settings.Settings().serialize_schema())
		default_settings.set_resource_id(bvt.name)

		load_settings = None
		if isDatabase:
			load_settings = view.get_load_settings(bvt.name)
		if load_settings is None:
			# FIXME:
			if universal_bvt is not None and "files.universal.architecturePreference" in options:
				load_settings = universal_bvt.get_load_settings_for_data(view)
				if load_settings is None:
					raise Exception(f"Could not load {options['files.universal.architecturePreference'][0]} from Universal image. Entry not found!")
				arch_list = json.loads(load_settings.get_string('loader.universal.architectures'))
				arch_entry = [entry for entry in arch_list if entry['architecture'] == options['files.universal.architecturePreference'][0]]
				if not arch_entry:
					raise Exception(f"Could not load {options['files.universal.architecturePreference'][0]} from Universal image. Entry not found!")

				load_settings = settings.Settings(core.BNGetUniqueIdentifierString())
				load_settings.deserialize_schema(arch_entry[0]['loadSchema'])
			else:
				load_settings = bvt.get_load_settings_for_data(view)
		if load_settings is None:
			raise Exception(f"Could not get load settings for binary view of type `{bvt.name}`")
		load_settings.set_resource_id(bvt.name)
		view.set_load_settings(bvt.name, load_settings)

		for key, value in options.items():
			if load_settings.contains(key):
				if not load_settings.set_json(key, json.dumps(value), view):
					raise ValueError("Setting: {} set operation failed!".format(key))
			elif default_settings.contains(key):
				if not default_settings.set_json(key, json.dumps(value), view):
					raise ValueError("Setting: {} set operation failed!".format(key))
			else:
				raise NotImplementedError("Setting: {} not available!".format(key))

		if isDatabase:
			view = view.file.open_existing_database(filename, progress_func)
			if view is None:
				raise Exception(f"Unable to open_existing_database with filename {filename}")
			bv = view.get_view_of_type(bvt.name)
		else:
			bv = bvt.create(view)

		if bv is None:
			return view
		elif update_analysis:
			bv.update_analysis_and_wait()
		return bv

	def parse(self, data:'BinaryView') -> Optional['BinaryView']:
		view = core.BNParseBinaryViewOfType(self.handle, data.handle)
		if view is None:
			return None
		return BinaryView(file_metadata=data.file, handle=view)

	def is_valid_for_data(self, data:'BinaryView') -> bool:
		return core.BNIsBinaryViewTypeValidForData(self.handle, data.handle)

	def get_default_load_settings_for_data(self, data:'BinaryView') -> Optional['settings.Settings']:
		load_settings = core.BNGetBinaryViewDefaultLoadSettingsForData(self.handle, data.handle)
		if load_settings is None:
			return None
		return settings.Settings(handle=load_settings)

	def get_load_settings_for_data(self, data:'BinaryView') -> Optional['settings.Settings']:
		view_handle = None
		if data is not None:
			view_handle = data.handle
		load_settings = core.BNGetBinaryViewLoadSettingsForData(self.handle, view_handle)
		if load_settings is None:
			return None
		return settings.Settings(handle=load_settings)

	def register_arch(self, ident:int, endian:Endianness, arch:'architecture.Architecture') -> None:
		core.BNRegisterArchitectureForViewType(self.handle, ident, endian, arch.handle)

	def get_arch(self, ident:int, endian:Endianness) -> Optional['architecture.Architecture']:
		arch = core.BNGetArchitectureForViewType(self.handle, ident, endian)
		if arch is None:
			return None
		return architecture.CoreArchitecture._from_cache(arch)

	def register_platform(self, ident:int, arch:'architecture.Architecture', plat:'_platform.Platform') -> None:
		core.BNRegisterPlatformForViewType(self.handle, ident, arch.handle, plat.handle)

	def register_default_platform(self, arch:'architecture.Architecture', plat:'_platform.Platform') -> None:
		core.BNRegisterDefaultPlatformForViewType(self.handle, arch.handle, plat.handle)

	def register_platform_recognizer(self, ident, endian, cb):
		def callback(cb, view, meta):
			try:
				file_metadata = binaryninja.filemetadata.FileMetadata(handle = core.BNGetFileForView(view))
				view_obj = binaryninja.binaryview.BinaryView(file_metadata = file_metadata, handle = core.BNNewViewReference(view))
				meta_obj = binaryninja.metadata.Metadata(handle = core.BNNewMetadataReference(meta))
				plat = cb(view_obj, meta_obj)
				if plat:
					return ctypes.cast(core.BNNewPlatformReference(plat.handle), ctypes.c_void_p).value
			except:
				binaryninja.log.log_error(traceback.format_exc())
			return None

		callback_obj = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(core.BNBinaryView), ctypes.POINTER(core.BNMetadata))(lambda ctxt, view, meta: callback(cb, view, meta))
		core.BNRegisterPlatformRecognizerForViewType(self.handle, ident, endian, callback_obj, None)
		self.__class__._platform_recognizers[len(self.__class__._platform_recognizers)] = callback_obj


	def get_platform(self, ident:int, arch:'architecture.Architecture') -> Optional['_platform.Platform']:
		plat = core.BNGetPlatformForViewType(self.handle, ident, arch.handle)
		if plat is None:
			return None
		return _platform.Platform(handle = plat)

	def recognize_platform(self, ident, endian, view, metadata):
		plat = core.BNRecognizePlatformForViewType(self.handle, ident, endian, view.handle, metadata.handle)
		if plat is None:
			return None
		return binaryninja.platform.Platform(handle = plat)

	@staticmethod
	def add_binaryview_finalized_event(callback:BinaryViewEvent.BinaryViewEventCallback) -> None:
		"""
		`add_binaryview_finalized_event` adds a callback that gets executed
		when new binaryview is finalized.
		For more details, please refer to the documentation of BinaryViewEvent.
		"""
		BinaryViewEvent.register(BinaryViewEventType.BinaryViewFinalizationEvent, callback)

	@staticmethod
	def add_binaryview_initial_analysis_completion_event(callback):
		"""
		`add_binaryview_initial_analysis_completion_event` adds a callback
		that gets executed after the initial analysis, as well as linear
		sweep and signature matcher (if they are configured to run) completed.
		For more details, please refer to the documentation of BinaryViewEvent.
		"""
		BinaryViewEvent.register(BinaryViewEventType.BinaryViewInitialAnalysisCompletionEvent, callback)


class Segment:
	def __init__(self, handle:core.BNSegmentHandle):
		self.handle = handle

	def __del__(self):
		if core is not None:
			core.BNFreeSegment(self.handle)

	def __repr__(self):
		r ="r" if self.readable else "-"
		w ="w" if self.writable else "-"
		x ="x" if self.executable else "-"
		return f"<segment: {self.start:#x}-{self.end:#x}, {r}{w}{x}>"

	def __len__(self):
		return core.BNSegmentGetLength(self.handle)

	def __eq__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return ctypes.addressof(self.handle.contents) == ctypes.addressof(other.handle.contents)

	def __ne__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return not (self == other)

	def __hash__(self):
		return hash(ctypes.addressof(self.handle.contents))

	@property
	def start(self) -> int:
		return core.BNSegmentGetStart(self.handle)

	@property
	def end(self) -> int:
		return core.BNSegmentGetEnd(self.handle)

	@property
	def executable(self) -> bool:
		return (core.BNSegmentGetFlags(self.handle) & SegmentFlag.SegmentExecutable) != 0

	@property
	def writable(self) -> bool:
		return (core.BNSegmentGetFlags(self.handle) & SegmentFlag.SegmentWritable) != 0

	@property
	def readable(self) -> bool:
		return (core.BNSegmentGetFlags(self.handle) & SegmentFlag.SegmentReadable) != 0

	@property
	def data_length(self) -> int:
		return core.BNSegmentGetDataLength(self.handle)

	@property
	def data_offset(self) -> int:
		return core.BNSegmentGetDataOffset(self.handle)

	@property
	def data_end(self) -> int:
		return core.BNSegmentGetDataEnd(self.handle)

	@property
	def relocation_count(self) -> int:
		return core.BNSegmentGetRelocationsCount(self.handle)

	@property
	def auto_defined(self) -> bool:
		return core.BNSegmentIsAutoDefined(self.handle)

	@property
	def relocation_ranges(self) -> Generator[Tuple[int, int], None, None]:
		"""List of relocation range tuples (read-only)"""

		count = ctypes.c_ulonglong()
		ranges = core.BNSegmentGetRelocationRanges(self.handle, count)
		assert ranges is not None, "core.BNSegmentGetRelocationRanges returned None"

		try:
			for i in range(0, count.value):
				yield (ranges[i].start, ranges[i].end)
		finally:
			core.BNFreeRelocationRanges(ranges, count)

	def relocation_ranges_at(self, addr:int) -> Generator[Tuple[int, int], None, None]:
		"""List of relocation range tuples (read-only)"""

		count = ctypes.c_ulonglong()
		ranges = core.BNSegmentGetRelocationRangesAtAddress(self.handle, addr, count)
		assert ranges is not None, "core.BNSegmentGetRelocationRangesAtAddress returned None"

		try:
			for i in range(0, count.value):
				yield (ranges[i].start, ranges[i].end)
		finally:
			core.BNFreeRelocationRanges(ranges, count)


class Section:
	def __init__(self, handle:core.BNSectionHandle):
		self.handle = handle

	def __del__(self):
		if core is not None:
			core.BNFreeSection(self.handle)

	def __repr__(self):
		return "<section {self.name}: {self.start:#x}-{self.end:#x}>"

	def __len__(self):
		return core.BNSectionGetLength(self.handle)

	def __eq__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return ctypes.addressof(self.handle.contents) == ctypes.addressof(other.handle.contents)

	def __ne__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return not (self == other)

	def __hash__(self):
		return hash(ctypes.addressof(self.handle.contents))

	@property
	def name(self) -> str:
		return core.BNSectionGetName(self.handle)

	@property
	def type(self) -> str:
		return core.BNSectionGetType(self.handle)

	@property
	def start(self) -> int:
		return core.BNSectionGetStart(self.handle)

	@property
	def linked_section(self) -> str:
		return core.BNSectionGetLinkedSection(self.handle)

	@property
	def info_section(self) -> str:
		return core.BNSectionGetInfoSection(self.handle)

	@property
	def info_data(self) -> int:
		return core.BNSectionGetInfoData(self.handle)

	@property
	def align(self) -> int:
		return core.BNSectionGetAlign(self.handle)

	@property
	def entry_size(self) -> int:
		return core.BNSectionGetEntrySize(self.handle)

	@property
	def semantics(self) -> SectionSemantics:
		return SectionSemantics(core.BNSectionGetSemantics(self.handle))

	@property
	def auto_defined(self) -> bool:
		return core.BNSectionIsAutoDefined(self.handle)

	@property
	def end(self) -> int:
		return self.start + len(self)


class TagType:
	def __init__(self, handle:core.BNTagTypeHandle):
		self.handle = handle

	def __del__(self):
		if core is not None:
			core.BNFreeTagType(self.handle)

	def __repr__(self):
		return f"<tag type {self.name}: {self.icon}>"

	def __eq__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return ctypes.addressof(self.handle.contents) == ctypes.addressof(other.handle.contents)

	def __ne__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return not (self == other)

	def __hash__(self):
		return hash(ctypes.addressof(self.handle.contents))

	@property
	def id(self) -> str:
		"""Unique id of the TagType"""
		return core.BNTagTypeGetId(self.handle)

	@property
	def name(self) -> str:
		"""Name of the TagType"""
		return core.BNTagTypeGetName(self.handle)

	@name.setter
	def name(self, value:str) -> None:
		core.BNTagTypeSetName(self.handle, value)

	@property
	def icon(self) -> str:
		"""Unicode str containing an emoji to be used as an icon"""
		return core.BNTagTypeGetIcon(self.handle)

	@icon.setter
	def icon(self, value:str) -> None:
		core.BNTagTypeSetIcon(self.handle, value)

	@property
	def visible(self) -> bool:
		"""Boolean for whether the tags of this type are visible"""
		return core.BNTagTypeGetVisible(self.handle)

	@visible.setter
	def visible(self, value:bool) -> None:
		core.BNTagTypeSetVisible(self.handle, value)

	@property
	def type(self) -> TagTypeType:
		"""Type from enums.TagTypeType"""
		return core.BNTagTypeGetType(self.handle)

	@type.setter
	def type(self, value:TagTypeType) -> None:
		core.BNTagTypeSetType(self.handle, value)


class Tag:
	def __init__(self, handle:core.BNTagHandle):
		self.handle = handle

	def __del__(self):
		if core is not None:
			core.BNFreeTag(self.handle)

	def __repr__(self):
		return "<tag {self.type.icon} {self.type.name}: {self.data}>"

	def __eq__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return ctypes.addressof(self.handle.contents) == ctypes.addressof(other.handle.contents)

	def __ne__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return not (self == other)

	def __hash__(self):
		return hash(ctypes.addressof(self.handle.contents))

	@property
	def id(self) -> str:
		return core.BNTagGetId(self.handle)

	@property
	def type(self) -> TagType:
		core_tag_type = core.BNTagGetType(self.handle)
		assert core_tag_type is not None, "core.BNTagGetType returned None"
		return TagType(core_tag_type)

	@property
	def data(self) -> str:
		return core.BNTagGetData(self.handle)

	@data.setter
	def data(self, value:str) -> None:
		core.BNTagSetData(self.handle, value)


class _BinaryViewAssociatedDataStore(associateddatastore._AssociatedDataStore):
	_defaults = {}


class BinaryView:
	"""
	``class BinaryView`` implements a view on binary data, and presents a queryable interface of a binary file. One key
	job of BinaryView is file format parsing which allows Binary Ninja to read, write, insert, remove portions
	of the file given a virtual address. For the purposes of this documentation we define a virtual address as the
	memory address that the various pieces of the physical file will be loaded at.

	A binary file does not have to have just one BinaryView, thus much of the interface to manipulate disassembly exists
	within or is accessed through a BinaryView. All files are guaranteed to have at least the ``Raw`` BinaryView. The
	``Raw`` BinaryView is simply a hex editor, but is helpful for manipulating binary files via their absolute addresses.

	BinaryViews are plugins and thus registered with Binary Ninja at startup, and thus should **never** be instantiated
	directly as this is already done. The list of available BinaryViews can be seen in the BinaryViewType class which
	provides an iterator and map of the various installed BinaryViews::

		>>> list(BinaryViewType)
		[<view type: 'Raw'>, <view type: 'ELF'>, <view type: 'Mach-O'>, <view type: 'PE'>]
		>>> BinaryViewType['ELF']
		<view type: 'ELF'>

	To open a file with a given BinaryView the following code can be used::

		>>> bv = BinaryViewType.get_view_of_file("/bin/ls")
		>>> bv
		<BinaryView: '/bin/ls', start 0x100000000, len 0xa000>

	`By convention in the rest of this document we will use bv to mean an open BinaryView of an executable file.`
	When a BinaryView is open on an executable view, analysis does not automatically run, this can be done by running
	the :func:`update_analysis_and_wait` method which disassembles the executable and returns when all disassembly is
	finished::

		>>> bv.update_analysis_and_wait()
		>>>

	Since BinaryNinja's analysis is multi-threaded (depending on version) this can also be done in the background by
	using the :func:`update_analysis` method instead.

	By standard python convention methods which start with '_' should be considered private and should not be called
	externally. Additionally, methods which begin with ``perform_`` should not be called either and are
	used explicitly for subclassing the BinaryView.

	.. note:: An important note on the ``*_user_*()`` methods. Binary Ninja makes a distinction between edits \
	performed by the user and actions performed by auto analysis.  Auto analysis actions that can quickly be recalculated \
	are not saved to the database. Auto analysis actions that take a long time and all user edits are stored in the \
	database (e.g. :func:`remove_user_function` rather than :func:`remove_function`). Thus use ``_user_`` methods if saving \
	to the database is desired.
	"""
	name: Optional[str] = None
	long_name: Optional[str]  = None
	_registered = False
	_registered_cb = None
	registered_view_type = None
	_associated_data = {}
	_registered_instances = []
	def __init__(self, file_metadata:'filemetadata.FileMetadata'=None, parent_view:'BinaryView'=None, handle:core.BNBinaryViewHandle=None):
		if handle is not None:
			_handle = handle
			if file_metadata is None:
				self._file = filemetadata.FileMetadata(handle=core.BNGetFileForView(handle))
			else:
				self._file = file_metadata
		elif self.__class__ is BinaryView:
			binaryninja._init_plugins()
			if file_metadata is None:
				file_metadata = filemetadata.FileMetadata()
			_handle = core.BNCreateBinaryDataView(file_metadata.handle)
			self._file = filemetadata.FileMetadata(handle=core.BNNewFileReference(file_metadata.handle))
		else:
			binaryninja._init_plugins()
			if not self.__class__._registered:
				raise TypeError("view type not registered")
			self._cb = core.BNCustomBinaryView()
			self._cb.context = 0
			self._cb.init = self._cb.init.__class__(self._init)
			self._cb.externalRefTaken = self._cb.externalRefTaken.__class__(self._external_ref_taken)
			self._cb.externalRefReleased = self._cb.externalRefReleased.__class__(self._external_ref_released)
			self._cb.read = self._cb.read.__class__(self._read)
			self._cb.write = self._cb.write.__class__(self._write)
			self._cb.insert = self._cb.insert.__class__(self._insert)
			self._cb.remove = self._cb.remove.__class__(self._remove)
			self._cb.getModification = self._cb.getModification.__class__(self._get_modification)
			self._cb.isValidOffset = self._cb.isValidOffset.__class__(self._is_valid_offset)
			self._cb.isOffsetReadable = self._cb.isOffsetReadable.__class__(self._is_offset_readable)
			self._cb.isOffsetWritable = self._cb.isOffsetWritable.__class__(self._is_offset_writable)
			self._cb.isOffsetExecutable = self._cb.isOffsetExecutable.__class__(self._is_offset_executable)
			self._cb.getNextValidOffset = self._cb.getNextValidOffset.__class__(self._get_next_valid_offset)
			self._cb.getStart = self._cb.getStart.__class__(self._get_start)
			self._cb.getLength = self._cb.getLength.__class__(self._get_length)
			self._cb.getEntryPoint = self._cb.getEntryPoint.__class__(self._get_entry_point)
			self._cb.isExecutable = self._cb.isExecutable.__class__(self._is_executable)
			self._cb.getDefaultEndianness = self._cb.getDefaultEndianness.__class__(self._get_default_endianness)
			self._cb.isRelocatable = self._cb.isRelocatable.__class__(self._is_relocatable)
			self._cb.getAddressSize = self._cb.getAddressSize.__class__(self._get_address_size)
			self._cb.save = self._cb.save.__class__(self._save)
			if file_metadata is None:
				raise Exception("Attempting to create a BinaryView with FileMetadata which is None")
			self._file = file_metadata
			_parent_view = None
			if parent_view is not None:
				_parent_view = parent_view.handle
			_handle = core.BNCreateCustomBinaryView(self.__class__.name, file_metadata.handle, _parent_view, self._cb)

		assert _handle is not None
		self.handle = _handle
		self._notifications = {}
		self._parse_only = False

	def __enter__(self):
		return self

	def __exit__(self, type, value, traceback):
		self.file.close()

	def __del__(self):
		if core is None:
			return
		for i in self._notifications.values():
			i._unregister()
		core.BNFreeBinaryView(self.handle)

	def __repr__(self):
		start = self.start
		length = len(self)
		if start != 0:
			size = f"start {start:#x}, len {length:#x}"
		else:
			size = f"len {length:#x}"
		filename = self._file.filename
		if len(filename) > 0:
			return f"<BinaryView: '{filename}', {size}>"
		return f"<BinaryView: {size}>"

	def __len__(self):
		return int(core.BNGetViewLength(self.handle))

	def __eq__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return ctypes.addressof(self.handle.contents) == ctypes.addressof(other.handle.contents)

	def __ne__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return not (self == other)

	def __hash__(self):
		return hash(ctypes.addressof(self.handle.contents))

	def __iter__(self) -> Generator['_function.Function', None, None]:
		count = ctypes.c_ulonglong(0)
		funcs = core.BNGetAnalysisFunctionList(self.handle, count)
		assert funcs is not None, "core.BNGetAnalysisFunctionList returned None"
		try:
			for i in range(0, count.value):
				yield _function.Function(self, core.BNNewFunctionReference(funcs[i]))
		finally:
			core.BNFreeFunctionList(funcs, count.value)

	def __getitem__(self, i) -> bytes:
		if isinstance(i, tuple):
			result = bytes()
			for s in i:
				result += self.__getitem__(s)
			return result
		elif isinstance(i, slice):
			if i.step is not None:
				raise IndexError("step not implemented")
			i = i.indices(self.end)
			start = i[0]
			stop = i[1]
			if stop <= start:
				return b""
			return self.read(start, stop - start)
		elif i < 0:
			if i >= -len(self):
				value = self.read(int(len(self) + i), 1)
				if len(value) == 0:
					raise IndexError("index not readable")
				return value
			raise IndexError("index out of range")
		elif (i >= self.start) and (i < self.end):
			value = self.read(int(i), 1)
			if len(value) == 0:
				raise IndexError("index not readable")
			return value
		else:
			raise IndexError("index out of range")

	def __setitem__(self, i, value):
		if isinstance(i, slice):
			if i.step is not None:
				raise IndexError("step not supported on assignment")
			i = i.indices(self.end)
			start = i[0]
			stop = i[1]
			if stop < start:
				stop = start
			if len(value) != (stop - start):
				self.remove(start, stop - start)
				self.insert(start, value)
			else:
				self.write(start, value)
		elif i < 0:
			if i >= -len(self):
				if len(value) != 1:
					raise ValueError("expected single byte for assignment")
				if self.write(int(len(self) + i), value) != 1:
					raise IndexError("index not writable")
			else:
				raise IndexError("index out of range")
		elif (i >= self.start) and (i < self.end):
			if len(value) != 1:
				raise ValueError("expected single byte for assignment")
			if self.write(int(i), value) != 1:
				raise IndexError("index not writable")
		else:
			raise IndexError("index out of range")

	@classmethod
	def register(cls):
		binaryninja._init_plugins()
		if cls.name is None:
			raise ValueError("view 'name' not defined")
		if cls.long_name is None:
			cls.long_name = cls.name
		cls._registered_cb = core.BNCustomBinaryViewType()
		cls._registered_cb.context = 0
		cls._registered_cb.create = cls._registered_cb.create.__class__(cls._create)
		cls._registered_cb.parse = cls._registered_cb.parse.__class__(cls._parse)
		cls._registered_cb.isValidForData = cls._registered_cb.isValidForData.__class__(cls._is_valid_for_data)
		cls._registered_cb.getLoadSettingsForData = cls._registered_cb.getLoadSettingsForData.__class__(cls._get_load_settings_for_data)
		view_handle = core.BNRegisterBinaryViewType(cls.name, cls.long_name, cls._registered_cb)
		assert view_handle is not None, "core.BNRegisterBinaryViewType returned None"
		cls.registered_view_type = BinaryViewType(view_handle)
		cls._registered = True

	@property
	def parse_only(self) -> bool:
		return self._parse_only

	@parse_only.setter
	def parse_only(self, value:bool) -> None:
		self._parse_only = value

	@classmethod
	def _create(cls, ctxt, data:core.BNBinaryView):
		try:
			file_metadata = filemetadata.FileMetadata(handle=core.BNGetFileForView(data))
			view = cls(BinaryView(file_metadata=file_metadata, handle=core.BNNewViewReference(data))) # type: ignore
			if view is None:
				return None
			view.parse_only = False
			view_handle = core.BNNewViewReference(view.handle)
			assert view_handle is not None, "core.BNNewViewReference returned None"
			return ctypes.cast(view_handle, ctypes.c_void_p).value
		except:
			log.log_error(traceback.format_exc())
			return None

	@classmethod
	def _parse(cls, ctxt, data:core.BNBinaryView):
		try:
			file_metadata = filemetadata.FileMetadata(handle=core.BNGetFileForView(data))
			view = cls(BinaryView(file_metadata=file_metadata, handle=core.BNNewViewReference(data))) # type: ignore
			if view is None:
				return None
			view.parse_only = True
			view_handle = core.BNNewViewReference(view.handle)
			assert view_handle is not None, "core.BNNewViewReference returned None"
			return ctypes.cast(view_handle, ctypes.c_void_p).value
		except:
			log.log_error(traceback.format_exc())
			return None

	@classmethod
	def _is_valid_for_data(cls, ctxt, data):
		try:
			# I'm not sure whats going on here even so I've suppressed the linter warning
			return cls.is_valid_for_data(BinaryView(handle=core.BNNewViewReference(data))) # type: ignore
		except:
			log.log_error(traceback.format_exc())
			return False

	@classmethod
	def _get_load_settings_for_data(cls, ctxt, data):
		try:
			attr = getattr(cls, "get_load_settings_for_data", None)
			if callable(attr):
				result = cls.get_load_settings_for_data(BinaryView(handle=core.BNNewViewReference(data)))  # type: ignore
				settings_handle = core.BNNewSettingsReference(result.handle)
				assert settings_handle is not None, "core.BNNewSettingsReference returned None"
				return ctypes.cast(settings_handle, ctypes.c_void_p).value
			else:
				return None
		except:
			log.log_error(traceback.format_exc())
			return None

	@staticmethod
	def open(src, file_metadata=None) -> Optional['BinaryView']:
		binaryninja._init_plugins()
		if isinstance(src, fileaccessor.FileAccessor):
			if file_metadata is None:
				file_metadata = filemetadata.FileMetadata()
			view = core.BNCreateBinaryDataViewFromFile(file_metadata.handle, src._cb)
		else:
			if file_metadata is None:
				file_metadata = filemetadata.FileMetadata(str(src))
			view = core.BNCreateBinaryDataViewFromFilename(file_metadata.handle, str(src))
		if view is None:
			return None
		return BinaryView(file_metadata=file_metadata, handle=view)

	@staticmethod
	def new(data:bytes=None, file_metadata:Optional['filemetadata.FileMetadata']=None) -> Optional['BinaryView']:
		binaryninja._init_plugins()
		if file_metadata is None:
			file_metadata = filemetadata.FileMetadata()
		if data is None:
			view = core.BNCreateBinaryDataView(file_metadata.handle)
		else:
			buf = databuffer.DataBuffer(data)
			view = core.BNCreateBinaryDataViewFromBuffer(file_metadata.handle, buf.handle)
		if view is None:
			return None
		return BinaryView(file_metadata=file_metadata, handle=view)

	@classmethod
	def _unregister(cls, view:core.BNBinaryView) -> None:
		handle = ctypes.cast(view, ctypes.c_void_p)
		if handle.value in cls._associated_data:
			del cls._associated_data[handle.value]

	@staticmethod
	def set_default_session_data(name:str, value:str) -> None:
		"""
		``set_default_session_data`` saves a variable to the BinaryView.

		:param str name: name of the variable to be saved
		:param str value: value of the variable to be saved

		:Example:
			>>> BinaryView.set_default_session_data("variable_name", "value")
			>>> bv.session_data.variable_name
			'value'
		"""
		_BinaryViewAssociatedDataStore.set_default(name, value)

	@property
	def basic_blocks(self) -> Generator['basicblock.BasicBlock', None, None]:
		"""A generator of all BasicBlock objects in the BinaryView"""
		for func in self:
			for block in func.basic_blocks:
				yield block

	@property
	def llil_basic_blocks(self) -> 'lowlevelil.LLILBasicBlocksType':
		"""A generator of all LowLevelILBasicBlock objects in the BinaryView"""
		for func in self:
			for il_block in func.low_level_il.basic_blocks:
				yield il_block

	@property
	def mlil_basic_blocks(self) -> 'mediumlevelil.MLILBasicBlocksType':
		"""A generator of all MediumLevelILBasicBlock objects in the BinaryView"""
		for func in self:
			for il_block in func.mlil.basic_blocks:
				yield il_block

	@property
	def hlil_basic_blocks(self) -> 'highlevelil.HLILBasicBlocksType':
		"""A generator of all HighLevelILBasicBlock objects in the BinaryView"""
		for func in self:
			for il_block in func.hlil.basic_blocks:
				yield il_block

	@property
	def instructions(self) -> InstructionsType:
		"""A generator of instruction tokens and their start addresses"""
		for block in self.basic_blocks:
			start = block.start
			for i in block:
				yield (i[0], start)
				start += i[1]

	@property
	def llil_instructions(self) -> 'lowlevelil.LLILInstructionsType':
		"""A generator of llil instructions"""
		for block in self.llil_basic_blocks:
			for i in block:
				yield i

	@property
	def mlil_instructions(self) -> 'mediumlevelil.MLILInstructionsType':
		"""A generator of mlil instructions"""
		for block in self.mlil_basic_blocks:
			for i in block:
				yield i

	@property
	def hlil_instructions(self) -> 'highlevelil.HLILInstructionsType':
		"""A generator of hlil instructions"""
		for block in self.hlil_basic_blocks:
			for i in block:
				yield i

	@property
	def parent_view(self) -> Optional['BinaryView']:
		"""View that contains the raw data used by this view (read-only)"""
		result = core.BNGetParentView(self.handle)
		if result is None:
			return None
		return BinaryView(handle=result)

	@property
	def modified(self) -> bool:
		"""boolean modification state of the BinaryView (read/write)"""
		return self._file.modified

	@modified.setter
	def modified(self, value:bool) -> None:
		self._file.modified = value

	@property
	def analysis_changed(self) -> bool:
		"""boolean analysis state changed of the currently running analysis (read-only)"""
		return self._file.analysis_changed

	@property
	def has_database(self) -> bool:
		"""boolean has a database been written to disk (read-only)"""
		return self._file.has_database

	@property
	def view(self) -> str:
		return self._file.view

	@view.setter
	def view(self, value:str):
		self._file.view = value

	@property
	def offset(self) -> int:
		return self._file.offset

	@offset.setter
	def offset(self, value:int) -> None:
		self._file.offset = value

	@property
	def file(self) -> 'filemetadata.FileMetadata':
		""":py:class:`FileMetadata` backing the BinaryView """
		return self._file

	@property
	def start(self) -> int:
		"""Start offset of the binary (read-only)"""
		return core.BNGetStartOffset(self.handle)

	@property
	def end(self) -> int:
		"""End offset of the binary (read-only)"""
		return core.BNGetEndOffset(self.handle)

	@property
	def entry_point(self) -> int:
		"""Entry point of the binary (read-only)"""
		return core.BNGetEntryPoint(self.handle)

	@property
	def arch(self) -> Optional['architecture.Architecture']:
		"""The architecture associated with the current :py:class:`BinaryView` (read/write)"""
		arch = core.BNGetDefaultArchitecture(self.handle)
		if arch is None:
			return None
		return architecture.CoreArchitecture._from_cache(handle=arch)

	@arch.setter
	def arch(self, value:'architecture.Architecture') -> None:
		if value is None:
			core.BNSetDefaultArchitecture(self.handle, None)
		else:
			core.BNSetDefaultArchitecture(self.handle, value.handle)

	@property
	def platform(self) -> Optional['_platform.Platform']:
		"""The platform associated with the current BinaryView (read/write)"""
		plat = core.BNGetDefaultPlatform(self.handle)
		if plat is None:
			return None
		return _platform.Platform(self.arch, handle=plat)

	@platform.setter
	def platform(self, value:Optional['_platform.Platform']) -> None:
		if value is None:
			core.BNSetDefaultPlatform(self.handle, None)
		else:
			core.BNSetDefaultPlatform(self.handle, value.handle)

	@property
	def endianness(self) -> Endianness:
		"""Endianness of the binary (read-only)"""
		return Endianness(core.BNGetDefaultEndianness(self.handle))

	@property
	def relocatable(self) -> bool:
		"""Boolean - is the binary relocatable (read-only)"""
		return core.BNIsRelocatable(self.handle)

	@property
	def address_size(self) -> int:
		"""Address size of the binary (read-only)"""
		return core.BNGetViewAddressSize(self.handle)

	@property
	def executable(self) -> bool:
		"""Whether the binary is an executable (read-only)"""
		return core.BNIsExecutableView(self.handle)

	@property
	def functions(self) -> Generator['_function.Function', None, None]:
		"""List of functions (read-only)"""
		count = ctypes.c_ulonglong(0)
		funcs = core.BNGetAnalysisFunctionList(self.handle, count)
		assert funcs is not None, "core.BNGetAnalysisFunctionList returned None"
		try:
			for i in range(0, count.value):
				yield _function.Function(self, core.BNNewFunctionReference(funcs[i]))
		finally:
			core.BNFreeFunctionList(funcs, count.value)

	@property
	def has_functions(self) -> bool:
		"""Boolean whether the binary has functions (read-only)"""
		return core.BNHasFunctions(self.handle)

	@property
	def has_symbols(self) -> bool:
		"""Boolean whether the binary has symbols (read-only)"""
		return core.BNHasSymbols(self.handle)

	@property
	def has_data_variables(self) -> bool:
		"""Boolean whether the binary has data variables (read-only)"""
		return core.BNHasDataVariables(self.handle)

	@property
	def entry_function(self) -> Optional['_function.Function']:
		"""Entry function (read-only)"""
		func = core.BNGetAnalysisEntryPoint(self.handle)
		if func is None:
			return None
		return _function.Function(self, func)

	@property
	def symbols(self) -> Mapping[str, List['_types.Symbol']]:
		"""
		Deprecated: Dict of symbols (read-only)
		This API is deprecated and will be removed in future versions.

		.. warning:: This method **should not** be used in any applications where speed is important as it \
			copies all symbols from the binaryview each time it is invoked.
		"""

		count = ctypes.c_ulonglong(0)
		syms = core.BNGetSymbols(self.handle, count, None)
		assert syms is not None, "core.BNGetSymbols returned None"
		result = defaultdict(list)
		for i in range(0, count.value):
			sym = _types.Symbol(None, None, None, handle=core.BNNewSymbolReference(syms[i]))
			result[sym.raw_name].append(sym)
		core.BNFreeSymbolList(syms, count.value)
		return result

	@staticmethod
	def internal_namespace() -> '_types.NameSpace':
		"""Internal namespace for the current BinaryView"""
		ns = core.BNGetInternalNameSpace()
		result = _types.NameSpace._from_core_struct(ns)
		core.BNFreeNameSpace(ns)
		return result

	@staticmethod
	def external_namespace() -> '_types.NameSpace':
		"""External namespace for the current BinaryView"""
		ns = core.BNGetExternalNameSpace()
		result = _types.NameSpace._from_core_struct(ns)
		core.BNFreeNameSpace(ns)
		return result

	@property
	def namespaces(self) -> List['_types.NameSpace']:
		"""Returns a list of namespaces for the current BinaryView"""
		count = ctypes.c_ulonglong(0)
		nameSpaceList = core.BNGetNameSpaces(self.handle, count)
		assert nameSpaceList is not None, "core.BNGetNameSpaces returned None"
		result = []
		for i in range(count.value):
			result.append(_types.NameSpace._from_core_struct(nameSpaceList[i]))
		core.BNFreeNameSpaceList(nameSpaceList, count.value)
		return result

	@property
	def view_type(self) -> str:
		"""View type (read-only)"""
		return core.BNGetViewType(self.handle)

	@property
	def available_view_types(self) -> List[BinaryViewType]:
		"""Available view types (read-only)"""
		count = ctypes.c_ulonglong(0)
		types = core.BNGetBinaryViewTypesForData(self.handle, count)
		result = []
		if types is None:
			return result
		for i in range(0, count.value):
			result.append(BinaryViewType(types[i]))
		core.BNFreeBinaryViewTypeList(types)
		return result

	@property
	def strings(self) -> List[str]:
		"""List of strings (read-only)"""
		return self.get_strings()

	@property
	def saved(self) -> bool:
		"""boolean state of whether or not the file has been saved (read/write)"""
		return self._file.saved

	@saved.setter
	def saved(self, value:bool) -> None:
		self._file.saved = value

	@property
	def analysis_info(self) -> AnalysisInfo:
		"""Provides instantaneous analysis state information and a list of current functions under analysis (read-only).
		All times are given in units of milliseconds (ms). Per-function `analysis_time` is the aggregation of time spent
		performing incremental updates and is reset on a full function update. Per-function `update_count` tracks the
		current number of incremental updates and is reset on a full function update. Per-function `submit_count` tracks the
		current number of full updates that have completed.

		.. note:: `submit_count` is currently not reset across analysis updates.

		"""
		info_ref = core.BNGetAnalysisInfo(self.handle)
		assert info_ref is not None, "core.BNGetAnalysisInfo returned None"
		info = info_ref[0]
		active_info_list:List[ActiveAnalysisInfo] = []
		for i in range(0, info.count):
			func = _function.Function(self, core.BNNewFunctionReference(info.activeInfo[i].func))
			active_info = ActiveAnalysisInfo(func, info.activeInfo[i].analysisTime, info.activeInfo[i].updateCount, info.activeInfo[i].submitCount)
			active_info_list.append(active_info)
		result = AnalysisInfo(info.state, info.analysisTime, active_info_list)
		core.BNFreeAnalysisInfo(info_ref)
		return result

	@property
	def analysis_progress(self) -> AnalysisProgress:
		"""Status of current analysis (read-only)"""
		result = core.BNGetAnalysisProgress(self.handle)
		return AnalysisProgress(result.state, result.count, result.total)

	@property
	def linear_disassembly(self):
		"""Iterator for all lines in the linear disassembly of the view"""
		return self.get_linear_disassembly(None)

	@property
	def data_vars(self):
		"""List of data variables (read-only)"""
		count = ctypes.c_ulonglong(0)
		var_list = core.BNGetDataVariables(self.handle, count)
		assert var_list is not None, "core.BNGetDataVariables returned None"
		result = {}
		for i in range(0, count.value):
			result[var_list[i].address] = DataVariable.from_core_struct(var_list[i], self)
		core.BNFreeDataVariables(var_list, count.value)
		return result

	@property
	def types(self) -> Mapping['_types.QualifiedName', '_types.Type']:
		"""Mapping of type names and types (read-only)"""
		count = ctypes.c_ulonglong(0)
		type_list = core.BNGetAnalysisTypeList(self.handle, count)
		assert type_list is not None, "core.BNGetAnalysisTypeList returned None"
		result = {}
		for i in range(0, count.value):
			name = _types.QualifiedName._from_core_struct(type_list[i].name)
			result[name] = _types.Type.create(core.BNNewTypeReference(type_list[i].type), platform = self.platform)
		core.BNFreeTypeList(type_list, count.value)
		return result

	@property
	def type_names(self) -> List['_types.Type']:
		"""List of defined type names (read-only)"""
		count = ctypes.c_ulonglong(0)
		name_list = core.BNGetAnalysisTypeNames(self.handle, count, "")
		assert name_list is not None, "core.BNGetAnalysisTypeNames returned None"
		result = []
		for i in range(0, count.value):
			result.append(_types.QualifiedName._from_core_struct(name_list[i]))
		core.BNFreeTypeNameList(name_list, count.value)
		return result


	@property
	def type_libraries(self):
		"""List of imported type libraries (read-only)"""
		count = ctypes.c_ulonglong(0)
		libraries = core.BNGetBinaryViewTypeLibraries(self.handle, count)
		assert libraries is not None, "core.BNGetBinaryViewTypeLibraries returned None"
		result = []
		for i in range(0, count.value):
			result.append(typelibrary.TypeLibrary(core.BNNewTypeLibraryReference(libraries[i])))
		core.BNFreeTypeLibraryList(libraries, count.value)
		return result


	@property
	def segments(self):
		"""List of segments (read-only)"""
		count = ctypes.c_ulonglong(0)
		segment_list = core.BNGetSegments(self.handle, count)
		assert segment_list is not None, "core.BNGetSegments returned None"
		result = []
		for i in range(0, count.value):
			segment_handle = core.BNNewSegmentReference(segment_list[i])
			assert segment_handle is not None, "core.BNNewSegmentReference returned None"
			result.append(Segment(segment_handle))
		core.BNFreeSegmentList(segment_list, count.value)
		return result

	@property
	def sections(self):
		"""Dictionary of sections (read-only)"""
		count = ctypes.c_ulonglong(0)
		section_list = core.BNGetSections(self.handle, count)
		assert section_list is not None, "core.BNGetSections returned None"
		result = {}
		for i in range(0, count.value):
			section_handle = core.BNNewSectionReference(section_list[i])
			assert section_handle is not None, "core.BNNewSectionReference returned None"
			result[core.BNSectionGetName(section_list[i])] = Section(section_handle)
		core.BNFreeSectionList(section_list, count.value)
		return result

	@property
	def allocated_ranges(self):
		"""List of valid address ranges for this view (read-only)"""
		count = ctypes.c_ulonglong(0)
		range_list = core.BNGetAllocatedRanges(self.handle, count)
		assert range_list is not None, "core.BNGetAllocatedRanges returned None"
		result = []
		for i in range(0, count.value):
			result.append(variable.AddressRange(range_list[i].start, range_list[i].end))
		core.BNFreeAddressRanges(range_list)
		return result

	@property
	def session_data(self):
		"""Dictionary object where plugins can store arbitrary data associated with the view"""
		# TODO:
		# assert isinstance(self.handle, int), "session_data called with no BinaryView.handle"
		handle = ctypes.cast(self.handle, ctypes.c_void_p)  # type: ignore
		if handle.value not in BinaryView._associated_data:
			obj = _BinaryViewAssociatedDataStore()
			BinaryView._associated_data[handle.value] = obj
			return obj
		else:
			return BinaryView._associated_data[handle.value]

	@property
	def global_pointer_value(self):
		"""Discovered value of the global pointer register, if the binary uses one (read-only)"""
		result = core.BNGetGlobalPointerValue(self.handle)
		return variable.RegisterValue.from_BNRegisterValue(result, self.arch)

	@property
	def parameters_for_analysis(self):
		return core.BNGetParametersForAnalysis(self.handle)

	@parameters_for_analysis.setter
	def parameters_for_analysis(self, params):
		core.BNSetParametersForAnalysis(self.handle, params)

	@property
	def max_function_size_for_analysis(self):
		"""Maximum size of function (sum of basic block sizes in bytes) for auto analysis"""
		return core.BNGetMaxFunctionSizeForAnalysis(self.handle)

	@max_function_size_for_analysis.setter
	def max_function_size_for_analysis(self, size):
		core.BNSetMaxFunctionSizeForAnalysis(self.handle, size)

	@property
	def relocation_ranges(self):
		"""List of relocation range tuples (read-only)"""
		count = ctypes.c_ulonglong()
		ranges = core.BNGetRelocationRanges(self.handle, count)
		assert ranges is not None, "core.BNGetRelocationRanges returned None"
		result = []
		for i in range(0, count.value):
			result.append((ranges[i].start, ranges[i].end))
		core.BNFreeRelocationRanges(ranges, count)
		return result

	def relocation_ranges_at(self, addr):
		"""List of relocation range tuples for a given address"""

		count = ctypes.c_ulonglong()
		ranges = core.BNGetRelocationRangesAtAddress(self.handle, addr, count)
		assert ranges is not None, "core.BNGetRelocationRangesAtAddress returned None"
		result = []
		for i in range(0, count.value):
			result.append((ranges[i].start, ranges[i].end))
		core.BNFreeRelocationRanges(ranges, count)
		return result

	@property
	def new_auto_function_analysis_suppressed(self):
		"""Whether or not automatically discovered functions will be analyzed"""
		return core.BNGetNewAutoFunctionAnalysisSuppressed(self.handle)

	@new_auto_function_analysis_suppressed.setter
	def new_auto_function_analysis_suppressed(self, suppress):
		core.BNSetNewAutoFunctionAnalysisSuppressed(self.handle, suppress)

	def _init(self, ctxt):
		try:
			return self.init()
		except:
			log.log_error(traceback.format_exc())
			return False

	def _external_ref_taken(self, ctxt):
		try:
			self.__class__._registered_instances.append(self)
		except:
			log.log_error(traceback.format_exc())

	def _external_ref_released(self, ctxt):
		try:
			self.__class__._registered_instances.remove(self)
		except:
			log.log_error(traceback.format_exc())

	def _read(self, ctxt, dest, offset, length):
		try:
			data = self.perform_read(offset, length)
			if data is None:
				return 0
			if len(data) > length:
				data = data[0:length]
			ctypes.memmove(dest, data, len(data))
			return len(data)
		except:
			log.log_error(traceback.format_exc())
			return 0

	def _write(self, ctxt, offset, src, length):
		try:
			data = ctypes.create_string_buffer(length)
			ctypes.memmove(data, src, length)
			return self.perform_write(offset, data.raw)
		except:
			log.log_error(traceback.format_exc())
			return 0

	def _insert(self, ctxt, offset, src, length):
		try:
			data = ctypes.create_string_buffer(length)
			ctypes.memmove(data, src, length)
			return self.perform_insert(offset, data.raw)
		except:
			log.log_error(traceback.format_exc())
			return 0

	def _remove(self, ctxt, offset, length):
		try:
			return self.perform_remove(offset, length)
		except:
			log.log_error(traceback.format_exc())
			return 0

	def _get_modification(self, ctxt, offset):
		try:
			return self.perform_get_modification(offset)
		except:
			log.log_error(traceback.format_exc())
			return ModificationStatus.Original

	def _is_valid_offset(self, ctxt, offset):
		try:
			return self.perform_is_valid_offset(offset)
		except:
			log.log_error(traceback.format_exc())
			return False

	def _is_offset_readable(self, ctxt, offset):
		try:
			return self.perform_is_offset_readable(offset)
		except:
			log.log_error(traceback.format_exc())
			return False

	def _is_offset_writable(self, ctxt, offset):
		try:
			return self.perform_is_offset_writable(offset)
		except:
			log.log_error(traceback.format_exc())
			return False

	def _is_offset_executable(self, ctxt, offset):
		try:
			return self.perform_is_offset_executable(offset)
		except:
			log.log_error(traceback.format_exc())
			return False

	def _get_next_valid_offset(self, ctxt, offset):
		try:
			return self.perform_get_next_valid_offset(offset)
		except:
			log.log_error(traceback.format_exc())
			return offset

	def _get_start(self, ctxt):
		try:
			return self.perform_get_start()
		except:
			log.log_error(traceback.format_exc())
			return 0

	def _get_length(self, ctxt):
		try:
			return self.perform_get_length()
		except:
			log.log_error(traceback.format_exc())
			return 0

	def _get_entry_point(self, ctxt):
		try:
			return self.perform_get_entry_point()
		except:
			log.log_error(traceback.format_exc())
			return 0

	def _is_executable(self, ctxt):
		try:
			return self.perform_is_executable()
		except:
			log.log_error(traceback.format_exc())
			return False

	def _get_default_endianness(self, ctxt):
		try:
			return self.perform_get_default_endianness()
		except:
			log.log_error(traceback.format_exc())
			return Endianness.LittleEndian

	def _is_relocatable(self, ctxt):
		try:
			return self.perform_is_relocatable()
		except:
			log.log_error(traceback.format_exc())
			return False

	def _get_address_size(self, ctxt):
		try:
			return self.perform_get_address_size()
		except:
			log.log_error(traceback.format_exc())
			return 8

	def _save(self, ctxt, file_accessor):
		try:
			return self.perform_save(fileaccessor.CoreFileAccessor(file_accessor))
		except:
			log.log_error(traceback.format_exc())
			return False

	def init(self):
		return True

	def disassembly_tokens(self, addr:int, arch:Optional['architecture.Architecture']=None) -> Generator[Tuple[List['_function.InstructionTextToken'], int], None, None]:
		if arch is None:
			if self.arch is None:
				raise Exception("Can not call method disassembly with no Architecture specified")
			arch = self.arch

		size = 1
		while size != 0:
			tokens, size = arch.get_instruction_text(self.read(addr, arch.max_instr_length), addr)
			addr += size
			if size == 0 or tokens is None:
				break
			yield (tokens, size)

	def disassembly_text(self, addr:int, arch:Optional['architecture.Architecture']=None) -> Generator[Tuple[str, int], None, None]:
		"""
		``disassembly_text`` helper function for getting disassembly of a given address

		:param int addr: virtual address of instruction
		:param Architecture arch: optional Architecture, ``self.arch`` is used if this parameter is None
		:return: a str representation of the instruction at virtual address ``addr`` or None
		:rtype: str or None
		:Example:

			>>> next(bv.disassembly_text(bv.entry_point))
			'push    ebp', 1
			>>>
		"""
		if arch is None:
			if self.arch is None:
				raise Exception("Can not call method disassembly with no Architecture specified")
			arch = self.arch

		size = 1
		while size != 0:
			tokens, size = arch.get_instruction_text(self.read(addr, arch.max_instr_length), addr)
			addr += size
			if size == 0 or tokens is None:
				break
			yield (''.join(str(a) for a in tokens).strip(), size)

	def perform_save(self, accessor):
		if self.parent_view is not None:
			return self.parent_view.save(accessor)
		return False

	@abc.abstractmethod
	def perform_get_address_size(self):
		raise NotImplementedError

	def perform_get_length(self):
		"""
		``perform_get_length`` implements a query for the size of the virtual address range used by
		the BinaryView.

		.. note:: This method **may** be overridden by custom BinaryViews. Use :func:`add_auto_segment` to provide \
		data without overriding this method.

		.. warning:: This method **must not** be called directly.

		:return: returns the size of the virtual address range used by the BinaryView.
		:rtype: int
		"""
		return 0

	def perform_read(self, addr:int, length:int) -> bytes:
		"""
		``perform_read`` implements a mapping between a virtual address and an absolute file offset, reading
		``length`` bytes from the rebased address ``addr``.

		.. note:: This method **may** be overridden by custom BinaryViews. Use :func:`add_auto_segment` to provide \
		data without overriding this method.

		.. warning:: This method **must not** be called directly.

		:param int addr: a virtual address to attempt to read from
		:param int length: the number of bytes to be read
		:return: length bytes read from addr, should return empty string on error
		:rtype: bytes
		"""
		return b""

	def perform_write(self, addr, data):
		"""
		``perform_write`` implements a mapping between a virtual address and an absolute file offset, writing
		the bytes ``data`` to rebased address ``addr``.

		.. note:: This method **may** be overridden by custom BinaryViews. Use :func:`add_auto_segment` to provide \
		data without overriding this method.

		.. warning:: This method **must not** be called directly.

		:param int addr: a virtual address
		:param str data: the data to be written
		:return: length of data written, should return 0 on error
		:rtype: int
		"""
		return 0

	def perform_insert(self, addr, data):
		"""
		``perform_insert`` implements a mapping between a virtual address and an absolute file offset, inserting
		the bytes ``data`` to rebased address ``addr``.

		.. note:: This method **may** be overridden by custom BinaryViews. If not overridden, inserting is disallowed

		.. warning:: This method **must not** be called directly.

		:param int addr: a virtual address
		:param str data: the data to be inserted
		:return: length of data inserted, should return 0 on error
		:rtype: int
		"""
		return 0

	def perform_remove(self, addr, length):
		"""
		``perform_remove`` implements a mapping between a virtual address and an absolute file offset, removing
		``length`` bytes from the rebased address ``addr``.

		.. note:: This method **may** be overridden by custom BinaryViews. If not overridden, removing data is disallowed

		.. warning:: This method **must not** be called directly.

		:param int addr: a virtual address
		:param str data: the data to be removed
		:return: length of data removed, should return 0 on error
		:rtype: int
		"""
		return 0

	def perform_get_modification(self, addr):
		"""
		``perform_get_modification`` implements query to the whether the virtual address ``addr`` is modified.

		.. note:: This method **may** be overridden by custom BinaryViews. Use :func:`add_auto_segment` to provide \
		data without overriding this method.

		.. warning:: This method **must not** be called directly.

		:param int addr: a virtual address to be checked
		:return: One of the following: Original = 0, Changed = 1, Inserted = 2
		:rtype: ModificationStatus
		"""
		return ModificationStatus.Original

	def perform_is_valid_offset(self, addr):
		"""
		``perform_is_valid_offset`` implements a check if an virtual address ``addr`` is valid.

		.. note:: This method **may** be overridden by custom BinaryViews. Use :func:`add_auto_segment` to provide \
		data without overriding this method.

		.. warning:: This method **must not** be called directly.

		:param int addr: a virtual address to be checked
		:return: true if the virtual address is valid, false if the virtual address is invalid or error
		:rtype: bool
		"""
		data = self.read(addr, 1)
		return (data is not None) and (len(data) == 1)

	def perform_is_offset_readable(self, offset):
		"""
		``perform_is_offset_readable`` implements a check if an virtual address is readable.

		.. note:: This method **may** be overridden by custom BinaryViews. Use :func:`add_auto_segment` to provide \
		data without overriding this method.

		.. warning:: This method **must not** be called directly.

		:param int offset: a virtual address to be checked
		:return: true if the virtual address is readable, false if the virtual address is not readable or error
		:rtype: bool
		"""
		return self.is_valid_offset(offset)

	def perform_is_offset_writable(self, addr):
		"""
		``perform_is_offset_writable`` implements a check if a virtual address ``addr`` is writable.

		.. note:: This method **may** be overridden by custom BinaryViews. Use :func:`add_auto_segment` to provide \
		data without overriding this method.

		.. warning:: This method **must not** be called directly.

		:param int addr: a virtual address to be checked
		:return: true if the virtual address is writable, false if the virtual address is not writable or error
		:rtype: bool
		"""
		return self.is_valid_offset(addr)

	def perform_is_offset_executable(self, addr):
		"""
		``perform_is_offset_executable`` implements a check if a virtual address ``addr`` is executable.

		.. note:: This method **may** be overridden by custom BinaryViews. Use :func:`add_auto_segment` to provide \
		data without overriding this method.

		.. warning:: This method **must not** be called directly.

		:param int addr: a virtual address to be checked
		:return: true if the virtual address is executable, false if the virtual address is not executable or error
		:rtype: int
		"""
		return self.is_valid_offset(addr)

	def perform_get_next_valid_offset(self, addr):
		"""
		``perform_get_next_valid_offset`` implements a query for the next valid readable, writable, or executable virtual
		memory address.

		.. note:: This method **may** be overridden by custom BinaryViews. Use :func:`add_auto_segment` to provide \
		data without overriding this method.

		.. warning:: This method **must not** be called directly.

		:param int addr: a virtual address to start checking from.
		:return: the next readable, writable, or executable virtual memory address
		:rtype: int
		"""
		if addr < self.perform_get_start():
			return self.perform_get_start()
		return addr

	def perform_get_start(self):
		"""
		``perform_get_start`` implements a query for the first readable, writable, or executable virtual address in
		the BinaryView.

		.. note:: This method **may** be overridden by custom BinaryViews. Use :func:`add_auto_segment` to provide \
		data without overriding this method.

		.. warning:: This method **must not** be called directly.

		:return: returns the first virtual address in the BinaryView.
		:rtype: int
		"""
		return 0

	def perform_get_entry_point(self):
		"""
		``perform_get_entry_point`` implements a query for the initial entry point for code execution.

		.. note:: This method **should** be implemented for custom BinaryViews that are executable.

		.. warning:: This method **must not** be called directly.

		:return: the virtual address of the entry point
		:rtype: int
		"""
		return 0

	def perform_is_executable(self):
		"""
		``perform_is_executable`` implements a check which returns true if the BinaryView is executable.

		.. note:: This method **must** be implemented for custom BinaryViews that are executable.

		.. warning:: This method **must not** be called directly.

		:return: true if the current BinaryView is executable, false if it is not executable or on error
		:rtype: bool
		"""
		return False

	def perform_get_default_endianness(self):
		"""
		``perform_get_default_endianness`` implements a check which returns true if the BinaryView is executable.

		.. note:: This method **may** be implemented for custom BinaryViews that are not LittleEndian.

		.. warning:: This method **must not** be called directly.

		:return: either :const:`Endianness.LittleEndian <binaryninja.enums.Endianness.LittleEndian>` or :const:`Endianness.BigEndian <binaryninja.enums.Endianness.BigEndian>`
		:rtype: Endianness
		"""
		return Endianness.LittleEndian

	def perform_is_relocatable(self):
		"""
		``perform_is_relocatable`` implements a check which returns true if the BinaryView is relocatable. Defaults to False

		.. note:: This method **may** be implemented for custom BinaryViews that are relocatable.

		.. warning:: This method **must not** be called directly.

		:return: True if the BinaryView is relocatable, False otherwise
		:rtype: boolean
		"""
		return False

	def create_database(self, filename, progress_func=None, settings=None):
		"""
		``create_database`` writes the current database (.bndb) out to the specified file.

		:param str filename: path and filename to write the bndb to, this string `should` have ".bndb" appended to it.
		:param callback progress_func: optional function to be called with the current progress and total count.
		:param SaveSettings settings: optional argument for special save options.
		:return: true on success, false on failure
		:rtype: bool
		:Example:
			>>> settings = SaveSettings()
			>>> bv.create_database(f"{bv.file.filename}.bndb", None, settings)
			True
		"""
		return self._file.create_database(filename, progress_func, settings)

	def save_auto_snapshot(self, progress_func=None, settings=None):
		"""
		``save_auto_snapshot`` saves the current database to the already created file.

		.. note:: :py:meth:`create_database` should have been called prior to executing this method

		:param callback progress_func: optional function to be called with the current progress and total count.
		:param SaveSettings settings: optional argument for special save options.
		:return: True if it successfully saved the snapshot, False otherwise
		:rtype: bool
		"""
		return self._file.save_auto_snapshot(progress_func, settings)

	def get_view_of_type(self, name):
		"""
		``get_view_of_type`` returns the BinaryView associated with the provided name if it exists.

		:param str name: Name of the view to be retrieved
		:return: BinaryView object associated with the provided name or None on failure
		:rtype: BinaryView or None
		"""
		return self._file.get_view_of_type(name)

	def begin_undo_actions(self):
		"""
		``begin_undo_actions`` start recording actions taken so the can be undone at some point.

		:rtype: None
		:Example:

			>>> bv.get_disassembly(0x100012f1)
			'xor     eax, eax'
			>>> bv.begin_undo_actions()
			>>> bv.convert_to_nop(0x100012f1)
			True
			>>> bv.commit_undo_actions()
			>>> bv.get_disassembly(0x100012f1)
			'nop'
			>>> bv.undo()
			>>> bv.get_disassembly(0x100012f1)
			'xor     eax, eax'
			>>>
		"""
		self._file.begin_undo_actions()

	def commit_undo_actions(self):
		"""
		``commit_undo_actions`` commit the actions taken since the last commit to the undo database.

		:rtype: None
		:Example:

			>>> bv.get_disassembly(0x100012f1)
			'xor     eax, eax'
			>>> bv.begin_undo_actions()
			>>> bv.convert_to_nop(0x100012f1)
			True
			>>> bv.commit_undo_actions()
			>>> bv.get_disassembly(0x100012f1)
			'nop'
			>>> bv.undo()
			>>> bv.get_disassembly(0x100012f1)
			'xor     eax, eax'
			>>>
		"""
		self._file.commit_undo_actions()

	def undo(self):
		"""
		``undo`` undo the last committed action in the undo database.

		:rtype: None
		:Example:

			>>> bv.get_disassembly(0x100012f1)
			'xor     eax, eax'
			>>> bv.begin_undo_actions()
			>>> bv.convert_to_nop(0x100012f1)
			True
			>>> bv.commit_undo_actions()
			>>> bv.get_disassembly(0x100012f1)
			'nop'
			>>> bv.undo()
			>>> bv.get_disassembly(0x100012f1)
			'xor     eax, eax'
			>>> bv.redo()
			>>> bv.get_disassembly(0x100012f1)
			'nop'
			>>>
		"""
		self._file.undo()

	def redo(self):
		"""
		``redo`` redo the last committed action in the undo database.

		:rtype: None
		:Example:

			>>> bv.get_disassembly(0x100012f1)
			'xor     eax, eax'
			>>> bv.begin_undo_actions()
			>>> bv.convert_to_nop(0x100012f1)
			True
			>>> bv.commit_undo_actions()
			>>> bv.get_disassembly(0x100012f1)
			'nop'
			>>> bv.undo()
			>>> bv.get_disassembly(0x100012f1)
			'xor     eax, eax'
			>>> bv.redo()
			>>> bv.get_disassembly(0x100012f1)
			'nop'
			>>>
		"""
		self._file.redo()

	def navigate(self, view, offset):
		"""
		``navigate`` navigates the UI to the specified virtual address

		.. note:: Despite the confusing name, ``view`` in this context is not a BinaryView but rather a string describing the different UI Views.  Check :py:attr:`view` while in different views to see examples such as ``Linear:ELF``, ``Graph:PE``.

		:param str view: virtual address to read from.
		:param int offset: address to navigate to
		:return: whether or not navigation succeeded
		:rtype: bool
		:Example:

			>>> import random
			>>> bv.navigate(bv.view, random.choice(list(bv.functions)).start)
			True
		"""
		return self._file.navigate(view, offset)

	def read(self, addr:int, length:int) -> bytes:
		"""
		``read`` returns the data reads at most ``length`` bytes from virtual address ``addr``.

		.. note:: Python2 returns a str, but Python3 returns a bytes object.  str(DataBufferObject) will \
 		still get you a str in either case.

		:param int addr: virtual address to read from.
		:param int length: number of bytes to read.
		:return: at most ``length`` bytes from the virtual address ``addr``, empty string on error or no data.
		:rtype: bytes
		:Example:

			>>> #Opening a x86_64 Mach-O binary
			>>> bv = BinaryViewType['Raw'].open("/bin/ls") #note that we are using open instead of get_view_of_file to get the raw view
			>>> bv.read(0,4)
			b\'\\xcf\\xfa\\xed\\xfe\'
		"""
		if (addr < 0) or (length < 0):
			raise ValueError("length and address must both be positive")
		buf = databuffer.DataBuffer(handle=core.BNReadViewBuffer(self.handle, addr, length))
		return bytes(buf)

	def read_int(self, address:int, size:int, sign:bool=True, endian:Optional[Endianness]=None) -> int:
		_endian = self.endianness
		if endian is not None:
			_endian = endian
		data = self.read(address, size)
		if len(data) != size:
			raise ValueError(f"Couldn't read {size} bytes from address: {address:#x}")
		return StructuredDataValue.int_from_bytes(data, size, sign, _endian)

	def read_pointer(self, address:int, size=None) -> int:
		_size = size
		if size is None:
			if self.arch is None:
				raise ValueError("Can't read pointer for BinaryView without an architecture")
			_size = self.arch.address_size
		return self.read_int(address, _size, False, self.endianness)

	def write(self, addr:int, data:bytes) -> int:
		"""
		``write`` writes the bytes in ``data`` to the virtual address ``addr``.

		:param int addr: virtual address to write to.
		:param bytes data: data to be written at addr.
		:return: number of bytes written to virtual address ``addr``
		:rtype: int
		:Example:

			>>> bv.read(0,4)
			b'BBBB'
			>>> bv.write(0, b"AAAA")
			4
			>>> bv.read(0,4)
			b'AAAA'
		"""
		if not (isinstance(data, bytes) or isinstance(data, bytearray) or isinstance(data, str)):
			raise TypeError("Must be bytes, bytearray, or str")
		else:
			buf = databuffer.DataBuffer(data)
		return core.BNWriteViewBuffer(self.handle, addr, buf.handle)

	def insert(self, addr:int, data:bytes) -> int:
		"""
		``insert`` inserts the bytes in ``data`` to the virtual address ``addr``.

		:param int addr: virtual address to write to.
		:param bytes data: data to be inserted at addr.
		:return: number of bytes inserted to virtual address ``addr``
		:rtype: int
		:Example:

			>>> bv.insert(0,"BBBB")
			4
			>>> bv.read(0,8)
			'BBBBAAAA'
		"""
		if not (isinstance(data, bytes) or isinstance(data, bytearray) or isinstance(data, str)):
			raise TypeError("Must be bytes, bytearray, or str")
		else:
			buf = databuffer.DataBuffer(data)
		return core.BNInsertViewBuffer(self.handle, addr, buf.handle)

	def remove(self, addr:int, length:int) -> int:
		"""
		``remove`` removes at most ``length`` bytes from virtual address ``addr``.

		:param int addr: virtual address to remove from.
		:param int length: number of bytes to remove.
		:return: number of bytes removed from virtual address ``addr``
		:rtype: int
		:Example:

			>>> bv.read(0,8)
			'BBBBAAAA'
			>>> bv.remove(0,4)
			4
			>>> bv.read(0,4)
			'AAAA'
		"""
		return core.BNRemoveViewData(self.handle, addr, length)

	def get_entropy(self, addr:int, length:int, block_size:int=0) -> List[float]:
		"""
		``get_entropy`` returns the shannon entropy given the start ``addr``, ``length`` in bytes, and optionally in
		``block_size`` chunks.

		:param int addr: virtual address
		:param int length: total length in bytes
		:param int block_size: optional block size
		:return: list of entropy values for each chunk
		:rtype: list(float)
		"""
		result = []
		if length == 0:
			return result
		if block_size == 0:
			block_size = length
		data = (ctypes.c_float * ((length // block_size) + 1))()
		length = core.BNGetEntropy(self.handle, addr, length, block_size, data)

		for i in range(0, length):
			result.append(float(data[i]))
		return result

	def get_modification(self, addr, length=None):
		"""
		``get_modification`` returns the modified bytes of up to ``length`` bytes from virtual address ``addr``, or if
		``length`` is None returns the ModificationStatus.

		:param int addr: virtual address to get modification from
		:param int length: optional length of modification
		:return: Either ModificationStatus of the byte at ``addr``, or string of modified bytes at ``addr``
		:rtype: ModificationStatus or str
		"""
		if length is None:
			return ModificationStatus(core.BNGetModification(self.handle, addr))
		data = (ModificationStatus * length)()
		length = core.BNGetModificationArray(self.handle, addr, data, length)
		return data[0:length]

	def is_valid_offset(self, addr):
		"""
		``is_valid_offset`` checks if an virtual address ``addr`` is valid .

		:param int addr: a virtual address to be checked
		:return: true if the virtual address is valid, false if the virtual address is invalid or error
		:rtype: bool
		"""
		return core.BNIsValidOffset(self.handle, addr)

	def is_offset_readable(self, addr):
		"""
		``is_offset_readable`` checks if an virtual address ``addr`` is valid for reading.

		:param int addr: a virtual address to be checked
		:return: true if the virtual address is valid for reading, false if the virtual address is invalid or error
		:rtype: bool
		"""
		return core.BNIsOffsetReadable(self.handle, addr)

	def is_offset_writable(self, addr):
		"""
		``is_offset_writable`` checks if an virtual address ``addr`` is valid for writing.

		:param int addr: a virtual address to be checked
		:return: true if the virtual address is valid for writing, false if the virtual address is invalid or error
		:rtype: bool
		"""
		return core.BNIsOffsetWritable(self.handle, addr)

	def is_offset_executable(self, addr):
		"""
		``is_offset_executable`` checks if an virtual address ``addr`` is valid for executing.

		:param int addr: a virtual address to be checked
		:return: true if the virtual address is valid for executing, false if the virtual address is invalid or error
		:rtype: bool
		"""
		return core.BNIsOffsetExecutable(self.handle, addr)

	def is_offset_code_semantics(self, addr):
		"""
		``is_offset_code_semantics`` checks if an virtual address ``addr`` is semantically valid for code.

		:param int addr: a virtual address to be checked
		:return: true if the virtual address is valid for code semantics, false if the virtual address is invalid or error
		:rtype: bool
		"""
		return core.BNIsOffsetCodeSemantics(self.handle, addr)

	def is_offset_extern_semantics(self, addr):
		"""
		``is_offset_extern_semantics`` checks if an virtual address ``addr`` is semantically valid for external references.

		:param int addr: a virtual address to be checked
		:return: true if the virtual address is valid for for external references, false if the virtual address is invalid or error
		:rtype: bool
		"""
		return core.BNIsOffsetExternSemantics(self.handle, addr)

	def is_offset_writable_semantics(self, addr):
		"""
		``is_offset_writable_semantics`` checks if an virtual address ``addr`` is semantically writable. Some sections
		may have writable permissions for linking purposes but can be treated as read-only for the purposes of
		analysis.

		:param int addr: a virtual address to be checked
		:return: true if the virtual address is valid for writing, false if the virtual address is invalid or error
		:rtype: bool
		"""
		return core.BNIsOffsetWritableSemantics(self.handle, addr)

	def save(self, dest):
		"""
		``save`` saves the original binary file to the provided destination ``dest`` along with any modifications.

		:param str dest: destination path and filename of file to be written
		:return: boolean True on success, False on failure
		:rtype: bool
		"""
		if isinstance(dest, fileaccessor.FileAccessor):
			return core.BNSaveToFile(self.handle, dest._cb)
		return core.BNSaveToFilename(self.handle, str(dest))

	def register_notification(self, notify:BinaryDataNotification):
		"""
		`register_notification` provides a mechanism for receiving callbacks for various analysis events. A full
		list of callbacks can be seen in :py:Class:`BinaryDataNotification`.

		:param BinaryDataNotification notify: notify is a subclassed instance of :py:Class:`BinaryDataNotification`.
		:rtype: None
		"""
		cb = BinaryDataNotificationCallbacks(self, notify)
		cb._register()
		self._notifications[notify] = cb

	def unregister_notification(self, notify:BinaryDataNotification):
		"""
		`unregister_notification` unregisters the :py:Class:`BinaryDataNotification` object passed to
		`register_notification`

		:param BinaryDataNotification notify: notify is a subclassed instance of :py:Class:`BinaryDataNotification`.
		:rtype: None
		"""
		if notify in self._notifications:
			self._notifications[notify]._unregister()
			del self._notifications[notify]

	def add_function(self, addr, plat=None):
		"""
		``add_function`` add a new function of the given ``plat`` at the virtual address ``addr``

		:param int addr: virtual address of the function to be added
		:param Platform plat: Platform for the function to be added
		:rtype: None
		:Example:

			>>> bv.add_function(1)
			>>> bv.functions
			[<func: x86_64@0x1>]

		"""
		if self.platform is None and plat is None:
			raise Exception("Default platform not set in BinaryView")
		if plat is None:
			plat = self.platform
		if not isinstance(plat, _platform.Platform):
			raise AttributeError("Provided platform is not of type `Platform`")
		core.BNAddFunctionForAnalysis(self.handle, plat.handle, addr)

	def add_entry_point(self, addr, plat=None):
		"""
		``add_entry_point`` adds an virtual address to start analysis from for a given plat.

		:param int addr: virtual address to start analysis from
		:param Platform plat: Platform for the entry point analysis
		:rtype: None
		:Example:
			>>> bv.add_entry_point(0xdeadbeef)
			>>>
		"""
		if self.platform is None and plat is None:
			raise Exception("Default platform not set in BinaryView")
		if plat is None:
			plat = self.platform
		if not isinstance(plat, _platform.Platform):
			raise AttributeError("Provided platform is not of type `Platform`")
		core.BNAddEntryPointForAnalysis(self.handle, plat.handle, addr)

	def remove_function(self, func):
		"""
		``remove_function`` removes the function ``func`` from the list of functions

		.. warning:: This method should only be used when the function that is removed is expected to re-appear after any other analysis executes that could re-add it. Most users will want to use :func:`remove_user_function` in their scripts.

		:param Function func: a Function object.
		:rtype: None
		:Example:

			>>> bv.functions
			[<func: x86_64@0x1>]
			>>> bv.remove_function(next(bv.functions))
			>>> bv.functions
			[]
		"""
		core.BNRemoveAnalysisFunction(self.handle, func.handle)

	def create_user_function(self, addr, plat=None):
		"""
		``create_user_function`` add a new *user* function of the given ``plat`` at the virtual address ``addr``

		:param int addr: virtual address of the *user* function to be added
		:param Platform plat: Platform for the function to be added
		:rtype: None
		:Example:

			>>> bv.create_user_function(1)
			>>> bv.functions
			[<func: x86_64@0x1>]

		"""
		if plat is None:
			if self.platform is None:
				raise Exception("Attempting to call create_user_function with no specified platform")
			plat = self.platform
		return _function.Function(self, core.BNCreateUserFunction(self.handle, plat.handle, addr))

	def remove_user_function(self, func):
		"""
		``remove_user_function`` removes the function ``func`` from the list of functions as a user action.

		.. note:: This API will prevent the function from being re-created if any analysis later triggers that would re-add it, unlike :func:`remove_function`.

		:param Function func: a Function object.
		:rtype: None
		:Example:

			>>> bv.functions
			[<func: x86_64@0x1>]
			>>> bv.remove_user_function(next(bv.functions))
			>>> bv.functions
			[]
		"""
		core.BNRemoveUserFunction(self.handle, func.handle)

	def add_analysis_option(self, name):
		"""
		``add_analysis_option`` adds an analysis option. Analysis options elaborate the analysis phase. The user must
		start analysis by calling either :func:`update_analysis` or :func:`update_analysis_and_wait`.

		:param str name: name of the analysis option. Available options are: "linearsweep", and "signaturematcher".

		:rtype: None
		:Example:

			>>> bv.add_analysis_option("linearsweep")
			>>> bv.update_analysis_and_wait()
		"""
		core.BNAddAnalysisOption(self.handle, name)

	def has_initial_analysis(self):
		"""
		``has_initial_analysis`` check for the presence of an initial analysis in this BinaryView.

		:return: True if the BinaryView has a valid initial analysis, False otherwise
		:rtype: bool
		"""
		return core.BNHasInitialAnalysis(self.handle)

	def set_analysis_hold(self, enable):
		"""
		``set_analysis_hold`` control the analysis hold for this BinaryView. Enabling analysis hold defers all future
		analysis updates, therefore causing :func:`update_analysis` or :func:`update_analysis_and_wait` to take no action.

		:rtype: None
		"""
		core.BNSetAnalysisHold(self.handle, enable)

	def update_analysis(self):
		"""
		``update_analysis`` asynchronously starts the analysis running and returns immediately. Analysis of BinaryViews
		does not occur automatically, the user must start analysis by calling either :func:`update_analysis` or
		:func:`update_analysis_and_wait`. An analysis update **must** be run after changes are made which could change
		analysis results such as adding functions.

		:rtype: None
		"""
		core.BNUpdateAnalysis(self.handle)

	def update_analysis_and_wait(self):
		"""
		``update_analysis_and_wait`` blocking call to update the analysis, this call returns when the analysis is
		complete.  Analysis of BinaryViews does not occur automatically, the user must start analysis by calling either
		:func:`update_analysis` or :func:`update_analysis_and_wait`. An analysis update **must** be run after changes are
		made which could change analysis results such as adding functions.

		:rtype: None
		"""
		core.BNUpdateAnalysisAndWait(self.handle)

	def abort_analysis(self):
		"""
		``abort_analysis`` will abort the currently running analysis.

		:rtype: None
		"""
		core.BNAbortAnalysis(self.handle)

	def define_data_var(self, addr, var_type):
		"""
		``define_data_var`` defines a non-user data variable ``var_type`` at the virtual address ``addr``.

		:param int addr: virtual address to define the given data variable
		:param Type var_type: type to be defined at the given virtual address
		:rtype: None
		:Example:

			>>> t = bv.parse_type_string("int foo")
			>>> t
			(<type: int32_t>, 'foo')
			>>> bv.define_data_var(bv.entry_point, t[0])
			>>>
		"""
		tc = var_type.to_core_struct()
		core.BNDefineDataVariable(self.handle, addr, tc)

	def define_user_data_var(self, addr, var_type):
		"""
		``define_user_data_var`` defines a user data variable ``var_type`` at the virtual address ``addr``.

		:param int addr: virtual address to define the given data variable
		:param binaryninja.Type var_type: type to be defined at the given virtual address
		:rtype: None
		:Example:

			>>> t = bv.parse_type_string("int foo")
			>>> t
			(<type: int32_t>, 'foo')
			>>> bv.define_user_data_var(bv.entry_point, t[0])
			>>>
		"""
		tc = var_type.to_core_struct()
		core.BNDefineUserDataVariable(self.handle, addr, tc)

	def undefine_data_var(self, addr):
		"""
		``undefine_data_var`` removes the non-user data variable at the virtual address ``addr``.

		:param int addr: virtual address to define the data variable to be removed
		:rtype: None
		:Example:

			>>> bv.undefine_data_var(bv.entry_point)
			>>>
		"""
		core.BNUndefineDataVariable(self.handle, addr)

	def undefine_user_data_var(self, addr):
		"""
		``undefine_user_data_var`` removes the user data variable at the virtual address ``addr``.

		:param int addr: virtual address to define the data variable to be removed
		:rtype: None
		:Example:

			>>> bv.undefine_user_data_var(bv.entry_point)
			>>>
		"""
		core.BNUndefineUserDataVariable(self.handle, addr)

	def get_data_var_at(self, addr):
		"""
		``get_data_var_at`` returns the data type at a given virtual address.

		:param int addr: virtual address to get the data type from
		:return: returns the DataVariable at the given virtual address, None on error.
		:rtype: DataVariable
		:Example:

			>>> t = bv.parse_type_string("int foo")
			>>> bv.define_data_var(bv.entry_point, t[0])
			>>> bv.get_data_var_at(bv.entry_point)
			<var 0x100001174: int32_t>

		"""
		var = core.BNDataVariable()
		if not core.BNGetDataVariableAtAddress(self.handle, addr, var):
			return None
		return DataVariable.from_core_struct(var, self)

	def get_functions_containing(self, addr, plat=None):
		"""
		``get_functions_containing`` returns a list of functions which contain the given address.

		:param int addr: virtual address to query.
		:rtype: list of Function objects
		"""
		count = ctypes.c_ulonglong(0)
		funcs = core.BNGetAnalysisFunctionsContainingAddress(self.handle, addr, count)
		assert funcs is not None, "core.BNGetAnalysisFunctionsContainingAddress returned None"
		result = []
		for i in range(0, count.value):
			result.append(_function.Function(self, core.BNNewFunctionReference(funcs[i])))
		core.BNFreeFunctionList(funcs, count.value)
		if plat is not None:
			result = [func for func in result if func.platform == plat]
		return result

	def get_functions_by_name(self, name, plat=None, ordered_filter=None):
		"""``get_functions_by_name`` returns a list of Function objects
		function with a Symbol of ``name``.

		:param str name: name of the functions
		:param Platform plat: (optional) platform
		:param list(SymbolType) ordered_filter: (optional) an ordered filter based on SymbolType
		:return: returns a list of Function objects or an empty list
		:rtype: list(Function)
		:Example:

			>>> bv.get_function_by_name("main"))
			<func: x86_64@0x1587>
			>>>
		"""
		if ordered_filter is not None:
			ordered_filter = [SymbolType.FunctionSymbol,
				SymbolType.ImportedFunctionSymbol,
				SymbolType.LibraryFunctionSymbol]
		if plat == None:
			plat = self.platform
		syms = self.get_symbols_by_name(name, ordered_filter=ordered_filter)
		fns = []
		for sym in syms:
			for fn in self.get_functions_at(sym.address):
				if fn.platform == plat:
					fns.append(fn)
		return fns

	def get_function_at(self, addr, plat=None):
		"""
		``get_function_at`` gets a Function object for the function that starts at virtual address ``addr``:

		:param int addr: starting virtual address of the desired function
		:param Platform plat: plat of the desired function
		:return: returns a Function object or None for the function at the virtual address provided
		:rtype: Function
		:Example:

			>>> bv.get_function_at(bv.entry_point)
			<func: x86_64@0x100001174>
			>>>
		"""
		if plat is None:
			plat = self.platform
		if plat is None:
			return None
		func = core.BNGetAnalysisFunction(self.handle, plat.handle, addr)
		if func is None:
			return None
		return _function.Function(self, func)

	def get_functions_at(self, addr):
		"""

		``get_functions_at`` get a list of binaryninja.Function objects (one for each valid platform) that start at the
		given virtual address. Binary Ninja does not limit the number of platforms in a given file thus there may be
		multiple functions defined from different architectures at the same location. This API allows you to query all
		of valid platforms.

		You may also be interested in :func:`get_functions_containing` which is useful for requesting all function
		that contain a given address

		:param int addr: virtual address of the desired Function object list.
		:return: a list of binaryninja.Function objects defined at the provided virtual address
		:rtype: list(Function)
		"""
		count = ctypes.c_ulonglong(0)
		funcs = core.BNGetAnalysisFunctionsForAddress(self.handle, addr, count)
		assert funcs is not None, "core.BNGetAnalysisFunctionsForAddress returned None"
		result = []
		for i in range(0, count.value):
			result.append(_function.Function(self, core.BNNewFunctionReference(funcs[i])))
		core.BNFreeFunctionList(funcs, count.value)
		return result

	def get_recent_function_at(self, addr):
		func = core.BNGetRecentAnalysisFunctionForAddress(self.handle, addr)
		if func is None:
			return None
		return _function.Function(self, func)

	def get_basic_blocks_at(self, addr):
		"""
		``get_basic_blocks_at`` get a list of :py:Class:`BasicBlock` objects which exist at the provided virtual address.

		:param int addr: virtual address of BasicBlock desired
		:return: a list of :py:Class:`BasicBlock` objects
		:rtype: list(BasicBlock)
		"""
		count = ctypes.c_ulonglong(0)
		blocks = core.BNGetBasicBlocksForAddress(self.handle, addr, count)
		assert blocks is not None, "core.BNGetBasicBlocksForAddress returned None"
		result = []
		for i in range(0, count.value):
			block_handle = core.BNNewBasicBlockReference(blocks[i])
			assert block_handle is not None, "core.BNNewBasicBlockReference is None"
			result.append(basicblock.BasicBlock(block_handle, self))
		core.BNFreeBasicBlockList(blocks, count.value)
		return result

	def get_basic_blocks_starting_at(self, addr):
		"""
		``get_basic_blocks_starting_at`` get a list of :py:Class:`BasicBlock` objects which start at the provided virtual address.

		:param int addr: virtual address of BasicBlock desired
		:return: a list of :py:Class:`BasicBlock` objects
		:rtype: list(BasicBlock)
		"""
		count = ctypes.c_ulonglong(0)
		blocks = core.BNGetBasicBlocksStartingAtAddress(self.handle, addr, count)
		assert blocks is not None, "core.BNGetBasicBlocksStartingAtAddress returned None"
		result = []
		for i in range(0, count.value):
			block_handle = core.BNNewBasicBlockReference(blocks[i])
			assert block_handle is not None, "core.BNNewBasicBlockReference returned None"
			result.append(basicblock.BasicBlock(block_handle, self))
		core.BNFreeBasicBlockList(blocks, count.value)
		return result

	def get_recent_basic_block_at(self, addr):
		block = core.BNGetRecentBasicBlockForAddress(self.handle, addr)
		if block is None:
			return None
		return basicblock.BasicBlock(block, self)

	def get_code_refs(self, addr:int, length:int=None) -> Generator['ReferenceSource', None, None]:
		"""
		``get_code_refs`` returns a Generator[ReferenceSource] objects (xrefs or cross-references) that point to the provided virtual address.
		This function returns both autoanalysis ("auto") and user-specified ("user") xrefs.
		To add a user-specified reference, see :func:`~Function.add_user_code_ref`.

		:param int addr: virtual address to query for references
		:param int length: optional length of query
		:return: Generator[References] for the given virtual address
		:rtype: Generator[ReferenceSource, None, None]
		:Example:

			>>> bv.get_code_refs(here)
			[<ref: x86@0x4165ff>]
			>>>

		"""
		count = ctypes.c_ulonglong(0)
		if length is None:
			refs = core.BNGetCodeReferences(self.handle, addr, count)
			assert refs is not None, "core.BNGetCodeReferences returned None"
		else:
			refs = core.BNGetCodeReferencesInRange(self.handle, addr, length, count)
			assert refs is not None, "core.BNGetCodeReferencesInRange returned None"

		try:
			for i in range(0, count.value):
				if refs[i].func:
					func = _function.Function(self, core.BNNewFunctionReference(refs[i].func))
				else:
					func = None
				if refs[i].arch:
					arch = architecture.CoreArchitecture._from_cache(refs[i].arch)
				else:
					arch = None

				yield ReferenceSource(func, arch, refs[i].addr)
		finally:
			core.BNFreeCodeReferences(refs, count.value)

	def get_code_refs_from(self, addr, func=None, arch=None, length=None):
		"""
		``get_code_refs_from`` returns a list of virtual addresses referenced by code in the function ``func``,
		of the architecture ``arch``, and at the address ``addr``. If no function is specified, references from
		all functions and containing the address will be returned. If no architecture is specified, the
		architecture of the function will be used.
		This function returns both autoanalysis ("auto") and user-specified ("user") xrefs.
		To add a user-specified reference, see :func:`~Function.add_user_code_ref`.

		:param int addr: virtual address to query for references
		:param int length: optional length of query
		:param Architecture arch: optional architecture of query
		:return: list of integers
		:rtype: list(integer)
		"""

		result = []
		funcs = self.get_functions_containing(addr) if func is None else [func]
		if not funcs:
			return []
		for src_func in funcs:
			src_arch = src_func.arch if arch is None else arch
			ref_src = core.BNReferenceSource(src_func.handle, src_arch.handle, addr)
			count = ctypes.c_ulonglong(0)
			if length is None:
				refs = core.BNGetCodeReferencesFrom(self.handle, ref_src, count)
				assert refs is not None, "core.BNGetCodeReferencesFrom returned None"
			else:
				refs = core.BNGetCodeReferencesFromInRange(self.handle, ref_src, length, count)
				assert refs is not None, "core.BNGetCodeReferencesFromInRange returned None"
			for i in range(0, count.value):
				result.append(refs[i])
			core.BNFreeAddressList(refs)
		return result

	def get_data_refs(self, addr:int, length:int=None) -> Generator[int, None, None]:
		"""
		``get_data_refs`` returns a list of virtual addresses of data which references ``addr``. Optionally specifying
		a length. When ``length`` is set ``get_data_refs`` returns the data which references in the range ``addr``-``addr``+``length``.
		This function returns both autoanalysis ("auto") and user-specified ("user") xrefs. To add a user-specified
		reference, see :func:`add_user_data_ref`.

		:param int addr: virtual address to query for references
		:param int length: optional length of query
		:return: list of integers
		:rtype: list(integer)
		:Example:

			>>> bv.get_data_refs(here)
			[4203812]
			>>>
		"""
		count = ctypes.c_ulonglong(0)
		if length is None:
			refs = core.BNGetDataReferences(self.handle, addr, count)
			assert refs is not None, "core.BNGetDataReferences returned None"
		else:
			refs = core.BNGetDataReferencesInRange(self.handle, addr, length, count)
			assert refs is not None, "core.BNGetDataReferencesInRange returned None"

		try:
			for i in range(0, count.value):
				yield refs[i]
		finally:
			core.BNFreeDataReferences(refs, count.value)

	def get_data_refs_from(self, addr:int, length:int=None) -> Generator[int, None, None]:
		"""
		``get_data_refs_from`` returns a list of virtual addresses referenced by the address ``addr``. Optionally specifying
		a length. When ``length`` is set ``get_data_refs_from`` returns the data referenced in the range ``addr``-``addr``+``length``.
		This function returns both autoanalysis ("auto") and user-specified ("user") xrefs. To add a user-specified
		reference, see :func:`add_user_data_ref`.

		:param int addr: virtual address to query for references
		:param int length: optional length of query
		:return: list of integers
		:rtype: list(integer)
		:Example:

			>>> bv.get_data_refs_from(here)
			[4200327]
			>>>
		"""
		count = ctypes.c_ulonglong(0)
		if length is None:
			refs = core.BNGetDataReferencesFrom(self.handle, addr, count)
			assert refs is not None, "core.BNGetDataReferencesFrom returned None"
		else:
			refs = core.BNGetDataReferencesFromInRange(self.handle, addr, length, count)
			assert refs is not None, "core.BNGetDataReferencesFromInRange returned None"

		try:
			for i in range(0, count.value):
				yield refs[i]
		finally:
			core.BNFreeDataReferences(refs, count.value)

	def get_code_refs_for_type(self, name:str) -> Generator[ReferenceSource, None, None]:
		"""
		``get_code_refs_for_type`` returns a Generator[ReferenceSource] objects (xrefs or cross-references) that reference the provided QualifiedName.

		:param QualifiedName name: name of type to query for references
		:return: List of References for the given type
		:rtype: list(ReferenceSource)
		:Example:

			>>> bv.get_code_refs_for_type('A')
			[<ref: x86@0x4165ff>]
			>>>

		"""
		count = ctypes.c_ulonglong(0)
		_name = _types.QualifiedName(name)._get_core_struct()
		refs = core.BNGetCodeReferencesForType(self.handle, _name, count)
		assert refs is not None, "core.BNGetCodeReferencesForType returned None"

		try:
			for i in range(0, count.value):
				if refs[i].func:
					func = _function.Function(self, core.BNNewFunctionReference(refs[i].func))
				else:
					func = None
				if refs[i].arch:
					arch = architecture.CoreArchitecture._from_cache(refs[i].arch)
				else:
					arch = None

				yield ReferenceSource(func, arch, refs[i].addr)
		finally:
			core.BNFreeCodeReferences(refs, count.value)


	def get_code_refs_for_type_field(self, name:str, offset:int) -> Generator['_types.TypeFieldReference', None, None]:
		"""
		``get_code_refs_for_type`` returns a Generator[TypeFieldReference] objects (xrefs or cross-references) that reference the provided type field.

		:param QualifiedName name: name of type to query for references
		:param int offset: offset of the field, relative to the type
		:return: Generator of References for the given type
		:rtype: Generator[TypeFieldReference]
		:Example:

			>>> bv.get_code_refs_for_type_field('A', 0x8)
			[<ref: x86@0x4165ff>]
			>>>

		"""
		count = ctypes.c_ulonglong(0)
		_name = _types.QualifiedName(name)._get_core_struct()
		refs = core.BNGetCodeReferencesForTypeField(self.handle, _name, offset, count)
		assert refs is not None, "core.BNGetCodeReferencesForTypeField returned None"

		try:
			for i in range(0, count.value):
				if refs[i].func:
					func = _function.Function(self, core.BNNewFunctionReference(refs[i].func))
				else:
					func = None
				if refs[i].arch:
					arch = architecture.CoreArchitecture._from_cache(refs[i].arch)
				else:
					arch = None
				addr = refs[i].addr
				size = refs[i].size
				typeObj = None
				if refs[i].incomingType.type:
					typeObj = types.Type(core.BNNewTypeReference(refs[i].incomingType.type),\
							confidence = refs[i].incomingType.confidence)
				yield TypeFieldReference(func, arch, addr, size, typeObj)
		finally:
			core.BNFreeTypeFieldReferences(refs, count.value)

	def get_data_refs_for_type(self, name:str) -> Generator[int, None, None]:
		"""
		``get_data_refs_for_type`` returns a list of virtual addresses of data which references the type ``name``.
		Note, the returned addresses are the actual start of the queried type. For example, suppose there is a DataVariable
		at 0x1000 that has type A, and type A contains type B at offset 0x10. Then `get_data_refs_for_type('B')` will
		return 0x1010 for it.

		:param QualifiedName name: name of type to query for references
		:return: list of integers
		:rtype: list(integer)
		:Example:

			>>> bv.get_data_refs_for_type('A')
			[4203812]
			>>>
		"""
		count = ctypes.c_ulonglong(0)
		_name = _types.QualifiedName(name)._get_core_struct()
		refs = core.BNGetDataReferencesForType(self.handle, _name, count)
		assert refs is not None, "core.BNGetDataReferencesForType returned None"

		try:
			for i in range(0, count.value):
				yield refs[i]
		finally:
			core.BNFreeDataReferences(refs, count.value)


	def get_data_refs_for_type_field(self, name, offset):
		"""
		``get_data_refs_for_type_field`` returns a list of virtual addresses of data which references the type ``name``.
		Note, the returned addresses are the actual start of the queried type field. For example, suppose there is a
		DataVariable at 0x1000 that has type A, and type A contains type B at offset 0x10.
		Then `get_data_refs_for_type_field('B', 0x8)` will return 0x1018 for it.

		:param QualifiedName name: name of type to query for references
		:param int offset: offset of the field, relative to the type
		:return: list of integers
		:rtype: list(integer)
		:Example:

			>>> bv.get_data_refs_for_type_field('A', 0x8)
			[4203812]
			>>>
		"""
		count = ctypes.c_ulonglong(0)
		name = _types.QualifiedName(name)._get_core_struct()
		refs = core.BNGetDataReferencesForTypeField(self.handle, name, offset, count)
		assert refs is not None, "core.BNGetDataReferencesForTypeField returned None"

		result = []
		for i in range(0, count.value):
			result.append(refs[i])
		core.BNFreeDataReferences(refs, count.value)
		return result


	def get_type_refs_for_type(self, name):
		"""
		``get_type_refs_for_type`` returns a list of TypeReferenceSource objects (xrefs or cross-references) that reference the provided QualifiedName.

		:param QualifiedName name: name of type to query for references
		:return: List of references for the given type
		:rtype: list(TypeReferenceSource)
		:Example:

			>>> bv.get_type_refs_for_type('A')
			['<type D, offset 0x8, direct>', '<type C, offset 0x10, indirect>']
			>>>

		"""
		count = ctypes.c_ulonglong(0)
		name = _types.QualifiedName(name)._get_core_struct()
		refs = core.BNGetTypeReferencesForType(self.handle, name, count)
		assert refs is not None, "core.BNGetTypeReferencesForType returned None"

		result = []
		for i in range(0, count.value):
			type_field = _types.TypeReferenceSource(_types.QualifiedName._from_core_struct(refs[i].name), refs[i].offset, refs[i].type)
			result.append(type_field)
		core.BNFreeTypeReferences(refs, count.value)
		return result


	def get_type_refs_for_type_field(self, name, offset):
		"""
		``get_type_refs_for_type`` returns a list of TypeReferenceSource objects (xrefs or cross-references) that reference the provided type field.

		:param QualifiedName name: name of type to query for references
		:param int offset: offset of the field, relative to the type
		:return: List of references for the given type
		:rtype: list(TypeReferenceSource)
		:Example:

			>>> bv.get_type_refs_for_type_field('A', 0x8)
			['<type D, offset 0x8, direct>', '<type C, offset 0x10, indirect>']
			>>>

		"""
		count = ctypes.c_ulonglong(0)
		name = _types.QualifiedName(name)._get_core_struct()
		refs = core.BNGetTypeReferencesForTypeField(self.handle, name, offset, count)
		assert refs is not None, "core.BNGetTypeReferencesForTypeField returned None"

		result = []
		for i in range(0, count.value):
			type_field = _types.TypeReferenceSource(_types.QualifiedName._from_core_struct(refs[i].name), refs[i].offset, refs[i].type)
			result.append(type_field)
		core.BNFreeTypeReferences(refs, count.value)
		return result

	def get_code_refs_for_type_from(self, addr, func = None, arch = None, length = None):
		"""
		``get_code_refs_for_type_from`` returns a list of types referenced by code in the function ``func``,
		of the architecture ``arch``, and at the address ``addr``. If no function is specified, references from
		all functions and containing the address will be returned. If no architecture is specified, the
		architecture of the function will be used.

		:param int addr: virtual address to query for references
		:param int length: optional length of query
		:return: list of references
		:rtype: list(TypeReferenceSource)
		"""
		result = []
		funcs = self.get_functions_containing(addr) if func is None else [func]
		if not funcs:
			return []
		for src_func in funcs:
			src_arch = src_func.arch if arch is None else arch
			ref_src = core.BNReferenceSource(src_func.handle, src_arch.handle, addr)
			count = ctypes.c_ulonglong(0)
			if length is None:
				refs = core.BNGetCodeReferencesForTypeFrom(self.handle, ref_src, count)
				assert refs is not None, "core.BNGetCodeReferencesForTypeFrom returned None"
			else:
				refs = core.BNGetCodeReferencesForTypeFromInRange(self.handle, ref_src, length, count)
				assert refs is not None, "core.BNGetCodeReferencesForTypeFromInRange returned None"
			for i in range(0, count.value):
				type_field = _types.TypeReferenceSource(_types.QualifiedName._from_core_struct(refs[i].name), refs[i].offset, refs[i].type)
				result.append(type_field)
			core.BNFreeTypeReferences(refs, count.value)
		return result

	def get_code_refs_for_type_fields_from(self, addr, func = None, arch = None, length = None):
		"""
		``get_code_refs_for_type_fields_from`` returns a list of type fields referenced by code in the function ``func``,
		of the architecture ``arch``, and at the address ``addr``. If no function is specified, references from
		all functions and containing the address will be returned. If no architecture is specified, the
		architecture of the function will be used.

		:param int addr: virtual address to query for references
		:param int length: optional length of query
		:return: list of references
		:rtype: list(TypeReferenceSource)
		"""
		result = []
		funcs = self.get_functions_containing(addr) if func is None else [func]
		if not funcs:
			return []
		for src_func in funcs:
			src_arch = src_func.arch if arch is None else arch
			ref_src = core.BNReferenceSource(src_func.handle, src_arch.handle, addr)
			count = ctypes.c_ulonglong(0)
			if length is None:
				refs = core.BNGetCodeReferencesForTypeFieldsFrom(self.handle, ref_src, count)
				assert refs is not None, "core.BNGetCodeReferencesForTypeFieldsFrom returned None"
			else:
				refs = core.BNGetCodeReferencesForTypeFieldsFromInRange(self.handle, ref_src, length, count)
				assert refs is not None, "core.BNGetCodeReferencesForTypeFieldsFromInRange returned None"
			for i in range(0, count.value):
				type_field = _types.TypeReferenceSource(_types.QualifiedName._from_core_struct(refs[i].name), refs[i].offset, refs[i].type)
				result.append(type_field)
			core.BNFreeTypeReferences(refs, count.value)
		return result

	def add_user_data_ref(self, from_addr, to_addr):
		"""
		``add_user_data_ref`` adds a user-specified data cross-reference (xref) from the address ``from_addr`` to the address ``to_addr``.
		If the reference already exists, no action is performed. To remove the reference, use :func:`remove_user_data_ref`.

		:param int from_addr: the reference's source virtual address.
		:param int to_addr: the reference's destination virtual address.
		:rtype: None
		"""
		core.BNAddUserDataReference(self.handle, from_addr, to_addr)


	def remove_user_data_ref(self, from_addr, to_addr):
		"""
		``remove_user_data_ref`` removes a user-specified data cross-reference (xref) from the address ``from_addr`` to the address ``to_addr``.
		This function will only remove user-specified references, not ones generated during autoanalysis.
		If the reference does not exist, no action is performed.

		:param int from_addr: the reference's source virtual address.
		:param int to_addr: the reference's destination virtual address.
		:rtype: None
		"""
		core.BNRemoveUserDataReference(self.handle, from_addr, to_addr)


	def get_all_fields_referenced(self, name):
		"""
		``get_all_fields_referenced`` returns a list of offsets in the QualifiedName
		specified by name, which are referenced by code.

		:param QualifiedName name: name of type to query for references
		:return: List of offsets
		:rtype: list(integer)
		:Example:

			>>> bv.get_all_fields_referenced('A')
			[0, 8, 16, 24, 32, 40]
			>>>

		"""
		count = ctypes.c_ulonglong(0)
		name = _types.QualifiedName(name)._get_core_struct()
		refs = core.BNGetAllFieldsReferenced(self.handle, name, count)
		assert refs is not None, "core.BNGetAllFieldsReferenced returned None"

		result = []
		for i in range(0, count.value):
			result.append(refs[i])

		core.BNFreeDataReferences(refs, count.value)
		return result

	def get_all_sizes_referenced(self, name):
		"""
		``get_all_sizes_referenced`` returns a map from field offset to a list of sizes of
		the accesses to it.

		:param QualifiedName name: name of type to query for references
		:return: A map from field offset to the	size of the code accesses to it
		:rtype: map
		:Example:

			>>> bv.get_all_sizes_referenced('B')
			{0: [1, 8], 8: [8], 16: [1, 8]}
			>>>

		"""
		count = ctypes.c_ulonglong(0)
		name = _types.QualifiedName(name)._get_core_struct()
		refs = core.BNGetAllSizesReferenced(self.handle, name, count)
		assert refs is not None, "core.BNGetAllSizesReferenced returned None"

		result = {}
		for i in range(0, count.value):
			result[refs[i].offset] = []
			for j in range(0, refs[i].count):
				result[refs[i].offset].append(refs[i].sizes[j])

		core.BNFreeTypeFieldReferenceSizeInfo(refs, count.value)
		return result

	def get_all_types_referenced(self, name):
		"""
		``get_all_types_referenced`` returns a map from field offset to a related to the
		type field access.

		:param QualifiedName name: name of type to query for references
		:return: A map from field offset to a list of incoming types written to it
		:rtype: map
		:Example:

			>>> bv.get_all_types_referenced('B')
			{0: [<type: char, 0% confidence>], 8: [<type: int64_t, 0% confidence>],
			16: [<type: char, 0% confidence>, <type: bool>]}
			>>>

		"""
		count = ctypes.c_ulonglong(0)
		name = _types.QualifiedName(name)._get_core_struct()
		refs = core.BNGetAllTypesReferenced(self.handle, name, count)
		assert refs is not None, "core.BNGetAllTypesReferenced returned None"

		result = {}
		for i in range(0, count.value):
			result[refs[i].offset] = []
			for j in range(0, refs[i].count):
				typeObj = _types.Type.create(core.BNNewTypeReference(refs[i].types[j].type),
					self.platform, refs[i].types[j].confidence)
				result[refs[i].offset].append(typeObj)

		core.BNFreeTypeFieldReferenceTypeInfo(refs, count.value)
		return result

	def get_sizes_referenced(self, name, offset):
		"""
		``get_sizes_referenced`` returns a list of sizes of the accesses to it.

		:param QualifiedName name: name of type to query for references
		:param int offset: offset of the field
		:return: a list of sizes of the accesses to it.
		:rtype: list
		:Example:

			>>> bv.get_sizes_referenced('B', 16)
			[1, 8]
			>>>

		"""
		count = ctypes.c_ulonglong(0)
		name = _types.QualifiedName(name)._get_core_struct()
		refs = core.BNGetSizesReferenced(self.handle, name, offset, count)
		assert refs is not None, "core.BNGetSizesReferenced returned None"

		result = []
		for i in range(0, count.value):
			result.append(refs[i])

		core.BNFreeTypeFieldReferenceSizes(refs, count.value)
		return result

	def get_types_referenced(self, name:'_types.QualifiedName', offset:int) -> List['_types.Type']:
		"""
		``get_types_referenced`` returns a list of types related to the type field access.

		:param QualifiedName name: name of type to query for references
		:param int offset: offset of the field
		:return: a list of types related to the type field access.
		:rtype: list
		:Example:

			>>> bv.get_types_referenced('B', 0x10)
			[<type: bool>, <type: char, 0% confidence>]
			>>>
		"""
		count = ctypes.c_ulonglong(0)
		_name = _types.QualifiedName(name)._get_core_struct()
		refs = core.BNGetTypesReferenced(self.handle, _name, offset, count)
		assert refs is not None, "core.BNGetTypesReferenced returned None"
		try:
			result = []
			for i in range(0, count.value):
				typeObj = _types.Type.create(core.BNNewTypeReference(refs[i].type),\
					confidence = refs[i].confidence)
				result.append(typeObj)
			return result
		finally:
			core.BNFreeTypeFieldReferenceTypes(refs, count.value)

	def create_structure_from_offset_access(self, name:'_types.QualifiedName') -> '_types.StructureType':
		newMemberAdded = ctypes.c_bool(False)
		_name = _types.QualifiedName(name)._get_core_struct()
		struct = core.BNCreateStructureFromOffsetAccess(self.handle, _name, newMemberAdded)
		if struct is None:
			raise Exception("BNCreateStructureFromOffsetAccess failed to create struct from offsets")
		return _types.StructureType.from_core_struct(struct)

	def create_structure_member_from_access(self, name:'_types.QualifiedName', offset:int) -> '_types.Type':
		_name = _types.QualifiedName(name)._get_core_struct()
		result = core.BNCreateStructureMemberFromAccess(self.handle, _name, offset)
		if not result.type:
			raise Exception("BNCreateStructureMemberFromAccess failed to create struct member offsets")

		return _types.Type.create(core.BNNewTypeReference(result.type),\
			confidence = result.confidence)

	def get_callers(self, addr:int) -> Generator[ReferenceSource, None, None]:
		"""
		``get_callers`` returns a list of ReferenceSource objects (xrefs or cross-references) that call the provided virtual address.
		In this case, tail calls, jumps, and ordinary calls are considered.

		:param int addr: virtual address of callee to query for callers
		:return: List of References that call the given virtual address
		:rtype: list(ReferenceSource)
		:Example:

			>>> bv.get_callers(here)
			[<ref: x86@0x4165ff>]
			>>>

		"""
		count = ctypes.c_ulonglong(0)
		refs = core.BNGetCallers(self.handle, addr, count)
		assert refs is not None, "core.BNGetCallers returned None"
		try:
			for i in range(0, count.value):
				if refs[i].func:
					func = _function.Function(self, core.BNNewFunctionReference(refs[i].func))
				else:
					func = None
				if refs[i].arch:
					arch = architecture.CoreArchitecture._from_cache(refs[i].arch)
				else:
					arch = None

				yield ReferenceSource(func, arch, refs[i].addr)
		finally:
			core.BNFreeCodeReferences(refs, count.value)

	def get_callees(self, addr:int, func:'_function.Function'=None, arch:'architecture.Architecture'=None) -> List[int]:
		"""
		``get_callees`` returns a list of virtual addresses called by the call site in the function ``func``,
		of the architecture ``arch``, and at the address ``addr``. If no function is specified, call sites from
		all functions and containing the address will be considered. If no architecture is specified, the
		architecture of the function will be used.

		:param int addr: virtual address of the call site to query for callees
		:param Function func: (optional) the function that the call site belongs to
		:param Architecture func: (optional) the architecture of the call site
		:return: list of integers
		:rtype: list(integer)
		"""

		result = []
		funcs = self.get_functions_containing(addr) if func is None else [func]
		if not funcs:
			return []
		for src_func in funcs:
			src_arch = src_func.arch if arch is None else arch
			assert src_arch is not None
			ref_src = core.BNReferenceSource(src_func.handle, src_arch.handle, addr)
			count = ctypes.c_ulonglong(0)
			refs = core.BNGetCallees(self.handle, ref_src, count)
			assert refs is not None, "core.BNGetCallees returned None"
			for i in range(0, count.value):
				result.append(refs[i])
			core.BNFreeAddressList(refs)
		return result

	def get_symbol_at(self, addr, namespace=None):
		"""
		``get_symbol_at`` returns the Symbol at the provided virtual address.

		:param int addr: virtual address to query for symbol
		:return: Symbol for the given virtual address
		:param NameSpace namespace: (optional) the namespace of the symbols to retrieve
		:rtype: Symbol
		:Example:

			>>> bv.get_symbol_at(bv.entry_point)
			<FunctionSymbol: "_start" @ 0x100001174>
			>>>
		"""
		if isinstance(namespace, str):
			namespace = _types.NameSpace(namespace)
		if isinstance(namespace, _types.NameSpace):
			namespace = namespace._get_core_struct()

		sym = core.BNGetSymbolByAddress(self.handle, addr, namespace)
		if sym is None:
			return None
		return _types.Symbol(None, None, None, handle = sym)

	def get_symbol_by_raw_name(self, name, namespace=None):
		"""
		``get_symbol_by_raw_name`` retrieves a Symbol object for the given a raw (mangled) name.

		:param str name: raw (mangled) name of Symbol to be retrieved
		:return: Symbol object corresponding to the provided raw name
		:param NameSpace namespace: (optional) the namespace to search for the given symbol
		:rtype: Symbol
		:Example:

			>>> bv.get_symbol_by_raw_name('?testf@Foobar@@SA?AW4foo@1@W421@@Z')
			<FunctionSymbol: "public: static enum Foobar::foo __cdecl Foobar::testf(enum Foobar::foo)" @ 0x10001100>
			>>>
		"""
		if isinstance(namespace, str):
			namespace = _types.NameSpace(namespace)
		if isinstance(namespace, _types.NameSpace):
			namespace = namespace._get_core_struct()
		sym = core.BNGetSymbolByRawName(self.handle, name, namespace)
		if sym is None:
			return None
		return _types.Symbol(None, None, None, handle = sym)

	def get_symbols_by_name(self, name, namespace=None, ordered_filter=None):
		"""
		``get_symbols_by_name`` retrieves a list of Symbol objects for the given symbol name and ordered filter

		:param str name: name of Symbol object to be retrieved
		:param NameSpace namespace: (optional) the namespace to search for the given symbol
		:param str namespace: (optional) the namespace to search for the given symbol
		:param list(SymbolType) ordered_filter: (optional) an ordered filter based on SymbolType
		:return: Symbol object corresponding to the provided name
		:rtype: Symbol
		:Example:

			>>> bv.get_symbols_by_name('?testf@Foobar@@SA?AW4foo@1@W421@@Z')
			[<FunctionSymbol: "public: static enum Foobar::foo __cdecl Foobar::testf(enum Foobar::foo)" @ 0x10001100>]
			>>>
		"""
		if ordered_filter is None:
			ordered_filter = [SymbolType.FunctionSymbol,
				SymbolType.ImportedFunctionSymbol,
				SymbolType.LibraryFunctionSymbol,
				SymbolType.DataSymbol,
				SymbolType.ImportedDataSymbol,
				SymbolType.ImportAddressSymbol,
				SymbolType.ExternalSymbol]
		if isinstance(namespace, str):
			namespace = _types.NameSpace(namespace)
		if isinstance(namespace, _types.NameSpace):
			namespace = namespace._get_core_struct()
		count = ctypes.c_ulonglong(0)
		syms = core.BNGetSymbolsByName(self.handle, name, count, namespace)
		assert syms is not None, "core.BNGetSymbolsByName returned None"
		result = []
		for i in range(0, count.value):
			result.append(_types.Symbol(None, None, None, handle = core.BNNewSymbolReference(syms[i])))
		core.BNFreeSymbolList(syms, count.value)
		result = sorted(filter(lambda sym: sym.type in ordered_filter, result), key=lambda sym: ordered_filter.index(sym.type))
		return result

	def get_symbols(self, start=None, length=None, namespace=None):
		"""
		``get_symbols`` retrieves the list of all Symbol objects in the optionally provided range.

		:param int start: optional start virtual address
		:param int length: optional length
		:return: list of all Symbol objects, or those Symbol objects in the range of ``start``-``start+length``
		:rtype: list(Symbol)
		:Example:

			>>> bv.get_symbols(0x1000200c, 1)
			[<ImportAddressSymbol: "KERNEL32!IsProcessorFeaturePresent" @ 0x1000200c>]
			>>>
		"""
		count = ctypes.c_ulonglong(0)
		if isinstance(namespace, str):
			namespace = _types.NameSpace(namespace)
		if isinstance(namespace, _types.NameSpace):
			namespace = namespace._get_core_struct()
		if start is None:
			syms = core.BNGetSymbols(self.handle, count, namespace)
			assert syms is not None, "core.BNGetSymbols returned None"
		else:
			syms = core.BNGetSymbolsInRange(self.handle, start, length, count, namespace)
			assert syms is not None, "core.BNGetSymbolsInRange returned None"
		result = []
		for i in range(0, count.value):
			result.append(_types.Symbol(None, None, None, handle = core.BNNewSymbolReference(syms[i])))
		core.BNFreeSymbolList(syms, count.value)
		return result

	def get_symbols_of_type(self, sym_type, start=None, length=None, namespace=None):
		"""
		``get_symbols_of_type`` retrieves a list of all Symbol objects of the provided symbol type in the optionally
		 provided range.

		:param SymbolType sym_type: A Symbol type: :py:Class:`Symbol`.
		:param int start: optional start virtual address
		:param int length: optional length
		:return: list of all Symbol objects of type sym_type, or those Symbol objects in the range of ``start``-``start+length``
		:rtype: list(Symbol)
		:Example:

			>>> bv.get_symbols_of_type(SymbolType.ImportAddressSymbol, 0x10002028, 1)
			[<ImportAddressSymbol: "KERNEL32!GetCurrentThreadId" @ 0x10002028>]
			>>>
		"""
		if isinstance(sym_type, str):
			sym_type = SymbolType[sym_type]
		if isinstance(namespace, str):
			namespace = _types.NameSpace(namespace)
		if isinstance(namespace, _types.NameSpace):
			namespace = namespace._get_core_struct()
		count = ctypes.c_ulonglong(0)
		if start is None:
			syms = core.BNGetSymbolsOfType(self.handle, sym_type, count, namespace)
			assert syms is not None, "core.BNGetSymbolsOfType returned None"
		else:
			syms = core.BNGetSymbolsOfTypeInRange(self.handle, sym_type, start, length, count)
			assert syms is not None, "core.BNGetSymbolsOfTypeInRange returned None"
		result = []
		for i in range(0, count.value):
			result.append(_types.Symbol(None, None, None, handle = core.BNNewSymbolReference(syms[i])))
		core.BNFreeSymbolList(syms, count.value)
		return result

	def define_auto_symbol(self, sym):
		"""
		``define_auto_symbol`` adds a symbol to the internal list of automatically discovered Symbol objects in a given
		namespace.

		.. warning:: If multiple symbols for the same address are defined, only the most recent symbol will ever be used.

		:param Symbol sym: the symbol to define
		:rtype: None
		"""
		core.BNDefineAutoSymbol(self.handle, sym.handle)

	def define_auto_symbol_and_var_or_function(self, sym, sym_type, plat=None):
		"""
		``define_auto_symbol_and_var_or_function``

		.. warning:: If multiple symbols for the same address are defined, only the most recent symbol will ever be used.

		:param Symbol sym: the symbol to define
		:param Type sym_type: Type of symbol being defined (can be None)
		:param Platform plat: (optional) platform
		:rtype: None
		"""
		if plat is None:
			if self.platform is None:
				raise Exception("Attempting to call define_auto_symbol_and_var_or_function without Platform specified")
			plat = self.platform
		elif not isinstance(plat, _platform.Platform):
			raise AttributeError("Provided platform is not of type `Platform`")

		if isinstance(sym_type, _types.Type):
			sym_type = sym_type.handle
		elif sym_type is not None:
			raise AttributeError("Provided sym_type is not of type `binaryninja.Type`")

		sym = core.BNDefineAutoSymbolAndVariableOrFunction(self.handle, plat.handle, sym.handle, sym_type)
		if sym is None:
			return None
		return _types.Symbol(None, None, None, handle = sym)

	def undefine_auto_symbol(self, sym):
		"""
		``undefine_auto_symbol`` removes a symbol from the internal list of automatically discovered Symbol objects.

		:param Symbol sym: the symbol to undefine
		:rtype: None
		"""
		core.BNUndefineAutoSymbol(self.handle, sym.handle)

	def define_user_symbol(self, sym):
		"""
		``define_user_symbol`` adds a symbol to the internal list of user added Symbol objects.

		.. warning:: If multiple symbols for the same address are defined, only the most recent symbol will ever be used.

		:param Symbol sym: the symbol to define
		:rtype: None
		"""
		core.BNDefineUserSymbol(self.handle, sym.handle)

	def undefine_user_symbol(self, sym):
		"""
		``undefine_user_symbol`` removes a symbol from the internal list of user added Symbol objects.

		:param Symbol sym: the symbol to undefine
		:rtype: None
		"""
		core.BNUndefineUserSymbol(self.handle, sym.handle)

	def define_imported_function(self, import_addr_sym, func, type=None):
		"""
		``define_imported_function`` defines an imported Function ``func`` with a ImportedFunctionSymbol type.

		:param Symbol import_addr_sym: A Symbol object with type ImportedFunctionSymbol
		:param Function func: A Function object to define as an imported function
		:rtype: None
		"""
		core.BNDefineImportedFunction(self.handle, import_addr_sym.handle, func.handle, None if type is None else type.handle)

	def create_tag_type(self, name, icon):
		"""
		``create_tag_type`` creates a new Tag Type and adds it to the view

		:param str name: The name for the tag
		:param str icon: The icon (recommended 1 emoji or 2 chars) for the tag
		:return: The created tag type
		:rtype: TagType
		:Example:

			>>> tt = bv.create_tag_type("Crabby Functions", "🦀")
			>>> bv.create_user_data_tag(here, tt, "Get Crabbed")
			>>>
		"""
		tag_handle = core.BNCreateTagType(self.handle, name, icon)
		assert tag_handle is not None, "core.BNCreateTagType returned None"
		tag_type = TagType(tag_handle)
		tag_type.name = name
		tag_type.icon = icon
		core.BNAddTagType(self.handle, tag_type.handle)
		return tag_type

	def remove_tag_type(self, tag_type):
		"""
		``remove_tag_type`` removes a new Tag Type and all tags that use it

		:param TagType tag_type: The Tag Type to remove
		:rtype: None
		"""
		core.BNRemoveTagType(self.handle, tag_type.handle)

	@property
	def tag_types(self):
		"""
		``tag_types`` gets a dictionary of all Tag Types present for the view,
		structured as {Tag Type Name => Tag Type}.

		:rtype: dict of (str, TagType)
		"""
		count = ctypes.c_ulonglong(0)
		types = core.BNGetTagTypes(self.handle, count)
		assert types is not None, "core.BNGetTagTypes returned None"
		result = {}
		for i in range(0, count.value):
			tag_handle = core.BNNewTagTypeReference(types[i])
			assert tag_handle is not None, "core.BNNewTagTypeReference returned None"
			tag = TagType(tag_handle)
			if tag.name in result:
				if type(result[tag.name]) == list:
					result[tag.name].append(tag)
				else:
					result[tag.name] = [result[tag.name], tag]
			else:
				result[tag.name] = tag
		core.BNFreeTagTypeList(types, count.value)
		return result

	def get_tag_type(self, name):
		"""
		Get a tag type by its name. Shorthand for get_tag_type_by_name()
		:param name: Name of the tag type
		:return: The relevant tag type, if it exists
		:rtype: TagType
		"""
		return self.get_tag_type_by_name(name)

	def get_tag_type_by_name(self, name):
		"""
		Get a tag type by its name
		:param name: Name of the tag type
		:return: The relevant tag type, if it exists
		:rtype: TagType
		"""
		tag_type = core.BNGetTagType(self.handle, name)
		if tag_type is None:
			return None
		return TagType(tag_type)

	def get_tag_type_by_id(self, id):
		"""
		Get a tag type by its id
		:param id: Id of the tag type
		:return: The relevant tag type, if it exists
		:rtype: TagType
		"""
		tag_type = core.BNGetTagTypeById(self.handle, id)
		if tag_type is None:
			return None
		return TagType(tag_type)

	def create_user_tag(self, type, data):
		return self.create_tag(type, data, True)

	def create_auto_tag(self, type, data):
		return self.create_tag(type, data, False)

	def create_tag(self, type:TagType, data:str, user:bool=True) -> Tag:
		"""
		``create_tag`` creates a new Tag object but does not add it anywhere.
		Use :py:meth:`create_user_data_tag` to create and add in one step.

		:param TagType type: The Tag Type for this Tag
		:param str data: Additional data for the Tag
		:param bool user: Whether or not a user tag
		:return: The created Tag
		:rtype: Tag
		:Example:

			>>> tt = bv.tag_types["Crashes"]
			>>> tag = bv.create_tag(tt, "Null pointer dereference", True)
			>>> bv.add_user_data_tag(here, tag)
			>>>
		"""
		tag_handle = core.BNCreateTag(type.handle, data)
		assert tag_handle is not None, "core.BNCreateTag returned None"
		tag = Tag(tag_handle)
		core.BNAddTag(self.handle, tag.handle, user)
		return tag

	def get_tag(self, id):
		"""
		Get a tag by its id. Note this does not tell you anything about where it is used.
		:param id: Tag id
		:return: The relevant tag, if it exists
		:rtype: Tag
		"""
		tag = core.BNGetTag(self.handle, id)
		if tag is None:
			return None
		return Tag(tag)

	@property
	def data_tags(self):
		"""
		``data_tags`` gets a list of all data Tags in the view.
		Tags are returned as a list of (address, Tag) pairs.

		:type: list(int, Tag)
		"""
		count = ctypes.c_ulonglong()
		tags = core.BNGetDataTagReferences(self.handle, count)
		assert tags is not None, "core.BNGetDataTagReferences returned None"
		result = []
		for i in range(0, count.value):
			tag_handle = core.BNNewTagReference(tags[i].tag)
			assert tag_handle is not None, "core.BNNewTagReference is not None"
			tag = Tag(tag_handle)
			result.append((tags[i].addr, tag))
		core.BNFreeTagReferences(tags, count.value)
		return result

	@property
	def auto_data_tags(self):
		"""
		``auto_data_tags`` gets a list of all auto-defined data Tags in the view.
		Tags are returned as a list of (address, Tag) pairs.

		:type: list(int, Tag)
		"""
		count = ctypes.c_ulonglong()
		refs = core.BNGetAutoDataTagReferences(self.handle, count)
		result = []
		for i in range(0, count.value):
			tag = Tag(core.BNNewTagReference(refs[i].tag))
			result.append((refs[i].addr, tag))
		core.BNFreeTagReferences(refs, count.value)
		return result

	@property
	def user_data_tags(self):
		"""
		``user_data_tags`` gets a list of all user data Tags in the view.
		Tags are returned as a list of (address, Tag) pairs.

		:type: list(int, Tag)
		"""
		count = ctypes.c_ulonglong()
		refs = core.BNGetUserDataTagReferences(self.handle, count)
		result = []
		for i in range(0, count.value):
			tag = Tag(core.BNNewTagReference(refs[i].tag))
			result.append((refs[i].addr, tag))
		core.BNFreeTagReferences(refs, count.value)
		return result

	def get_data_tags_at(self, addr):
		"""
		``get_data_tags_at`` gets a list of all Tags for a data address.

		:param int addr: Address to get tags at
		:return: A list of data Tags
		:rtype: list(Tag)
		"""
		count = ctypes.c_ulonglong()
		tags = core.BNGetDataTags(self.handle, addr, count)
		assert tags is not None, "core.BNGetDataTags returned None"
		result = []
		for i in range(0, count.value):
			tag_handle = core.BNNewTagReference(tags[i])
			assert tag_handle is not None, "core.BNNewTagReference is not None"
			result.append(Tag(tag_handle))
		core.BNFreeTagList(tags, count.value)
		return result

	def get_auto_data_tags_at(self, addr):
		"""
		``get_auto_data_tags_at`` gets a list of all auto-defined Tags for a data address.

		:param int addr: Address to get tags at
		:return: A list of data Tags
		:rtype: list(Tag)
		"""
		count = ctypes.c_ulonglong()
		tags = core.BNGetAutoDataTags(self.handle, addr, count)
		result = []
		for i in range(0, count.value):
			result.append(Tag(core.BNNewTagReference(tags[i])))
		core.BNFreeTagList(tags, count.value)
		return result

	def get_user_data_tags_at(self, addr):
		"""
		``get_user_data_tags_at`` gets a list of all user Tags for a data address.

		:param int addr: Address to get tags at
		:return: A list of data Tags
		:rtype: list(Tag)
		"""
		count = ctypes.c_ulonglong()
		tags = core.BNGetUserDataTags(self.handle, addr, count)
		result = []
		for i in range(0, count.value):
			result.append(Tag(core.BNNewTagReference(tags[i])))
		core.BNFreeTagList(tags, count.value)
		return result

	def get_data_tags_of_type(self, addr, tag_type):
		"""
		``get_data_tags_of_type`` gets a list of all Tags for a data address of a given type.

		:param int addr: Address to get tags at
		:param TagType tag_type: TagType object to match in searching
		:return: A list of data Tags
		:rtype: list(Tag)
		"""
		count = ctypes.c_ulonglong()
		tags = core.BNGetDataTagsOfType(self.handle, addr, tag_type.handle, count)
		result = []
		for i in range(0, count.value):
			result.append(Tag(core.BNNewTagReference(tags[i])))
		core.BNFreeTagList(tags, count.value)
		return result

	def get_auto_data_tags_of_type(self, addr, tag_type):
		"""
		``get_auto_data_tags_of_type`` gets a list of all auto-defined Tags for a data address of a given type.

		:param int addr: Address to get tags at
		:param TagType tag_type: TagType object to match in searching
		:return: A list of data Tags
		:rtype: list(Tag)
		"""
		count = ctypes.c_ulonglong()
		tags = core.BNGetAutoDataTagsOfType(self.handle, addr, tag_type.handle, count)
		assert tags is not None, "core.BNGetAutoDataTagsOfType returned None"
		result = []
		for i in range(0, count.value):
			tag_handle = core.BNNewTagReference(tags[i])
			assert tag_handle is not None, "core.BNNewTagReference returned None"
			result.append(Tag(tag_handle))
		core.BNFreeTagList(tags, count.value)
		return result

	def get_user_data_tags_of_type(self, addr, tag_type):
		"""
		``get_user_data_tags_of_type`` gets a list of all user Tags for a data address of a given type.

		:param int addr: Address to get tags at
		:param TagType tag_type: TagType object to match in searching
		:return: A list of data Tags
		:rtype: list(Tag)
		"""
		count = ctypes.c_ulonglong()
		tags = core.BNGetUserDataTagsOfType(self.handle, addr, tag_type.handle, count)
		result = []
		for i in range(0, count.value):
			result.append(Tag(core.BNNewTagReference(tags[i])))
		core.BNFreeTagList(tags, count.value)
		return result

	def get_data_tags_in_range(self, address_range):
		"""
		``get_data_tags_in_range`` gets a list of all data Tags in a given range.
		Range is inclusive at the start, exclusive at the end.

		:param AddressRange address_range: Address range from which to get tags
		:return: A list of (address, data tag) tuples
		:rtype: list((int, Tag))
		"""
		count = ctypes.c_ulonglong()
		refs = core.BNGetDataTagsInRange(self.handle, address_range.start, address_range.end, count)
		result = []
		for i in range(0, count.value):
			tag = Tag(core.BNNewTagReference(refs[i].tag))
			result.append((refs[i].addr, tag))
		core.BNFreeTagReferences(refs, count.value)
		return result

	def get_auto_data_tags_in_range(self, address_range):
		"""
		``get_auto_data_tags_in_range`` gets a list of all auto-defined data Tags in a given range.
		Range is inclusive at the start, exclusive at the end.

		:param AddressRange address_range: Address range from which to get tags
		:return: A list of (address, data tag) tuples
		:rtype: list((int, Tag))
		"""
		count = ctypes.c_ulonglong()
		refs = core.BNGetAutoDataTagsInRange(self.handle, address_range.start, address_range.end, count)
		result = []
		for i in range(0, count.value):
			tag = Tag(core.BNNewTagReference(refs[i].tag))
			result.append((refs[i].addr, tag))
		core.BNFreeTagReferences(refs, count.value)
		return result

	def get_user_data_tags_in_range(self, address_range):
		"""
		``get_user_data_tags_in_range`` gets a list of all user data Tags in a given range.
		Range is inclusive at the start, exclusive at the end.

		:param AddressRange address_range: Address range from which to get tags
		:return: A list of (address, data tag) tuples
		:rtype: list((int, Tag))
		"""
		count = ctypes.c_ulonglong()
		refs = core.BNGetUserDataTagsInRange(self.handle, address_range.start, address_range.end, count)
		result = []
		for i in range(0, count.value):
			tag = Tag(core.BNNewTagReference(refs[i].tag))
			result.append((refs[i].addr, tag))
		core.BNFreeTagReferences(refs, count.value)
		return result

	def add_user_data_tag(self, addr, tag):
		"""
		``add_user_data_tag`` adds an already-created Tag object at a data address.
		Since this adds a user tag, it will be added to the current undo buffer.
		If you want want to create the tag as well, consider using
		:meth:`create_user_data_tag <binaryninja.binaryview.BinaryView.create_user_data_tag>`

		:param int addr: Address at which to add the tag
		:param Tag tag: Tag object to be added
		:rtype: None
		"""
		core.BNAddUserDataTag(self.handle, addr, tag.handle)

	def create_user_data_tag(self, addr, type, data, unique=False):
		"""
		``create_user_data_tag`` creates and adds a Tag object at a data
		address. Since this adds a user tag, it will be added to the current
		undo buffer.

		This API is appropriate for generic data tags, for functions,
		consider using :meth:`create_user_function_tag <Function.create_user_function_tag>` or for
		specific addresses inside of functions: :meth:`create_user_address_tag <Function.create_user_address_tag>`.

		:param int addr: Address at which to add the tag
		:param TagType type: Tag Type for the Tag that is created
		:param str data: Additional data for the Tag
		:param bool unique: If a tag already exists at this location with this data, don't add another
		:return: The created Tag
		:rtype: Tag
		:Example:

			>>> tt = bv.tag_types["Crashes"]
			>>> bv.create_user_data_tag(here, tt, "String data to associate with this tag")
			>>>
		"""
		if unique:
			tags = self.get_data_tags_at(addr)
			for tag in tags:
				if tag.type == type and tag.data == data:
					return tag

		tag = self.create_tag(type, data, True)
		core.BNAddUserDataTag(self.handle, addr, tag.handle)
		return tag

	def remove_user_data_tag(self, addr, tag):
		"""
		``remove_user_data_tag`` removes a Tag object at a data address.
		Since this removes a user tag, it will be added to the current undo buffer.

		:param int addr: Address at which to remove the tag
		:param Tag tag: Tag object to be removed
		:rtype: None
		"""
		core.BNRemoveUserDataTag(self.handle, addr, tag.handle)

	def remove_user_data_tags_of_type(self, addr, tag_type):
		"""
		``remove_user_data_tags_of_type`` removes all data tags at the given address of the given type.
		Since this removes user tags, it will be added to the current undo buffer.

		:param int addr: Address at which to add the tags
		:param TagType tag_type: TagType object to match for removing
		:rtype: None
		"""
		core.BNRemoveUserDataTagsOfType(self.handle, addr, tag_type.handle)

	def add_auto_data_tag(self, addr, tag):
		"""
		``add_auto_data_tag`` adds an already-created Tag object at a data address.
		If you want want to create the tag as well, consider using
		:meth:`create_auto_data_tag <binaryninja.binaryview.BinaryView.create_auto_data_tag>`

		:param int addr: Address at which to add the tag
		:param Tag tag: Tag object to be added
		:rtype: None
		"""
		core.BNAddAutoDataTag(self.handle, addr, tag.handle)

	def create_auto_data_tag(self, addr, type, data, unique=False):
		"""
		``create_auto_data_tag`` creates and adds a Tag object at a data address.

		:param int addr: Address at which to add the tag
		:param TagType type: Tag Type for the Tag that is created
		:param str data: Additional data for the Tag
		:param bool unique: If a tag already exists at this location with this data, don't add another
		:return: The created Tag
		:rtype: Tag
		"""
		if unique:
			tags = self.get_data_tags_at(addr)
			for tag in tags:
				if tag.type == type and tag.data == data:
					return tag

		tag = self.create_tag(type, data, False)
		core.BNAddAutoDataTag(self.handle, addr, tag.handle)
		return tag

	def remove_auto_data_tag(self, addr, tag):
		"""
		``remove_auto_data_tag`` removes a Tag object at a data address.
		Since this removes a user tag, it will be added to the current undo buffer.

		:param int addr: Address at which to remove the tag
		:param Tag tag: Tag object to be removed
		:rtype: None
		"""
		core.BNRemoveAutoDataTag(self.handle, addr, tag.handle)

	def remove_auto_data_tags_of_type(self, addr, tag_type):
		"""
		``remove_auto_data_tags_of_type`` removes all data tags at the given address of the given type.
		Since this removes user tags, it will be added to the current undo buffer.

		:param int addr: Address at which to add the tags
		:param TagType tag_type: TagType object to match for removing
		:rtype: None
		"""
		core.BNRemoveAutoDataTagsOfType(self.handle, addr, tag_type.handle)

	def can_assemble(self, arch=None):
		"""
		``can_assemble`` queries the architecture plugin to determine if the architecture can assemble instructions.

		:return: True if the architecture can assemble, False otherwise
		:rtype: bool
		:Example:

			>>> bv.can_assemble()
			True
			>>>
		"""
		if arch is None:
			if self.arch is None:
				raise Exception("Attempting to call can_assemble without an Architecture specified")
			arch = self.arch
		return core.BNCanAssemble(self.handle, arch.handle)

	def is_never_branch_patch_available(self, addr, arch=None):
		"""
		``is_never_branch_patch_available`` queries the architecture plugin to determine if the instruction at the
		instruction at ``addr`` can be made to **never branch**. The actual logic of which is implemented in the
		``perform_is_never_branch_patch_available`` in the corresponding architecture.

		:param int addr: the virtual address of the instruction to be patched
		:param Architecture arch: (optional) the architecture of the instructions if different from the default
		:return: True if the instruction can be patched, False otherwise
		:rtype: bool
		:Example:

			>>> bv.get_disassembly(0x100012ed)
			'test    eax, eax'
			>>> bv.is_never_branch_patch_available(0x100012ed)
			False
			>>> bv.get_disassembly(0x100012ef)
			'jg      0x100012f5'
			>>> bv.is_never_branch_patch_available(0x100012ef)
			True
			>>>
		"""
		if arch is None:
			if self.arch is None:
				raise Exception("Attempting to call can_assemble without an Architecture specified")
			arch = self.arch
		return core.BNIsNeverBranchPatchAvailable(self.handle, arch.handle, addr)

	def is_always_branch_patch_available(self, addr, arch=None):
		"""
		``is_always_branch_patch_available`` queries the architecture plugin to determine if the
		instruction at ``addr`` can be made to **always branch**. The actual logic of which is implemented in the
		``perform_is_always_branch_patch_available`` in the corresponding architecture.

		:param int addr: the virtual address of the instruction to be patched
		:param Architecture arch: (optional) the architecture for the current view
		:return: True if the instruction can be patched, False otherwise
		:rtype: bool
		:Example:

			>>> bv.get_disassembly(0x100012ed)
			'test    eax, eax'
			>>> bv.is_always_branch_patch_available(0x100012ed)
			False
			>>> bv.get_disassembly(0x100012ef)
			'jg      0x100012f5'
			>>> bv.is_always_branch_patch_available(0x100012ef)
			True
			>>>
		"""
		if arch is None:
			if self.arch is None:
				raise Exception("Attempting to call can_assemble without an Architecture specified")
			arch = self.arch
		return core.BNIsAlwaysBranchPatchAvailable(self.handle, arch.handle, addr)

	def is_invert_branch_patch_available(self, addr, arch=None):
		"""
		``is_invert_branch_patch_available`` queries the architecture plugin to determine if the instruction at ``addr``
		is a branch that can be inverted. The actual logic of which is implemented in the
		``perform_is_invert_branch_patch_available`` in the corresponding architecture.

		:param int addr: the virtual address of the instruction to be patched
		:param Architecture arch: (optional) the architecture of the instructions if different from the default
		:return: True if the instruction can be patched, False otherwise
		:rtype: bool
		:Example:

			>>> bv.get_disassembly(0x100012ed)
			'test    eax, eax'
			>>> bv.is_invert_branch_patch_available(0x100012ed)
			False
			>>> bv.get_disassembly(0x100012ef)
			'jg      0x100012f5'
			>>> bv.is_invert_branch_patch_available(0x100012ef)
			True
			>>>

		"""
		if arch is None:
			if self.arch is None:
				raise Exception("Attempting to call can_assemble without an Architecture specified")
			arch = self.arch
		return core.BNIsInvertBranchPatchAvailable(self.handle, arch.handle, addr)

	def is_skip_and_return_zero_patch_available(self, addr, arch=None):
		"""
		``is_skip_and_return_zero_patch_available`` queries the architecture plugin to determine if the
		instruction at ``addr`` is similar to an x86 "call"  instruction which can be made to return zero.  The actual
		logic of which is implemented in the ``perform_is_skip_and_return_zero_patch_available`` in the corresponding
		architecture.

		:param int addr: the virtual address of the instruction to be patched
		:param Architecture arch: (optional) the architecture of the instructions if different from the default
		:return: True if the instruction can be patched, False otherwise
		:rtype: bool
		:Example:

			>>> bv.get_disassembly(0x100012f6)
			'mov     dword [0x10003020], eax'
			>>> bv.is_skip_and_return_zero_patch_available(0x100012f6)
			False
			>>> bv.get_disassembly(0x100012fb)
			'call    0x10001629'
			>>> bv.is_skip_and_return_zero_patch_available(0x100012fb)
			True
			>>>
		"""
		if arch is None:
			if self.arch is None:
				raise Exception("Attempting to call can_assemble without an Architecture specified")
			arch = self.arch
		return core.BNIsSkipAndReturnZeroPatchAvailable(self.handle, arch.handle, addr)

	def is_skip_and_return_value_patch_available(self, addr, arch=None):
		"""
		``is_skip_and_return_value_patch_available`` queries the architecture plugin to determine if the
		instruction at ``addr`` is similar to an x86 "call" instruction which can be made to return a value. The actual
		logic of which is implemented in the ``perform_is_skip_and_return_value_patch_available`` in the corresponding
		architecture.

		:param int addr: the virtual address of the instruction to be patched
		:param Architecture arch: (optional) the architecture of the instructions if different from the default
		:return: True if the instruction can be patched, False otherwise
		:rtype: bool
		:Example:

			>>> bv.get_disassembly(0x100012f6)
			'mov     dword [0x10003020], eax'
			>>> bv.is_skip_and_return_value_patch_available(0x100012f6)
			False
			>>> bv.get_disassembly(0x100012fb)
			'call    0x10001629'
			>>> bv.is_skip_and_return_value_patch_available(0x100012fb)
			True
			>>>
		"""
		if arch is None:
			if self.arch is None:
				raise Exception("Attempting to call can_assemble without an Architecture specified")
			arch = self.arch
		return core.BNIsSkipAndReturnValuePatchAvailable(self.handle, arch.handle, addr)

	def convert_to_nop(self, addr, arch=None):
		"""
		``convert_to_nop`` converts the instruction at virtual address ``addr`` to a nop of the provided architecture.

		.. note:: This API performs a binary patch, analysis may need to be updated afterward. Additionally the binary \
		file must be saved in order to preserve the changes made.

		:param int addr: virtual address of the instruction to convert to nops
		:param Architecture arch: (optional) the architecture of the instructions if different from the default
		:return: True on success, False on failure.
		:rtype: bool
		:Example:

			>>> bv.get_disassembly(0x100012fb)
			'call    0x10001629'
			>>> bv.convert_to_nop(0x100012fb)
			True
			>>> #The above 'call' instruction is 5 bytes, a nop in x86 is 1 byte,
			>>> # thus 5 nops are used:
			>>> bv.get_disassembly(0x100012fb)
			'nop'
			>>> bv.get_next_disassembly()
			'nop'
			>>> bv.get_next_disassembly()
			'nop'
			>>> bv.get_next_disassembly()
			'nop'
			>>> bv.get_next_disassembly()
			'nop'
			>>> bv.get_next_disassembly()
			'mov     byte [ebp-0x1c], al'
		"""
		if arch is None:
			if self.arch is None:
				raise Exception("Attempting to call can_assemble without an Architecture specified")
			arch = self.arch
		return core.BNConvertToNop(self.handle, arch.handle, addr)

	def always_branch(self, addr, arch=None):
		"""
		``always_branch`` convert the instruction of architecture ``arch`` at the virtual address ``addr`` to an
		unconditional branch.

		.. note:: This API performs a binary patch, analysis may need to be updated afterward. Additionally the binary \
		file must be saved in order to preserve the changes made.

		:param int addr: virtual address of the instruction to be modified
		:param Architecture arch: (optional) the architecture of the instructions if different from the default
		:return: True on success, False on failure.
		:rtype: bool
		:Example:

			>>> bv.get_disassembly(0x100012ef)
			'jg      0x100012f5'
			>>> bv.always_branch(0x100012ef)
			True
			>>> bv.get_disassembly(0x100012ef)
			'jmp     0x100012f5'
			>>>
		"""
		if arch is None:
			if self.arch is None:
				raise Exception("Attempting to call can_assemble without an Architecture specified")
			arch = self.arch
		return core.BNAlwaysBranch(self.handle, arch.handle, addr)

	def never_branch(self, addr, arch=None):
		"""
		``never_branch`` convert the branch instruction of architecture ``arch`` at the virtual address ``addr`` to
		a fall through.

		.. note:: This API performs a binary patch, analysis may need to be updated afterward. Additionally the binary\
		file must be saved in order to preserve the changes made.

		:param int addr: virtual address of the instruction to be modified
		:param Architecture arch: (optional) the architecture of the instructions if different from the default
		:return: True on success, False on failure.
		:rtype: bool
		:Example:

			>>> bv.get_disassembly(0x1000130e)
			'jne     0x10001317'
			>>> bv.never_branch(0x1000130e)
			True
			>>> bv.get_disassembly(0x1000130e)
			'nop'
			>>>
		"""
		if arch is None:
			if self.arch is None:
				raise Exception("Attempting to call can_assemble without an Architecture specified")
			arch = self.arch
		return core.BNConvertToNop(self.handle, arch.handle, addr)

	def invert_branch(self, addr, arch=None):
		"""
		``invert_branch`` convert the branch instruction of architecture ``arch`` at the virtual address ``addr`` to the
		inverse branch.

		.. note:: This API performs a binary patch, analysis may need to be updated afterward. Additionally the binary \
		file must be saved in order to preserve the changes made.

		:param int addr: virtual address of the instruction to be modified
		:param Architecture arch: (optional) the architecture of the instructions if different from the default
		:return: True on success, False on failure.
		:rtype: bool
		:Example:

			>>> bv.get_disassembly(0x1000130e)
			'je      0x10001317'
			>>> bv.invert_branch(0x1000130e)
			True
			>>>
			>>> bv.get_disassembly(0x1000130e)
			'jne     0x10001317'
			>>>
		"""
		if arch is None:
			if self.arch is None:
				raise Exception("Attempting to call can_assemble without an Architecture specified")
			arch = self.arch
		return core.BNInvertBranch(self.handle, arch.handle, addr)

	def skip_and_return_value(self, addr, value, arch=None):
		"""
		``skip_and_return_value`` convert the ``call`` instruction of architecture ``arch`` at the virtual address
		``addr`` to the equivalent of returning a value.

		:param int addr: virtual address of the instruction to be modified
		:param int value: value to make the instruction *return*
		:param Architecture arch: (optional) the architecture of the instructions if different from the default
		:return: True on success, False on failure.
		:rtype: bool
		:Example:

			>>> bv.get_disassembly(0x1000132a)
			'call    0x1000134a'
			>>> bv.skip_and_return_value(0x1000132a, 42)
			True
			>>> #The return value from x86 functions is stored in eax thus:
			>>> bv.get_disassembly(0x1000132a)
			'mov     eax, 0x2a'
			>>>
		"""
		if arch is None:
			if self.arch is None:
				raise Exception("Attempting to call can_assemble without an Architecture specified")
			arch = self.arch
		return core.BNSkipAndReturnValue(self.handle, arch.handle, addr, value)

	def get_instruction_length(self, addr, arch=None):
		"""
		``get_instruction_length`` returns the number of bytes in the instruction of Architecture ``arch`` at the virtual
		address ``addr``

		:param int addr: virtual address of the instruction query
		:param Architecture arch: (optional) the architecture of the instructions if different from the default
		:return: Number of bytes in instruction
		:rtype: int
		:Example:

			>>> bv.get_disassembly(0x100012f1)
			'xor     eax, eax'
			>>> bv.get_instruction_length(0x100012f1)
			2L
			>>>
		"""
		if arch is None:
			if self.arch is None:
				raise Exception("Attempting to call can_assemble without an Architecture specified")
			arch = self.arch
		return core.BNGetInstructionLength(self.handle, arch.handle, addr)

	def notify_data_written(self, offset, length):
		core.BNNotifyDataWritten(self.handle, offset, length)

	def notify_data_inserted(self, offset, length):
		core.BNNotifyDataInserted(self.handle, offset, length)

	def notify_data_removed(self, offset, length):
		core.BNNotifyDataRemoved(self.handle, offset, length)

	def get_strings(self, start = None, length = None):
		"""
		``get_strings`` returns a list of strings defined in the binary in the optional virtual address range:
		``start-(start+length)``

		Note that this API will only return strings that have been identified by the string-analysis and thus governed by the minimum and maximum length settings and unrelated to the type system.

		:param int start: optional virtual address to start the string list from, defaults to start of the binary
		:param int length: optional length range to return strings from, defaults to length of the binary
		:return: a list of all strings or a list of strings defined between ``start`` and ``start+length``
		:rtype: list(StringReference)
		:Example:

			>>> bv.get_strings(0x1000004d, 1)
			[<AsciiString: 0x1000004d, len 0x2c>]
			>>>
		"""
		count = ctypes.c_ulonglong(0)
		if start is None:
			strings = core.BNGetStrings(self.handle, count)
			assert strings is not None, "core.BNGetStrings returned None"
		else:
			if length is None:
				length = self.end - start
			strings = core.BNGetStringsInRange(self.handle, start, length, count)
			assert strings is not None, "core.BNGetStringsInRange returned None"
		result = []
		for i in range(0, count.value):
			result.append(StringReference(self, StringType(strings[i].type), strings[i].start, strings[i].length))
		core.BNFreeStringReferenceList(strings)
		return result

	def get_string_at(self, addr, partial=False):
		"""
		``get_string_at`` returns the string that falls on given virtual address.

		.. note:: This returns discovered strings and is therefore governed by `analysis.limits.minStringLength` and other settings. For an alternative API that simply returns any potential c-string at a given location, use :py:meth:`get_ascii_string_at`.

		:param int addr: virtual address to get the string from
		:param bool partial: whether to return a partial string reference or not
		:return: returns the StringReference at the given virtual address, otherwise None.
		:rtype: StringReference
		:Example:

			>>> bv.get_string_at(0x40302f)
			<StringType.AsciiString: 0x403028, len 0x12>

		"""
		str_ref = core.BNStringReference()
		if not core.BNGetStringAtAddress(self.handle, addr, str_ref):
			return None
		if str_ref.type != StringType.AsciiString:
			partial = False
			log.log_warn("Partial string not supported at {}".format(hex(addr)))
		start = addr if partial else str_ref.start
		length = str_ref.length - (addr - str_ref.start) if partial else str_ref.length
		return StringReference(self, StringType(str_ref.type), start, length)

	def get_ascii_string_at(self, addr, min_length=4, max_length=None, require_cstring=True):
		"""
		``get_ascii_string_at`` returns an ascii string found at ``addr``.

		.. note:: This returns an ascii string irrespective of whether the core analysis identified a string at that location. For an alternative API that uses existing identified strings, use :py:meth:`get_string_at`.

		:param int addr: virtual address to start the string
		:param int min_length: minimum length to define a string
		:param int max_length: max length string to return
		:param bool require_cstring: only return 0x0-terminated strings
		:return: the string found at ``addr`` or None if a string does not exist
		:rtype: StringReference or None
		:Example:

			>>> s1 = bv.get_ascii_string_at(0x70d0)
			>>> s1
			<AsciiString: 0x70d0, len 0xb>
			>>> s1.value
			'AWAVAUATUSH'
			>>> s2 = bv.get_ascii_string_at(0x70d1)
			>>> s2
			<AsciiString: 0x70d1, len 0xa>
			>>> s2.value
			'WAVAUATUSH'
		"""
		if not isinstance(addr, int):
			raise AttributeError("Input address (" + str(addr) + ") is not a number.")
		if addr < self.start or addr >= self.end:
			return None

		br = BinaryReader(self)
		br.seek(addr)
		length = 0
		c = br.read8()
		while c is not None and c > 0 and c <= 0x7f:
			if length == max_length:
				break
			length += 1
			c = br.read8()
		if length < min_length:
			return None
		if require_cstring and c != 0:
			return None
		return StringReference(self, StringType.AsciiString, addr, length)

	def add_analysis_completion_event(self, callback):
		"""
		``add_analysis_completion_event`` sets up a call back function to be called when analysis has been completed.
		This is helpful when using :func:`update_analysis` which does not wait for analysis completion before returning.

		The callee of this function is not responsible for maintaining the lifetime of the returned AnalysisCompletionEvent object.

		.. warning:: The built-in python console automatically updates analysis after every command is run, which means this call back may not behave as expected if entered interactively.

		:param callback callback: A function to be called with no parameters when analysis has completed.
		:return: An initialized AnalysisCompletionEvent object.
		:rtype: AnalysisCompletionEvent
		:Example:

			>>> def completionEvent():
			...   print("done")
			...
			>>> bv.add_analysis_completion_event(completionEvent)
			<binaryninja.AnalysisCompletionEvent object at 0x10a2c9f10>
			>>> bv.update_analysis()
			done
			>>>
		"""
		return AnalysisCompletionEvent(self, callback)

	def get_next_function_start_after(self, addr):
		"""
		``get_next_function_start_after`` returns the virtual address of the Function that occurs after the virtual address
		``addr``

		:param int addr: the virtual address to start looking from.
		:return: the virtual address of the next Function
		:rtype: int
		:Example:

			>>> bv.get_next_function_start_after(bv.entry_point)
			268441061L
			>>> hex(bv.get_next_function_start_after(bv.entry_point))
			'0x100015e5L'
			>>> hex(bv.get_next_function_start_after(0x100015e5))
			'0x10001629L'
			>>> hex(bv.get_next_function_start_after(0x10001629))
			'0x1000165eL'
			>>>
		"""
		return core.BNGetNextFunctionStartAfterAddress(self.handle, addr)

	def get_next_basic_block_start_after(self, addr):
		"""
		``get_next_basic_block_start_after`` returns the virtual address of the BasicBlock that occurs after the virtual
		 address ``addr``

		:param int addr: the virtual address to start looking from.
		:return: the virtual address of the next BasicBlock
		:rtype: int
		:Example:

			>>> hex(bv.get_next_basic_block_start_after(bv.entry_point))
			'0x100014a8L'
			>>> hex(bv.get_next_basic_block_start_after(0x100014a8))
			'0x100014adL'
			>>>
		"""
		return core.BNGetNextBasicBlockStartAfterAddress(self.handle, addr)

	def get_next_data_after(self, addr):
		"""
		``get_next_data_after`` retrieves the virtual address of the next non-code byte.

		:param int addr: the virtual address to start looking from.
		:return: the virtual address of the next data byte which is data, not code
		:rtype: int
		:Example:

			>>> hex(bv.get_next_data_after(0x10000000))
			'0x10000001L'
		"""
		return core.BNGetNextDataAfterAddress(self.handle, addr)

	def get_next_data_var_after(self, addr):
		"""
		``get_next_data_var_after`` retrieves the next :py:Class:`DataVariable`, or None.

		:param int addr: the virtual address to start looking from.
		:return: the next :py:Class:`DataVariable`
		:rtype: DataVariable
		:Example:

			>>> bv.get_next_data_var_after(0x10000000)
			<var 0x1000003c: int32_t>
			>>>
		"""
		while True:
			next_data_var_start = core.BNGetNextDataVariableStartAfterAddress(self.handle, addr)
			if next_data_var_start == self.end:
				return None
			var = core.BNDataVariable()
			if not core.BNGetDataVariableAtAddress(self.handle, next_data_var_start, var):
				return None
			if var.address < next_data_var_start:
				addr = var.address + core.BNGetTypeWidth(var.type)
				continue
			break
		return DataVariable.from_core_struct(var, self)

	def get_next_data_var_start_after(self, addr):
		"""
		``get_next_data_var_start_after`` retrieves the next virtual address of the next :py:Class:`DataVariable`

		:param int addr: the virtual address to start looking from.
		:return: the virtual address of the next :py:Class:`DataVariable`
		:rtype: int
		:Example:

			>>> hex(bv.get_next_data_var_start_after(0x10000000))
			'0x1000003cL'
			>>> bv.get_data_var_at(0x1000003c)
			<var 0x1000003c: int32_t>
			>>>
		"""
		return core.BNGetNextDataVariableStartAfterAddress(self.handle, addr)

	def get_previous_function_start_before(self, addr):
		"""
		``get_previous_function_start_before`` returns the virtual address of the Function that occurs prior to the
		virtual address provided

		:param int addr: the virtual address to start looking from.
		:return: the virtual address of the previous Function
		:rtype: int
		:Example:

			>>> hex(bv.entry_point)
			'0x1000149fL'
			>>> hex(bv.get_next_function_start_after(bv.entry_point))
			'0x100015e5L'
			>>> hex(bv.get_previous_function_start_before(0x100015e5))
			'0x1000149fL'
			>>>
		"""
		return core.BNGetPreviousFunctionStartBeforeAddress(self.handle, addr)

	def get_previous_basic_block_start_before(self, addr):
		"""
		``get_previous_basic_block_start_before`` returns the virtual address of the BasicBlock that occurs prior to the
		provided virtual address

		:param int addr: the virtual address to start looking from.
		:return: the virtual address of the previous BasicBlock
		:rtype: int
		:Example:

			>>> hex(bv.entry_point)
			'0x1000149fL'
			>>> hex(bv.get_next_basic_block_start_after(bv.entry_point))
			'0x100014a8L'
			>>> hex(bv.get_previous_basic_block_start_before(0x100014a8))
			'0x1000149fL'
			>>>
		"""
		return core.BNGetPreviousBasicBlockStartBeforeAddress(self.handle, addr)

	def get_previous_basic_block_end_before(self, addr):
		"""
		``get_previous_basic_block_end_before``

		:param int addr: the virtual address to start looking from.
		:return: the virtual address of the previous BasicBlock end
		:rtype: int
		:Example:
			>>> hex(bv.entry_point)
			'0x1000149fL'
			>>> hex(bv.get_next_basic_block_start_after(bv.entry_point))
			'0x100014a8L'
			>>> hex(bv.get_previous_basic_block_end_before(0x100014a8))
			'0x100014a8L'
		"""
		return core.BNGetPreviousBasicBlockEndBeforeAddress(self.handle, addr)

	def get_previous_data_before(self, addr):
		"""
		``get_previous_data_before``

		:param int addr: the virtual address to start looking from.
		:return: the virtual address of the previous data (non-code) byte
		:rtype: int
		:Example:

			>>> hex(bv.get_previous_data_before(0x1000001))
			'0x1000000L'
			>>>
		"""
		return core.BNGetPreviousDataBeforeAddress(self.handle, addr)

	def get_previous_data_var_before(self, addr):
		"""
		``get_previous_data_var_before`` retrieves the previous :py:Class:`DataVariable`, or None.

		:param int addr: the virtual address to start looking from.
		:return: the previous :py:Class:`DataVariable`
		:rtype: DataVariable
		:Example:

			>>> bv.get_previous_data_var_before(0x1000003c)
			<var 0x10000000: int16_t>
			>>>
		"""
		prev_data_var_start = core.BNGetPreviousDataVariableStartBeforeAddress(self.handle, addr)
		if prev_data_var_start == addr:
			return None
		var = core.BNDataVariable()
		if not core.BNGetDataVariableAtAddress(self.handle, prev_data_var_start, var):
			return None
		return DataVariable.from_core_struct(var, self)

	def get_previous_data_var_start_before(self, addr):
		"""
		``get_previous_data_var_start_before``

		:param int addr: the virtual address to start looking from.
		:return: the virtual address of the previous :py:Class:`DataVariable`
		:rtype: int
		:Example:

			>>> hex(bv.get_previous_data_var_start_before(0x1000003c))
			'0x10000000L'
			>>> bv.get_data_var_at(0x10000000)
			<var 0x10000000: int16_t>
			>>>
		"""
		return core.BNGetPreviousDataVariableStartBeforeAddress(self.handle, addr)

	def get_linear_disassembly_position_at(self, addr, settings=None):
		"""
		``get_linear_disassembly_position_at`` instantiates a :py:class:`LinearViewCursor <binaryninja.lineardisassembly.LinearViewCursor>` object for use in
		:py:meth:`get_previous_linear_disassembly_lines` or :py:meth:`get_next_linear_disassembly_lines`.

		:param int addr: virtual address of linear disassembly position
		:param DisassemblySettings settings: an instantiated :py:class:`DisassemblySettings` object, defaults to None which will use default settings
		:return: An instantiated :py:class:`LinearViewCursor` object for the provided virtual address
		:rtype: LinearViewCursor
		:Example:

			>>> settings = DisassemblySettings()
			>>> pos = bv.get_linear_disassembly_position_at(0x1000149f, settings)
			>>> lines = bv.get_previous_linear_disassembly_lines(pos)
			>>> lines
			[<0x1000149a: pop     esi>, <0x1000149b: pop     ebp>,
			<0x1000149c: retn    0xc>, <0x1000149f: >]
		"""
		pos = lineardisassembly.LinearViewCursor(lineardisassembly.LinearViewObject.disassembly(self, settings))
		pos.seek_to_address(addr)
		return pos

	def get_previous_linear_disassembly_lines(self, pos):
		"""
		``get_previous_linear_disassembly_lines`` retrieves a list of :py:class:`LinearDisassemblyLine` objects for the
		previous disassembly lines, and updates the LinearViewCursor passed in. This function can be called
		repeatedly to get more lines of linear disassembly.

		:param LinearViewCursor pos: Position to start retrieving linear disassembly lines from
		:return: a list of :py:class:`LinearDisassemblyLine` objects for the previous lines.
		:Example:

			>>> settings = DisassemblySettings()
			>>> pos = bv.get_linear_disassembly_position_at(0x1000149a, settings)
			>>> bv.get_previous_linear_disassembly_lines(pos)
			[<0x10001488: push    dword [ebp+0x10 {arg_c}]>, ... , <0x1000149a: >]
			>>> bv.get_previous_linear_disassembly_lines(pos)
			[<0x10001483: xor     eax, eax  {0x0}>, ... , <0x10001488: >]
		"""
		result = []
		while len(result) == 0:
			if not pos.previous():
				return result
			result = pos.lines
		return result

	def get_next_linear_disassembly_lines(self, pos):
		"""
		``get_next_linear_disassembly_lines`` retrieves a list of :py:class:`LinearDisassemblyLine` objects for the
		next disassembly lines, and updates the LinearViewCursor passed in. This function can be called
		repeatedly to get more lines of linear disassembly.

		:param LinearViewCursor pos: Position to start retrieving linear disassembly lines from
		:return: a list of :py:class:`LinearDisassemblyLine` objects for the next lines.
		:Example:

			>>> settings = DisassemblySettings()
			>>> pos = bv.get_linear_disassembly_position_at(0x10001483, settings)
			>>> bv.get_next_linear_disassembly_lines(pos)
			[<0x10001483: xor     eax, eax  {0x0}>, <0x10001485: inc     eax  {0x1}>, ... , <0x10001488: >]
			>>> bv.get_next_linear_disassembly_lines(pos)
			[<0x10001488: push    dword [ebp+0x10 {arg_c}]>, ... , <0x1000149a: >]
			>>>
		"""
		result = []
		while len(result) == 0:
			result = pos.lines
			if not pos.next():
				return result
		return result

	def get_linear_disassembly(self, settings=None):
		"""
		``get_linear_disassembly`` gets an iterator for all lines in the linear disassembly of the view for the given
		disassembly settings.

		.. note:: linear_disassembly doesn't just return disassembly it will return a single line from the linear view,\
		 and thus will contain both data views, and disassembly.

		:param DisassemblySettings settings: instance specifying the desired output formatting. Defaults to None which will use default settings.
		:return: An iterator containing formatted disassembly lines.
		:rtype: LinearDisassemblyIterator
		:Example:

			>>> settings = DisassemblySettings()
			>>> lines = bv.get_linear_disassembly(settings)
			>>> for line in lines:
			...  print(line)
			...  break
			...
			cf fa ed fe 07 00 00 01  ........
		"""
		class LinearDisassemblyIterator:
			def __init__(self, view, settings):
				self._view = view
				self._settings = settings

			def __iter__(self):
				pos = lineardisassembly.LinearViewCursor(lineardisassembly.LinearViewObject.disassembly(
					self.view, self.settings))
				while True:
					lines = self._view.get_next_linear_disassembly_lines(pos)
					if len(lines) == 0:
						break
					for line in lines:
						yield line

			@property
			def view(self):
				return self._view

			@view.setter
			def view(self, value):
				self._view = value

			@property
			def settings(self):
				return self._settings

			@settings.setter
			def settings(self, value):
				self._settings = value


		return iter(LinearDisassemblyIterator(self, settings))

	def parse_type_string(self, text):
		"""
		``parse_type_string`` parses string containing C into a single type :py:Class:`Type`.
		In contrast to the :py:'platform

		:param str text: C source code string of type to create
		:return: A tuple of a :py:Class:`Type` and type name
		:rtype: tuple(Type, QualifiedName)
		:Example:

			>>> bv.parse_type_string("int foo")
			(<type: int32_t>, 'foo')
			>>>
		"""
		if not isinstance(text, str):
			raise AttributeError("Source must be a string")
		result = core.BNQualifiedNameAndType()
		errors = ctypes.c_char_p()
		type_list = core.BNQualifiedNameList()
		type_list.count = 0
		if not core.BNParseTypeString(self.handle, text, result, errors, type_list):
			assert errors.value is not None, "core.BNParseTypeString returned 'errors' set to None"
			error_str = errors.value.decode("utf-8")
			core.free_string(errors)
			raise SyntaxError(error_str)
		type_obj = _types.Type.create(core.BNNewTypeReference(result.type), platform = self.platform)
		name = _types.QualifiedName._from_core_struct(result.name)
		core.BNFreeQualifiedNameAndType(result)
		return type_obj, name

	def parse_types_from_string(self, text):
		"""
		``parse_types_from_string`` parses string containing C into a :py:Class:`TypeParserResult` objects. This API
		unlike the :py:meth:`Platform.parse_types_from_source` allows the reference of types already defined
		in the BinaryView.

		:param str text: C source code string of types, variables, and function types, to create
		:return: :py:class:`TypeParserResult` (a SyntaxError is thrown on parse error)
		:rtype: TypeParserResult
		:Example:

			>>> bv.parse_types_from_string('int foo;\\nint bar(int x);\\nstruct bas{int x,y;};\\n')
			({types: {'bas': <type: struct bas>}, variables: {'foo': <type: int32_t>}, functions:{'bar':
			<type: int32_t(int32_t x)>}}, '')
			>>>
		"""
		if not isinstance(text, str):
			raise AttributeError("Source must be a string")

		parse = core.BNTypeParserResult()
		errors = ctypes.c_char_p()
		type_list = core.BNQualifiedNameList()
		type_list.count = 0
		if not core.BNParseTypesString(self.handle, text, parse, errors, type_list):
			assert errors.value is not None, "core.BNParseTypesString returned errors set to None"
			error_str = errors.value.decode("utf-8")
			core.free_string(errors)
			raise SyntaxError(error_str)

		type_dict:Mapping[_types.QualifiedName, _types.Type] = {}
		variables:Mapping[_types.QualifiedName, _types.Type] = {}
		functions:Mapping[_types.QualifiedName, _types.Type] = {}
		for i in range(0, parse.typeCount):
			name = _types.QualifiedName._from_core_struct(parse.types[i].name)
			type_dict[name] = _types.Type.create(core.BNNewTypeReference(parse.types[i].type), platform = self.platform)
		for i in range(0, parse.variableCount):
			name = _types.QualifiedName._from_core_struct(parse.variables[i].name)
			variables[name] = _types.Type.create(core.BNNewTypeReference(parse.variables[i].type), platform = self.platform)
		for i in range(0, parse.functionCount):
			name = _types.QualifiedName._from_core_struct(parse.functions[i].name)
			functions[name] = _types.Type.create(core.BNNewTypeReference(parse.functions[i].type), platform = self.platform)
		core.BNFreeTypeParserResult(parse)
		return _types.TypeParserResult(type_dict, variables, functions)

	def parse_possiblevalueset(self, value, state, here=0):
		"""
		Evaluates a string representation of a PossibleValueSet into an instance of the ``PossibleValueSet`` value.

		.. note:: Values are evaluated based on the rules as specified for :py:meth:`parse_expression` API. This implies that a ``ConstantValue [0x4000].d`` can be provided given that 4 bytes can be read at ``0x4000``. All constants are considered to be in hexadecimal form by default.

		The parser uses the following rules:
			- ConstantValue - ``<value>``
			- ConstantPointerValue - ``<value>``
			- StackFrameOffset - ``<value>``
			- SignedRangeValue - ``<value>:<value>:<value>{,<value>:<value>:<value>}*`` (Multiple ValueRanges can be provided by separating them by commas)
			- UnsignedRangeValue - ``<value>:<value>:<value>{,<value>:<value>:<value>}*`` (Multiple ValueRanges can be provided by separating them by commas)
			- InSetOfValues - ``<value>{,<value>}*``
			- NotInSetOfValues - ``<value>{,<value>}*``

		:param str value: PossibleValueSet value to be parsed
		:param RegisterValueType state: State for which the value is to be parsed
		:param int here: (optional) Base address for relative expressions, defaults to zero
		:rtype: PossibleValueSet
		:Example:

			>>> psv_c = bv.parse_possiblevalueset("400", RegisterValueType.ConstantValue)
			>>> psv_c
			<const 0x400>
			>>> psv_ur = bv.parse_possiblevalueset("1:10:1", RegisterValueType.UnsignedRangeValue)
			>>> psv_ur
			<unsigned ranges: [<range: 0x1 to 0x10>]>
			>>> psv_is = bv.parse_possiblevalueset("1,2,3", RegisterValueType.InSetOfValues)
			>>> psv_is
			<in set([0x1, 0x2, 0x3])>
			>>>
		"""
		result = core.BNPossibleValueSet()
		errors = ctypes.c_char_p()
		if value == None:
			value = ''
		if not core.BNParsePossibleValueSet(self.handle, value, state, result, here, errors):
			if errors:
				assert errors.value is not None, "core.BNParseTypesString returned errors set to None"
				error_str = errors.value.decode("utf-8")
			else:
				error_str = "Error parsing specified PossibleValueSet"
			core.BNFreePossibleValueSet(result)
			core.free_string(errors)
			raise ValueError(error_str)
		return variable.PossibleValueSet(self.arch, result)

	def get_type_by_name(self, name:'types.QualifiedName') -> Optional['types.Type']:
		"""
		``get_type_by_name`` returns the defined type whose name corresponds with the provided ``name``

		:param QualifiedName name: Type name to lookup
		:return: A :py:Class:`Type` or None if the type does not exist
		:rtype: Type or None
		:Example:

			>>> type, name = bv.parse_type_string("int foo")
			>>> bv.define_user_type(name, type)
			>>> bv.get_type_by_name(name)
			<type: int32_t>
			>>>
		"""
		name = _types.QualifiedName(name)._get_core_struct()
		obj = core.BNGetAnalysisTypeByName(self.handle, name)
		if not obj:
			return None
		return _types.Type.create(obj, platform = self.platform)

	def get_type_by_id(self, id):
		"""
		``get_type_by_id`` returns the defined type whose unique identifier corresponds with the provided ``id``

		:param str id: Unique identifier to lookup
		:return: A :py:Class:`Type` or None if the type does not exist
		:rtype: Type or None
		:Example:

			>>> type, name = bv.parse_type_string("int foo")
			>>> type_id = Type.generate_auto_type_id("source", name)
			>>> bv.define_type(type_id, name, type)
			>>> bv.get_type_by_id(type_id)
			<type: int32_t>
			>>>
		"""
		obj = core.BNGetAnalysisTypeById(self.handle, id)
		if not obj:
			return None
		return _types.Type.create(obj, platform = self.platform)

	def get_type_name_by_id(self, id):
		"""
		``get_type_name_by_id`` returns the defined type name whose unique identifier corresponds with the provided ``id``

		:param str id: Unique identifier to lookup
		:return: A QualifiedName or None if the type does not exist
		:rtype: QualifiedName or None
		:Example:

			>>> type, name = bv.parse_type_string("int foo")
			>>> type_id = Type.generate_auto_type_id("source", name)
			>>> bv.define_type(type_id, name, type)
			'foo'
			>>> bv.get_type_name_by_id(type_id)
			'foo'
			>>>
		"""
		name = core.BNGetAnalysisTypeNameById(self.handle, id)
		result = _types.QualifiedName._from_core_struct(name)
		core.BNFreeQualifiedName(name)
		if len(result) == 0:
			return None
		return result

	def get_type_id(self, name):
		"""
		``get_type_id`` returns the unique identifier of the defined type whose name corresponds with the
		provided ``name``

		:param QualifiedName name: Type name to lookup
		:return: The unique identifier of the type
		:rtype: str
		:Example:

			>>> type, name = bv.parse_type_string("int foo")
			>>> type_id = Type.generate_auto_type_id("source", name)
			>>> registered_name = bv.define_type(type_id, name, type)
			>>> bv.get_type_id(registered_name) == type_id
			True
			>>>
		"""
		name = _types.QualifiedName(name)._get_core_struct()
		return core.BNGetAnalysisTypeId(self.handle, name)

	def add_type_library(self, lib):
		"""
		``add_type_library`` make the contents of a type library available for type/import resolution

		:param TypeLibrary lib: library to register with the view
		:rtype: None
		"""
		if not isinstance(lib, typelibrary.TypeLibrary):
			raise ValueError("must pass in a TypeLibrary object")
		core.BNAddBinaryViewTypeLibrary(self.handle, lib.handle)

	def get_type_library(self, name):
		"""
		``get_type_library`` returns the TypeLibrary

		:param str name: Library name to lookup
		:return: The Type Library object, if any
		:rtype: TypeLibrary or None
		:Example:

		"""
		handle = core.BNGetBinaryViewTypeLibrary(self.handle, name)
		if handle is None:
			return None
		return typelibrary.TypeLibrary(handle)

	def is_type_auto_defined(self, name):
		"""
		``is_type_auto_defined`` queries the user type list of name. If name is not in the *user* type list then the name
		is considered an *auto* type.

		:param QualifiedName name: Name of type to query
		:return: True if the type is not a *user* type. False if the type is a *user* type.
		:Example:
			>>> bv.is_type_auto_defined("foo")
			True
			>>> bv.define_user_type("foo", bv.parse_type_string("struct {int x,y;}")[0])
			>>> bv.is_type_auto_defined("foo")
			False
			>>>
		"""
		name = _types.QualifiedName(name)._get_core_struct()
		return core.BNIsAnalysisTypeAutoDefined(self.handle, name)

	def define_type(self, type_id:str, default_name:'_types.QualifiedName', type_obj:'_types.Type'):
		"""
		``define_type`` registers a :py:Class:`Type` ``type_obj`` of the given ``name`` in the global list of types for
		the current :py:Class:`BinaryView`. This method should only be used for automatically generated types.

		:param str type_id: Unique identifier for the automatically generated type
		:param QualifiedName default_name: Name of the type to be registered
		:param Type type_obj: Type object to be registered
		:return: Registered name of the type. May not be the same as the requested name if the user has renamed types.
		:rtype: QualifiedName
		:Example:

			>>> type, name = bv.parse_type_string("int foo")
			>>> registered_name = bv.define_type(Type.generate_auto_type_id("source", name), name, type)
			>>> bv.get_type_by_name(registered_name)
			<type: int32_t>
		"""
		name = _types.QualifiedName(default_name)._get_core_struct()
		reg_name = core.BNDefineAnalysisType(self.handle, type_id, name, type_obj.handle)
		result = _types.QualifiedName._from_core_struct(reg_name)
		core.BNFreeQualifiedName(reg_name)
		return result

	def define_user_type(self, name:Union[str,'_types.QualifiedName'], type_obj:'_types.Type') -> None:
		"""
		``define_user_type`` registers a :py:Class:`Type` ``type_obj`` of the given ``name`` in the global list of user
		types for the current :py:Class:`BinaryView`.

		:param QualifiedName name: Name of the user type to be registered
		:param Type type_obj: Type object to be registered
		:rtype: None
		:Example:

			>>> type, name = bv.parse_type_string("int foo")
			>>> bv.define_user_type(name, type)
			>>> bv.get_type_by_name(name)
			<type: int32_t>
		"""
		if isinstance(name, str):
			name = _types.QualifiedName(name)
		core.BNDefineUserAnalysisType(self.handle, name._get_core_struct(), type_obj.handle)

	def undefine_type(self, type_id):
		"""
		``undefine_type`` removes a :py:Class:`Type` from the global list of types for the current :py:Class:`BinaryView`

		:param str type_id: Unique identifier of type to be undefined
		:rtype: None
		:Example:

			>>> type, name = bv.parse_type_string("int foo")
			>>> type_id = Type.generate_auto_type_id("source", name)
			>>> bv.define_type(type_id, name, type)
			>>> bv.get_type_by_name(name)
			<type: int32_t>
			>>> bv.undefine_type(type_id)
			>>> bv.get_type_by_name(name)
			>>>
		"""
		core.BNUndefineAnalysisType(self.handle, type_id)

	def undefine_user_type(self, name):
		"""
		``undefine_user_type`` removes a :py:Class:`Type` from the global list of user types for the current
		:py:Class:`BinaryView`

		:param QualifiedName name: Name of user type to be undefined
		:rtype: None
		:Example:

			>>> type, name = bv.parse_type_string("int foo")
			>>> bv.define_user_type(name, type)
			>>> bv.get_type_by_name(name)
			<type: int32_t>
			>>> bv.undefine_user_type(name)
			>>> bv.get_type_by_name(name)
			>>>
		"""
		name = _types.QualifiedName(name)._get_core_struct()
		core.BNUndefineUserAnalysisType(self.handle, name)

	def rename_type(self, old_name, new_name):
		"""
		``rename_type`` renames a type in the global list of types for the current :py:Class:`BinaryView`

		:param QualifiedName old_name: Existing name of type to be renamed
		:param QualifiedName new_name: New name of type to be renamed
		:rtype: None
		:Example:

			>>> type, name = bv.parse_type_string("int foo")
			>>> bv.define_user_type(name, type)
			>>> bv.get_type_by_name("foo")
			<type: int32_t>
			>>> bv.rename_type("foo", "bar")
			>>> bv.get_type_by_name("bar")
			<type: int32_t>
			>>>
		"""
		old_name = _types.QualifiedName(old_name)._get_core_struct()
		new_name = _types.QualifiedName(new_name)._get_core_struct()
		core.BNRenameAnalysisType(self.handle, old_name, new_name)

	def import_library_type(self, name, lib = None):
		"""
		``import_library_type`` recursively imports a type from the specified type library, or, if
		no library was explicitly provided, the first type library associated with the current :py:Class:`BinaryView`
		that provides the name requested.

		This may have the impact of loading other type libraries as dependencies on other type libraries are lazily resolved
		when references to types provided by them are first encountered.

		Note that the name actually inserted into the view may not match the name as it exists in the type library in
		the event of a name conflict. To aid in this, the :py:Class:`Type` object returned is a `NamedTypeReference` to
		the deconflicted name used.

		:param QualifiedName name:
		:param TypeLibrary lib:
		:return: a `NamedTypeReference` to the type, taking into account any renaming performed
		:rtype: Type
		"""
		if not isinstance(name, _types.QualifiedName):
			name = _types.QualifiedName(name)
		handle = core.BNBinaryViewImportTypeLibraryType(self.handle, None if lib is None else lib.handle, name._get_core_struct())
		if handle is None:
			return None
		return _types.Type.create(handle, platform = self.platform)

	def import_library_object(self, name, lib = None):
		"""
		``import_library_object`` recursively imports an object from the specified type library, or, if
		no library was explicitly provided, the first type library associated with the current :py:Class:`BinaryView`
		that provides the name requested.

		This may have the impact of loading other type libraries as dependencies on other type libraries are lazily resolved
		when references to types provided by them are first encountered.

		:param QualifiedName name:
		:param TypeLibrary lib:
		:return: the object type, with any interior `NamedTypeReferences` renamed as necessary to be appropriate for the current view
		:rtype: Type
		"""
		if not isinstance(name, _types.QualifiedName):
			name = _types.QualifiedName(name)
		handle = core.BNBinaryViewImportTypeLibraryObject(self.handle, None if lib is None else lib.handle, name._get_core_struct())
		if handle is None:
			return None
		return _types.Type.create(handle, platform = self.platform)

	def export_type_to_library(self, lib, name, type_obj):
		"""
		``export_type_to_library`` recursively exports ``type_obj`` into ``lib`` as a type with name ``name``

		As other referenced types are encountered, they are either copied into the destination type library or
		else the type library that provided the referenced type is added as a dependency for the destination library.

		:param TypeLibrary lib:
		:param QualifiedName name:
		:param Type type_obj:
		:rtype: None
		"""
		if not isinstance(name, _types.QualifiedName):
			name = _types.QualifiedName(name)
		if not isinstance(lib, typelibrary.TypeLibrary):
			raise ValueError("lib must be a TypeLibrary object")
		if not isinstance(type_obj, _types.Type):
			raise ValueError("type_obj must be a Type object")
		core.BNBinaryViewExportTypeToTypeLibrary(self.handle, lib.handle, name._get_core_struct(), type_obj.handle)

	def export_object_to_library(self, lib, name, type_obj):
		"""
		``export_object_to_library`` recursively exports ``type_obj`` into ``lib`` as an object with name ``name``

		As other referenced types are encountered, they are either copied into the destination type library or
		else the type library that provided the referenced type is added as a dependency for the destination library.

		:param TypeLibrary lib:
		:param QualifiedName name:
		:param Type type_obj:
		:rtype: None
		"""
		if not isinstance(name, _types.QualifiedName):
			name = _types.QualifiedName(name)
		if not isinstance(lib, typelibrary.TypeLibrary):
			raise ValueError("lib must be a TypeLibrary object")
		if not isinstance(type_obj, _types.Type):
			raise ValueError("type_obj must be a Type object")
		core.BNBinaryViewExportObjectToTypeLibrary(self.handle, lib.handle, name._get_core_struct(), type_obj.handle)

	def register_platform_types(self, platform):
		"""
		``register_platform_types`` ensures that the platform-specific types for a :py:Class:`Platform` are available
		for the current :py:Class:`BinaryView`. This is automatically performed when adding a new function or setting
		the default platform.

		:param Platform platform: Platform containing types to be registered
		:rtype: None
		:Example:

			>>> platform = Platform["linux-x86"]
			>>> bv.register_platform_types(platform)
			>>>
		"""
		core.BNRegisterPlatformTypes(self.handle, platform.handle)

	def find_next_data(self, start, data, flags=FindFlag.FindCaseSensitive):
		"""
		``find_next_data`` searches for the bytes ``data`` starting at the virtual address ``start`` until the end of the BinaryView.

		:param int start: virtual address to start searching from.
		:param Union[bytes, bytearray, str] data: data to search for
		:param FindFlag flags: (optional) defaults to case-insensitive data search

			==================== ============================
			FindFlag             Description
			==================== ============================
			FindCaseSensitive    Case-sensitive search
			FindCaseInsensitive  Case-insensitive search
			==================== ============================
		"""
		if not isinstance(data, bytes):
			raise TypeError("Must be bytes, bytearray, or str")
		else:
			buf = databuffer.DataBuffer(data)
		result = ctypes.c_ulonglong()
		if not core.BNFindNextData(self.handle, start, buf.handle, result, flags):
			return None
		return result.value


	def find_next_text(self, start, text, settings=None, flags=FindFlag.FindCaseSensitive,\
		graph_type = FunctionGraphType.NormalFunctionGraph):
		"""
		``find_next_text`` searches for string ``text`` occurring in the linear view output starting at the virtual
		address ``start`` until the end of the BinaryView.

		:param int start: virtual address to start searching from.
		:param str text: text to search for
		:param DisassemblySettings settings: disassembly settings
		:param FindFlag flags: (optional) defaults to case-insensitive data search

			==================== ============================
			FindFlag             Description
			==================== ============================
			FindCaseSensitive    Case-sensitive search
			FindCaseInsensitive  Case-insensitive search
			==================== ============================
		:param FunctionGraphType graph_type: the IL to search within
		"""
		if not isinstance(text, str):
			raise TypeError("text parameter is not str type")
		if settings is None:
			settings = _function.DisassemblySettings()
		if not isinstance(settings, _function.DisassemblySettings):
			raise TypeError("settings parameter is not DisassemblySettings type")

		result = ctypes.c_ulonglong()
		if not core.BNFindNextText(self.handle, start, text, result, settings.handle, flags,\
			graph_type):
			return None
		return result.value

	def find_next_constant(self, start, constant, settings=None,\
		graph_type = FunctionGraphType.NormalFunctionGraph):
		"""
		``find_next_constant`` searches for integer constant ``constant`` occurring in the linear view output starting at the virtual
		address ``start`` until the end of the BinaryView.

		:param int start: virtual address to start searching from.
		:param int constant: constant to search for
		:param DisassemblySettings settings: disassembly settings
		:param FunctionGraphType graph_type: the IL to search within
		"""
		if not isinstance(constant, int):
			raise TypeError("constant parameter is not integral type")
		if settings is None:
			settings = _function.DisassemblySettings()
		if not isinstance(settings, _function.DisassemblySettings):
			raise TypeError("settings parameter is not DisassemblySettings type")

		result = ctypes.c_ulonglong()
		if not core.BNFindNextConstant(self.handle, start, constant, result, settings.handle,\
			graph_type):
			return None
		return result.value

	class QueueGenerator:
		def __init__(self, t, results):
			self.thread = t
			self.results = results
			t.start()

		def __iter__(self):
			return self

		def __next__(self):
			while True:
				if not self.results.empty():
					return self.results.get()

				if (not self.thread.is_alive()) and self.results.empty():
					raise StopIteration

	def find_all_data(self, start, end, data, flags = FindFlag.FindCaseSensitive,\
		progress_func = None, match_callback = None):
		"""
		``find_all_data`` searches for the bytes ``data`` starting at the virtual address ``start``
		until the virtual address ``end``. Once a match is found, the ``match_callback`` is called.

		:param int start: virtual address to start searching from.
		:param int end: virtual address to end the search.
		:param Union[bytes, bytearray, str] data: data to search for
		:param FindFlag flags: (optional) defaults to case-insensitive data search

			==================== ============================
			FindFlag             Description
			==================== ============================
			FindCaseSensitive    Case-sensitive search
			FindCaseInsensitive  Case-insensitive search
			==================== ============================
		:param callback progress_func: optional function to be called with the current progress
		and total count. This function should return a boolean value that decides whether the
		search should continue or stop
		:param callback match_callback: function that gets called when a match is found. The
		callback takes two parameters, i.e., the address of the match, and the actual DataBuffer
		that satisfies the search. If this parameter is None, this function becomes a generator
		and yields a tuple of the matching address and the matched DataBuffer. This function
		can return a boolean value that decides whether the search should continue or stop
		:rtype bool: whether any (one or more) match is found for the search
		"""
		if not isinstance(data, bytes):
			raise TypeError("data parameter must be bytes, bytearray, or str")
		else:
			buf = databuffer.DataBuffer(data)
		if not isinstance(flags, FindFlag):
			raise TypeError('flag parameter must have type FindFlag')

		if progress_func:
			progress_func_obj = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
				ctypes.c_ulonglong, ctypes.c_ulonglong)\
				(lambda ctxt, cur, total: progress_func(cur, total))
		else:
			progress_func_obj = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
				ctypes.c_ulonglong, ctypes.c_ulonglong)\
				(lambda ctxt, cur, total: True)

		if match_callback:
			# the `not match_callback(...) is False` tolerates the users who forget to return
			# `True` from inside the callback
			match_callback_obj = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
				ctypes.c_ulonglong, ctypes.POINTER(core.BNDataBuffer))\
				(lambda ctxt, addr, match: not match_callback(addr, databuffer.DataBuffer(handle = match)) is False)
			return core.BNFindAllDataWithProgress(self.handle, start, end, buf.handle, flags,
				None, progress_func_obj, None, match_callback_obj)
		else:
			results = queue.Queue()
			match_callback_obj = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
				ctypes.c_ulonglong,	ctypes.POINTER(core.BNDataBuffer))\
				(lambda ctxt, addr, match:
					results.put((addr, databuffer.DataBuffer(handle = match))) or True)

			t = threading.Thread(target = lambda: core.BNFindAllDataWithProgress(self.handle,
				start, end, buf.handle, flags, None, progress_func_obj, None, match_callback_obj))

			return self.QueueGenerator(t, results)

	def _LinearDisassemblyLine_convertor(self, lines):
		func = None
		block = None
		line = lines[0]
		if line.function:
			func = _function.Function(self, core.BNNewFunctionReference(line.function))
		if line.block:
			block_handle = core.BNNewBasicBlockReference(line.block)
			assert block_handle is not None, "core.BNNewBasicBlockReference returned None"
			block = basicblock.BasicBlock(block_handle, self)
		color = highlight.HighlightColor._from_core_struct(line.contents.highlight)
		addr = line.contents.addr
		tokens = _function.InstructionTextToken._from_core_struct(line.contents.tokens, line.contents.count)
		contents = _function.DisassemblyTextLine(tokens, addr, color = color)
		return lineardisassembly.LinearDisassemblyLine(line.type, func, block, contents)

	def find_all_text(self, start, end, text, settings = None,
		flags = FindFlag.FindCaseSensitive,
		graph_type = FunctionGraphType.NormalFunctionGraph, progress_func = None,
		match_callback = None):
		"""
		``find_all_text`` searches for string ``text`` occurring in the linear view output starting
		at the virtual address ``start`` until the virtual address ``end``. Once a match is found,
		the ``match_callback`` is called.

		:param int start: virtual address to start searching from.
		:param int end: virtual address to end the search.
		:param str text: text to search for
		:param DisassemblySettings settings: DisassemblySettings object used to render the text
		to be searched
		:param FindFlag flags: (optional) defaults to case-insensitive data search

			==================== ============================
			FindFlag             Description
			==================== ============================
			FindCaseSensitive    Case-sensitive search
			FindCaseInsensitive  Case-insensitive search
			==================== ============================
		:param FunctionGraphType graph_type: the IL to search within
		:param callback progress_func: optional function to be called with the current progress
		and total count. This function should return a boolean value that decides whether the
		search should continue or stop
		:param callback match_callback: function that gets called when a match is found. The
		callback takes three parameters, i.e., the address of the match, and the actual string
		that satisfies the search, and the LinearDisassemblyLine that contains the matching
		line. If this parameter is None, this function becomes a generator
		and yields a tuple of the matching address, the matched string, and the matching
		LinearDisassemblyLine. This function can return a boolean value that decides whether
		the search should continue or stop
		:rtype bool: whether any (one or more) match is found for the search
		"""
		if not isinstance(text, str):
			raise TypeError("text parameter is not str type")
		if settings is None:
			settings = _function.DisassemblySettings()
		if not isinstance(settings, _function.DisassemblySettings):
			raise TypeError("settings parameter is not DisassemblySettings type")
		if not isinstance(flags, FindFlag):
			raise TypeError('flag parameter must have type FindFlag')

		if progress_func:
			progress_func_obj = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
				ctypes.c_ulonglong, ctypes.c_ulonglong)\
				(lambda ctxt, cur, total: progress_func(cur, total))
		else:
			progress_func_obj = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
				ctypes.c_ulonglong, ctypes.c_ulonglong)\
				(lambda ctxt, cur, total: True)

		if match_callback:
			# The reason we use `not match_callback(...) is False` is the user tends to happily
			# deal with the returned data, but forget to return True at the end of the callback.
			# Then only the first result will be returned.
			match_callback_obj = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
				ctypes.c_ulonglong, ctypes.c_char_p,
				ctypes.POINTER(core.BNLinearDisassemblyLine))\
				(lambda ctxt, addr, match, line: not match_callback(addr, match,\
					self._LinearDisassemblyLine_convertor(line)) is False)

			return core.BNFindAllTextWithProgress(self.handle, start, end, text,
				settings.handle, flags, graph_type, None, progress_func_obj, None, match_callback_obj)
		else:
			results = queue.Queue()
			match_callback_obj = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
				ctypes.c_ulonglong, ctypes.c_char_p,
				ctypes.POINTER(core.BNLinearDisassemblyLine))\
				(lambda ctxt, addr, match, line: results.put((addr, match,\
					self._LinearDisassemblyLine_convertor(line))) or True)

			t = threading.Thread(target = lambda: core.BNFindAllTextWithProgress(self.handle,
				start, end, text, settings.handle, flags, graph_type, None, progress_func_obj, None,
				match_callback_obj))

			return self.QueueGenerator(t, results)

	def find_all_constant(self, start, end, constant, settings = None,
		graph_type = FunctionGraphType.NormalFunctionGraph, progress_func = None,
		match_callback = None):
		"""
		``find_all_constant`` searches for the integer constant ``constant`` starting at the
		virtual address ``start`` until the virtual address ``end``. Once a match is found,
		the ``match_callback`` is called.

		:param int start: virtual address to start searching from.
		:param int end: virtual address to end the search.
		:param int constant: constant to search for
		:param DisassemblySettings settings: DisassemblySettings object used to render the text
		to be searched
		:param FunctionGraphType graph_type: the IL to search within
		:param callback progress_func: optional function to be called with the current progress
		and total count. This function should return a boolean value that decides whether the
		search should continue or stop
		:param callback match_callback: function that gets called when a match is found. The
		callback takes two parameters, i.e., the address of the match, and the
		LinearDisassemblyLine that contains the matching line. If this parameter is None,
		this function becomes a generator and yields the the matching address and the
		matching LinearDisassemblyLine. This function can return a boolean value that
		decides whether the search should continue or stop
		:rtype bool: whether any (one or more) match is found for the search
		"""
		if not isinstance(constant, int):
			raise TypeError("constant parameter is not integral type")
		if settings is None:
			settings = _function.DisassemblySettings()
		if not isinstance(settings, _function.DisassemblySettings):
			raise TypeError("settings parameter is not DisassemblySettings type")

		if progress_func:
			progress_func_obj = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
				ctypes.c_ulonglong, ctypes.c_ulonglong)\
				(lambda ctxt, cur, total: progress_func(cur, total))
		else:
			progress_func_obj = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
				ctypes.c_ulonglong, ctypes.c_ulonglong)\
				(lambda ctxt, cur, total: True)

		if match_callback:
			match_callback_obj = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,\
				ctypes.c_ulonglong, ctypes.POINTER(core.BNLinearDisassemblyLine))\
				(lambda ctxt, addr, line: not match_callback(addr,\
					self._LinearDisassemblyLine_convertor(line)) is False)

			return core.BNFindAllConstantWithProgress(self.handle, start, end, constant,
				settings.handle, graph_type, None, progress_func_obj, None, match_callback_obj)
		else:
			results = queue.Queue()
			match_callback_obj = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,\
				ctypes.c_ulonglong, ctypes.POINTER(core.BNLinearDisassemblyLine))\
				(lambda ctxt, addr, line: results.put((addr,\
					self._LinearDisassemblyLine_convertor(line))) or True)

			t = threading.Thread(target = lambda: core.BNFindAllConstantWithProgress(self.handle,
				start, end, constant, settings.handle, graph_type, None, progress_func_obj, None,\
				match_callback_obj))

			return self.QueueGenerator(t, results)

	def reanalyze(self):
		"""
		``reanalyze`` causes all functions to be reanalyzed. This function does not wait for the analysis to finish.

		:rtype: None
		"""
		core.BNReanalyzeAllFunctions(self.handle)

	@property
	def workflow(self):
		handle = core.BNGetWorkflowForBinaryView(self.handle)
		if handle is None:
			return None
		return binaryninja.Workflow(handle = handle)

	def rebase(self, address, force = False, progress_func = None):
		"""
		``rebase`` rebase the existing :py:class:`BinaryView` into a new :py:class:`BinaryView` at the specified virtual address

		.. note:: This method does not update cooresponding UI components. If the `BinaryView` is associated with \
		UI components then initiate the rebase operation within the UI, e.g. using the command palette. If working with views that \
		are not associated with UI components while the UI is active, then set ``force`` to ``True`` to enable rebasing.

		:param int address: virtual address of the start of the :py:class:`BinaryView`
		:param bool force: enable rebasing while the UI is active
		:return: the new :py:class:`BinaryView` object or ``None`` on failure
		:rtype: :py:class:`BinaryView` or ``None``
		"""
		result = False
		if core.BNIsUIEnabled() and not force:
			log.log_warn("The BinaryView rebase API does not update cooresponding UI components. If the BinaryView is not associated with the UI rerun with 'force = True'.")
			return None
		if progress_func is None:
			result = core.BNRebase(self.handle, address)
		else:
			result = core.BNRebaseWithProgress(self.handle, address, None, ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_ulonglong)(
				lambda ctxt, cur, total: progress_func(cur, total)))
		if result:
			return self.get_view_of_type(self.view_type)
		else:
			return None

	def show_plain_text_report(self, title, contents):
		core.BNShowPlainTextReport(self.handle, title, contents)

	def show_markdown_report(self, title, contents, plaintext = ""):
		"""
		``show_markdown_report`` displays the markdown contents in UI applications and plaintext in command-line
		applications. Markdown reports support hyperlinking into the BinaryView. Hyperlinks can be specified as follows:
		``binaryninja://?expr=_start`` Where ``expr=`` specifies an expression parsable by the :py:meth:`parse_expression` API.

		.. note:: This API functions differently on the command-line vs the UI. In the UI a pop-up is used. On the command-line \
		a simple text prompt is used.

		:param str contents: markdown contents to display
		:param str plaintext: Plain text version to display (used on the command-line)
		:rtype: None
		:Example:
			>>> bv.show_markdown_report("title", "##Contents", "Plain text contents")
			Plain text contents
		"""
		core.BNShowMarkdownReport(self.handle, title, contents, plaintext)

	def show_html_report(self, title, contents, plaintext = ""):
		"""
		``show_html_report`` displays the HTML contents in UI applications and plaintext in command-line
		applications. HTML reports support hyperlinking into the BinaryView. Hyperlinks can be specified as follows:
		``binaryninja://?expr=_start`` Where ``expr=`` specifies an expression parsable by the :py:meth:`parse_expression` API.

		.. note:: This API function differently on the command-line vs the UI. In the UI a pop-up is used. On the command-line \
			a simple text prompt is used.

		:param str contents: HTML contents to display
		:param str plaintext: Plain text version to display (used on the command-line)
		:rtype: None
		:Example:
			>>> bv.show_html_report("title", "<h1>Contents</h1>", "Plain text contents")
			Plain text contents
		"""
		core.BNShowHTMLReport(self.handle, title, contents, plaintext)

	def show_graph_report(self, title, graph):
		"""
		``show_graph_report`` displays a :py:Class:`FlowGraph` object `graph` in a new tab with ``title``.

		:param title: Title of the graph
		:type title: Plain text string title
		:param graph: The graph you wish to display
		:type graph: :py:Class:`FlowGraph` object
		"""
		core.BNShowGraphReport(self.handle, title, graph.handle)

	def get_address_input(self, prompt, title, current_address = None):
		if current_address is None:
			current_address = self._file.offset
		value = ctypes.c_ulonglong()
		if not core.BNGetAddressInput(value, prompt, title, self.handle, current_address):
			return None
		return value.value

	def add_auto_segment(self, start, length, data_offset, data_length, flags):
		core.BNAddAutoSegment(self.handle, start, length, data_offset, data_length, flags)

	def remove_auto_segment(self, start, length):
		"""
		``remove_auto_segment`` removes an automatically generated segment from the current segment mapping.

		:param int start: virtual address of the start of the segment
		:param int length: length of the segment
		:rtype: None

		.. warning:: This action is not persistent across saving of a BNDB and must be re-applied each time a BNDB is loaded.

		"""
		core.BNRemoveAutoSegment(self.handle, start, length)

	def add_user_segment(self, start, length, data_offset, data_length, flags):
		"""
		``add_user_segment`` creates a user-defined segment that specifies how data from the raw file is mapped into a virtual address space.

		:param int start: virtual address of the start of the segment
		:param int length: length of the segment (may be larger than the source data)
		:param int data_offset: offset from the parent view
		:param int data_length: length of the data from the parent view
		:param enums.SegmentFlag flags: SegmentFlags
		:rtype: None
		"""
		core.BNAddUserSegment(self.handle, start, length, data_offset, data_length, flags)

	def remove_user_segment(self, start, length):
		core.BNRemoveUserSegment(self.handle, start, length)

	def get_segment_at(self, addr):
		seg = core.BNGetSegmentAt(self.handle, addr)
		if not seg:
			return None
		segment_handle = core.BNNewSegmentReference(seg)
		assert segment_handle is not None, "core.BNNewSegmentReference returned None"
		return Segment(segment_handle)

	def get_address_for_data_offset(self, offset):
		"""
		``get_address_for_data_offset`` returns the virtual address that maps to the specific file offset

		:param int offset: file offset
		:return: the virtual address of the first segment that contains that file location
		:rtype: Int
		"""
		address = ctypes.c_ulonglong()
		if not core.BNGetAddressForDataOffset(self.handle, offset, address):
			return None
		return address.value

	def add_auto_section(self, name, start, length, semantics = SectionSemantics.DefaultSectionSemantics,
		type = "", align = 1, entry_size = 1, linked_section = "", info_section = "", info_data = 0):
		core.BNAddAutoSection(self.handle, name, start, length, semantics, type, align, entry_size, linked_section,
			info_section, info_data)

	def remove_auto_section(self, name):
		core.BNRemoveAutoSection(self.handle, name)

	def add_user_section(self, name, start, length, semantics = SectionSemantics.DefaultSectionSemantics,
		type = "", align = 1, entry_size = 1, linked_section = "", info_section = "", info_data = 0):
		"""
		``add_user_section`` creates a user-defined section that can help inform analysis by clarifying what types of
		data exist in what ranges. Note that all data specified must already be mapped by an existing segment.

		:param str name: name of the section
		:param int start: virtual address of the start of the section
		:param int length: length of the section
		:param enums.SectionSemantics semantics: SectionSemantics of the section
		:param str type: optional type
		:param int align: optional byte alignment
		:param int entry_size: optional entry size
		:param str linked_section: optional name of a linked section
		:param str info_section: optional name of an associated informational section
		:param int info_data: optional info data
		:rtype: None
		"""
		core.BNAddUserSection(self.handle, name, start, length, semantics, type, align, entry_size, linked_section,
			info_section, info_data)

	def remove_user_section(self, name):
		core.BNRemoveUserSection(self.handle, name)

	def get_sections_at(self, addr):
		count = ctypes.c_ulonglong(0)
		section_list = core.BNGetSectionsAt(self.handle, addr, count)
		assert section_list is not None, "core.BNGetSectionsAt returned None"
		result = []
		for i in range(0, count.value):
			section_handle = core.BNNewSectionReference(section_list[i])
			assert section_handle is not None, "core.BNNewSectionReference returned None"
			result.append(Section(section_handle))
		core.BNFreeSectionList(section_list, count.value)
		return result

	def get_section_by_name(self, name):
		section = core.BNGetSectionByName(self.handle, name)
		if section is None:
			return None
		section_handle = core.BNNewSectionReference(section)
		assert section_handle is not None, "core.BNNewSectionReference returned None"
		result = Section(section_handle)
		return result

	def get_unique_section_names(self, name_list):
		incoming_names = (ctypes.c_char_p * len(name_list))()
		for i in range(0, len(name_list)):
			incoming_names[i] = name_list[i].decode("utf-8")
		outgoing_names = core.BNGetUniqueSectionNames(self.handle, incoming_names, len(name_list))
		assert outgoing_names is not None, "core.BNGetUniqueSectionNames returned None"
		result = []
		for i in range(0, len(name_list)):
			result.append(str(outgoing_names[i]))
		core.BNFreeStringList(outgoing_names, len(name_list))
		return result

	@property
	def address_comments(self):
		"""
		Returns a read-only dict of the address comments attached to this BinaryView

		Note that these are different from function-level comments which are specific to each _function.
		For annotating code, it is recommended to use comments attached to functions rather than address
		comments attached to the BinaryView. On the other hand, BinaryView comments can be attached to data
		whereas function comments cannot.
		To create a function-level comment, use :func:`~Function.set_comment_at`.
		"""
		count = ctypes.c_ulonglong()
		addrs = core.BNGetGlobalCommentedAddresses(self.handle, count)
		assert addrs is not None, "core.BNGetGlobalCommentedAddresses returned None"
		result = {}
		for i in range(0, count.value):
			result[addrs[i]] = self.get_comment_at(addrs[i])
		core.BNFreeAddressList(addrs)
		return result

	def get_comment_at(self, addr):
		"""
		``get_comment_at`` returns the address-based comment attached to the given address in this BinaryView
		Note that address-based comments are different from function-level comments which are specific to each _function.
		For more information, see :func:`address_comments`.
		:param int addr: virtual address within the current BinaryView to apply the comment to
		:rtype: str

		"""
		return core.BNGetGlobalCommentForAddress(self.handle, addr)

	def set_comment_at(self, addr, comment):
		"""
		``set_comment_at`` sets a comment for the BinaryView at the address specified

		Note that these are different from function-level comments which are specific to each _function. \
		For more information, see :func:`address_comments`.

		:param int addr: virtual address within the current BinaryView to apply the comment to
		:param str comment: string comment to apply
		:rtype: None
		:Example:

			>>> bv.set_comment_at(here, "hi")

		"""
		core.BNSetGlobalCommentForAddress(self.handle, addr, comment)

	@property
	def debug_info(self) -> "binaryninja.debuginfo.DebugInfo":
		"""The current debug info object for this binary view"""
		return binaryninja.debuginfo.DebugInfo(core.BNNewDebugInfoReference(core.BNGetDebugInfo(self.handle)))

	@debug_info.setter
	def debug_info(self, value: "binaryninja.debuginfo.DebugInfo") -> Union[None, 'NotImplemented']:
		"""Sets the debug info for the current binary view"""
		if not isinstance(value, binaryninja.debuginfo.DebugInfo):
			return NotImplemented
		core.BNSetDebugInfo(self.handle, value.handle)

	def apply_debug_info(self, value: "binaryninja.debuginfo.DebugInfo") -> Union[None, 'NotImplemented']:
		"""Sets the debug info and applies its contents to the current binary view"""
		if not isinstance(value, binaryninja.debuginfo.DebugInfo):
			return NotImplemented
		core.BNApplyDebugInfo(self.handle, value.handle)

	def query_metadata(self, key):
		"""
		`query_metadata` retrieves a metadata associated with the given key stored in the current BinaryView.

		:param str key: key to query
		:rtype: metadata associated with the key
		:Example:

			>>> bv.store_metadata("integer", 1337)
			>>> bv.query_metadata("integer")
			1337L
			>>> bv.store_metadata("list", [1,2,3])
			>>> bv.query_metadata("list")
			[1L, 2L, 3L]
			>>> bv.store_metadata("string", "my_data")
			>>> bv.query_metadata("string")
			'my_data'
		"""
		md_handle = core.BNBinaryViewQueryMetadata(self.handle, key)
		if md_handle is None:
			raise KeyError(key)
		return metadata.Metadata(handle=md_handle).value

	def store_metadata(self, key, md, isAuto = False):
		"""
		`store_metadata` stores an object for the given key in the current BinaryView. Objects stored using
		`store_metadata` can be retrieved when the database is reopened. Objects stored are not arbitrary python
		objects! The values stored must be able to be held in a Metadata object. See :py:class:`Metadata`
		for more information. Python objects could obviously be serialized using pickle but this intentionally
		a task left to the user since there is the potential security issues.

		:param str key: key value to associate the Metadata object with
		:param Varies md: object to store.
		:param bool isAuto: whether the metadata is an auto metadata. Most metadata should
		keep this as False. Only those automatically generated metadata should have this set
		to True. Auto metadata is not saved into the database and is presumably re-generated
		when re-opening the database.
		:rtype: None
		:Example:

			>>> bv.store_metadata("integer", 1337)
			>>> bv.query_metadata("integer")
			1337L
			>>> bv.store_metadata("list", [1,2,3])
			>>> bv.query_metadata("list")
			[1L, 2L, 3L]
			>>> bv.store_metadata("string", "my_data")
			>>> bv.query_metadata("string")
			'my_data'
		"""
		if not isinstance(md, metadata.Metadata):
			md = metadata.Metadata(md)
		core.BNBinaryViewStoreMetadata(self.handle, key, md.handle, isAuto)

	def remove_metadata(self, key):
		"""
		`remove_metadata` removes the metadata associated with key from the current BinaryView.

		:param str key: key associated with metadata to remove from the BinaryView
		:rtype: None
		:Example:

			>>> bv.store_metadata("integer", 1337)
			>>> bv.remove_metadata("integer")
		"""
		core.BNBinaryViewRemoveMetadata(self.handle, key)

	def get_load_settings_type_names(self):
		"""
		``get_load_settings_type_names`` retrieve a list :py:class:`BinaryViewType` names for which load settings exist in \
		this :py:class:`BinaryView` context

		:return: list of :py:class:`BinaryViewType` names
		:rtype: list(str)
		"""
		result = []
		count = ctypes.c_ulonglong(0)
		names = core.BNBinaryViewGetLoadSettingsTypeNames(self.handle, count)
		assert names is not None, "core.BNBinaryViewGetLoadSettingsTypeNames returned None"
		for i in range(count.value):
			result.append(names[i])
		core.BNFreeStringList(names, count)
		return result

	def get_load_settings(self, type_name):
		"""
		``get_load_settings`` retrieve a :py:class:`Settings` object which defines the load settings for the given :py:class:`BinaryViewType` ``type_name``

		:param str type_name: the :py:class:`BinaryViewType` name
		:return: the load settings
		:rtype: :py:class:`Settings`, or ``None``
		"""
		settings_handle = core.BNBinaryViewGetLoadSettings(self.handle, type_name)
		if settings_handle is None:
			return None
		return settings.Settings(handle=settings_handle)

	def set_load_settings(self, type_name, settings):
		"""
		``set_load_settings`` set a :py:class:`settings.Settings` object which defines the load settings for the given :py:class:`BinaryViewType` ``type_name``

		:param str type_name: the :py:class:`BinaryViewType` name
		:param Settings settings: the load settings
		:rtype: None
		"""
		if settings is not None:
			settings = settings.handle
		core.BNBinaryViewSetLoadSettings(self.handle, type_name, settings)

	def __setattr__(self, name, value):
		try:
			object.__setattr__(self, name, value)
		except AttributeError:
			raise AttributeError("attribute '%s' is read only" % name)

	def parse_expression(self, expression, here=0):
		r"""
		Evaluates a string expression to an integer value.

		The parser uses the following rules:

			- Symbols are defined by the lexer as ``[A-Za-z0-9_:<>][A-Za-z0-9_:$\-<>]+`` or anything enclosed in either single or double quotes
			- Symbols are everything in ``bv.symbols``, unnamed DataVariables (i.e. ``data_00005000``), unnamed functions (i.e. ``sub_00005000``), or section names (i.e. ``.text``)
			- Numbers are defaulted to hexadecimal thus `_printf + 10` is equivalent to `printf + 0x10` If decimal numbers required use the decimal prefix.
			- Since numbers and symbols can be ambiguous its recommended that you prefix your numbers with the following:

				- ``0x`` - Hexadecimal
				- ``0n`` - Decimal
				- ``0`` - Octal

			- In the case of an ambiguous number/symbol (one with no prefix) for instance ``12345`` we will first attempt
			  to look up the string as a symbol, if a symbol is found its address is used, otherwise we attempt to convert
			  it to a hexadecimal number.
			- The following operations are valid: ``+, -, \*, /, %, (), &, \|, ^, ~``
			- In addition to the above operators there are dereference operators similar to BNIL style IL:

				- ``[<expression>]`` - read the `current address size` at ``<expression>``
				- ``[<expression>].b`` - read the byte at ``<expression>``
				- ``[<expression>].w`` - read the word (2 bytes) at ``<expression>``
				- ``[<expression>].d`` - read the dword (4 bytes) at ``<expression>``
				- ``[<expression>].q`` - read the quadword (8 bytes) at ``<expression>``

			- The ``$here`` (or more succinctly: ``$``) keyword can be used in calculations and is defined as the ``here`` parameter, or the currently selected address
			- The ``$start``/``$end`` keyword represents the address of the first/last bytes in the file respectively

		:param str expression: Arithmetic expression to be evaluated
		:param int here: (optional) Base address for relative expressions, defaults to zero
		:rtype: int
		"""
		offset = ctypes.c_ulonglong()
		errors = ctypes.c_char_p()
		if not core.BNParseExpression(self.handle, expression, offset, here, errors):
			assert errors.value is not None, "core.BNParseExpression returned errors set to None"
			error_str = errors.value.decode("utf-8")
			core.free_string(errors)
			raise ValueError(error_str)
		return offset.value

	def eval(self, expression, here=0):
		"""
		Evaluates an string expression to an integer value. This is a more concise alias for the :py:meth:`parse_expression` API
		"""
		return self.parse_expression(expression, here)


class BinaryReader:
	"""
	``class BinaryReader`` is a convenience class for reading binary data.

	BinaryReader can be instantiated as follows and the rest of the document will start from this context ::

		>>> from binaryninja import *
		>>> bv = BinaryViewType.get_view_of_file("/bin/ls")
		>>> br = BinaryReader(bv)
		>>> hex(br.read32())
		'0xfeedfacfL'
		>>>

	Or using the optional endian parameter ::

		>>> from binaryninja import *
		>>> br = BinaryReader(bv, Endianness.BigEndian)
		>>> hex(br.read32())
		'0xcffaedfeL'
		>>>
	"""
	def __init__(self, view:'BinaryView', endian:Optional[Endianness]=None):
		self._handle = core.BNCreateBinaryReader(view.handle)
		assert self._handle is not None, "core.BNCreateBinaryReader returned None"
		if endian is None:
			core.BNSetBinaryReaderEndianness(self._handle, view.endianness)
		else:
			core.BNSetBinaryReaderEndianness(self._handle, endian)
		print(self._handle)

	def __del__(self):
		if core is not None:
			core.BNFreeBinaryReader(self._handle)

	def __eq__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		assert self._handle is not None
		assert other._handle is not None
		return ctypes.addressof(self._handle.contents) == ctypes.addressof(other._handle.contents)

	def __ne__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return not (self == other)

	def __hash__(self):
		assert self._handle is not None
		return hash(ctypes.addressof(self._handle.contents))

	@property
	def endianness(self) -> Endianness:
		"""
		The Endianness to read data. (read/write)

		:getter: returns the endianness of the reader
		:setter: sets the endianness of the reader (BigEndian or LittleEndian)
		:type: Endianness
		"""
		return core.BNGetBinaryReaderEndianness(self._handle)

	@endianness.setter
	def endianness(self, value):
		core.BNSetBinaryReaderEndianness(self._handle, value)

	@property
	def offset(self):
		"""
		The current read offset (read/write).

		:getter: returns the current internal offset
		:setter: sets the internal offset
		:type: int
		"""
		return core.BNGetReaderPosition(self._handle)

	@offset.setter
	def offset(self, value):
		core.BNSeekBinaryReader(self._handle, value)

	@property
	def eof(self):
		"""
		Is end of file (read-only)

		:getter: returns boolean, true if end of file, false otherwise
		:type: bool
		"""
		return core.BNIsEndOfFile(self._handle)

	def read(self, length):
		"""
		``read`` returns ``length`` bytes read from the current offset, adding ``length`` to offset.

		:param int length: number of bytes to read.
		:return: ``length`` bytes from current offset
		:rtype: str, or None on failure
		:Example:

			>>> br.read(8)
			'\\xcf\\xfa\\xed\\xfe\\x07\\x00\\x00\\x01'
			>>>
		"""
		dest = ctypes.create_string_buffer(length)
		if not core.BNReadData(self._handle, dest, length):
			return None
		return dest.raw

	def read8(self):
		"""
		``read8`` returns a one byte integer from offset incrementing the offset.

		:return: byte at offset.
		:rtype: int, or None on failure
		:Example:

			>>> br.seek(0x100000000)
			>>> br.read8()
			207
			>>>
		"""
		result = ctypes.c_ubyte()
		if not core.BNRead8(self._handle, result):
			return None
		return result.value

	def read16(self):
		"""
		``read16`` returns a two byte integer from offset incrementing the offset by two, using specified endianness.

		:return: a two byte integer at offset.
		:rtype: int, or None on failure
		:Example:

			>>> br.seek(0x100000000)
			>>> hex(br.read16())
			'0xfacf'
			>>>
		"""
		result = ctypes.c_ushort()
		if not core.BNRead16(self._handle, result):
			return None
		return result.value

	def read32(self):
		"""
		``read32`` returns a four byte integer from offset incrementing the offset by four, using specified endianness.

		:return: a four byte integer at offset.
		:rtype: int, or None on failure
		:Example:

			>>> br.seek(0x100000000)
			>>> hex(br.read32())
			'0xfeedfacfL'
			>>>
		"""
		result = ctypes.c_uint()
		if not core.BNRead32(self._handle, result):
			return None
		return result.value

	def read64(self):
		"""
		``read64`` returns an eight byte integer from offset incrementing the offset by eight, using specified endianness.

		:return: an eight byte integer at offset.
		:rtype: int, or None on failure
		:Example:

			>>> br.seek(0x100000000)
			>>> hex(br.read64())
			'0x1000007feedfacfL'
			>>>
		"""
		result = ctypes.c_ulonglong()
		if not core.BNRead64(self._handle, result):
			return None
		return result.value

	def read16le(self):
		"""
		``read16le`` returns a two byte little endian integer from offset incrementing the offset by two.

		:return: a two byte integer at offset.
		:rtype: int, or None on failure
		:Example:

			>>> br.seek(0x100000000)
			>>> hex(br.read16le())
			'0xfacf'
			>>>
		"""
		result = self.read(2)
		if (result is None) or (len(result) != 2):
			return None
		return struct.unpack("<H", result)[0]

	def read32le(self):
		"""
		``read32le`` returns a four byte little endian integer from offset incrementing the offset by four.

		:return: a four byte integer at offset.
		:rtype: int, or None on failure
		:Example:

			>>> br.seek(0x100000000)
			>>> hex(br.read32le())
			'0xfeedfacf'
			>>>
		"""
		result = self.read(4)
		if (result is None) or (len(result) != 4):
			return None
		return struct.unpack("<I", result)[0]

	def read64le(self):
		"""
		``read64le`` returns an eight byte little endian integer from offset incrementing the offset by eight.

		:return: a eight byte integer at offset.
		:rtype: int, or None on failure
		:Example:

			>>> br.seek(0x100000000)
			>>> hex(br.read64le())
			'0x1000007feedfacf'
			>>>
		"""
		result = self.read(8)
		if (result is None) or (len(result) != 8):
			return None
		return struct.unpack("<Q", result)[0]

	def read16be(self):
		"""
		``read16be`` returns a two byte big endian integer from offset incrementing the offset by two.

		:return: a two byte integer at offset.
		:rtype: int, or None on failure
		:Example:

			>>> br.seek(0x100000000)
			>>> hex(br.read16be())
			'0xcffa'
			>>>
		"""
		result = self.read(2)
		if (result is None) or (len(result) != 2):
			return None
		return struct.unpack(">H", result)[0]

	def read32be(self):
		"""
		``read32be`` returns a four byte big endian integer from offset incrementing the offset by four.

		:return: a four byte integer at offset.
		:rtype: int, or None on failure
		:Example:

			>>> br.seek(0x100000000)
			>>> hex(br.read32be())
			'0xcffaedfe'
			>>>
		"""
		result = self.read(4)
		if (result is None) or (len(result) != 4):
			return None
		return struct.unpack(">I", result)[0]

	def read64be(self):
		"""
		``read64be`` returns an eight byte big endian integer from offset incrementing the offset by eight.

		:return: a eight byte integer at offset.
		:rtype: int, or None on failure
		:Example:

			>>> br.seek(0x100000000)
			>>> hex(br.read64be())
			'0xcffaedfe07000001L'
		"""
		result = self.read(8)
		if (result is None) or (len(result) != 8):
			return None
		return struct.unpack(">Q", result)[0]

	def seek(self, offset):
		"""
		``seek`` update internal offset to ``offset``.

		:param int offset: offset to set the internal offset to
		:rtype: None
		:Example:

			>>> hex(br.offset)
			'0x100000008L'
			>>> br.seek(0x100000000)
			>>> hex(br.offset)
			'0x100000000L'
			>>>
		"""
		core.BNSeekBinaryReader(self._handle, offset)

	def seek_relative(self, offset):
		"""
		``seek_relative`` updates the internal offset by ``offset``.

		:param int offset: offset to add to the internal offset
		:rtype: None
		:Example:

			>>> hex(br.offset)
			'0x100000008L'
			>>> br.seek_relative(-8)
			>>> hex(br.offset)
			'0x100000000L'
			>>>
		"""
		core.BNSeekBinaryReaderRelative(self._handle, offset)


class BinaryWriter:
	"""
	``class BinaryWriter`` is a convenience class for writing binary data.

	BinaryWriter can be instantiated as follows and the rest of the document will start from this context ::

		>>> from binaryninja import *
		>>> bv = BinaryViewType.get_view_of_file("/bin/ls")
		>>> br = BinaryReader(bv)
		>>> br.offset
		4294967296
		>>> bw = BinaryWriter(bv)
		>>>

	Or using the optional endian parameter ::

		>>> from binaryninja import *
		>>> bv = BinaryViewType.get_view_of_file("/bin/ls")
		>>> br = BinaryReader(bv, Endianness.BigEndian)
		>>> bw = BinaryWriter(bv, Endianness.BigEndian)
		>>>
	"""
	def __init__(self, view, endian = None):
		self._handle = core.BNCreateBinaryWriter(view.handle)
		assert self._handle is not None, "core.BNCreateBinaryWriter returned None"
		if endian is None:
			core.BNSetBinaryWriterEndianness(self._handle, view.endianness)
		else:
			core.BNSetBinaryWriterEndianness(self._handle, endian)

	def __del__(self):
		if core is not None:
			core.BNFreeBinaryWriter(self._handle)

	def __eq__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		assert self._handle is not None
		assert other._handle is not None
		return ctypes.addressof(self._handle.contents) == ctypes.addressof(other._handle.contents)

	def __ne__(self, other):
		if not isinstance(other, self.__class__):
			return NotImplemented
		return not (self == other)

	def __hash__(self):
		assert self._handle is not None
		return hash(ctypes.addressof(self._handle.contents))

	@property
	def endianness(self):
		"""
		The Endianness to written data. (read/write)

		:getter: returns the endianness of the reader
		:setter: sets the endianness of the reader (BigEndian or LittleEndian)
		:type: Endianness
		"""
		return core.BNGetBinaryWriterEndianness(self._handle)

	@endianness.setter
	def endianness(self, value):
		core.BNSetBinaryWriterEndianness(self._handle, value)

	@property
	def offset(self):
		"""
		The current write offset (read/write).

		:getter: returns the current internal offset
		:setter: sets the internal offset
		:type: int
		"""
		return core.BNGetWriterPosition(self._handle)

	@offset.setter
	def offset(self, value):
		core.BNSeekBinaryWriter(self._handle, value)

	def write(self, value):
		"""
		``write`` writes ``len(value)`` bytes to the internal offset, without regard to endianness.

		:param str value: bytes to be written at current offset
		:return: boolean True on success, False on failure.
		:rtype: bool
		:Example:

			>>> bw.write("AAAA")
			True
			>>> br.read(4)
			'AAAA'
			>>>
		"""

		value = value.decode("utf-8")
		buf = ctypes.create_string_buffer(len(value))
		ctypes.memmove(buf, value, len(value))
		return core.BNWriteData(self._handle, buf, len(value))

	def write8(self, value):
		"""
		``write8`` lowest order byte from the integer ``value`` to the current offset.

		:param str value: bytes to be written at current offset
		:return: boolean
		:rtype: bool
		:Example:

			>>> bw.write8(0x42)
			True
			>>> br.read(1)
			'B'
			>>>
		"""
		return core.BNWrite8(self._handle, value)

	def write16(self, value):
		"""
		``write16`` writes the lowest order two bytes from the integer ``value`` to the current offset, using internal endianness.

		:param int value: integer value to write.
		:return: boolean True on success, False on failure.
		:rtype: bool
		"""
		return core.BNWrite16(self._handle, value)

	def write32(self, value):
		"""
		``write32`` writes the lowest order four bytes from the integer ``value`` to the current offset, using internal endianness.

		:param int value: integer value to write.
		:return: boolean True on success, False on failure.
		:rtype: bool
		"""
		return core.BNWrite32(self._handle, value)

	def write64(self, value):
		"""
		``write64`` writes the lowest order eight bytes from the integer ``value`` to the current offset, using internal endianness.

		:param int value: integer value to write.
		:return: boolean True on success, False on failure.
		:rtype: bool
		"""
		return core.BNWrite64(self._handle, value)

	def write16le(self, value):
		"""
		``write16le`` writes the lowest order two bytes from the little endian integer ``value`` to the current offset.

		:param int value: integer value to write.
		:return: boolean True on success, False on failure.
		:rtype: bool
		"""
		value = struct.pack("<H", value)
		return self.write(value)

	def write32le(self, value):
		"""
		``write32le`` writes the lowest order four bytes from the little endian integer ``value`` to the current offset.

		:param int value: integer value to write.
		:return: boolean True on success, False on failure.
		:rtype: bool
		"""
		value = struct.pack("<I", value)
		return self.write(value)

	def write64le(self, value):
		"""
		``write64le`` writes the lowest order eight bytes from the little endian integer ``value`` to the current offset.

		:param int value: integer value to write.
		:return: boolean True on success, False on failure.
		:rtype: bool
		"""
		value = struct.pack("<Q", value)
		return self.write(value)

	def write16be(self, value):
		"""
		``write16be`` writes the lowest order two bytes from the big endian integer ``value`` to the current offset.

		:param int value: integer value to write.
		:return: boolean True on success, False on failure.
		:rtype: bool
		"""
		value = struct.pack(">H", value)
		return self.write(value)

	def write32be(self, value):
		"""
		``write32be`` writes the lowest order four bytes from the big endian integer ``value`` to the current offset.

		:param int value: integer value to write.
		:return: boolean True on success, False on failure.
		:rtype: bool
		"""
		value = struct.pack(">I", value)
		return self.write(value)

	def write64be(self, value):
		"""
		``write64be`` writes the lowest order eight bytes from the big endian integer ``value`` to the current offset.

		:param int value: integer value to write.
		:return: boolean True on success, False on failure.
		:rtype: bool
		"""
		value = struct.pack(">Q", value)
		return self.write(value)

	def seek(self, offset):
		"""
		``seek`` update internal offset to ``offset``.

		:param int offset: offset to set the internal offset to
		:rtype: None
		:Example:

			>>> hex(bw.offset)
			'0x100000008L'
			>>> bw.seek(0x100000000)
			>>> hex(bw.offset)
			'0x100000000L'
			>>>
		"""
		core.BNSeekBinaryWriter(self._handle, offset)

	def seek_relative(self, offset):
		"""
		``seek_relative`` updates the internal offset by ``offset``.

		:param int offset: offset to add to the internal offset
		:rtype: None
		:Example:

			>>> hex(bw.offset)
			'0x100000008L'
			>>> bw.seek_relative(-8)
			>>> hex(bw.offset)
			'0x100000000L'
			>>>
		"""
		core.BNSeekBinaryWriterRelative(self._handle, offset)


@dataclass
class StructuredDataValue:
	type:'_types.Type'
	value:bytes
	endian:Endianness

	def __str__(self):
		decode_str = "{}B".format(self.type.width)
		return ' '.join(["{:02x}".format(x) for x in struct.unpack(decode_str, self.value)])

	def __repr__(self):
		return f"<StructuredDataValue type:{self.type} value:{self}>"

	def __int__(self):
		if isinstance(self.type, _types.PointerType):
			return self.int_from_bytes(self.value, self.type.width, False, self.endian)
		elif isinstance(self.type, (_types.IntegerType, _types.EnumerationType)):
			return self.int_from_bytes(self.value, self.type.width, bool(self.type.signed), self.endian)
		raise Exception("Attempting to coerce non integral type to an integer")

	@staticmethod
	def int_from_bytes(data:bytes, width:int, sign:bool, endian:Optional[Endianness]=None):
		if width == 1:
			code = "B"
		elif width == 2:
			code = "H"
		elif width == 4:
			code = "I"
		elif width == 8:
			code = "Q"
		else:
			raise Exception("Could not convert to integer with width {}".format(width))

		_endian = "<" if endian == Endianness.LittleEndian else ">"
		if sign:
			code = code.lower()
		return struct.unpack(f"{_endian}{code}", data)[0]

	def __float__(self):
		if not isinstance(self.type, _types.FloatType):
			raise Exception("Attempting to coerce non float type to a float")
		endian = "<" if self.endian == Endianness.LittleEndian else ">"
		if self.type.width == 2:
			code = "e"
		elif self.type.width == 4:
			code = "f"
		elif self.type.width == 8:
			code = "d"
		else:
			raise Exception("Could not convert to float with width {}".format(self.type.width))
		return struct.unpack(f"{endian}{code}", self.value)[0]

	@property
	def int(self):
		return int(self)

	@property
	def float(self):
		return float(self)

# class StructuredDataView:
# 	@staticmethod
# 	def create(view:'BinaryView', structure_name:str, address:int, endian:Optional[Endianness]=None):
# 		s = view.get_type_by_name(structure_name)
# 		if s is None:
# 			raise Exception(f"Could not find structure with name: {structure_name}")

# 		if s.type_class == TypeClass.NamedTypeReferenceClass:
# 			s = view.get_type_by_id(s.named_type_reference.id)
# 		if s.type_class != TypeClass.StructureTypeClass:
# 			raise Exception(f"{self.structure_name} is not a StructureTypeClass, got: {s.type_class}")

# 		self._structure = s.structure

# 		return make_dataclass(f"{structure_name}_{address}_{endian}", fields)

# class StructuredDataView:
	# """
	# 	``class StructuredDataView`` is a convenience class for reading structured binary data.

	# 	StructuredDataView can be instantiated as follows:

	# 		>>> from binaryninja import *
	# 		>>> bv = BinaryViewType.get_view_of_file("/bin/ls")
	# 		>>> structure = "Elf64_Header"
	# 		>>> address = bv.start
	# 		>>> elf = StructuredDataView(bv, structure, address)
	# 		>>>

	# 	Once instantiated, members can be accessed:

	# 		>>> print("{:x}".format(elf.machine))
	# 		003e
	# 		>>>

	# 	"""
	# _structure = None
	# _structure_name = None
	# _address = 0
	# _bv = None

	# def __init__(self, bv:'BinaryView', structure_name:str, address:int, endian:Optional[Endianness]=None):
	# 	self._bv = bv
	# 	if endian is None:
	# 		if bv.arch is not None:
	# 			endian = bv.arch.endianness
	# 		else:
	# 			raise Exception("Can not instantiate StructuredDataView without specifying and Endianness")
	# 	self._structure_name = structure_name
	# 	self._address = address
	# 	self._members = OrderedDict()
	# 	self._endian = endian
	# 	self._lookup_structure()
	# 	self._define_members()

	# def __repr__(self):
	# 	return f"<StructuredDataView type:{self._structure_name} " + \
	# 		f"size:{self._structure.width:#x} address:{self._address:#x}>"

	# def __len__(self):
	# 	assert self._structure is not None, "self._structure is None"
	# 	return self._structure.width

	# def __getattr__(self, key):
	# 	m = self._members.get(key, None)
	# 	if m is None:
	# 		return self.__getattribute__(key)

	# 	return self[key]

	# def __getitem__(self, key):
	# 	m = self._members.get(key, None)
	# 	if m is None:
	# 		return m

	# 	ty = m.type
	# 	offset = m.offset
	# 	width = ty.width

	# 	value = self.view.read(self._address + offset, width)
	# 	return StructuredDataValue(ty, value, self._endian)

	# @property
	# def view(self) -> 'BinaryView':
	# 	assert self._bv is not None
	# 	return self._bv

	# @property
	# def structure(self) -> '_types.Structure':
	# 	assert self._structure is not None
	# 	return self._structure

	# def __str__(self):
	# 	rv = "struct {name} 0x{addr:x} {{\n".format(name=self._structure_name, addr=self._address)
	# 	for k in self._members:
	# 		m = self._members[k]

	# 		ty = m.type
	# 		offset = m.offset

	# 		formatted_offset = "{:=+x}".format(offset)
	# 		formatted_type = "{:s} {:s}".format(str(ty), k)

	# 		value = self[k]
	# 		assert value is not None
	# 		if value.width in (1, 2, 4, 8):
	# 			formatted_value = str.zfill("{:x}".format(value.int), value.width * 2)
	# 		else:
	# 			formatted_value = str(value)

	# 		rv += "\t{:>6s} {:40s} = {:30s}\n".format(formatted_offset, formatted_type, formatted_value)

	# 	rv += "}\n"

	# 	return rv

	# def _lookup_structure(self):
	# 	s = self.view.get_type_by_name(self._structure_name)
	# 	if s is None:
	# 		raise Exception(f"Could not find structure with name: {self._structure_name}")

	# 	if s.type_class != TypeClass.StructureTypeClass:
	# 		raise Exception(f"{self._structure_name} is not a StructureTypeClass, got: {s.type_class}")

	# 	self._structure = s.structure

	# def _define_members(self):
	# 	for m in self.structure.members:
	# 		self._members[m.name] = m


@dataclass(frozen=True)
class CoreDataVariable:
	address:int
	type:'_types.Type'
	auto_discovered:bool


@dataclass
class DataVariable:
	core_data_var:CoreDataVariable
	view:'BinaryView'

	@classmethod
	def from_core_struct(cls, var:core.BNDataVariable, view:'BinaryView'):
		var_type = _types.Type.create(core.BNNewTypeReference(var.type), platform=view.platform,
			confidence=var.typeConfidence)
		return cls(CoreDataVariable(var.address, var_type, var.autoDiscovered), view)

	@property
	def data_refs_from(self) -> Optional[Generator[int, None, None]]:
		"""data cross references from this data variable (read-only)"""
		return self.view.get_data_refs_from(self.address, max(1, len(self)))

	@property
	def data_refs(self) -> Optional[Generator[int, None, None]]:
		"""data cross references to this data variable (read-only)"""
		return self.view.get_data_refs(self.address, max(1, len(self)))

	@property
	def code_refs(self) -> Generator['ReferenceSource', None, None]:
		"""code references to this data variable (read-only)"""
		return self.view.get_code_refs(self.address, max(1, len(self)))

	def __len__(self):
		return len(self.core_data_var.type)

	def __repr__(self):
		return f"<var {self.address:#x}: {self.type}>"

	@property
	def address(self) -> int:
		return self.core_data_var.address

	def _value_helper(self, t:'_types.Type', data:bytes):
		sdv = StructuredDataValue(t, data, self.view.endianness)
		if isinstance(t, (_types.VoidType, _types.FunctionType)): #, _types.VarArgsType, _types.ValueType)):
			return None
		elif isinstance(t, _types.BoolType):
			return bool(sdv)
		elif isinstance(t, (_types.IntegerType, _types.PointerType)):
			return int(sdv)
		elif isinstance(t, _types.FloatType):
			return float(sdv)
		elif isinstance(t, _types.WideCharType):
			return data.decode("utf-8")
		elif isinstance(t, _types.StructureType):
			result = {}
			for member in t.members:
				member_data = data[member.offset: member.offset+member.type.width]
				result[member.name] = self._value_helper(member.type, member_data)
			return result
		elif isinstance(t, _types.EnumerationType):
			value = int(sdv)
			for member in t.members:
				if int(member) == value:
					return member
			return value
		elif isinstance(t, _types.ArrayType):
			result = []
			if t.element_type is None:
				raise ValueError("Can not get value for Array type with no element type")
			for i in range(t.count):
				offset = i * t.element_type.width
				element_data = data[offset: offset + t.element_type.width]
				result.append(self._value_helper(t.element_type, element_data))
			return result
		elif isinstance(t, _types.NamedTypeReference):
			target = self.view.get_type_by_id(t.id)
			assert target is not None
			return self._value_helper(target, data)
		assert False, f"Unhandled `Type` {type(t)}"

	@property
	def value(self) -> Any:
		data = self.view.read(self.address, self.type.width)
		if len(data) != self.type.width:
			raise Exception(f"Failed to read bytes at address {self.address} of width {self.type.width}")
		return self._value_helper(self.type, data)


		# elif t.type_class == TypeClass.StructureTypeClass:

		# elif t.type_class == TypeClass.EnumerationTypeClass:

		# elif t.type_class == TypeClass.PointerTypeClass:

		# elif t.type_class == TypeClass.ArrayTypeClass:

		# elif t.type_class == TypeClass.FunctionTypeClass:

		# elif t.type_class == TypeClass.VarArgsTypeClass:

		# elif t.type_class == TypeClass.ValueTypeClass:

		# elif t.type_class == TypeClass.NamedTypeReferenceClass:

		# elif t.type_class == TypeClass.WideCharTypeClass:

	@value.setter
	def value(self, value:bytes) -> None:
		self.view.write(self.address, value)

	@property
	def type(self) -> '_types.Type':
		return self.core_data_var.type

	@type.setter
	def type(self, value:Optional['_types.Type']) -> None:  # type: ignore
		self.view.define_user_data_var(self.address, value)
		if value is None:
			self.core_data_var = CoreDataVariable(self.address, _types.VoidType.create(), False)
		else:
			self.core_data_var = CoreDataVariable(self.address, value, False)

	@property
	def auto_discovered(self) -> bool:
		return self.core_data_var.auto_discovered

	@view.setter
	def view(self, value):
		self._view = value

	@property
	def symbol(self) -> Optional['_types.Symbol']:
		return self.view.get_symbol_at(self.address)

	@symbol.setter
	def symbol(self, value:Optional[Union[str, '_types.Symbol']]) -> None:  # type: ignore
		if value is None or value == "":
			if self.symbol is not None:
				self.view.undefine_user_symbol(self.symbol)
		elif isinstance(value, str):
			symbol = _types.Symbol(SymbolType.DataSymbol, self.address, value)
			self.view.define_user_symbol(symbol)
		elif isinstance(value, _types.Symbol):
			self.view.define_user_symbol(value)
		else:
			raise ValueError("Unknown supported for symbol assignment")

	@property
	def name(self) -> Optional[str]:
		if self.symbol is None:
			return None
		return self.symbol.name

	@name.setter
	def name(self, value:str) -> None:
		self.symbol = value


class DataVariableAndName(DataVariable):
	def __init__(self, addr: int, var_type: types.Type, var_name: str, auto_discovered: bool, view: "BinaryView" = None) -> None:
		super(DataVariableAndName, self).__init__(addr, var_type, auto_discovered, view)
		self.name = var_name

	def __repr__(self) -> str:
		return "<var 0x%x: %s %s>" % (self.address, str(self.type), self.name)
""" This file is a modified version of rlcompleter.py from the Python
project under the Python Software Foundation License 2:
https://github.com/python/cpython/blob/master/Lib/rlcompleter.py
https://github.com/python/cpython/blob/master/LICENSE

The only changes made were to modify the regular expression in attr_matches
and all code that relied on GNU readline (the later more for readability as
it wasn't required).

--------------

Word completion for GNU readline.

The completer completes keywords, built-ins and globals in a selectable
namespace (which defaults to __main__); when completing NAME.NAME..., it
evaluates (!) the expression up to the last dot and completes its attributes.

It's very cool to do "import sys" type "sys.", hit the completion key (twice),
and see the list of names defined by the sys module!

Tip: to use the tab key as the completion key, call

	readline.parse_and_bind("tab: complete")

Notes:

- Exceptions raised by the completer function are *ignored* (and generally cause
  the completion to fail).  This is a feature -- since readline sets the tty
  device in raw (or cbreak) mode, printing a traceback wouldn't work well
  without some complicated hoopla to save, reset and restore the tty state.

- The evaluation of the NAME.NAME... form may cause arbitrary application
  defined code to be executed if an object with a __getattr__ hook is found.
  Since it is the responsibility of the application (or the user) to enable this
  feature, I consider this an acceptable risk.  More complicated expressions
  (e.g. function calls or indexing operations) are *not* evaluated.

- When the original stdin is not a tty device, GNU readline is never
  used, and this module (and the readline module) are silently inactive.

"""

import atexit
import __main__
import inspect
import sys
from typing import Optional

__all__ = ["Completer"]


def fnsignature(obj):
	if sys.version_info[0:2] >= (3, 5):
		try:
			sig = str(inspect.signature(obj))
		except:
			sig = "()"
		return sig
	else:
		try:
			args = inspect.getargspec(obj).args
			args.remove('self')
			sig = "(" + ','.join(args) + ")"
		except:
			sig = "()"
		return sig


class Completer:
	def __init__(self, namespace=None):
		"""Create a new completer for the command line.

		Completer([namespace]) -> completer instance.

		If unspecified, the default namespace where completions are performed
		is __main__ (technically, __main__.__dict__). Namespaces should be
		given as dictionaries.

		Completer instances should be used as the completion mechanism of
		readline via the set_completer() call:

		readline.set_completer(Completer(my_namespace).complete)
		"""

		if namespace and not isinstance(namespace, dict):
			raise TypeError('namespace must be a dictionary')

		# Don't bind to namespace quite yet, but flag whether the user wants a
		# specific namespace or to use __main__.__dict__. This will allow us
		# to bind to __main__.__dict__ at completion time, not now.
		if namespace is None:
			self.use_main_ns = 1
		else:
			self.use_main_ns = 0
			self.namespace = namespace

	def complete(self, text: str, state) -> Optional[str]:
		"""Return the next possible completion for 'text'.

		This is called successively with state == 0, 1, 2, ... until it
		returns None.  The completion should begin with 'text'.

		"""
		if self.use_main_ns:
			self.namespace = __main__.__dict__

		if not text.strip():
			if state == 0:
				return '\t'
			else:
				return None

		if state == 0:
			if "." in text:
				self.matches = self.attr_matches(text)
			else:
				self.matches = self.global_matches(text)
		try:
			return self.matches[state]
		except IndexError:
			return None

	def _callable_postfix(self, val, word):
		if callable(val) and not inspect.isclass(val):
			word = word + fnsignature(val)
		return word

	def global_matches(self, text):
		"""Compute matches when text is a simple name.

		Return a list of all keywords, built-in functions and names currently
		defined in self.namespace that match.

		"""
		import keyword
		matches = []
		seen = {"__builtins__"}
		n = len(text)
		for word in keyword.kwlist:
			if word[:n] == text:
				seen.add(word)
				if word in {'finally', 'try'}:
					word = word + ':'
				elif word not in {'False', 'None', 'True', 'break', 'continue', 'pass', 'else'}:
					word = word + ' '
				matches.append(word)
		#Not sure why in the console builtins becomes a dict but this works for now.
		if hasattr(__builtins__, '__dict__'):  # type: ignore # remove this ignore > pyright 1.1.149
			builtins = __builtins__.__dict__  # type: ignore # remove this ignore > pyright 1.1.149
		else:
			builtins = __builtins__  # type: ignore # remove this ignore > pyright 1.1.149
		for nspace in [self.namespace, builtins]:
			for word, val in nspace.items():
				if word[:n] == text and word not in seen:
					seen.add(word)
					matches.append(self._callable_postfix(val, word))
		return matches

	def attr_matches(self, text):
		"""Compute matches when text contains a dot.

		Assuming the text is of the form NAME.NAME....[NAME], and is
		evaluable in self.namespace, it will be evaluated and its attributes
		(as revealed by dir()) are used as possible completions.  (For class
		instances, class members are also considered.)

		WARNING: this can still invoke arbitrary C code, if an object
		with a __getattr__ hook is evaluated.

		"""
		import re
		m = re.match(r"([\w\[\]]+(\.[\w\[\]]+)*)\.([\w\[\]]*)", text)
		if not m:
			return []
		expr, attr = m.group(1, 3)
		try:
			thisobject = eval(expr, self.namespace)
		except Exception:
			return []

		# get the content of the object, except __builtins__
		words = set(dir(thisobject))
		words.discard("__builtins__")

		if hasattr(thisobject, '__class__'):
			words.add('__class__')
			words.update(get_class_members(thisobject.__class__))
		matches = []
		n = len(attr)
		if attr == '':
			noprefix = '_'
		elif attr == '_':
			noprefix = '__'
		else:
			noprefix = None
		while True:
			for word in words:
				if (word[:n] == attr and not (noprefix and word[:n + 1] == noprefix)):
					match = f"{expr}.{word}"
					try:
						val = inspect.getattr_static(thisobject, word)
					except Exception:
						pass  # Include even if attribute not set
					else:
						match = self._callable_postfix(val, match)
					matches.append(match)
			if matches or not noprefix:
				break
			if noprefix == '_':
				noprefix = '__'
			else:
				noprefix = None
		matches.sort()
		return matches


def get_class_members(klass):
	ret = dir(klass)
	if hasattr(klass, '__bases__'):
		for base in klass.__bases__:
			ret = ret + get_class_members(base)
	return ret

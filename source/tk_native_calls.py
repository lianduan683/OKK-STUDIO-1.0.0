"""Run message-dispatching Win32 calls inside tkinter's Tcl entry context.

SetParent/ShowWindow/SetWindowPos can synchronously invoke Tk bindings. A
direct ctypes call bypasses _tkinter's ENTER_TCL thread-state setup; a nested
Python binding can then abort the process in PyEval_RestoreThread(NULL).
Register a Tcl C command instead of a tkinter Python command so tk.call keeps
that context alive while the native operation dispatches its messages.
"""
from __future__ import annotations

import ctypes as C
import threading
import tkinter as tk


_ObjCommand = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p,
                         C.c_int, C.POINTER(C.c_void_p))


class TkNativeCalls:
    def __init__(self, root):
        self.tk = root.tk
        self.thread = threading.get_ident()
        self.pending = []
        self.command = '::okk_native_' + str(id(self))
        self.callback = _ObjCommand(self._dispatch)
        version = str(self.tk.call('info', 'tclversion')).replace('.', '')
        self.library = C.CDLL('tcl' + version + 't.dll')
        create = self.library.Tcl_CreateObjCommand
        create.argtypes = [C.c_void_p, C.c_char_p, _ObjCommand, C.c_void_p, C.c_void_p]
        create.restype = C.c_void_p
        if not create(self.tk.interpaddr(), self.command.encode('ascii'), self.callback, None, None):
            raise tk.TclError('无法注册浮动面板原生窗口命令。')

    @classmethod
    def for_widget(cls, widget):
        root = widget._root()
        runner = getattr(root, '_okk_native_calls', None)
        if runner is None:
            # Tcl retains a function pointer, not a Python reference. Anchor
            # the callback for the entire lifetime of this Tk interpreter.
            runner = root._okk_native_calls = cls(root)
        return runner

    def _dispatch(self, _data, _interpreter, count, _arguments):
        if count != 1 or not self.pending:
            return 1  # TCL_ERROR; only call() supplies a pending operation.
        frame = self.pending[-1]
        C.set_last_error(frame['last_error'])
        try:
            frame['result'] = frame['function'](*frame['args'])
        except BaseException as error:
            frame['error'] = error
        finally:
            frame['last_error'] = C.get_last_error()
        return 0

    def call(self, function, *args):
        if threading.get_ident() != self.thread:
            raise RuntimeError('原生面板窗口只能由 Tk 界面线程操作。')
        frame = dict(function=function, args=args, result=None, error=None,
                     last_error=C.get_last_error())
        self.pending.append(frame)
        try:
            self.tk.call(self.command)
        finally:
            self.pending.pop()
            C.set_last_error(frame['last_error'])
        if frame['error'] is not None:
            raise frame['error']
        return frame['result']

from .util_py import py_helper


class PyBase:
    def py_prefix(self):
        return "Hello"


class PyPolite(PyBase):
    def py_greet(self, name):
        return self.py_prefix() + ", " + py_helper(name)


def py_run():
    return PyPolite().py_greet("world")

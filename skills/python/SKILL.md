# Python Coding Standards

When to Use

- Writing or modifying Python files (.py)
- Reviewing Python code for style and correctness
- Debugging Python runtime errors

Examples

- Use type hints on every public function and method.
- Prefer f-strings over %-formatting and .format().
- Use `pathlib.Path` instead of `os.path` for new code.

Common Pitfalls

- Mutable default arguments (`def f(x=[])`).
- Late binding in closures (use default args to capture).
- Bare `except:` clauses — always catch a specific exception.

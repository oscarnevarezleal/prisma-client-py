def quote(string: str) -> str:
    """Surround the given string with single quotes. e.g.

    foo -> 'foo'

    This does not do any form of escaping, the input is expected to not contain any single quotes.
    """
    return "'" + string + "'"


def unquote_forward_refs(type_: str) -> str:
    """Strip the forward-reference quoting from a rendered type expression. e.g.

    Optional[List['models.Post']] -> Optional[List[models.Post]]

    Required by the `msgspec` model backend on Python 3.8, where
    `typing.ForwardRef._evaluate` does not recurse into the type it evaluates
    (3.9 added that). Under `from __future__ import annotations` msgspec hands
    the whole annotation to `_eval_type` as one forward reference, so on 3.8 a
    *nested* string literal survives as an unresolved `ForwardRef` and msgspec
    rejects it with `Type 'ForwardRef(...)' is not supported`. Writing every
    name unquoted makes the single `eval` produce a concrete type on every
    supported version.

    Unquoting is safe for these annotations because msgspec resolves them
    lazily, on first decode, by which point the module is fully initialised —
    so names defined later in the module (cyclic relations) still resolve.

    The generated type expressions use quotes only as forward-reference
    delimiters, never as part of a value (there are no `Literal['...']` types
    among them), so a plain strip is sufficient. That assumption is asserted
    rather than left implicit: a `Literal` reaching here would be silently
    turned into a name lookup, and the failure would surface far away, as a
    `NameError` on first decode.
    """
    assert 'Literal[' not in type_, f'cannot unquote a type containing a string value: {type_}'
    return type_.replace("'", '')

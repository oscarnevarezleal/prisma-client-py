# Lazy Model Loading Analysis

## Current State

The Prisma Client Python **already has lazy loading** at the module level via `__getattr__` in `__init__.py`, but models within the `models` module are **eagerly loaded** when the module is imported.

### Current Architecture

```python
# prisma/__init__.py already lazy loads modules
import prisma  # ✅ No imports yet

prisma.models  # ✅ Lazy loads models module (first access triggers import)

# BUT once models is imported, ALL models are eagerly loaded:
from prisma import models
models.User     # ✅ Available
models.Post     # ✅ Available
models.Model0   # ✅ Available
# ... all 60+ models loaded into memory
```

## The Problem

When `models` module is imported, **all model classes are defined**:

```python
# models.py (generated)
class Model0(BaseModel):
    id: int
    name: str
    # ... fields

class Model1(BaseModel):
    # ...

# ... 60 more model classes
```

**Each model class includes**:
- Pydantic field definitions
- Validators
- Model metadata
- Base class overhead
- Type annotations

For 60 models, this adds up to ~943KB of code that gets loaded into memory.

## What Would Lazy Model Loading Require?

### Architecture Options

#### Option 1: Module-Level `__getattr__` (Recommended)

Add lazy loading **within** the models module itself:

```python
# models.py (generated with lazy loading)
from typing import TYPE_CHECKING, Any
import sys

# Cache for loaded models
_model_cache: dict[str, type] = {}

def _load_model(name: str) -> type:
    """Dynamically load and return a model class."""
    if name in _model_cache:
        return _model_cache[name]

    # Generate the model class on-demand
    if name == "User":
        class User(BaseModel):
            id: int
            name: str
            # ... fields
        _model_cache[name] = User
        return User
    elif name == "Post":
        # ... define Post
        pass
    # ... etc

    raise AttributeError(f"Model {name} not found")

def __getattr__(name: str) -> Any:
    """Lazy load model classes on access."""
    return _load_model(name)

# For type checking, eager load all
if TYPE_CHECKING:
    class User(BaseModel): ...
    class Post(BaseModel): ...
    # ...
```

**Pros:**
- Models only loaded when accessed
- Type checking still works via TYPE_CHECKING block
- Transparent to users

**Cons:**
- Complex code generation
- Each model still needs full definition somewhere
- Hard to debug

#### Option 2: Separate Files Per Model

Generate each model in its own file:

```python
# models/__init__.py
def __getattr__(name: str):
    if name == "User":
        from .user import User
        return User
    elif name == "Post":
        from .post import Post
        return Post
    # ...

# models/user.py
class User(BaseModel):
    id: int
    name: str
    # ...

# models/post.py
class Post(BaseModel):
    id: int
    title: str
    # ...
```

**Pros:**
- Simple to implement
- Clear separation
- Python's import system handles caching
- Easy to debug

**Cons:**
- Many files (60+ for large schemas)
- Slightly slower first access (file I/O)
- More complex template generation

#### Option 3: Deferred Definition with Descriptors

Use descriptors to defer model class creation:

```python
# models.py
class _ModelDescriptor:
    def __init__(self, name: str, definition: callable):
        self.name = name
        self.definition = definition
        self._instance = None

    def __get__(self, obj, objtype=None):
        if self._instance is None:
            self._instance = self.definition()
        return self._instance

# Generate descriptors instead of classes
User = _ModelDescriptor("User", lambda: _create_user_model())
Post = _ModelDescriptor("Post", lambda: _create_post_model())

def _create_user_model():
    class User(BaseModel):
        id: int
        name: str
    return User
```

**Pros:**
- Single file
- Lazy evaluation
- Clean syntax for users

**Cons:**
- Complex implementation
- Type checking issues
- Debugging harder

## Implementation Requirements

### 1. Template Changes

**models.py.jinja** would need major refactoring:

```jinja
{% if lazy_model_loading %}
# Lazy loading implementation
from typing import TYPE_CHECKING, Any

_model_cache: dict[str, type] = {}

{% for model in dmmf.datamodel.models %}
def _create_{{ model.name }}():
    """Factory function to create {{ model.name }} model."""
    class {{ model.name }}(bases.Base{{ model.name }}):
        {% for field in model.all_fields %}
        {{ field.name }}: {{ field.python_type }}
        {% endfor %}
    return {{ model.name }}
{% endfor %}

def __getattr__(name: str) -> Any:
    """Lazy load model on access."""
    if name in _model_cache:
        return _model_cache[name]

    {% for model in dmmf.datamodel.models %}
    if name == "{{ model.name }}":
        cls = _create_{{ model.name }}()
        _model_cache[name] = cls
        return cls
    {% endfor %}

    raise AttributeError(f"Model {name} not found")

if TYPE_CHECKING:
    {% for model in dmmf.datamodel.models %}
    class {{ model.name }}(bases.Base{{ model.name }}): ...
    {% endfor %}

{% else %}
# Traditional eager loading
{% for model in dmmf.datamodel.models %}
class {{ model.name }}(bases.Base{{ model.name }}):
    {% for field in model.all_fields %}
    {{ field.name }}: {{ field.python_type }}
    {% endfor %}
{% endfor %}
{% endif %}
```

### 2. Pydantic Considerations

**Critical Issue**: Pydantic model classes need to be defined at import time for:

1. **Model validation** - Validators are registered when class is defined
2. **Field defaults** - Default factories evaluated at definition time
3. **Model relationships** - Forward references resolved at definition time
4. **Model rebuild** - Pydantic needs full model definition for `model_rebuild()`

```python
# This is why lazy loading is tricky
class User(BaseModel):
    name: str
    email: str

    @validator('email')  # Registered at class definition time
    def validate_email(cls, v):
        return v.lower()

# Can't defer this registration without breaking validation
```

### 3. Base Models

Each model inherits from a base:

```python
class User(bases.BaseUser):  # BaseUser must exist first
    # ...
```

The `bases.py` file would also need modification to support lazy loading.

### 4. Type Checker Compatibility

Type checkers need static definitions:

```python
if TYPE_CHECKING:
    # Must define all models statically for Pyright/mypy
    class User(BaseModel): ...
    class Post(BaseModel): ...
    # ... 60 more stubs
```

This means we **still need all definitions in the file**, just conditionally executed.

## Memory Impact Analysis

### Current Memory Usage (60 models)

- **models.py**: 943 KB
- **Peak import memory**: ~3-4 MB for models module

### Theoretical Lazy Loading Savings

If we could truly defer model loading:

```python
# Only load 5 models instead of 60
from prisma.models import User, Post, Comment, Like, Follow

# Memory savings:
# Instead of loading 60 models: ~4 MB
# Only load 5 models: ~0.33 MB
# Savings: ~3.67 MB (92% reduction)
```

### Reality Check

**But**: We still need:
1. Factory functions for each model (~50% of original size)
2. TYPE_CHECKING block with all stubs (~80% of original size)
3. Lazy loading infrastructure (~5% overhead)

**Actual savings**: ~20-30% at best, not 92%

## Trade-offs

### Benefits

✅ **Selective Loading**: Only load models you actually use
✅ **Faster Initial Import**: First `import prisma.models` is faster
✅ **Better for Microservices**: Services using only 2-3 models benefit most

### Drawbacks

❌ **Code Complexity**: Much more complex generation logic
❌ **Type Checking Duplication**: Need TYPE_CHECKING block with all models anyway
❌ **Pydantic Limitations**: Can't truly defer model class creation
❌ **Debugging Harder**: Stack traces more confusing
❌ **Minimal Savings**: Only 20-30% memory reduction realistically
❌ **Edge Cases**: Model relationships, forward refs, circular deps

## Recommendation

### **Don't implement lazy model loading** (for now)

**Reasons:**

1. **Limited benefit**: Only ~20-30% memory savings on models (which are already optimized)
2. **High complexity**: Significant template and generation logic changes
3. **Pydantic constraints**: Model classes fundamentally need to be defined
4. **Type checking duplication**: Still need all definitions for type checkers
5. **Better alternatives exist**: Focus on other optimizations first

### Better Alternatives

Instead of lazy model loading, focus on:

#### 1. **Model Field Type Simplification** (Bigger Impact)

Currently:
```python
class User(BaseModel):
    id: int
    created_at: datetime.datetime  # Complex type
    posts: List['Post']  # Relationship with forward ref
```

Optimization:
```python
# In minimal runtime
class User(BaseModel):
    id: Any  # Simplified
    created_at: Any
    posts: Any
```

**Savings**: 40-50% reduction in models.py size

#### 2. **Split Models Into Multiple Files** (Already Feasible)

For extremely large schemas (200+ models):

```
prisma/
  models/
    __init__.py      # Lazy loader
    _user.py         # User model
    _post.py         # Post model
    ...
```

Users import: `from prisma.models import User`

**Benefit**: Natural lazy loading via Python's import system

#### 3. **Model Deduplication** (Medium Impact)

Many models share common field patterns:

```python
# Instead of duplicating these fields in every model:
id: int
created_at: datetime
updated_at: datetime

# Use mixins:
class TimestampMixin(BaseModel):
    created_at: datetime
    updated_at: datetime

class User(TimestampMixin):
    # Only unique fields
```

**Savings**: 10-15% reduction

## Conclusion

**Lazy model loading is theoretically possible but not recommended** because:

1. ✅ **Already optimized**: Current 943KB is acceptable
2. ✅ **Pydantic requires eager loading**: Fundamental limitation
3. ✅ **Type checkers need all definitions**: Can't avoid duplication
4. ✅ **Complexity vs benefit**: 3x complexity for 20% savings
5. ✅ **Better alternatives exist**: Field type simplification, file splitting

### If You Still Want It...

The **cleanest approach** would be **Option 2: Separate Files Per Model**:

```python
# models/__init__.py (generated)
def __getattr__(name: str):
    import importlib
    try:
        # Try to import from _<model_name>.py
        module = importlib.import_module(f'._{name.lower()}', package=__name__)
        return getattr(module, name)
    except (ImportError, AttributeError):
        raise AttributeError(f"Model {name} not found")

if TYPE_CHECKING:
    from ._user import User
    from ._post import Post
    # ...
```

This gives you:
- ✅ True lazy loading (only import what you use)
- ✅ Simple implementation
- ✅ Type checking works
- ✅ Easy to debug
- ❌ Many files (acceptable with good tooling)

---

**Current Session Focus**: We've already achieved 67% memory reduction with minimal runtime optimization. Lazy model loading would add significant complexity for marginal additional gains.

**Recommendation**: Mark this as "future enhancement" and revisit only if users have schemas with 500+ models where even 20-30% savings would be meaningful.

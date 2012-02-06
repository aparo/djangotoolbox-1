# All fields except for BlobField written by Jonas Haag <jonas@lophus.org>

from django.core.exceptions import ValidationError
from django.db import models
from django.db.utils import IntegrityError
from django.utils.importlib import import_module


__all__ = ('RawField', 'ListField', 'DictField', 'SetField',
           'BlobField', 'EmbeddedModelField', 'LegacyEmbeddedModelField',)


EMPTY_ITER = ()


class _HandleAssignment(object):
    """
    A placeholder class that provides a way to set the attribute on the model.
    """
    def __init__(self, field):
        self.field = field

    def __get__(self, obj, type=None):
        if obj is None:
            raise AttributeError('Can only be accessed via an instance.')
        return obj.__dict__[self.field.name]

    def __set__(self, obj, value):
        obj.__dict__[self.field.name] = self.field.to_python(value)


class RawField(models.Field):
    """
    Generic field to store anything your database backend allows you to.
    No validation or conversions are done for this field.
    """
    def get_internal_type(self):
        """
        Returns this field's kind. Nonrel fields are meant to extend
        the set of standard fields, so fields subclassing them should
        get the same internal type, rather than their own class name.
        """
        return 'RawField'


class AbstractIterableField(models.Field):
    """
    Abstract field for fields for storing iterable data type like
    ``list``, ``set`` and ``dict``.

    You can pass an instance of a field as the first argument.
    If you do, the iterable items will be piped through the passed
    field's validation and conversion routines, converting the items
    to the appropriate data type.
    """
    def __init__(self, item_field=None, *args, **kwargs):
        default = kwargs.get('default', None if kwargs.get('null') else EMPTY_ITER)
        if default is not None and not callable(default):
            # ensure a new object is created every time the default is accessed
            kwargs['default'] = lambda: self._type(default)

        super(AbstractIterableField, self).__init__(*args, **kwargs)

        if item_field is None:
            item_field = RawField()
        elif callable(item_field):
            item_field = item_field()
        self.item_field = item_field

    def contribute_to_class(self, cls, name):
        self.item_field.model = cls
        self.item_field.name = name
        super(AbstractIterableField, self).contribute_to_class(cls, name)

        # If items field uses SubfieldBase, we also need to.
        metaclass = getattr(self.item_field, '__metaclass__', None)
        if issubclass(metaclass, models.SubfieldBase):
            setattr(cls, self.name, _HandleAssignment(self))

    def _convert(self, func, values, *args, **kwargs):
        if isinstance(values, (list, tuple, set, frozenset)):
            return self._type(func(value, *args, **kwargs) for value in values)
        return values

    def to_python(self, value):
        return self._convert(self.item_field.to_python, value)

    def pre_save(self, model_instance, add):
        class fake_instance(object):
            pass
        fake_instance = fake_instance()
        def wrapper(value):
            assert not hasattr(self.item_field, 'attname')
            fake_instance.value = value
            self.item_field.attname = 'value'
            try:
                return self.item_field.pre_save(fake_instance, add)
            finally:
                del self.item_field.attname

        return self._convert(wrapper, getattr(model_instance, self.attname))

    def get_db_prep_value(self, value, connection, prepared=False):
        return self._convert(self.item_field.get_db_prep_value, value,
                             connection=connection, prepared=prepared)

    def get_db_prep_save(self, value, connection):
        return self._convert(self.item_field.get_db_prep_save, value,
                             connection=connection)

    def get_db_prep_lookup(self, lookup_type, value, connection, prepared=False):

        # TODO/XXX: Remove as_lookup_value() once we have a cleaner solution
        # for dot-notation queries (related to A() / Q() queries, but doesn't
        # seem anything actually uses this; see:
        # https://groups.google.com/group/django-non-relational/browse_thread/thread/6056f8384c9caf04/89eeb9fb22ad16f3).
        if hasattr(value, 'as_lookup_value'):
            value = value.as_lookup_value(self, lookup_type, connection)

        return self.item_field.get_db_prep_lookup(lookup_type, value,
            connection=connection, prepared=prepared)

    def validate(self, values, model_instance):
        try:
            iter(values)
        except TypeError:
            raise ValidationError('Value of type %r is not iterable' %
                                      type(values))

    def formfield(self, **kwargs):
        raise NotImplementedError('No form field implemented for %r' %
                                      type(self))


class ListField(AbstractIterableField):
    """
    Field representing a Python ``list``.

    If the optional keyword argument `ordering` is given, it must be a
    callable that is passed to :meth:`list.sort` as `key` argument. If
    `ordering` is given, the items in the list will be sorted before
    sending them to the database.
    """
    _type = list

    def __init__(self, *args, **kwargs):
        self.ordering = kwargs.pop('ordering', None)
        if self.ordering is not None and not callable(self.ordering):
            raise TypeError("'ordering' has to be a callable or None, "
                            "not of type %r" %  type(self.ordering))
        super(ListField, self).__init__(*args, **kwargs)

    def get_internal_type(self):
        return 'ListField'

    def pre_save(self, model_instance, add):
        values = getattr(model_instance, self.attname)
        if values is None:
            return None
        if values and self.ordering:
            values.sort(key=self.ordering)
        return super(ListField, self).pre_save(model_instance, add)


class SetField(AbstractIterableField):
    """
    Field representing a Python ``set``.
    """
    _type = set

    def get_internal_type(self):
        return 'SetField'

    def value_to_string(self, obj):
        """
        Custom method for serialization, as JSON doesn't support
        serializing sets.
        """
        return list(self._get_val_from_obj(obj))


class DictField(AbstractIterableField):
    """
    Field representing a Python ``dict``.

    Type conversions described in :class:`AbstractIterableField` only
    affect values of the dictionary, not keys. Depending on the
    back-end, keys that aren't strings might not be allowed.
    """
    _type = dict

    def get_internal_type(self):
        return 'DictField'

    def _convert(self, func, values, *args, **kwargs):
        if values is None:
            return None
        return dict((key, func(value, *args, **kwargs))
                     for key, value in values.iteritems())

    def validate(self, values, model_instance):
        if not isinstance(values, dict):
            raise ValidationError('Value is of type %r. Should be a dict.' %
                                      type(values))


class BlobField(models.Field):
    """
    A field for storing blobs of binary data.

    The value might either be a string (or something that can be
    converted to a string), or a file-like object.

    In the latter case, the object has to provide a ``read`` method
    from which the blob is read.
    """
    def get_internal_type(self):
        return 'BlobField'

    def formfield(self, **kwargs):
        # A file widget is provided, but use model FileField or ImageField
        # for storing specific files most of the time
        from .widgets import BlobWidget
        from django.forms import FileField
        defaults = {'form_class': FileField, 'widget': BlobWidget}
        defaults.update(kwargs)
        return super(BlobField, self).formfield(**defaults)

    def get_db_prep_value(self, value, connection, prepared=False):
        if hasattr(value, 'read'):
            return value.read()
        else:
            return str(value)

    def get_db_prep_lookup(self, lookup_type, value, connection, prepared=False):
        raise TypeError("BlobFields do not support lookups")

    def value_to_string(self, obj):
        return str(self._get_val_from_obj(obj))


class EmbeddedModelField(models.Field):
    """
    Field that allows you to embed a model instance.

    :param embedded_model: (optional) The model class of instances we
                           will be embedding; may also be passed as a
                           string, similar to relation fields

    TODO: Make sure to delegate all signals and other field methods to
          the embedded instance (not just pre_save, get_db_prep_* and
          to_python).
    """
    __metaclass__ = models.SubfieldBase

    def __init__(self, embedded_model=None, *args, **kwargs):
        self.embedded_model = embedded_model
        kwargs.setdefault('default', None)
        super(EmbeddedModelField, self).__init__(*args, **kwargs)

    def get_internal_type(self):
        return 'EmbeddedModelField'

    def _set_model(self, model):
        """
        Resolves embedded model class once the field knows the model it
        belongs to.

        If the model argument passed to __init__ was a string, we need
        to make sure to resolve that string to the corresponding model
        class, similar to relation fields.
        However, we need to know our own model to generate a valid key
        for the embedded model class lookup and EmbeddedModelFields are
        not contributed_to_class if used in iterable fields. Thus we
        rely on the collection field telling us its model (by setting
        our 'model' attribute in its contribute_to_class method).
        """
        if model is not None and isinstance(self.embedded_model, basestring):
            def _resolve_lookup(self_, resolved_model, model):
                self.embedded_model = resolved_model
            from django.db.models.fields.related import add_lazy_relation
            add_lazy_relation(model, self, self.embedded_model, _resolve_lookup)

        self._model = model

    model = property(lambda self: self._model, _set_model)

    def model_for_data(self, data):
        """
        Returns the fixed embedded_model this field was initialized
        with (typed embedding) or tries to determine the model from
        _module / _model keys in the data (untyped embedding).

        We give precedence to the field's definition model, as silently
        using a differing serialized one could hide some data integrity
        problems.

        Note that a single untyped EmbeddedModelField may process
        instances of different models (especially when used as a type
        of a collection field).
        """
        module = data.pop('_module', None)
        model = data.pop('_model', None)
        if self.embedded_model is not None:
            return self.embedded_model
        elif module is not None:
            return getattr(import_module(module), model)
        else:
            raise IntegrityError('Untyped EmbeddedModelField trying to load '
                                 'data without serialized model class info')

    def to_python(self, value):
        """
        Passes embedded model fields' values through embedded fields
        to_python methods and reinstiatates the embedded instance.

        We expect to receive a field.attname => value dict together
        with a model class from back-end database deconversion (which
        needs to know fields of the model beforehand).
        """

        # Either we the database deconversion has already has
        # determined or we got deserialized dict that may still
        # contain model class info.
        if isinstance(value, tuple):
            embedded_model, values = value
        elif isinstance(value, dict):
            embedded_model, values = self.model_for_data(value), value
        else:
            return value

        # Pass values through respective fields' to_python, leaving
        # fields for which no value is specified uninitialized.
        values = dict((field.attname, field.to_python(values[field.attname]))
                      for field in embedded_model._meta.fields
                      if field.attname in values)

        # Create the model instance.
        # Note: the double underline is not a typo -- this lets the
        # model know that the object already exists in the database.
        return embedded_model(__entity_exists=True, **values)

    def get_db_prep_save(self, embedded_instance, **kwargs):
        """
        Applies pre_save and get_db_prep_save of embedded instance
        fields and passes a field => value mapping down to database
        type conversions.

        The embedded instance will be saved as a column => value dict
        in the end (possibly augmented with info about instance's model
        for untyped embedding), but because we need to apply database
        type conversions on embedded instance fields' values and for
        these we need to know fields those values come from, we need to
        entrust the database layer with creating the dict.
        """
        if embedded_instance is None:
            return None

        # The field's value should be an instance of the model given in
        # its declaration or at least of some model.
        embedded_model = self.embedded_model or models.Model
        if not isinstance(embedded_instance, embedded_model):
            raise TypeError('Expected instance of type %r, not %r' %
                                (embedded_model, type(embedded_instance)))

        # Apply pre_save and get_db_prep_save of embedded instance
        # fields, create the field => value mapping to be passed to
        # storage preprocessing.
        field_values = {}
        add = not embedded_instance._entity_exists
        for field in embedded_instance._meta.fields:
            value = field.get_db_prep_save(
                field.pre_save(embedded_instance, add), **kwargs)

            # Exclude unset primary keys (e.g. {"id" : None}).
            # TODO: Why?
            if field.primary_key and value is None:
                continue

            field_values[field] = value

        # Let untyped fields store model info alongside values.
        # We use fake RawFields for additional values to avoid passing
        # embedded_instance to database conversions and to give
        # back-ends a chance to apply generic conversions.
        if self.embedded_model is None:
            module_field = RawField()
            module_field.set_attributes_from_name('_module')
            model_field = RawField()
            model_field.set_attributes_from_name('_model')
            field_values.update(
                ((module_field, embedded_instance.__class__.__module__),
                 (model_field, embedded_instance.__class__.__name__)))

        # This instance will exist in the database soon.
        # TODO: Ensure that this doesn't cause race conditions.
        embedded_instance._entity_exists = True

        return field_values

    # TODO/XXX: Remove this once we have a cleaner solution.
    def get_db_prep_lookup(self, lookup_type, value, connection, prepared=False):
        if hasattr(value, 'as_lookup_value'):
            value = value.as_lookup_value(self, lookup_type, connection)
        return value


class LegacyEmbeddedModelField(EmbeddedModelField):
    """
    Wrapper around :class:`EmbeddedModelField` that keeps backwards
    compatibility with data generated by older nonrel versions.

    When fields were still part of Mongo engine, in its version 0.2
    the layout of serialized model instances changed: the _app key is
    not stored anymore, while a _model name must be accompanied by a
    _module key, also _id's are no longer added automatically.

    In version X? we started to pass embedded instance fields' values
    through back-end database type conversion / deconversion (e.g. key
    value -> ObjectId).
    """
    def model_for_data(self, data):
        """
        Removes legacy model info from data.

        TODO: Maybe its OK to remove this already?
        """
        data.pop('_app', None)
        if '_module' not in data:
            data.pop('_model', None)
        if '_id' in data:
            data['id'] = data.pop('_id')

        return super(LegacyEmbeddedModelField, self).model_for_data(data)

import datetime
import random

from django.conf import settings
from django.db.models.fields import NOT_PROVIDED, DecimalField
from django.db.models.sql import aggregates as sqlaggregates
from django.db.models.sql.compiler import SQLCompiler
from django.db.models.sql.constants import LOOKUP_SEP, MULTI, SINGLE
from django.db.models.sql.where import AND, OR
from django.db.utils import DatabaseError, IntegrityError
from django.utils.tree import Node


EMULATED_OPS = {
    'exact': lambda x, y: y in x if isinstance(x, (list,tuple)) else x == y,
    'iexact': lambda x, y: x.lower() == y.lower(),
    'startswith': lambda x, y: x.startswith(y),
    'istartswith': lambda x, y: x.lower().startswith(y.lower()),
    'isnull': lambda x, y: x is None if y else x is not None,
    'in': lambda x, y: x in y,
    'lt': lambda x, y: x < y,
    'lte': lambda x, y: x <= y,
    'gt': lambda x, y: x > y,
    'gte': lambda x, y: x >= y,
}


class NonrelQuery(object):
    """
    Base class for nonrel queries. Provides in-memory filtering and
    ordering and a framework for converting SQL constraint tree built
    by Django to a representation more suitable for most nonrel
    databases.
    """

    # ----------------------------------------------
    # Public API
    # ----------------------------------------------
    def __init__(self, compiler, fields):
        self.fields = fields
        self.compiler = compiler
        self.connection = compiler.connection
        self.query = self.compiler.query
        self._negated = False

    def fetch(self, low_mark=0, high_mark=None):
        raise NotImplementedError('Not implemented')

    def count(self, limit=None):
        raise NotImplementedError('Not implemented')

    def delete(self):
        raise NotImplementedError('Not implemented')

    def order_by(self, ordering):
        raise NotImplementedError('Not implemented')

    def add_filter(self, column, lookup_type, negated, db_type, value):
        """
        Adds a single constraint to the query. Called by add_filters for
        each constraint in the WHERE tree built by Django.
        """
        raise NotImplementedError('Not implemented')

    def add_filters(self, filters):
        """
        Converts a constraint tree (sql.where.WhereNode) created by
        Django's SQL query machinery to nonrel style filters, calling
        add_filter for each of them.

        This assumes the database doesn't support alternatives of
        constraints, you should override this method if it does.
        """
        if filters.negated:
            self._negated = not self._negated

        if not self._negated and filters.connector != AND:
            raise DatabaseError('Only AND filters are supported.')

        # Remove unneeded children from the tree.
        children = self._get_children(filters.children)

        if self._negated and filters.connector != OR and len(children) > 1:
            raise DatabaseError("When negating a whole filter subgroup "
                                "(e.g., a Q object) the subgroup filters must "
                                "be connected via OR, so the non-relational "
                                "backend can convert them like this: "
                                '"not (a OR b) => (not a) AND (not b)".')

        # Recuresively call the method for tree nodes, add a filter for
        # each leaf.
        for child in children:
            if isinstance(child, Node):
                self.add_filters(child)
                continue

            column, lookup_type, db_type, value = self._decode_child(child)
            self.add_filter(column, lookup_type, self._negated, db_type, value)

        if filters.negated:
            self._negated = not self._negated

    # ----------------------------------------------
    # Internal API for reuse by subclasses
    # ----------------------------------------------
    def _decode_child(self, child):
        """
        Produces arguments suitable for add_filter from a single WHERE
        tree leaf.
        """
        constraint, lookup_type, annotation, value = child
        packed, value = constraint.process(lookup_type, value, self.connection)
        alias, column, db_type = packed

        if alias and alias != self.query.model._meta.db_table:
            raise DatabaseError("This database doesn't support JOINs "
                                "and multi-table inheritance.")

        value = self._normalize_lookup_value(
            value, annotation, lookup_type, constraint.field)

        return column, lookup_type, db_type, value

    def _normalize_lookup_value(self, value, annotation, lookup_type, field):
        """
        Undoes preparations done by Field.get_db_prep_lookup
        inconvenient for nonrel back-ends.

        TODO: Move to DatabaseOperations too?
        """

        # Undo Field.get_db_prep_lookup putting most values in a list.
        if lookup_type not in ('in', 'range', 'year') and \
            isinstance(value, (tuple, list)):

            if len(value) > 1:
                raise DatabaseError('Filter lookup type was: %s. Expected the '
                                'filters value not to be a list. Only "in"-filters '
                                'can be used with lists.'
                                % lookup_type)
            elif lookup_type == 'isnull':
                value = annotation
            else:
                value = value[0]

        # Handle lazy strings.
        # TODO: Test and better explanation is needed; if this is needed
        # should be moved to convert_value_for_db.
        if isinstance(value, unicode):
            value = unicode(value)
        elif isinstance(value, str):
            value = str(value)

        # Remove percents added by Field.get_db_prep_lookup (useful
        # if one were to use the value in a LIKE expression).
        if lookup_type in ('startswith', 'istartswith'):
            value = value[:-1]
        elif lookup_type in ('endswith', 'iendswith'):
            value = value[1:]
        elif lookup_type in ('contains', 'icontains'):
            value = value[1:-1]

        # Workaround for Django only defining get_db_prep_save, rather
        # than get_db_prep_value, causing DatabaseOperations.value_to_db_decimal
        # not to be applied for lookups.
        # TODO: Should be removed if it changes.
        if isinstance(field, DecimalField):
            value = self.connection.ops.value_to_db_decimal(
                field.to_python(value), field.max_digits, field.decimal_places)

        return value

    def _get_children(self, children):
        """
        Filters out WHERE tree nodes not needed for nonrel queries.
        """

        # Filter out nodes that were automatically added by sql.Query,
        # but are not necessary with emulated negation handling code.
        result = []
        for child in children:
            if isinstance(child, tuple):
                constraint = child[0]
                lookup_type = child[1]
                if lookup_type == 'isnull' and constraint.field is None:
                    continue
            result.append(child)
        return result

    def _matches_filters(self, entity, filters):
        # Filters without rules match everything
        if not filters.children:
            return True

        result = filters.connector == AND

        for child in filters.children:
            if isinstance(child, Node):
                submatch = self._matches_filters(entity, child)
            else:
                constraint, lookup_type, annotation, value = child
                packed, value = constraint.process(lookup_type, value, self.connection)
                alias, column, db_type = packed
                if alias != self.query.model._meta.db_table:
                    raise DatabaseError("This database doesn't support JOINs "
                                        "and multi-table inheritance.")

                # Django fields always return a list (see Field.get_db_prep_lookup)
                # except if get_db_prep_lookup got overridden by a subclass
                if lookup_type != 'in' and isinstance(value, (tuple, list)):
                    if len(value) > 1:
                        raise DatabaseError('Filter lookup type was: %s. '
                            'Expected the filters value not to be a list. '
                            'Only "in"-filters can be used with lists.'
                            % lookup_type)
                    elif lookup_type == 'isnull':
                        value = annotation
                    else:
                        value = value[0]

                if entity[column] is None:
                    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
                        submatch = lookup_type in ('lt', 'lte')
                    elif lookup_type in ('startswith', 'contains', 'endswith', 'iexact',
                                         'istartswith', 'icontains', 'iendswith'):
                        submatch = False
                    else:
                        submatch = EMULATED_OPS[lookup_type](entity[column], value)
                else:
                    submatch = EMULATED_OPS[lookup_type](entity[column], value)

            if filters.connector == OR and submatch:
                result = True
                break
            elif filters.connector == AND and not submatch:
                result = False
                break

        if filters.negated:
            return not result
        return result

    def _order_in_memory(self, lhs, rhs):
        for column, descending in self.compiler._get_ordering():
            result = cmp(lhs.get(column), rhs.get(column))
            if descending:
                result *= -1
            if result != 0:
                return result
        return 0

    def convert_value_from_db(self, db_type, value):
        return self.compiler.convert_value_from_db(db_type, value)

    def convert_value_for_db(self, db_type, value):
        return self.compiler.convert_value_for_db(db_type, value)


class NonrelCompiler(SQLCompiler):
    """
    Base class for non-relational compilers.
    """

    # ----------------------------------------------
    # Public API
    # ----------------------------------------------
    def results_iter(self):
        """
        Returns an iterator over the results from executing this query.
        """
        self.check_query()
        fields = self.get_fields()
        low_mark = self.query.low_mark
        high_mark = self.query.high_mark
        for entity in self.build_query(fields).fetch(low_mark, high_mark):
            yield self._make_result(entity, fields)

    def has_results(self):
        return self.get_count(check_exists=True)

    def execute_sql(self, result_type=MULTI):
        """
        Handles aggregate/count queries
        """
        aggregates = self.query.aggregate_select.values()
        # Simulate a count()
        if aggregates:
            assert len(aggregates) == 1
            aggregate = aggregates[0]
            assert isinstance(aggregate, sqlaggregates.Count)
            meta = self.query.get_meta()
            assert aggregate.col == '*' or aggregate.col == (meta.db_table, meta.pk.column)
            count = self.get_count()
            if result_type is SINGLE:
                return [count]
            elif result_type is MULTI:
                return [[count]]
        raise NotImplementedError('The database backend only supports count() queries')

    # ----------------------------------------------
    # Additional NonrelCompiler API
    # ----------------------------------------------
    def _make_result(self, entity, fields):
        result = []
        for field in fields:
            value = entity.get(field.column, NOT_PROVIDED)
            if value is NOT_PROVIDED:
                value = field.get_default()
            else:
                value = self.convert_value_from_db(
                    field.db_type(connection=self.connection), value)
            if value is None and not field.null:
                raise IntegrityError("Non-nullable field %s can't be None!" % field.name)
            result.append(value)
        return result

    def check_query(self):
        if (len([a for a in self.query.alias_map if self.query.alias_refcount[a]]) > 1
                or self.query.distinct or self.query.extra or self.query.having):
            raise DatabaseError('This query is not supported by the database.')

    def get_count(self, check_exists=False):
        """
        Counts matches using the current filter constraints.
        """
        if check_exists:
            high_mark = 1
        else:
            high_mark = self.query.high_mark
        return self.build_query().count(high_mark)

    def build_query(self, fields=None):
        if fields is None:
            fields = self.get_fields()
        query = self.query_class(self, fields)
        query.add_filters(self.query.where)
        query.order_by(self._get_ordering())

        # This at least satisfies the most basic unit tests
        if settings.DEBUG:
            self.connection.queries.append({'sql': repr(query)})
        return query

    def get_fields(self):
        """
        Returns the fields which should get loaded from the back-end by
        self.query
        """
        # We only set this up here because
        # related_select_fields isn't populated until
        # execute_sql() has been called.
        if self.query.select_fields:
            fields = self.query.select_fields + self.query.related_select_fields
        else:
            fields = self.query.model._meta.fields
        # If the field was deferred, exclude it from being passed
        # into `resolve_columns` because it wasn't selected.
        only_load = self.deferred_to_columns()
        if only_load:
            db_table = self.query.model._meta.db_table
            only_load = dict((k, v) for k, v in only_load.items()
                             if v or k == db_table)
            if len(only_load.keys()) > 1:
                raise DatabaseError('Multi-table inheritance is not supported '
                                    'by non-relational DBs.' + repr(only_load))
            fields = [f for f in fields if db_table in only_load and
                      f.column in only_load[db_table]]

        query_model = self.query.model
        if query_model._meta.proxy:
            query_model = query_model._meta.proxy_for_model

        for field in fields:
            if field.model._meta != query_model._meta:
                raise DatabaseError('Multi-table inheritance is not supported '
                                    'by non-relational DBs.')
        return fields

    def _get_ordering(self):
        opts = self.query.get_meta()
        if not self.query.default_ordering:
            ordering = self.query.order_by
        else:
            ordering = self.query.order_by or opts.ordering
        for order in ordering:
            if LOOKUP_SEP in order:
                raise DatabaseError("Ordering can't span tables on non-relational backends (%s)" % order)
            if order == '?':
                raise DatabaseError("Randomized ordering isn't supported by the backend")
            order = order.lstrip('+')
            descending = order.startswith('-')
            field = order.lstrip('-')
            if field == 'pk':
                field = opts.pk.name
            if not self.query.standard_ordering:
                descending = not descending
            yield (opts.get_field(field).column, descending)

    def _parse_db_type(self, db_type):
        """
        Separates elements of db_type into a tuple. Used for separating
        subtype of iterable fields.

        TODO: Do this in NonrelDatabaseCreation instead?
        """
        try:
            db_type, db_subtype = db_type.split(':', 1)
        except ValueError:
            db_subtype = None
        return db_type, db_subtype

    def convert_value_for_db(self, db_type, value):
        """
        Converts a standard Python value to a type that can be stored
        by the database.

        There are some other ways to convert values for the database in
        Django (BaseDatabaseOperations.value_to_db_* / convert_values),
        but there are some problems with them:
        -- there are no methods for string / integer conversion or for
           nonrel specific fields (e.g. iterables, blobs);
        -- some conversions are not specific to a field kind and can't
           rely on field internal_type (e.g. key conversions);
        -- some standard fields do not call value_to_db_* for each
           operation (e.g. DecimalField only defines get_db_value_save,
           so the conversion is not applied to lookup values).
        Nevertheless standard methods should be preferred.

        TODO: Handle AbstractIterableFields here (e.g. let them use
        'list:subtype' as db_type, and convert elements of all values
        using this type.

        TODO: This should belong to DatabaseOperations.

        :param db_type: Database type or encoding that should be used.
        :param value: Value to convert.
        """

        db_type, db_subtype = self._parse_db_type(db_type)

        # Convert all values in a list or set using its subtype.
        # We store both as lists on default.
        if db_type == 'ListField' or db_type == 'SetField':

            # Note that value for a list field lookup may be an iterable
            # list element, that should be converted as a single value.
            # TODO: What about looking up a list in a list of lists?
            if isinstance(value, (list, tuple, set)):
                value = [self.convert_value_for_db(db_subtype, subvalue)
                         for subvalue in value]

        # Convert dict values, pickle and store it as a Blob.
        # TODO: Only values, not keys?
        elif db_type == 'DictField':
            if isinstance(value, dict):
                value = dict((key, self.convert_value_for_db(db_subtype, subvalue))
                              for key, subvalue in value.iteritems())

        return value

    def convert_value_from_db(self, db_type, value):
        """
        Converts a database type to a standard Python type.

        If you encoded a value for storage in the database, reverse the
        encoding here. This implementation only provides reference
        implementations for nonrel fields (ListField, SetField etc.).

        :param db_type: Encoding / decoding procedure identifier.
        :param value: A value received from the database.
        """

        db_type, db_subtype = self._parse_db_type(db_type)

        # Deconvert each value in a list, return a set for the set type.
        if db_type == 'ListField' or db_type == 'SetField':
            value = [self.convert_value_from_db(db_subtype, subvalue)
                     for subvalue in value]
            if db_type == 'SetField':
                value = set(value)

        # We may have encoded dict values, so now decode them.
        elif db_type == 'DictField':
            value = dict((key, self.convert_value_from_db(db_subtype, subvalue))
                          for key, subvalue in value.iteritems())

        # Call standard convert_values method, so queries don't
        # remember about this. TODO
#        value = self.connection.ops.convert_value(value, field)

        return value


class NonrelInsertCompiler(object):
    def execute_sql(self, return_id=False):
        data = {}
        for (field, value), column in zip(self.query.values, self.query.columns):
            if field is not None:
                if not field.null and value is None:
                    raise IntegrityError("You can't set %s (a non-nullable "
                                         "field) to None!" % field.name)
                value = self.convert_value_for_db(
                    field.db_type(connection=self.connection), value)
            data[column] = value
        return self.insert(data, return_id=return_id)

    def insert(self, values, return_id):
        """
        :param values: The model object as a list of (column, value) pairs
        :param return_id: Whether to return the id of the newly created entity
        """
        raise NotImplementedError


class NonrelUpdateCompiler(object):
    def execute_sql(self, result_type):
        values = []
        for field, _, value in self.query.values:
            if hasattr(value, 'prepare_database_save'):
                value = value.prepare_database_save(field)
            else:
                value = field.get_db_prep_save(value, connection=self.connection)
            value = self.convert_value_for_db(
                field.db_type(connection=self.connection), value)
            values.append((field, value))
        return self.update(values)

    def update(self, values):
        """
        :param values: A list of (field, new-value) pairs
        """
        raise NotImplementedError


class NonrelDeleteCompiler(object):
    def execute_sql(self, result_type=MULTI):
        self.build_query([self.query.get_meta().pk]).delete()
